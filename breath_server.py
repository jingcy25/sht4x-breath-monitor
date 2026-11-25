#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
呼吸监测后端：SHT4x 采集 + 呼吸检测 + WebSocket 实时推送 + REST 历史 + SQLite 落库。

运行：  python3 -u breath_server.py [--port 8000]
访问：  浏览器打开 http://<树莓派IP>:8000 （实时 + 历史仪表盘）

接口：
  GET  /                   仪表盘页面
  WS   /ws                 每 50ms 推一条实时样本
  GET  /api/status         当前状态
  GET  /api/history?range=1h|24h   历史（每分钟呼吸频率 + 暂停事件）
  GET  /api/events         呼吸暂停事件列表
  POST /api/config         更新阈值 / 暂停秒数
  GET  /api/export.csv     导出原始数据
"""
import argparse
import json
import os
import math
import sqlite3
import threading
import time
from collections import deque

import breath_sensors
from flask import Flask, jsonify, request, Response, send_from_directory
from flask_sock import Sock
import atexit

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(17, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(27, GPIO.OUT, initial=GPIO.LOW)
    atexit.register(GPIO.cleanup)
    GPIO_OK = True
    print("alarm GPIO ready: buzzer=GPIO17 led=GPIO27", flush=True)
except Exception as exc:
    GPIO = None
    GPIO_OK = False
    print("alarm GPIO unavailable: %s" % exc, flush=True)


def alarm_worker():
    """Three short beeps with a pause between bursts, LED in sync."""
    while True:
        if not alarm_event.is_set():
            if GPIO_OK:
                GPIO.output(17, 0)
                GPIO.output(27, 0)
            time.sleep(0.05)
            continue
        for _ in range(3):
            if not alarm_event.is_set():
                break
            GPIO.output(17, 1)
            GPIO.output(27, 1)
            time.sleep(0.15)
            GPIO.output(17, 0)
            GPIO.output(27, 0)
            if not alarm_event.is_set():
                break
            time.sleep(0.15)
        if alarm_event.is_set():
            time.sleep(0.9)


if GPIO_OK:
    threading.Thread(target=alarm_worker, daemon=True).start()


class BreathDetector:
    """基于湿度偏离基线的呼吸检测器（带迟滞）。"""

    def __init__(self, tau=2.0, th_up=1.2, th_down=0.3):
        self.tau = tau
        self.th_up = th_up
        self.th_down = th_down
        self.baseline = None
        self.state = "low"
        self._last_t = None

    def update(self, t, h):
        if self.baseline is None:
            self.baseline = h
        dt = (t - self._last_t) if self._last_t is not None else 0.05
        self._last_t = t
        alpha = math.exp(-dt / self.tau)
        self.baseline = alpha * self.baseline + (1.0 - alpha) * h
        signal = h - self.baseline
        breath = False
        if self.state == "low" and signal >= self.th_up:
            self.state = "high"
            breath = True
        elif self.state == "high" and signal <= self.th_down:
            self.state = "low"
        return signal, breath


DB = "breath.db"
config = {"apnea_sec": 20.0, "th_up": 1.2}
cfg_lock = threading.Lock()
latest = {"t": 0, "humidity": 0.0, "signal": 0.0, "breath": 0,
          "apnea": 0, "bpm": 0.0, "temp": 0.0,
          "env_temp": None, "env_hum": None, "lux": None, "fsr": None}
latest_lock = threading.Lock()
clients = []
clients_lock = threading.Lock()
apnea_start = {"v": None}
alarm_event = threading.Event()


def init_db():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS samples(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, humidity REAL,
        signal REAL, breath INTEGER, apnea INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON samples(ts)")
    conn.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_ts REAL, end_ts REAL, duration REAL)""")
    conn.commit()
    conn.close()


