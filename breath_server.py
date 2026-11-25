#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
呼吸监测后端（整合版）：
- SHT4x 口鼻呼吸（B 主通道）
- FSR 在床锚点（A 层，仅 presence，不做 B）
- VEML7700 光照（开/关灯、相机门控）
- 摄像头：浏览器端 Teachable Machine Pose（Sit/Lie），POST /api/camera
- SpO2：复用 spo2/ 子项目，1 Hz 记录，不参与 20 s 判定
- A 层：FSR + 光照 + 相机 2-of-3；只有 ASLEEP 才使能 B
- B 层：SHT4x 单通道；事件时长存总暂停；不应期/超时复位

运行：  python3 -u breath_server.py [--port 8000]
访问：  http://<树莓派IP>:8000
"""
import argparse
import atexit
import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque

import breath_sensors
from flask import Flask, jsonify, request, Response, send_from_directory
from flask_sock import Sock

try:
    import RPi.GPIO as GPIO

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(17, GPIO.OUT, initial=GPIO.LOW)   # buzzer
    GPIO.setup(27, GPIO.OUT, initial=GPIO.LOW)   # LED (teammate wiring)
    atexit.register(GPIO.cleanup)
    GPIO_OK = True
    print("alarm GPIO ready: buzzer=GPIO17 led=GPIO27", flush=True)
except Exception as exc:  # pragma: no cover - desktop fallback
    GPIO = None
    GPIO_OK = False
    print("alarm GPIO unavailable: %s" % exc, flush=True)


# ---------------------------------------------------------------------------
# alarm
# ---------------------------------------------------------------------------
alarm_event = threading.Event()
alarm_stats = {"bursts": 0, "ticks": 0, "beeps": 0}
alarm_override = {"until": 0.0}


def alarm_active():
    return alarm_event.is_set() or time.time() < alarm_override["until"]


def alarm_worker():
    """Three short beeps per burst, LED in sync; independent of acquisition."""
    print("alarm worker started", flush=True)
    while True:
        alarm_stats["ticks"] += 1
        if not alarm_active():
            if GPIO_OK:
                GPIO.output(17, 0)
                GPIO.output(27, 0)
            time.sleep(0.05)
            continue
        for _ in range(3):
            if not alarm_active():
                break
            if GPIO_OK:
                GPIO.output(17, 1)
                GPIO.output(27, 1)
            alarm_stats["beeps"] += 1
            time.sleep(0.15)
            if GPIO_OK:
                GPIO.output(17, 0)
                GPIO.output(27, 0)
            if not alarm_active():
                break
            time.sleep(0.15)
        if alarm_active():
            alarm_stats["bursts"] += 1
            time.sleep(0.9)


if GPIO_OK:
    threading.Thread(target=alarm_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# SHT4x breath detector (B)
# ---------------------------------------------------------------------------
class BreathDetector:
    """Humidity-deviation detector with hysteresis + refractory + timeout.

    The teammate's original detector could stick in ``high`` when the signal
    never fell below ``th_down`` (humid night / sensor near mouth), silently
    missing the next breath. We add:
    - 1.5 s refractory so one breath is counted once;
    - 5 s high-state timeout so a stuck state self-resets;
    - explicit ``last_breath_t`` for logging/tests.
    """

    def __init__(self, tau=2.0, th_up=1.2, th_down=0.3,
                 refractory_s=1.5, high_timeout_s=5.0):
        self.tau = tau
        self.th_up = th_up
        self.th_down = th_down
        self.refractory_s = refractory_s
        self.high_timeout_s = high_timeout_s
        self.baseline = None
        self.state = "low"
        self._last_t = None
        self._high_since = None
        self._blocked = False
        self.last_breath_t = -1e9

    def update(self, t, h):
        if self.baseline is None:
            self.baseline = h
        dt = (t - self._last_t) if self._last_t is not None else 0.05
        self._last_t = t
        alpha = math.exp(-dt / self.tau)
        self.baseline = alpha * self.baseline + (1.0 - alpha) * h
        signal = h - self.baseline
        breath = False

        if self.state == "high" and self._high_since is not None:
            if (t - self._high_since) > self.high_timeout_s:
                self.state = "low"
                self._high_since = None
                self._blocked = True

        if self.state == "low":
            if self._blocked:
                if signal <= self.th_down:
                    self._blocked = False
            elif signal >= self.th_up and (t - self.last_breath_t) >= self.refractory_s:
                self.state = "high"
                self._high_since = t
                self.last_breath_t = t
                breath = True
        elif self.state == "high":
            if signal <= self.th_down:
                self.state = "low"
                self._high_since = None
                self._blocked = False

        return signal, breath


# ---------------------------------------------------------------------------
# shared state / config
# ---------------------------------------------------------------------------
DB = "breath.db"
config = {
    "apnea_sec": 20.0,
    "th_up": 1.2,
    "fsr_in_bed": 0.100,
    "fsr_presets": [0.05, 0.10, 0.20],  # TODO: replace with measured values
    "fsr_invert": False,                # True if pressure increases voltage
    "fsr_hyst": 0.030,
    "fsr_debounce_s": 10.0,
    "sleep_onset_s": 300.0,             # 5 min; demo can lower
    "lux_dark": 10.0,
    "warn_sec": 10.0,
}
cfg_lock = threading.Lock()

latest = {
    "t": 0, "humidity": 0.0, "signal": 0.0, "breath": 0,
    "apnea": 0, "bpm": 0.0, "temp": 0.0,
    "env_temp": None, "env_hum": None, "lux": None, "fsr": None,
    "in_bed": 0, "standby": 1,
    "a_state": "standby", "a_confidence": "low",
    "camera_posture": None, "camera_conf": None,
    "spo2": None, "hr": None, "pi": None, "sqi": None,
    "spo2_valid": 0, "spo2_ts": 0.0,
}
latest_lock = threading.Lock()

clients = []
clients_lock = threading.Lock()

camera_lock = threading.Lock()
camera_state = {"posture": None, "confidence": None, "ts": 0.0}

camera_jpeg = b""
camera_jpeg_lock = threading.Lock()
camera_frame_ts = 0.0
camera_frame_bytes = 0

# Sensor handles published by the acquisition loop so /api/selftest can probe
# the same objects without opening a second bus handle.
dev_lock = threading.Lock()
devices = {"breath": None, "light": None, "adc": None}


def camera_loop():
    """Single camera reader; MJPEG clients consume the latest JPEG.

    Opening ``cv2.VideoCapture`` inside a Flask streaming generator can block
    the first response on some UVC cameras. A background reader is more robust
    and supports multiple viewers.
    """
    try:
        import cv2
    except Exception as exc:
        print("camera: OpenCV unavailable: %s" % exc, flush=True)
        return
    global camera_jpeg, camera_frame_ts, camera_frame_bytes
    while True:
        cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
        if not cap.isOpened():
            print("camera: /dev/video0 open failed; retrying", flush=True)
            time.sleep(3.0)
            continue
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
            cap.set(cv2.CAP_PROP_FPS, 5)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            print("camera: /dev/video0 opened", flush=True)
            while True:
                ok, frame = cap.read()
                if not ok:
                    print("camera: read failed; reopening", flush=True)
                    break
                ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ok:
                    data = jpg.tobytes()
                    with camera_jpeg_lock:
                        camera_jpeg = data
                    camera_frame_ts = time.time()
                    camera_frame_bytes = len(data)
                time.sleep(0.03)
        except Exception as exc:
            print("camera loop error: %s" % exc, flush=True)
        finally:
            try:
                cap.release()
            except Exception:
                pass
        time.sleep(1.0)


# ---------------------------------------------------------------------------
# SpO2 worker (1 Hz, no 20 s vote)
# ---------------------------------------------------------------------------
def _on_spo2(sample):
    with latest_lock:
        latest["spo2"] = sample.get("spo2")
        latest["hr"] = sample.get("hr")
        latest["pi"] = sample.get("pi")
        latest["sqi"] = sample.get("sqi")
        latest["spo2_valid"] = sample.get("valid", 0)
        latest["spo2_ts"] = sample.get("ts", time.time())


def _on_spo2_status(msg):
    print("[spo2] %s" % msg, flush=True)


spo2_worker = None  # created in main() so cwd/root is correct


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS samples(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, humidity REAL,
        signal REAL, breath INTEGER, apnea INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON samples(ts)")
    conn.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_ts REAL, alarm_ts REAL, end_ts REAL, duration REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS a_states(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, state TEXT, confidence TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS camera_states(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, posture TEXT, confidence REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS spo2_samples(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, hr REAL, spo2 REAL,
        pi REAL, sqi REAL, valid INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts REAL, end_ts REAL,
        sleep_onset_ts REAL, quality TEXT)""")
    # migrate older events table
    cols = [row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "alarm_ts" not in cols:
        try:
            conn.execute("ALTER TABLE events ADD COLUMN alarm_ts REAL")
        except Exception:
            pass
    conn.commit()
    conn.close()


def _i2c_present(bus: int, addr: int, timeout_s: float = 1.5, tries: int = 3) -> bool:
    """Probe an I2C address in a child process using an SMBus quick write.

    A quick write only sends the address + write bit and waits for ACK, which
    is exactly what ``i2cdetect`` does.  A plain ``read_byte`` is *not* a valid
    presence test: SHT4x NACKs any bare read until it receives a measurement
    command (0xFD), so read-probing wrongly reported "SHT4x not found".

    The probe runs under ``timeout`` because a missing device on a software
    I2C bus can block ``smbus2`` indefinitely.  A single NACK right after boot
    (bus busy while the camera opens) is normal, so retry a few times.
    """
    code = (
        "import smbus2; "
        "b=smbus2.SMBus(%d); "
        "b.write_quick(%d); "
        "b.close()" % (bus, addr)
    )
    for attempt in range(tries):
        try:
            proc = subprocess.run(
                ["timeout", str(timeout_s), sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout_s + 1.0,
            )
            if proc.returncode == 0:
                return True
        except Exception:
            pass
        if attempt + 1 < tries:
            time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# A-layer helpers
# ---------------------------------------------------------------------------
def _camera_posture(t, lux):
    """Return (posture, confidence) only when fresh and lights-on."""
    with camera_lock:
        posture = camera_state["posture"]
        confidence = camera_state["confidence"]
        ts = camera_state["ts"]
    if posture is None or (t - ts) > 5.0:
        return None, None
    if posture not in ("lying", "sitting"):
        return None, None
    # Camera only works when there is light; VEML is the gate.
    if lux is not None and lux < config["lux_dark"]:
        return None, None
    return posture, confidence


def _a_confidence(in_bed, posture, lux):
    if not in_bed:
        return "low"
    votes = 1  # FSR
    if posture in ("lying", "sitting"):
        votes += 1
    if lux is not None:
        votes += 1
    return "high" if votes >= 3 else ("medium" if votes == 2 else "low")


# ---------------------------------------------------------------------------
# acquisition / fusion loop
# ---------------------------------------------------------------------------
def _init_sensor(kind: str):
    """Create one sensor object, or return None when it is not wired."""
    if kind == "breath":
        addr, label = 0x44, "SHT4x"
        if not _i2c_present(1, addr):
            return None
        try:
            return breath_sensors.SHT4x(1)
        except Exception as exc:
            print("%s unavailable: %s" % (label, exc), flush=True)
            return None
    if kind == "light":
        addr, label = 0x10, "VEML7700"
        if not _i2c_present(1, addr):
            return None
        try:
            return breath_sensors.VEML7700(1)
        except Exception as exc:
            print("%s unavailable: %s" % (label, exc), flush=True)
            return None
    if kind == "adc":
        addr, label = 0x48, "ADS1115"
        if not _i2c_present(1, addr):
            return None
        try:
            return breath_sensors.ADS1115(1)
        except Exception as exc:
            print("%s unavailable: %s" % (label, exc), flush=True)
            return None
    raise ValueError(kind)


def sensor_loop():
    breath_sensor = _init_sensor("breath")
    if breath_sensor is None:
        print("SHT4x breath sensor not found at 0x44 (i2c-1); will retry", flush=True)

    light_sensor = _init_sensor("light")
    if light_sensor is None:
        print("VEML7700 not found at 0x10 (i2c-1); will retry", flush=True)

    adc = _init_sensor("adc")
    if adc is None:
        print("ADS1115 not found at 0x48 (i2c-1); will retry", flush=True)

    last_probe = time.time()

    env_temp = env_hum = None
    lux = None
    fsr = None
    fsr_prev = None
    fsr_motion = 0.0
    last_slow = 0.0
    last_fsr = 0.0

    det = BreathDetector()
    last_breath = time.time()
    breath_times = deque(maxlen=240)
    conn = sqlite3.connect(DB)
    buf = []
    last_flush = time.time()
    last_a_state = None
    last_spo2_ts = 0.0

    in_bed = False
    bed_candidate = False
    candidate_since = 0.0
    a_state = "standby"
    sleep_since = None
    pause_start = None
    gap_until = 0.0
    last_temp = 0.0
    last_h = 0.0
    last_standby = 0.0

    while True:
        try:
            t = time.time()

            # FSR presence, 2 Hz (not for B)
            if adc is not None and (t - last_fsr) > 0.5:
                try:
                    fsr, _ = adc.read_voltage(0)
                except Exception:
                    fsr = None
                last_fsr = t
                if fsr is not None:
                    if fsr_prev is not None:
                        fsr_motion = 0.85 * fsr_motion + 0.15 * abs(fsr - fsr_prev)
                    fsr_prev = fsr
                else:
                    fsr_motion = 0.0

            with cfg_lock:
                threshold = float(config["fsr_in_bed"])
                hyst = float(config["fsr_hyst"])
                invert = bool(config["fsr_invert"])
                debounce_s = float(config["fsr_debounce_s"])
                apnea_sec = float(config["apnea_sec"])
                sleep_onset_s = float(config["sleep_onset_s"])
                lux_dark = float(config["lux_dark"])
                det.th_up = float(config["th_up"])

            if fsr is None:
                candidate = False
            elif invert:
                candidate = fsr > (threshold - (hyst if in_bed else 0.0))
            else:
                candidate = fsr < (threshold + (hyst if in_bed else 0.0))

            if candidate != bed_candidate:
                bed_candidate = candidate
                candidate_since = t
            elif (t - candidate_since) >= debounce_s and in_bed != bed_candidate:
                in_bed = bed_candidate
                det = BreathDetector()
                last_breath = t
                breath_times.clear()
                pause_start = None
                sleep_since = None
                if in_bed:
                    gap_until = 0.0
                    a_state = "awake"
                else:
                    gap_until = t + 900.0  # 15 min bathroom gap
                    a_state = "gap"
                alarm_event.clear()
                if in_bed and spo2_worker is not None:
                    spo2_worker.start()
                elif not in_bed and spo2_worker is not None:
                    spo2_worker.stop()
                print("bed state: %s" % ("in_bed" if in_bed else "standby"), flush=True)

            # slow sensors (VEML only; environment SHT4x removed)
            if (t - last_slow) > 2.0:
                if light_sensor is not None:
                    try:
                        _, lux = light_sensor.read()
                    except Exception:
                        lux = None
                last_slow = t

            # hot-plug retry: pick up sensors connected after the service started
            if (t - last_probe) > 5.0 and (
                breath_sensor is None or light_sensor is None or adc is None
            ):
                last_probe = t
                if breath_sensor is None:
                    breath_sensor = _init_sensor("breath")
                    if breath_sensor is not None:
                        print("SHT4x connected at 0x44 (i2c-1)", flush=True)
                if light_sensor is None:
                    light_sensor = _init_sensor("light")
                    if light_sensor is not None:
                        print("VEML7700 connected at 0x10 (i2c-1)", flush=True)
                if adc is None:
                    adc = _init_sensor("adc")
                    if adc is not None:
                        print("ADS1115 connected at 0x48 (i2c-1)", flush=True)

            with dev_lock:
                devices["breath"] = breath_sensor
                devices["light"] = light_sensor
                devices["adc"] = adc

            posture, posture_conf = _camera_posture(t, lux)

            signal = 0.0
            breath = False
            apnea = False
            bpm = 0

            if in_bed and breath_sensor is not None:
                try:
                    temp, h = breath_sensor.read()
                except Exception as exc:
                    temp, h = last_temp, last_h
                    print("SHT4x read error: %s" % exc, flush=True)
                last_temp, last_h = temp, h
                signal, breath = det.update(t, h)
                if breath:
                    last_breath = t
                    breath_times.append(t)

                regular = sum(1 for x in breath_times if t - x <= 30.0) >= 3
                sleep_context = (
                    posture == "lying"
                    or (posture is None and (lux is None or lux < lux_dark))
                )

                if posture == "sitting":
                    a_state = "awake"
                    sleep_since = None
                elif sleep_context:
                    if regular and fsr_motion < 0.05:
                        if sleep_since is None:
                            sleep_since = t
                        if (t - sleep_since) >= sleep_onset_s:
                            a_state = "asleep"
                        else:
                            a_state = "awake"
                    else:
                        sleep_since = None
                        # keep ASLEEP through a real apnea; wake only on motion
                        if a_state != "asleep":
                            a_state = "awake"
                else:
                    sleep_since = None
                    a_state = "awake"

                if fsr_motion > 0.08 and a_state == "asleep":
                    a_state = "awake"

                apnea = a_state == "asleep" and (t - last_breath) > apnea_sec
                if apnea:
                    alarm_event.set()
                    if pause_start is None:
                        pause_start = last_breath
                else:
                    alarm_event.clear()
                    if pause_start is not None:
                        duration = t - pause_start
                        conn.execute(
                            "INSERT INTO events(start_ts,alarm_ts,end_ts,duration) VALUES(?,?,?,?)",
                            (pause_start, pause_start + apnea_sec, t, duration),
                        )
                        conn.commit()
                        pause_start = None

                bpm = sum(1 for x in breath_times if t - x <= 60.0)
                buf.append((t, h, signal, 1 if breath else 0, 1 if apnea else 0))
                if (time.time() - last_flush) > 1.0:
                    conn.executemany(
                        "INSERT INTO samples(ts,humidity,signal,breath,apnea) VALUES(?,?,?,?,?)",
                        buf,
                    )
                    conn.commit()
                    buf = []
                    last_flush = time.time()
            else:
                temp, h = last_temp, last_h
                if not in_bed:
                    a_state = "gap" if t < gap_until else "standby"
                    sleep_since = None
                    alarm_event.clear()
                    # Standby readout only (display/sensor health); never feeds
                    # the breath detector and never raises an alarm.
                    if breath_sensor is not None and (t - last_standby) > 2.0:
                        last_standby = t
                        try:
                            last_temp, last_h = breath_sensor.read()
                            temp, h = last_temp, last_h
                        except Exception:
                            pass

            # SpO2 is logging-only; no 20 s vote
            with latest_lock:
                spo2 = latest.get("spo2")
                hr = latest.get("hr")
                pi = latest.get("pi")
                sqi = latest.get("sqi")
                spo2_valid = latest.get("spo2_valid", 0)
                spo2_ts = latest.get("spo2_ts", 0.0)

            if spo2_ts and spo2_ts != last_spo2_ts and spo2 is not None:
                conn.execute(
                    "INSERT INTO spo2_samples(ts,hr,spo2,pi,sqi,valid) VALUES(?,?,?,?,?,?)",
                    (spo2_ts, hr, spo2, pi, sqi, spo2_valid),
                )
                conn.commit()
                last_spo2_ts = spo2_ts

            if a_state != last_a_state:
                conn.execute(
                    "INSERT INTO a_states(ts,state,confidence) VALUES(?,?,?)",
                    (t, a_state, _a_confidence(in_bed, posture, lux)),
                )
                conn.commit()
                last_a_state = a_state

            with camera_lock:
                raw_posture = camera_state["posture"]
                raw_conf = camera_state["confidence"]
                raw_ts = camera_state["ts"]

            msg = {
                "t": t,
                "humidity": round(h, 2),
                "signal": round(signal, 3),
                "breath": 1 if breath else 0,
                "apnea": 1 if apnea else 0,
                "bpm": round(bpm, 1),
                "temp": round(temp, 1),
                "in_bed": 1 if in_bed else 0,
                "standby": 0 if in_bed else 1,
                "a_state": a_state,
                "a_confidence": _a_confidence(in_bed, posture, lux),
                "camera_posture": posture,
                "camera_conf": None if posture_conf is None else round(float(posture_conf), 3),
                # raw browser result before the lux gate, for debugging the link
                "camera_raw": raw_posture,
                "camera_raw_conf": None if raw_conf is None else round(float(raw_conf), 3),
                "camera_raw_age": None if not raw_ts else round(t - raw_ts, 1),
                "env_temp": None if env_temp is None else round(env_temp, 1),
                "env_hum": None if env_hum is None else round(env_hum, 1),
                "lux": None if lux is None else round(lux, 1),
                "fsr": None if fsr is None else round(fsr, 3),
                "fsr_motion": round(fsr_motion, 4),
                "spo2": None if spo2 is None else round(float(spo2), 1),
                "hr": None if hr is None else round(float(hr), 0),
                "pi": None if pi is None else round(float(pi), 2),
                "sqi": None if sqi is None else round(float(sqi), 2),
                "spo2_valid": int(spo2_valid or 0),
            }

            with latest_lock:
                latest.update(msg)

            text = json.dumps(msg)
            with clients_lock:
                for c in list(clients):
                    try:
                        c.send(text)
                    except Exception:
                        pass

            time.sleep(0.05)
        except Exception as e:
            print("sensor error: %s" % e, flush=True)
            time.sleep(1.0)


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__)
sock = Sock(app)


@app.route("/")
def index():
    return send_from_directory(os.getcwd(), "index.html")


@app.route("/camera")
def camera_page():
    return send_from_directory(os.getcwd(), "camera.html")


@app.route("/chart.umd.min.js")
def chart_js():
    return send_from_directory(os.getcwd(), "chart.umd.min.js")


@app.route("/video_feed")
def video_feed():
    def gen():
        last = None
        while True:
            with camera_jpeg_lock:
                jpg = camera_jpeg
            if jpg and jpg is not last:
                last = jpg
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                       str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            else:
                time.sleep(0.05)

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/snapshot")
def snapshot():
    with camera_jpeg_lock:
        jpg = camera_jpeg
    if not jpg:
        return Response("camera not ready", status=503, mimetype="text/plain")
    return Response(jpg, mimetype="image/jpeg")


@app.route("/api/camera", methods=["POST"])
def api_camera():
    data = request.get_json(silent=True) or {}
    posture = str(data.get("posture", "unknown")).lower()
    if posture not in ("lying", "sitting", "unknown"):
        posture = "unknown"
    try:
        confidence = float(data.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    with camera_lock:
        camera_state["posture"] = None if posture == "unknown" else posture
        camera_state["confidence"] = confidence
        camera_state["ts"] = time.time()
    try:
        conn = sqlite3.connect(DB)
        conn.execute(
            "INSERT INTO camera_states(ts,posture,confidence) VALUES(?,?,?)",
            (time.time(), posture, confidence),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    return jsonify({"ok": True, "posture": posture, "confidence": confidence})


@app.route("/api/alarm_state")
def alarm_state():
    return jsonify({
        "alarm": alarm_event.is_set(),
        "bursts": alarm_stats["bursts"],
        "ticks": alarm_stats["ticks"],
        "beeps": alarm_stats["beeps"],
    })


@app.route("/api/test_alarm")
def test_alarm():
    if not GPIO_OK:
        return jsonify({"ok": False, "error": "gpio unavailable"})
    alarm_override["until"] = time.time() + 1.3
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
        "SELECT start_ts, alarm_ts, duration FROM events WHERE start_ts>=? ORDER BY start_ts",
        (since,))
    evs = [{"x": s, "alarm_ts": a, "dur": round(d, 1)} for s, a, d in cur.fetchall()]
    conn.close()
    return jsonify({"bpm": bpm, "events": evs})


@app.route("/api/events")
def api_events():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT start_ts, alarm_ts, duration FROM events ORDER BY start_ts DESC LIMIT 200")
    rows = [{"x": s, "alarm_ts": a, "dur": round(d, 1)} for s, a, d in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/selftest")
def api_selftest():
    """One-shot hardware health check (used during wiring/demo setup)."""
    out = {"ts": time.time()}

    with dev_lock:
        b, l, a = devices["breath"], devices["light"], devices["adc"]

    sht = {"addr": "0x44/i2c-1", "present": b is not None}
    if b is not None:
        try:
            temp, rh = b.read()
            sht.update({"ok": True, "temp_c": round(temp, 2), "rh_pct": round(rh, 2)})
        except Exception as exc:
            sht.update({"ok": False, "error": str(exc)})
    out["sht4x"] = sht

    veml = {"addr": "0x10/i2c-1", "present": l is not None}
    if l is not None:
        try:
            raw, lux = l.read()
            veml.update({"ok": True, "raw": raw, "lux": round(lux, 2)})
        except Exception as exc:
            veml.update({"ok": False, "error": str(exc)})
    out["veml7700"] = veml

    adc_info = {"addr": "0x48/i2c-1", "present": a is not None}
    if a is not None:
        chans = {}
        for ch in range(4):
            samples = []
            for _ in range(6):
                try:
                    volt, _raw = a.read_voltage(ch)
                    samples.append(volt)
                except Exception as exc:
                    adc_info["error"] = str(exc)
                    break
                time.sleep(0.1)
            if samples:
                lo, hi = min(samples), max(samples)
                chans["AIN%d" % ch] = {
                    "min_v": round(lo, 3),
                    "max_v": round(hi, 3),
                    "mean_v": round(sum(samples) / len(samples), 3),
                    "swing_v": round(hi - lo, 3),
                }
        if chans:
            main = chans["AIN0"]
            adc_info.update({"ok": True, "channels": chans})
            adc_info.update({k: main[k] for k in ("min_v", "max_v", "mean_v", "swing_v")})
            # A floating input wanders hundreds of mV with no load on the bed.
            adc_info["verdict"] = "floating" if main["swing_v"] > 0.20 else "stable"
    out["ads1115"] = adc_info

    age = time.time() - camera_frame_ts if camera_frame_ts else None
    out["camera"] = {
        "ok": age is not None and age < 3.0,
        "frame_age_s": None if age is None else round(age, 1),
        "jpeg_bytes": camera_frame_bytes,
        "posture": camera_state.get("posture"),
        "posture_ts": camera_state.get("ts"),
    }

    out["config"] = {k: config[k] for k in ("fsr_in_bed", "fsr_invert", "th_up",
                                            "apnea_sec", "lux_dark", "sleep_onset_s")}
    return jsonify(out)


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(silent=True) or {}
    with cfg_lock:
        if "th_up" in data:
            config["th_up"] = float(data["th_up"])
        if "apnea_sec" in data:
            config["apnea_sec"] = float(data["apnea_sec"])
        if "fsr_in_bed" in data:
            config["fsr_in_bed"] = float(data["fsr_in_bed"])
        if "fsr_invert" in data:
            config["fsr_invert"] = bool(data["fsr_invert"])
        if "sleep_onset_s" in data:
            config["sleep_onset_s"] = float(data["sleep_onset_s"])
        if "lux_dark" in data:
            config["lux_dark"] = float(data["lux_dark"])
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
            ws.receive(timeout=10)
    except Exception:
        pass
    finally:
        with clients_lock:
            if ws in clients:
                clients.remove(ws)


def main():
    global DB, spo2_worker
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--th-up", type=float, default=1.2)
    ap.add_argument("--apnea", type=float, default=20.0)
    ap.add_argument("--fsr", type=float, default=0.100)
    ap.add_argument("--fsr-invert", action="store_true")
    ap.add_argument("--sleep-onset", type=float, default=300.0)
    args = ap.parse_args()

    DB = args.db
    config["th_up"] = args.th_up
    config["apnea_sec"] = args.apnea
    config["fsr_in_bed"] = args.fsr
    config["fsr_invert"] = bool(args.fsr_invert)
    config["sleep_onset_s"] = args.sleep_onset

    init_db()

    from spo2_worker import SpO2Worker

    spo2_worker = SpO2Worker(
        root=os.path.dirname(os.path.abspath(__file__)),
        on_sample=_on_spo2,
        on_status=_on_spo2_status,
    )

    threading.Thread(target=sensor_loop, daemon=True).start()
    threading.Thread(target=camera_loop, daemon=True).start()
    print("呼吸监测后端已启动: http://0.0.0.0:%d" % args.port, flush=True)
    print("FSR preset=%.3f V (invert=%s); camera=/camera; SpO2=1 Hz log-only"
          % (config["fsr_in_bed"], config["fsr_invert"]), flush=True)
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
