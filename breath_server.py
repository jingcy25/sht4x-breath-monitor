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
import queue
import select
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
    # FSR motion gating.  Set fsr_motion_gate=False (--no-fsr-motion) to let a
    # session fall asleep and stay asleep regardless of bed-pressure jitter --
    # needed while the FSR is uncalibrated or floating.
    "fsr_motion_gate": True,
    "fsr_motion_max": 0.05,   # motion ceiling while falling asleep
    "fsr_motion_wake": 0.08,  # motion that wakes an already-asleep session
    # Sleep-onset rule.  "Regular breathing" normally means N breaths in 30 s,
    # but a patient who stops breathing before falling asleep would then never
    # reach ASLEEP and never alarm, so a silence longer than the apnea
    # threshold also counts as sleep onset.
    "regular_min_breaths": 3,
    "silence_counts_as_sleep": True,
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

# WebSocket fan-out.  The acquisition loop must never block on socket I/O: a
# backgrounded browser tab stops draining its socket, the kernel send buffer
# fills and a blocking send() would freeze sampling (and with it the alarm
# evaluation) indefinitely.  The loop only drops the newest message here.
ws_queue = queue.Queue(maxsize=1)


def _broadcast(text: str) -> None:
    """Hand a frame to the publisher thread; never blocks."""
    try:
        ws_queue.put_nowait(text)
    except queue.Full:
        try:
            ws_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            ws_queue.put_nowait(text)
        except queue.Full:
            pass


def _drop_client(c) -> None:
    with clients_lock:
        if c in clients:
            clients.remove(c)
    try:
        c.close()
    except Exception:
        pass


def ws_publisher() -> None:
    """Single sender; skips and drops clients that are not draining."""
    while True:
        text = ws_queue.get()
        with clients_lock:
            snapshot = list(clients)
        for c in snapshot:
            sock = getattr(c, "sock", None)
            try:
                if sock is not None:
                    _, writable, _ = select.select([], [sock], [], 0.0)
                    if not writable:
                        _drop_client(c)  # not reading: do not wait for it
                        continue
                c.send(text)
            except Exception:
                _drop_client(c)

camera_lock = threading.Lock()
camera_state = {"posture": None, "confidence": None, "ts": 0.0}

camera_jpeg = b""
camera_jpeg_lock = threading.Lock()
camera_frame_ts = 0.0
camera_frame_bytes = 0

# The camera only runs while somebody is watching. Keeping a UVC camera
# streaming 24/7 is what browns out a Pi Zero 2W on a shared 5 V rail, so the
# capture thread releases the device after this many seconds without demand.
camera_demand = 0.0
CAMERA_IDLE_S = 15.0


def camera_touch():
    """Mark that a viewer wants frames (called by the HTTP endpoints)."""
    global camera_demand
    camera_demand = time.time()

# Sensor handles published by the acquisition loop so /api/selftest can probe
# the same objects without opening a second bus handle.
dev_lock = threading.Lock()
devices = {"breath": None, "light": None, "adc": None}

# One I2C transaction at a time: SHT4x needs a two-step (command, then read)
# sequence, and a second thread cutting in between makes the sensor NACK.
bus_lock = threading.Lock()


def _read_retry(fn, tries: int = 3, gap: float = 0.04):
    """Run an I2C transaction under the bus lock, retrying transient NACKs."""
    last = None
    for attempt in range(tries):
        try:
            with bus_lock:
                return fn()
        except Exception as exc:
            last = exc
            time.sleep(gap)
    raise last


def loop_watchdog() -> None:
    """Log when the acquisition loop stops publishing (frozen sampling)."""
    last_warn = 0.0
    while True:
        time.sleep(5.0)
        with latest_lock:
            t_last = float(latest.get("t") or 0.0)
        if not t_last:
            continue
        age = time.time() - t_last
        if age > 15.0 and (time.time() - last_warn) > 60.0:
            last_warn = time.time()
            print("WARNING: sensor loop stale for %.0f s" % age, flush=True)