def sensor_loop():
    breath_sensor = breath_sensors.SHT4x(1)
    try:
        env_sensor = breath_sensors.SHT4x(3)
        print("environment SHT4x: i2c-3", flush=True)
    except Exception as exc:
        env_sensor = None
        print("environment SHT4x unavailable (i2c-3): %s" % exc, flush=True)
    try:
        light_sensor = breath_sensors.VEML7700(1)
    except Exception as exc:
        light_sensor = None
        print("VEML7700 unavailable: %s" % exc, flush=True)
    try:
        adc = breath_sensors.ADS1115(1)
    except Exception as exc:
        adc = None
        print("ADS1115 unavailable: %s" % exc, flush=True)

    env_temp = env_hum = None
    lux = None
    fsr = None
    last_slow = 0.0
    last_fsr = 0.0

    det = BreathDetector()
    last_breath = time.time()
    breath_times = deque(maxlen=240)
    conn = sqlite3.connect(DB)
    buf = []
    last_flush = time.time()

    while True:
        try:
            t = time.time()
            temp, h = breath_sensor.read()
            signal, breath = det.update(t, h)
            if breath:
                last_breath = t
                breath_times.append(t)

            with cfg_lock:
                det.th_up = config["th_up"]
                apnea_sec = config["apnea_sec"]

            apnea = (t - last_breath) > apnea_sec

            if apnea:
                alarm_event.set()
            else:
                alarm_event.clear()

            # 呼吸暂停事件记录
            if apnea and apnea_start["v"] is None:
                apnea_start["v"] = last_breath + apnea_sec
            if not apnea and apnea_start["v"] is not None:
                st = apnea_start["v"]
                conn.execute(
                    "INSERT INTO events(start_ts,end_ts,duration) VALUES(?,?,?)",
                    (st, t, t - st))
                conn.commit()
                apnea_start["v"] = None

            bpm = sum(1 for x in breath_times if t - x <= 60)

            if t - last_slow > 2.0:
                if env_sensor is not None:
                    try:
                        env_temp, env_hum = env_sensor.read()
                    except Exception:
                        env_temp = env_hum = None
                if light_sensor is not None:
                    try:
                        _, lux = light_sensor.read()
                    except Exception:
                        lux = None
                last_slow = t

            if adc is not None and t - last_fsr > 1.0:
                try:
                    fsr, _ = adc.read_voltage(0)
                except Exception:
                    fsr = None
                last_fsr = t

            msg = {"t": t, "humidity": round(h, 2), "signal": round(signal, 3),
                   "breath": 1 if breath else 0, "apnea": 1 if apnea else 0,
                   "bpm": round(bpm, 1), "temp": round(temp, 1),
                   "env_temp": None if env_temp is None else round(env_temp, 1),
                   "env_hum": None if env_hum is None else round(env_hum, 1),
                   "lux": None if lux is None else round(lux, 1),
                   "fsr": None if fsr is None else round(fsr, 3)}

            with latest_lock:
                latest.update(msg)

            buf.append((t, h, signal, 1 if breath else 0, 1 if apnea else 0))
            if time.time() - last_flush > 1.0:
                conn.executemany(
                    "INSERT INTO samples(ts,humidity,signal,breath,apnea) VALUES(?,?,?,?,?)",
                    buf)
                conn.commit()
                buf = []
                last_flush = time.time()

            text = json.dumps(msg)
            with clients_lock:
                for c in list(clients):
                    try:
                        c.send(text)
                    except Exception:
                        pass

            time.sleep(0.05)
        except Exception as e:
            print("sensor error:", e, flush=True)
            time.sleep(1.0)


app = Flask(__name__)
sock = Sock(app)


@app.route("/")
def index():
    return send_from_directory(os.getcwd(), "index.html")


@app.route("/chart.umd.min.js")
def chart_js():
    return send_from_directory(os.getcwd(), "chart.umd.min.js")


@app.route("/api/test_alarm")
def test_alarm():
    if not GPIO_OK:
        return jsonify({"ok": False, "error": "gpio unavailable"})
    already = alarm_event.is_set()
    alarm_event.set()
    time.sleep(1.3)
    if not already:
        alarm_event.clear()
    return jsonify({"ok": True, "pattern": "3 beeps"})


@app.route("/api/status")
def api_status():
    with latest_lock:
        return jsonify(dict(latest))


@app.route("/api/history")
def api_history():
    rng = request.args.get("range", "1h")
    hours = 24 if rng == "24h" else 1
    since = time.time() - hours * 3600
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "SELECT CAST(ts/60 AS INTEGER) m, SUM(breath) FROM samples "
        "WHERE ts>=? GROUP BY m ORDER BY m", (since,))
    bpm = [{"x": m * 60, "y": round(float(c), 1)} for m, c in cur.fetchall()]
    cur.execute(
        "SELECT start_ts, duration FROM events WHERE start_ts>=? ORDER BY start_ts",
        (since,))
    evs = [{"x": s, "y": 0, "dur": round(d, 1)} for s, d in cur.fetchall()]
    conn.close()
    return jsonify({"bpm": bpm, "events": evs})


@app.route("/api/events")
def api_events():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT start_ts, duration FROM events ORDER BY start_ts DESC LIMIT 200")
    rows = [{"x": s, "y": 0, "dur": round(d, 1)} for s, d in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(silent=True) or {}
    with cfg_lock:
        if "th_up" in data:
            config["th_up"] = float(data["th_up"])
        if "apnea_sec" in data:
            config["apnea_sec"] = float(data["apnea_sec"])
        return jsonify({"ok": True, "config": dict(config)})


@app.route("/api/export.csv")
def api_export():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT ts, humidity, signal, breath, apnea FROM samples "
                "ORDER BY ts DESC LIMIT 500000")
    rows = cur.fetchall()
    conn.close()

    def gen():
        yield "ts,humidity,signal,breath,apnea\n"
        for r in rows:
            yield ",".join(str(x) for x in r) + "\n"

    return Response(gen(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=breath.csv"})


@sock.route("/ws")
def ws(ws):
    with clients_lock:
        clients.append(ws)
    try:
        while True:
            ws.receive(timeout=10)  # 接收客户端配置消息（暂忽略），同时保持连接
    except Exception:
        pass
    finally:
        with clients_lock:
            if ws in clients:
                clients.remove(ws)


def main():
    global DB
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--th-up", type=float, default=1.2)
    ap.add_argument("--apnea", type=float, default=20.0)
    args = ap.parse_args()
    DB = args.db
    config["th_up"] = args.th_up
    config["apnea_sec"] = args.apnea
    init_db()
    threading.Thread(target=sensor_loop, daemon=True).start()
    print("呼吸监测后端已启动: http://0.0.0.0:%d" % args.port, flush=True)
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
