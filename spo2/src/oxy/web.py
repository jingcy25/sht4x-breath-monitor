"""Browser UI for the PPG breath-hold detector.

The page exists to collect *ground truth*: the operator presses a button before
holding their breath and again when breathing resumes. Those labels are written
next to the detector's own state and score, which is exactly what is needed to
measure latency and false positives on real data instead of guessing thresholds.

Deliberately dependency-free: ``http.server`` + ``json`` + a static HTML file,
so it runs on a fresh Raspberry Pi OS image with nothing installed.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

HTML_PATH = Path(__file__).with_name("web_hold.html")

LABEL_HEADER = [
    "wall_time",
    "monotonic_s",
    "event",
    "state",
    "score",
    "hr_bpm",
    "spo2_pct",
    "resp_mod",
    "rsa",
    "pwa",
]

VALID_EVENTS = ("hold_start", "hold_end", "normal_start", "normal_end")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class HoldWebApp:
    """Shared state between the acquisition thread and the HTTP server."""

    def __init__(
        self,
        labels_path: str,
        threshold: float = 0.55,
        on_recalibrate: Optional[Callable[[], None]] = None,
        history_limit: int = 600,
    ) -> None:
        self.labels_path = labels_path
        self.threshold = float(threshold)
        self.on_recalibrate = on_recalibrate

        self._lock = threading.Lock()
        self._labels: List[Dict[str, Any]] = []
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history_limit)
        self._latest: Dict[str, Any] = {
            "t": 0.0,
            "state": "warming",
            "score": 0.0,
            "confidence": 0.0,
            "hr": None,
            "spo2": None,
            "pi": None,
            "sqi": None,
            "finger": False,
            "features": {},
            "drops": {},
            "baseline": None,
            "calib_progress": None,
        }
        self._ensure_labels_file()

    def _ensure_labels_file(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.labels_path))
        os.makedirs(directory, exist_ok=True)
        if not os.path.exists(self.labels_path):
            with open(self.labels_path, "w", newline="", encoding="utf-8") as handle:
                handle.write(",".join(LABEL_HEADER) + "\n")

    # ---------------------------------------------------------------- state
    def publish(self, result: Dict[str, Any], hold: Dict[str, Any]) -> None:
        record = {
            "t": hold.get("t", 0.0),
            "state": hold.get("state", "warming"),
            "score": hold.get("score", 0.0),
            "confidence": hold.get("confidence", 0.0),
            "hr": result.get("hr_bpm"),
            "spo2": result.get("spo2_pct"),
            "pi": result.get("pi_pct"),
            "sqi": result.get("sqi"),
            "finger": bool(result.get("finger")),
            "features": hold.get("features", {}),
            "drops": hold.get("drops", {}),
            "baseline": hold.get("baseline"),
            "calib_progress": hold.get("calib_progress"),
        }
        with self._lock:
            self._latest = record
            self._history.append(
                {
                    "t": record["t"],
                    "state": record["state"],
                    "score": record["score"],
                    "hr": record["hr"],
                    "spo2": record["spo2"],
                }
            )

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            payload = dict(self._latest)
            payload["threshold"] = self.threshold
            payload["label_count"] = len(self._labels)
            payload["labels"] = list(self._labels[-12:])
        return payload

    def history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)

    def add_label(self, event: str) -> Dict[str, Any]:
        if event not in VALID_EVENTS:
            raise ValueError("unknown event %r" % event)

        snapshot = self.snapshot()
        features = snapshot.get("features") or {}

        def number(value: Any) -> str:
            return "" if value is None else "%g" % round(float(value), 4)

        row = [
            _now_iso(),
            "%.3f" % float(snapshot.get("t") or 0.0),
            event,
            snapshot.get("state", ""),
            number(snapshot.get("score")),
            number(snapshot.get("hr")),
            number(snapshot.get("spo2")),
            number(features.get("resp_mod")),
            number(features.get("rsa")),
            number(features.get("pwa")),
        ]
        with self._lock:
            with open(self.labels_path, "a", newline="", encoding="utf-8") as handle:
                handle.write(",".join(row) + "\n")
            label = {
                "wall_time": row[0],
                "t": float(snapshot.get("t") or 0.0),
                "event": event,
                "state": snapshot.get("state", ""),
                "score": snapshot.get("score", 0.0),
            }
            self._labels.append(label)
        return label

    def request_recalibration(self) -> bool:
        if self.on_recalibrate is None:
            return False
        self.on_recalibrate()
        return True

    # ---------------------------------------------------------------- server
    def serve(self, host: str = "0.0.0.0", port: int = 8001) -> ThreadingHTTPServer:
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "oxy-hold/0.1"

            def log_message(self, *args: Any) -> None:
                return

            def _send(self, code: int, body: bytes, content_type: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, payload: Any) -> None:
                self._send(
                    code,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )

            def do_GET(self) -> None:  # noqa: N802
                if self.path in ("/", "/index.html"):
                    try:
                        body = HTML_PATH.read_bytes()
                    except OSError:
                        self._json(500, {"error": "web_hold.html not found"})
                        return
                    self._send(200, body, "text/html; charset=utf-8")
                elif self.path.startswith("/api/state"):
                    self._json(200, app.snapshot())
                elif self.path.startswith("/api/history"):
                    self._json(200, {"records": app.history()})
                elif self.path.startswith("/api/labels"):
                    self._json(200, {"labels": app.snapshot().get("labels", [])})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    self._json(400, {"error": "invalid JSON"})
                    return

                if self.path.startswith("/api/label"):
                    try:
                        label = app.add_label(str(payload.get("event", "")))
                    except ValueError as exc:
                        self._json(400, {"error": str(exc)})
                        return
                    self._json(200, {"ok": True, "label": label})
                elif self.path.startswith("/api/calibrate"):
                    self._json(200, {"ok": app.request_recalibration()})
                else:
                    self._json(404, {"error": "not found"})

        server = ThreadingHTTPServer((host, port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server