def camera_loop():
    """Single camera reader; MJPEG clients consume the latest JPEG.

    Opening ``cv2.VideoCapture`` inside a Flask streaming generator can block
    the first response on some UVC cameras, so a background reader owns the
    device.  It only holds the device while a viewer is active: a UVC camera
    streaming forever is a real brown-out risk on a Pi Zero 2W.
    """
    try:
        import cv2
    except Exception as exc:
        print("camera: OpenCV unavailable: %s" % exc, flush=True)
        return
    global camera_jpeg, camera_frame_ts, camera_frame_bytes
    cap = None
    while True:
        if (time.time() - camera_demand) > CAMERA_IDLE_S:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
                cap = None
                print("camera: idle, device released", flush=True)
            time.sleep(1.0)
            continue
        if cap is None:
            cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
            if not cap.isOpened():
                cap = None
                print("camera: /dev/video0 open failed; retrying", flush=True)
                time.sleep(3.0)
                continue
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
                cap.set(cv2.CAP_PROP_FPS, 5)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            print("camera: /dev/video0 opened", flush=True)
        try:
            ok, frame = cap.read()
            if not ok:
                print("camera: read failed; reopening", flush=True)
                try:
                    cap.release()
                except Exception:
                    pass
                cap = None
                time.sleep(1.0)
                continue
            ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                data = jpg.tobytes()
                with camera_jpeg_lock:
                    camera_jpeg = data
                camera_frame_ts = time.time()
                camera_frame_bytes = len(data)
        except Exception as exc:
            print("camera loop error: %s" % exc, flush=True)
            try:
                cap.release()
            except Exception:
                pass
            cap = None
            time.sleep(1.0)
            continue
        time.sleep(0.03)


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
        spo2_state["last_sample_ts"] = latest["spo2_ts"]


def _on_spo2_status(msg):
    print("[spo2] %s" % msg, flush=True)
    low = msg.lower()
    with latest_lock:
        if "started" in low:
            spo2_state["status"] = "running"
            if ": " in msg:
                spo2_state["csv"] = msg.rsplit(": ", 1)[-1].strip()
        elif "exited" in low:
            spo2_state["status"] = "exited"
        elif "failed" in low or "not found" in low:
            spo2_state["status"] = "absent"
        elif "stopped" in low:
            spo2_state["status"] = "idle"
        spo2_state["detail"] = msg


spo2_worker = None  # created in main() so cwd/root is correct

# Visible SpO2 state: the sensor is optional, so the UI/API must be able to
# tell "no hardware" from "no finger" from "worker crashed".
spo2_state = {
    "status": "idle",   # idle | running | exited | absent
    "detail": "",
    "csv": None,
    "last_sample_ts": 0.0,
    "hardware": None,   # True/False from an 0x57 probe, None = not probed yet
}


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
class PostureFilter:
    """Debounce single-frame pose flips from the browser classifier.

    The TM pose model is *confident but unstable* on a blurry frame: it
    alternates lying/sitting at ~0.9 confidence, and one `sitting` frame used
    to drop the session out of ASLEEP and reset the sleep-onset timer. A new
    posture must therefore persist before it replaces the current one, and
    `sitting` (the "wake up" vote) has to persist longer than `lying`.

    This is temporal smoothing, not a new vote: the A-layer still sees one
    posture, just a stable one.
    """

    def __init__(self, hold_s=None, drop_s=8.0):
        self.current = None
        self.current_conf = None
        self.drop_s = float(drop_s)
        self.hold = {"lying": 2.0, "sitting": 3.0}
        if hold_s is not None:
            for key in self.hold:
                self.hold[key] = float(hold_s)
        self._cand = None
        self._cand_since = 0.0
        self._last_seen = 0.0

    def update(self, t, posture, conf):
        if posture in ("lying", "sitting"):
            self._last_seen = t
            if posture == self.current:
                self._cand = None
                self.current_conf = conf
            else:
                if self._cand != posture:
                    self._cand = posture
                    self._cand_since = t
                if (t - self._cand_since) >= self.hold.get(posture, 2.0):
                    self.current = posture
                    self.current_conf = conf
                    self._cand = None
        elif self.current is not None and (t - self._last_seen) > self.drop_s:
            self.current = None
            self.current_conf = None
            self._cand = None
        return self.current, self.current_conf


posture_filter = PostureFilter()


