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
import logging
import os
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

        self._maxlen = max(32, int(round(self.window_s * self.fs)))
        self._red = np.zeros(0, dtype=float)
        self._ir = np.zeros(0, dtype=float)
        self._sample_count = 0
        self._summary_file = None
        self._summary_writer = None
        self._raw_file = None
        self._raw_writer = None

        self._wall_start = time.time()
        self.stats: Dict[str, Any] = {
            "emitted": 0,
            "valid": 0,
            "valid_ratio": 0.0,
            "mean_hr": None,
            "mean_spo2": None,
            "overflow_events": 0,
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
            self._summary_writer.writerow(SUMMARY_HEADER)

        if self.raw_path:
            directory = os.path.dirname(os.path.abspath(self.raw_path))
            os.makedirs(directory, exist_ok=True)
            self._raw_file = open(self.raw_path, "w", newline="", encoding="utf-8")
            self._raw_writer = csv.writer(self._raw_file)
            self._raw_writer.writerow(RAW_HEADER)

    def _close_outputs(self) -> None:
        for handle in (self._summary_file, self._raw_file):
            if handle is not None:
                try:
                    handle.flush()
                    handle.close()
                except Exception:  # pragma: no cover - shutdown best effort
                    LOGGER.exception("failed to close output file")
        self._summary_file = None
        self._raw_file = None

    def _append(self, samples: list) -> None:
        if not samples:
            return
        red_chunk = np.fromiter((sample[0] for sample in samples), dtype=float)
        ir_chunk = np.fromiter((sample[1] for sample in samples), dtype=float)

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

        result = dsp.analyze_window(
            self._red,
            self._ir,
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

        if self._summary_writer is not None:
            self._summary_writer.writerow(
                [
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
            )

        if not self.quiet:
            if not result["finger"]:
                state = "NO FINGER"
            elif not result["valid"]:
                state = "LOW SIGNAL"
            else:
                state = "OK"
            print(
                "t=%6.1fs  HR=%s bpm  SpO2=%s%%  PI=%s%%  SQI=%.2f  red=%6d ir=%6d  [%s]"
                % (
                    elapsed_s,
                    _format_optional(result["hr_bpm"], 0),
                    _format_optional(result["spo2_pct"], 1),
                    _format_optional(result["pi_pct"], 3),
                    result["sqi"],
                    int(result["red_dc"]),
                    int(result["ir_dc"]),
                    state,
                ),
                flush=True,
            )
        return result

    # ------------------------------------------------------------------ run
    def run(self, duration_s: float = 0.0) -> Dict[str, Any]:
        """Run until ``duration_s`` elapses (0 means until interrupted)."""
        self._open_outputs()
        start = self.clock.monotonic()
        last_emit = start
        samples_per_poll = max(1, int(round(self.poll_interval_s * self.fs)))

        try:
            try:
                while True:
                    elapsed = self.clock.monotonic() - start
                    if duration_s > 0 and elapsed >= duration_s:
                        break

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

                    self.clock.sleep(self.poll_interval_s)
            except KeyboardInterrupt:
                LOGGER.info("acquisition interrupted by user")
        finally:
            self._close_outputs()

        emitted = self.stats["emitted"]
        valid = self.stats["valid"]
        self.stats["valid_ratio"] = (valid / emitted) if emitted else 0.0
        self.stats["mean_hr"] = (self._hr_sum / valid) if valid else None
        self.stats["mean_spo2"] = (self._spo2_sum / valid) if valid else None
        self.stats["elapsed_s"] = self.clock.monotonic() - start
        return self.stats
