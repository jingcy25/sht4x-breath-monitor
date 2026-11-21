"""Acquisition loop: FIFO -> ring buffer -> analysis -> CSV + status line.

The loop is deliberately split into three pieces so a second data source can be
added later (microphone / camera for breath-hold detection) without touching the
CSV or the analysis code:

* ``_append``  : acquisition
* ``_emit``    : feature extraction + persistence
* ``_write_*`` : output
"""

from __future__ import annotations

import csv
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol, Tuple

import numpy as np

from . import dsp

LOGGER = logging.getLogger(__name__)

SUMMARY_HEADER = [
    "timestamp_iso",
    "monotonic_s",
    "hr_bpm",
    "spo2_pct",
    "r_ratio",
    "pi_pct",
    "sqi",
    "finger",
    "valid",
    "red_dc",
    "ir_dc",
    "window_samples",
]

RAW_HEADER = ["timestamp_iso", "monotonic_s", "red", "ir"]

HOLD_EXTRA_HEADER = [
    "hold_state",
    "hold_score",
    "resp_mod",
    "rsa",
    "pwa",
    "hr_delta",
]


class DeviceProtocol(Protocol):
    def read_fifo(self) -> Tuple[list, bool]: ...

    def close(self) -> None: ...


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().isoformat(
        timespec="milliseconds"
    )


def _format_optional(value: Optional[float], decimals: int) -> str:
    return "--" if value is None else ("%." + str(decimals) + "f") % value