def _camera_posture(t, lux):
    """Return the *smoothed* (posture, confidence), gated by ambient light."""
    with camera_lock:
        raw = camera_state["posture"]
        confidence = camera_state["confidence"]
        ts = camera_state["ts"]
    fresh = raw is not None and (t - ts) <= 6.0
    posture, smoothed_conf = posture_filter.update(t, raw if fresh else None,
                                                   confidence if fresh else None)
    if posture is None:
        return None, None
    # Camera only works when there is light; VEML is the gate.
    if lux is not None and lux < config["lux_dark"]:
        return None, None
    return posture, smoothed_conf


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
                    fsr, _ = _read_retry(lambda: adc.read_voltage(0))
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
                motion_gate = bool(config["fsr_motion_gate"])
                motion_max = float(config["fsr_motion_max"])
                motion_wake = float(config["fsr_motion_wake"])
                min_breaths = int(config["regular_min_breaths"])
                silence_counts = bool(config["silence_counts_as_sleep"])
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
                # A pause that is still open when the session ends must be
                # recorded, otherwise the alarm is lost from the events table.
                if pause_start is not None and not bed_candidate:
                    try:
                        conn.execute(
                            "INSERT INTO events(start_ts,alarm_ts,end_ts,duration) "
                            "VALUES(?,?,?,?)",
                            (pause_start, pause_start + apnea_sec, t, t - pause_start),
                        )
                        conn.commit()
                    except Exception as exc:
                        print("event insert failed: %s" % exc, flush=True)
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
                print("bed state: %s (a_state=%s)" % ("in_bed" if in_bed else "out_of_bed", a_state), flush=True)

            # slow sensors (VEML only; environment SHT4x removed)
            if (t - last_slow) > 2.0:
                if light_sensor is not None:
                    try:
                        _, lux = _read_retry(light_sensor.read)
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
                    temp, h = _read_retry(breath_sensor.read)
                except Exception as exc:
                    temp, h = last_temp, last_h
                    print("SHT4x read error: %s" % exc, flush=True)
                last_temp, last_h = temp, h
                signal, breath = det.update(t, h)
                if breath:
                    last_breath = t
                    breath_times.append(t)

                silence_s = t - last_breath
                breaths_30 = sum(1 for x in breath_times if t - x <= 30.0)
                regular = breaths_30 >= min_breaths
                if not regular and silence_counts and silence_s > apnea_sec:
                    # A silence longer than the apnea threshold is itself a
                    # sleep-onset signal: otherwise a patient who stops
                    # breathing before the onset window is never ASLEEP and
                    # therefore never alarms.
                    regular = True
                sleep_context = (
                    posture == "lying"
                    or (posture is None and (lux is None or lux < lux_dark))
                )

                if posture == "sitting":
                    a_state = "awake"
                    sleep_since = None
                elif sleep_context:
                    if regular and (not motion_gate or fsr_motion < motion_max):
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

                if motion_gate and fsr_motion > motion_wake and a_state == "asleep":
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
                            last_temp, last_h = _read_retry(breath_sensor.read)
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
                "spo2_state": spo2_state["status"],
            }

            with latest_lock:
                latest.update(msg)

            text = json.dumps(msg)
            _broadcast(text)

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
    camera_touch()

    def gen():
        last = None
        while True:
            camera_touch()
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
    camera_touch()
    for _ in range(40):  # give the capture thread time to open the device
        with camera_jpeg_lock:
            jpg = camera_jpeg
        if jpg:
            break
        time.sleep(0.1)
    if not jpg:
        return Response("camera not ready", status=503, mimetype="text/plain")
    return Response(jpg, mimetype="image/jpeg")


@app.route("/api/camera", methods=["POST"])
def api_camera():
    camera_touch()
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
            temp, rh = _read_retry(b.read)
            sht.update({"ok": True, "temp_c": round(temp, 2), "rh_pct": round(rh, 2)})
        except Exception as exc:
            sht.update({"ok": False, "error": str(exc)})
    out["sht4x"] = sht

    veml = {"addr": "0x10/i2c-1", "present": l is not None}
    if l is not None:
        try:
            raw, lux = _read_retry(l.read)
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
                    volt, _raw = _read_retry(lambda: a.read_voltage(ch))
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

    # SpO2 is optional hardware; say explicitly whether 0x57 answers at all.
    hw = _i2c_present(1, 0x57, timeout_s=1.0, tries=1)
    spo2_state["hardware"] = hw
    last_ts = spo2_state["last_sample_ts"]
    out["spo2"] = {
        "addr": "0x57/i2c-1",
        "hardware_present": hw,
        "worker": spo2_state["status"],
        "detail": spo2_state["detail"],
        "csv": spo2_state["csv"],
        "last_sample_age_s": None if not last_ts else round(time.time() - last_ts, 1),
        "hint": ("MAX30102 未接：查 3.3V / GND / SDA(Pin3) / SCL(Pin5)，"
                 "i2cdetect -y 1 应看到 0x57") if not hw else
                ("硬件在位；只有 in_bed 时才启动采集，手指贴住后 UI 才有读数"),
    }

    out["config"] = {k: config[k] for k in ("fsr_in_bed", "fsr_invert", "th_up",
                                            "apnea_sec", "lux_dark", "sleep_onset_s",
                                            "fsr_motion_gate", "fsr_motion_max",
                                            "fsr_motion_wake", "regular_min_breaths",
                                            "silence_counts_as_sleep")}
    return jsonify(out)


