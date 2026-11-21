"""Synthetic PPG source and a fake device.

This exists for three reasons:

1. the DSP pipeline can be verified on a laptop with no sensor attached;
2. the self-test can assert that a known heart rate and SpO2 are recovered;
3. demos can be prepared without a finger on the sensor.

The generated waveform is a two-bump cardiac template (systolic peak plus
dicrotic wave) modulated by respiration, which is the shape a reflective finger
PPG actually has. Amplitudes are chosen so the recovered R ratio lands on the
requested SpO2.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

import numpy as np

from . import dsp


def pulse_template(count: int, fs: float, hr: float) -> np.ndarray:
    """Normalised, zero-mean cardiac waveform (unit standard deviation)."""
    time_axis = np.arange(count, dtype=float) / fs
    period = 60.0 / hr
    phase = (time_axis % period) / period

    waveform = (
        0.75 * np.exp(-0.5 * ((phase - 0.22) / 0.075) ** 2)
        + 0.30 * np.exp(-0.5 * ((phase - 0.50) / 0.110) ** 2)
        + 0.08 * np.exp(-0.5 * ((phase - 0.75) / 0.080) ** 2)
    )
    waveform = waveform - waveform.mean()
    scale = float(waveform.std())
    if scale <= 0.0:
        return np.zeros(count, dtype=float)
    return waveform / scale


def generate(
    duration_s: float,
    fs: float = 100.0,
    hr: float = 72.0,
    spo2: float = 97.0,
    pi_ir: float = 0.01,
    noise_counts: float = 40.0,
    dc_ir: float = 100000.0,
    dc_red: float = 60000.0,
    seed: int = 7,
    finger: bool = True,
) -> Dict[str, object]:
    """Generate a raw (red, ir) recording with known ground truth.

    ``pi_ir`` is the IR perfusion index as a fraction (0.01 = 1%). The red
    pulsatile amplitude is scaled by the R value that corresponds to ``spo2``,
    so ``dsp.estimate_spo2`` should recover the requested value.
    """
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    if not 30.0 <= hr <= 240.0:
        raise ValueError("hr must be within [30, 240]")
    if not 70.0 <= spo2 <= 100.0:
        raise ValueError("spo2 must be within [70, 100]")

    rng = np.random.default_rng(seed)
    count = int(round(duration_s * fs))
    time_axis = np.arange(count, dtype=float) / fs

    ratio = dsp.r_from_spo2(spo2)
    cardiac = pulse_template(count, fs, hr)

    respiration = 1.0 + 0.12 * np.sin(2.0 * math.pi * 0.25 * time_axis)
    drift = 1.0 + 0.03 * np.sin(2.0 * math.pi * 0.05 * time_axis)

    if finger:
        ir_signal = dc_ir * respiration * drift * (1.0 + pi_ir * cardiac)
        red_signal = dc_red * respiration * drift * (1.0 + pi_ir * ratio * cardiac)
        ir_noise = rng.normal(0.0, noise_counts, count)
        red_noise = rng.normal(0.0, noise_counts * 1.2, count)
    else:
        # Open air: dark current plus a little ambient pickup.
        ir_signal = np.full(count, 600.0)
        red_signal = np.full(count, 400.0)
        ir_noise = rng.normal(0.0, 12.0, count)
        red_noise = rng.normal(0.0, 12.0, count)

    red = np.clip(np.round(red_signal + red_noise), 0, dsp.ADC_MAX - 1).astype(np.int32)
    ir = np.clip(np.round(ir_signal + ir_noise), 0, dsp.ADC_MAX - 1).astype(np.int32)

    return {
        "red": red,
        "ir": ir,
        "fs": fs,
        "hr": hr,
        "spo2": spo2,
        "r": ratio,
        "pi_ir": pi_ir,
        "finger": finger,
        "duration_s": duration_s,
    }


class FakeDevice:
    """Duck-typed stand-in for :class:`oxy.driver.MAX30102`."""

    def __init__(self, recording: Dict[str, object], samples_per_poll: int) -> None:
        self._red = np.asarray(recording["red"], dtype=np.int32)
        self._ir = np.asarray(recording["ir"], dtype=np.int32)
        self._chunk = max(1, int(samples_per_poll))
        self._index = 0
        self._temperature_c = 31.0

    @property
    def part_id(self) -> int:
        return 0x15

    def read_fifo(self) -> Tuple[List[Tuple[int, int]], bool]:
        start = self._index
        stop = min(start + self._chunk, self._red.size)
        self._index = stop
        samples = [
            (int(self._red[i]), int(self._ir[i])) for i in range(start, stop)
        ]
        return samples, False

    def read_temperature_c(self) -> float:
        return self._temperature_c

    def close(self) -> None:
        return None


class VirtualClock:
    """Clock that advances only when ``sleep`` is called, so long runs are instant."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += max(0.0, float(seconds))


def build_recording(**kwargs: object) -> Dict[str, object]:
    """Thin alias kept for readability at call sites."""
    return generate(**kwargs)  # type: ignore[arg-type]