class Monitor:
    """Poll the sensor, analyse a sliding window, write two CSV streams."""

    def __init__(
        self,
        device: DeviceProtocol,
        fs: float = 100.0,
        window_s: float = 5.0,
        poll_interval_s: float = 0.2,
        emit_interval_s: float = 1.0,
        clock: Any = None,
        coeffs: Tuple[float, float, float] = dsp.DEFAULT_COEFFS,
        min_sqi: float = 0.55,
        finger_dc_ir: float = 5000.0,
        min_pi: float = 0.02,
        max_pi: float = 5.0,
        hr_min: float = 30.0,
        hr_max: float = 240.0,
        summary_path: Optional[str] = None,
        raw_path: Optional[str] = None,
        raw_stride: int = 1,
        quiet: bool = False,
        hold_detector: Any = None,
        jsonl_path: Optional[str] = None,
        threaded: Optional[bool] = None,
    ) -> None:
        self.device = device
        self.fs = float(fs)
        self.window_s = float(window_s)
        self.poll_interval_s = float(poll_interval_s)
        self.emit_interval_s = float(emit_interval_s)
        self.clock = clock if clock is not None else time
        self.coeffs = coeffs
        self.min_sqi = float(min_sqi)
        self.finger_dc_ir = float(finger_dc_ir)
        self.min_pi = float(min_pi)
        self.max_pi = float(max_pi)
        self.hr_min = float(hr_min)
        self.hr_max = float(hr_max)
        self.summary_path = summary_path
        self.raw_path = raw_path
        self.raw_stride = max(1, int(raw_stride))
        self.quiet = quiet
        self.hold_detector = hold_detector
        self.jsonl_path = jsonl_path
        # Hardware runs threaded so a slow analysis cannot starve the FIFO.
        # Simulated runs keep the single-threaded, deterministic path.
        self.threaded = bool(threaded) if threaded is not None else (clock is None)
        self._lock = threading.Lock()
        self._running = False

        required_s = self.window_s
        if hold_detector is not None:
            required_s = max(required_s, float(getattr(hold_detector, "slow_window_s", required_s)))
        self._maxlen = max(32, int(round(required_s * self.fs)))
        self._red = np.zeros(0, dtype=float)
        self._ir = np.zeros(0, dtype=float)
        self._sample_count = 0
        self._summary_file = None
        self._summary_writer = None
        self._raw_file = None
        self._raw_writer = None
        self._jsonl_file = None
        self._last_hold_state: Optional[str] = None

        self._wall_start = time.time()
        self.stats: Dict[str, Any] = {
            "emitted": 0,
            "valid": 0,
            "valid_ratio": 0.0,
            "mean_hr": None,
            "mean_spo2": None,
            "overflow_events": 0,
            "hold_seconds": 0,
            "hold_transitions": [],
        }
        self._hr_sum = 0.0
        self._spo2_sum = 0.0

    # ------------------------------------------------------------- plumbing
    def _open_outputs(self) -> None:
        if self.summary_path:
            directory = os.path.dirname(os.path.abspath(self.summary_path))
            os.makedirs(directory, exist_ok=True)
            self._summary_file = open(self.summary_path, "w", newline="", encoding="utf-8")
            self._summary_writer = csv.writer(self._summary_file)
            header = list(SUMMARY_HEADER)
            if self.hold_detector is not None:
                header += HOLD_EXTRA_HEADER
            self._summary_writer.writerow(header)

        if self.raw_path:
            directory = os.path.dirname(os.path.abspath(self.raw_path))
            os.makedirs(directory, exist_ok=True)
            self._raw_file = open(self.raw_path, "w", newline="", encoding="utf-8")
            self._raw_writer = csv.writer(self._raw_file)
            self._raw_writer.writerow(RAW_HEADER)

        if self.jsonl_path:
            directory = os.path.dirname(os.path.abspath(self.jsonl_path))
            os.makedirs(directory, exist_ok=True)
            self._jsonl_file = open(self.jsonl_path, "w", encoding="utf-8")

    def _close_outputs(self) -> None:
        for handle in (self._summary_file, self._raw_file, self._jsonl_file):
            if handle is not None:
                try:
                    handle.flush()
                    handle.close()
                except Exception:  # pragma: no cover - shutdown best effort
                    LOGGER.exception("failed to close output file")
        self._summary_file = None
        self._raw_file = None
        self._jsonl_file = None

    def _append(self, samples: list) -> None:
        if not samples:
            return
        red_chunk = np.fromiter((sample[0] for sample in samples), dtype=float)
        ir_chunk = np.fromiter((sample[1] for sample in samples), dtype=float)

        with self._lock:
            self._red = np.concatenate((self._red, red_chunk))[-self._maxlen :]
            self._ir = np.concatenate((self._ir, ir_chunk))[-self._maxlen :]

        if self._raw_writer is not None:
            for offset, (red, ir) in enumerate(samples):
                if offset % self.raw_stride:
                    continue
                mono = (self._sample_count + offset) / self.fs
                self._raw_writer.writerow(
                    [_iso(self._wall_start + mono), "%.3f" % mono, int(red), int(ir)]
                )
        self._sample_count += len(samples)

    # ------------------------------------------------------------ per-1s row
    def _emit(self, elapsed_s: float) -> Dict[str, Any]:
        minimum = int(round(2.0 * self.fs))
        if self._red.size < minimum:
            return {"valid": False, "finger": False, "warmup": True}

        with self._lock:
            red_snapshot = self._red
            ir_snapshot = self._ir

        window_samples = max(32, int(round(self.window_s * self.fs)))
        result = dsp.analyze_window(
            red_snapshot[-window_samples:],
            ir_snapshot[-window_samples:],
            self.fs,
            coeffs=self.coeffs,
            min_sqi=self.min_sqi,
            finger_dc_ir=self.finger_dc_ir,
            min_pi=self.min_pi,
            max_pi=self.max_pi,
            hr_min=self.hr_min,
            hr_max=self.hr_max,
        )

        self.stats["emitted"] += 1
        if result["valid"]:
            self.stats["valid"] += 1
            self._hr_sum += float(result["hr_bpm"])
            self._spo2_sum += float(result["spo2_pct"])

        hold_record = None
        if self.hold_detector is not None:
            hold_record = self.hold_detector.update(red_snapshot, ir_snapshot, elapsed_s)
            if hold_record["state"] == "hold":
                self.stats["hold_seconds"] += 1
            if (
                self._last_hold_state is not None
                and hold_record["state"] != self._last_hold_state
            ):
                self.stats["hold_transitions"].append(
                    {
                        "t": round(elapsed_s, 1),
                        "from": self._last_hold_state,
                        "to": hold_record["state"],
                    }
                )
            self._last_hold_state = hold_record["state"]
            if self._jsonl_file is not None:
                self._jsonl_file.write(json.dumps(hold_record, ensure_ascii=False) + "\n")
                self._jsonl_file.flush()

        if self._summary_writer is not None:
            row = [
                _iso(self._wall_start + elapsed_s),
                "%.3f" % elapsed_s,
                "" if result["hr_bpm"] is None else "%.1f" % result["hr_bpm"],
                "" if result["spo2_pct"] is None else "%.1f" % result["spo2_pct"],
                "" if result["r_ratio"] is None else "%.4f" % result["r_ratio"],
                "" if result["pi_pct"] is None else "%.3f" % result["pi_pct"],
                "%.3f" % result["sqi"],
                int(result["finger"]),
                int(result["valid"]),
                int(result["red_dc"]),
                int(result["ir_dc"]),
                int(result["window_samples"]),
            ]
            if hold_record is not None:
                features = hold_record["features"]
                row += [
                    hold_record["state"],
                    "%.3f" % hold_record["score"],
                    _format_optional(features.get("resp_mod"), 4),
                    _format_optional(features.get("rsa"), 3),
                    _format_optional(features.get("pwa"), 3),
                    _format_optional(features.get("hr_delta"), 1),
                ]
            self._summary_writer.writerow(row)

        if not self.quiet:
            if not result["finger"]:
                state = "NO FINGER"
            elif not result["valid"]:
                state = "LOW SIGNAL"
            else:
                state = "OK"
            line = "t=%6.1fs  HR=%s bpm  SpO2=%s%%  PI=%s%%  SQI=%.2f  red=%6d ir=%6d  [%s]" % (
                elapsed_s,
                _format_optional(result["hr_bpm"], 0),
                _format_optional(result["spo2_pct"], 1),
                _format_optional(result["pi_pct"], 3),
                result["sqi"],
                int(result["red_dc"]),
                int(result["ir_dc"]),
                state,
            )
            if hold_record is not None:
                line += "  HOLD=%s %s" % (
                    _format_optional(hold_record["score"], 2),
                    hold_record["state"],
                )
            print(line, flush=True)
        return result

    def _read_loop(self, samples_per_poll: int) -> None:
        """Drain the FIFO independently of how slow the analysis is."""
        while self._running:
            try:
                samples, overflowed = self.device.read_fifo()
                if overflowed:
                    with self._lock:
                        self.stats["overflow_events"] += 1
                    LOGGER.warning("FIFO overflow detected; samples were lost")
                if samples:
                    self._append(samples)
                    if len(samples) < samples_per_poll:
                        LOGGER.debug("short FIFO read: %d samples", len(samples))
            except Exception:
                LOGGER.exception("FIFO read failed; retrying")
            self.clock.sleep(self.poll_interval_s)

    # ------------------------------------------------------------------ run
    def run(self, duration_s: float = 0.0) -> Dict[str, Any]:
        """Run until ``duration_s`` elapses (0 means until interrupted)."""
        self._open_outputs()
        start = self.clock.monotonic()
        last_emit = start
        samples_per_poll = max(1, int(round(self.poll_interval_s * self.fs)))

        self._running = True
        reader: Optional[threading.Thread] = None
        if self.threaded:
            reader = threading.Thread(
                target=self._read_loop, args=(samples_per_poll,), daemon=True
            )
            reader.start()

        try:
            try:
                while True:
                    elapsed = self.clock.monotonic() - start
                    if duration_s > 0 and elapsed >= duration_s:
                        break

                    if not self.threaded:
                        samples, overflowed = self.device.read_fifo()
                        if overflowed:
                            self.stats["overflow_events"] += 1
                            LOGGER.warning("FIFO overflow detected; samples were lost")
                        self._append(samples)

                        if 0 < len(samples) < samples_per_poll:
                            LOGGER.warning(
                                "short FIFO read (%d samples); the Pi is falling behind",
                                len(samples),
                            )

                    now = self.clock.monotonic()
                    if now - last_emit >= self.emit_interval_s - 1e-6:
                        result = self._emit(now - start)
                        if not result.get("warmup"):
                            last_emit = now

                    if self.threaded:
                        self.clock.sleep(min(0.05, self.emit_interval_s))
                    else:
                        self.clock.sleep(self.poll_interval_s)
            except KeyboardInterrupt:
                LOGGER.info("acquisition interrupted by user")
        finally:
            self._running = False
            if reader is not None:
                reader.join(timeout=2.0)
            self._close_outputs()

        emitted = self.stats["emitted"]
        valid = self.stats["valid"]
        self.stats["valid_ratio"] = (valid / emitted) if emitted else 0.0
        self.stats["mean_hr"] = (self._hr_sum / valid) if valid else None
        self.stats["mean_spo2"] = (self._spo2_sum / valid) if valid else None
        self.stats["elapsed_s"] = self.clock.monotonic() - start
        return self.stats