@app.route("/api/report")
def api_report():
    """One-click sleep report: a print-ready HTML page for the last N hours."""
    try:
        hours = float(request.args.get("hours", 12))
    except Exception:
        hours = 12.0
    hours = max(1.0, min(72.0, hours))
    now = time.time()
    since = now - hours * 3600.0

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # --- A-state segments ---------------------------------------------------
    cur.execute("SELECT ts, state FROM a_states WHERE ts >= ? ORDER BY ts",
                (since - 3600.0,))
    rows = cur.fetchall()
    segs = []
    for i, (ts, st) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else now
        if end <= since:
            continue
        segs.append((max(ts, since), min(end, now), st))

    dur = {"awake": 0.0, "asleep": 0.0, "gap": 0.0, "standby": 0.0}
    for a, b, st in segs:
        dur[st] = dur.get(st, 0.0) + max(0.0, b - a)
    in_bed_s = dur.get("awake", 0.0) + dur.get("asleep", 0.0)

    # --- samples ------------------------------------------------------------
    cur.execute("SELECT COUNT(*), SUM(breath), MIN(humidity), AVG(humidity), "
                "MAX(humidity) FROM samples WHERE ts >= ?", (since,))
    n_s, n_breath, h_min, h_avg, h_max = cur.fetchone()
    n_s = n_s or 0
    n_breath = n_breath or 0
    sample_hz = 20.0
    covered_s = n_s / sample_hz if n_s else 0.0

    cur.execute("SELECT CAST(ts/3600 AS INTEGER) h, SUM(breath), COUNT(*) "
                "FROM samples WHERE ts >= ? GROUP BY h ORDER BY h", (since,))
    hourly = [(int(h), int(b or 0), int(c or 0)) for h, b, c in cur.fetchall()]

    # --- events -------------------------------------------------------------
    cur.execute("SELECT start_ts, alarm_ts, end_ts, duration FROM events "
                "WHERE start_ts >= ? ORDER BY start_ts", (since,))
    events = cur.fetchall()

    # --- SpO2 ---------------------------------------------------------------
    cur.execute("SELECT COUNT(*), AVG(spo2), MIN(spo2), AVG(hr) FROM spo2_samples "
                "WHERE ts >= ? AND valid = 1", (since,))
    sp_n, sp_avg, sp_min, hr_avg = cur.fetchone()
    conn.close()

    def hm(sec):
        sec = int(max(0, sec))
        if sec < 60:
            return "%d s" % sec
        m, s = divmod(sec, 60)
        if m < 60:
            return "%d min %02d s" % (m, s)
        h, m = divmod(m, 60)
        return "%d h %02d min" % (h, m)

    def clock(ts):
        return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))

    kpi = [
        ("在床时长", hm(in_bed_s)),
        ("入睡时长 (ASLEEP)", hm(dur.get("asleep", 0.0))),
        ("清醒时长 (AWAKE)", hm(dur.get("awake", 0.0))),
        ("暂停事件", "%d 次" % len(events)),
        ("总暂停时长", hm(sum(float(e[3] or 0) for e in events))),
        ("最长暂停", hm(max([float(e[3] or 0) for e in events], default=0.0))),
        ("平均呼吸率", ("%.1f 次/分" % (n_breath * 60.0 / covered_s)) if covered_s > 60 else "—"),
        ("口鼻湿度", ("%.1f → %.1f %%RH（均 %.1f）" % (h_min or 0, h_max or 0, h_avg or 0))
         if n_s else "—"),
    ]

    kpi_html = "".join(
        '<div class="kpi"><div class="k">%s</div><div class="v">%s</div></div>' % (k, v)
        for k, v in kpi
    )

    state_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % ({"awake": "清醒", "asleep": "已入睡", "gap": "离床（间隙）",
            "standby": "待机"}.get(st, st), clock(a), clock(b), hm(b - a))
        for a, b, st in segs[-40:]
    ) or '<tr><td colspan="4" class="muted">该时段没有状态记录</td></tr>'

    max_rate = 0.0
    hourly_rows = []
    for h, b, c in hourly:
        secs = c / sample_hz
        rate = (b * 60.0 / secs) if secs > 30 else 0.0
        max_rate = max(max_rate, rate)
        hourly_rows.append((h, b, rate, secs))

    hour_html = ""
    for h, b, rate, secs in hourly_rows:
        width = 0 if max_rate <= 0 else int(100 * rate / max_rate)
        hour_html += (
            '<tr><td>%s</td><td>%d</td><td>%.1f</td>'
            '<td class="bar"><i style="width:%d%%"></i></td></tr>'
            % (time.strftime("%m-%d %H:00", time.localtime(h * 3600)), b, rate, width)
        )
    hour_html = hour_html or '<tr><td colspan="4" class="muted">无样本</td></tr>'

    ev_html = "".join(
        "<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (i + 1, clock(s), clock(a), hm(float(d or 0)))
        for i, (s, a, e, d) in enumerate(events)
    ) or '<tr><td colspan="4" class="muted">该时段未记录到呼吸暂停事件</td></tr>'

    if sp_n:
        spo2_html = ("<p><b>%d</b> 个有效样本，平均 <b>%.1f%%</b>，最低 <b>%.0f%%</b>，"
                     "平均心率 <b>%.0f bpm</b>。</p>" % (sp_n, sp_avg or 0, sp_min or 0, hr_avg or 0))
    else:
        spo2_html = ('<p class="muted">该时段没有有效血氧样本（MAX30102 未接或未佩戴）。'
                     '当前状态：%s。</p>' % (spo2_state["status"]))

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>睡眠报告 %(range)s</title>
<style>
  :root{--ink:#11161c;--muted:#6b7684;--line:#dfe5ec;--jade:#1f9d63;--amber:#b7791f}
  *{box-sizing:border-box}
  body{margin:0;padding:32px;background:#f6f8fa;color:var(--ink);
       font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
  .sheet{max-width:900px;margin:0 auto;background:#fff;padding:36px 40px 44px;
         border:1px solid var(--line);border-radius:14px}
  h1{margin:0 0 6px;font-size:24px}
  .sub{color:var(--muted);font-size:13px;line-height:1.7}
  h2{font-size:15px;margin:30px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
  .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}
  .kpi>div{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
  .kpi .k{font-size:11px;color:var(--muted)}
  .kpi .v{font-size:16px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
  table{width:100%%;border-collapse:collapse;font-size:13px}
  th,td{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left}
  th{color:var(--muted);font-weight:600;font-size:12px}
  td.bar{width:34%%}
  td.bar i{display:block;height:9px;background:var(--jade);border-radius:3px;min-width:1px}
  .muted{color:var(--muted)}
  .note{font-size:12px;color:var(--muted);line-height:1.8;margin-top:12px}
  .toolbar{margin-top:26px;display:flex;gap:10px}
  button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--line);
         background:#fff;cursor:pointer}
  @media print{body{background:#fff;padding:0}.sheet{border:0;border-radius:0}.toolbar{display:none}}
</style></head><body><div class="sheet">
  <h1>睡眠呼吸监测报告</h1>
  <div class="sub">统计区间：%(start)s — %(end)s（最近 %(hours).0f 小时）<br>
    设备：raspberrypi-09 · 生成于 %(now)s · 采样约 %(hz).0f Hz</div>

  <div class="kpis">%(kpi)s</div>

  <h2>A 状态时间线</h2>
  <table><thead><tr><th>状态</th><th>开始</th><th>结束</th><th>时长</th></tr></thead>
  <tbody>%(states)s</tbody></table>

  <h2>呼吸暂停事件</h2>
  <table><thead><tr><th>#</th><th>暂停开始</th><th>触发报警</th><th>总暂停时长</th></tr></thead>
  <tbody>%(events)s</tbody></table>

  <h2>每小时呼吸率</h2>
  <table><thead><tr><th>小时</th><th>呼吸次数</th><th>次/分</th><th>相对强度</th></tr></thead>
  <tbody>%(hours_rows)s</tbody></table>

  <h2>血氧 / 心率</h2>
  %(spo2)s

  <div class="note">
    判定口径：A 层由 FSR 在床锚点 +（开灯时相机躺/坐，关灯时光照）+ 口鼻温湿度呼吸共同决定；
    只有进入「已入睡」后，连续无呼吸 &gt; %(apnea).0f s 才触发报警（当前入睡判定时长 %(onset).0f s）。<br>
    局限：SHT4x 单通道易受说话、翻身、口鼻偏移影响；FSR 未标定时在床判定不可靠；
    本报告为课程演示用途，不构成医学结论。
  </div>

  <div class="toolbar">
    <button onclick="window.print()">打印 / 存为 PDF</button>
    <button onclick="location.href='/api/report.csv?hours=%(hours).0f'">导出 CSV</button>
  </div>
</div></body></html>""" % {
        "range": time.strftime("%m-%d %H:%M", time.localtime(since)),
        "start": clock(since),
        "end": clock(now),
        "now": clock(now),
        "hours": hours,
        "hz": sample_hz,
        "kpi": kpi_html,
        "states": state_rows,
        "events": ev_html,
        "hours_rows": hour_html,
        "spo2": spo2_html,
        "apnea": config["apnea_sec"],
        "onset": config["sleep_onset_s"],
    }
    return Response(html, mimetype="text/html")


@app.route("/api/report.csv")
def api_report_csv():
    """Same window as /api/report, but raw rows for the appendix."""
    try:
        hours = float(request.args.get("hours", 12))
    except Exception:
        hours = 12.0
    hours = max(1.0, min(72.0, hours))
    since = time.time() - hours * 3600.0
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ts, humidity, signal, breath, apnea FROM samples WHERE ts >= ? "
        "ORDER BY ts", (since,)).fetchall()
    conn.close()

    def gen():
        yield "ts,iso_time,humidity,signal,breath,apnea\n"
        for ts, hum, sig, br, ap in rows:
            yield "%s,%s,%s,%s,%s,%s\n" % (
                ts, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                hum, sig, br, ap)
    return Response(gen(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sleep_report.csv"})


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
        if "fsr_motion_gate" in data:
            config["fsr_motion_gate"] = bool(data["fsr_motion_gate"])
        if "fsr_motion_max" in data:
            config["fsr_motion_max"] = float(data["fsr_motion_max"])
        if "fsr_motion_wake" in data:
            config["fsr_motion_wake"] = float(data["fsr_motion_wake"])
        if "regular_min_breaths" in data:
            config["regular_min_breaths"] = int(data["regular_min_breaths"])
        if "silence_counts_as_sleep" in data:
            config["silence_counts_as_sleep"] = bool(data["silence_counts_as_sleep"])
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
    ap.add_argument(
        "--no-fsr-motion",
        action="store_true",
        help="ignore FSR motion entirely (both the fall-asleep ceiling and the "
             "wake-on-motion rule); useful while the FSR is uncalibrated",
    )
    ap.add_argument("--sleep-onset", type=float, default=300.0)
    args = ap.parse_args()

    DB = args.db
    config["th_up"] = args.th_up
    config["apnea_sec"] = args.apnea
    config["fsr_in_bed"] = args.fsr
    config["fsr_invert"] = bool(args.fsr_invert)
    config["sleep_onset_s"] = args.sleep_onset
    if args.no_fsr_motion:
        config["fsr_motion_gate"] = False

    init_db()

    from spo2_worker import SpO2Worker

    spo2_worker = SpO2Worker(
        root=os.path.dirname(os.path.abspath(__file__)),
        on_sample=_on_spo2,
        on_status=_on_spo2_status,
    )

    threading.Thread(target=sensor_loop, daemon=True).start()
    threading.Thread(target=camera_loop, daemon=True).start()
    threading.Thread(target=ws_publisher, daemon=True).start()
    threading.Thread(target=loop_watchdog, daemon=True).start()
    print("呼吸监测后端已启动: http://0.0.0.0:%d" % args.port, flush=True)
    print("FSR preset=%.3f V (invert=%s); camera=/camera; SpO2=1 Hz log-only"
          % (config["fsr_in_bed"], config["fsr_invert"]), flush=True)
    print("sleep onset=%.0f s; FSR motion gate=%s"
          % (config["sleep_onset_s"], "on" if config["fsr_motion_gate"] else "OFF"),
          flush=True)
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
