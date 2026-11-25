"""Synthetic PPG source with a real respiratory component.

The waveform is phase-modulated by an instantaneous heart rate, so it carries
the three signals a breath-hold detector actually needs:

* respiratory baseline modulation (~0.25 Hz) on the DC level
* respiratory sinus arrhythmia (RSA) on the heart rate
* pulse amplitude modulation (PWA) on the cardiac envelope

A ``hold_start`` / ``hold_duration`` pair fades all three out and adds a small
bradycardia, which is what a voluntary breath hold looks like in PPG.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import dsp


def pulse_shape(phase: np.ndarray) -> np.ndarray:
    """Cardiac morphology as a function of beat phase in [0, 1)."""
    wrapped = np.mod(np.asarray(phase, dtype=float), 1.0)
    return (
        0.75 * np.exp(-0.5 * ((wrapped - 0.22) / 0.075) ** 2)
        + 0.30 * np.exp(-0.5 * ((wrapped - 0.50) / 0.110) ** 2)
        + 0.08 * np.exp(-0.5 * ((wrapped - 0.75) / 0.080) ** 2)
    )


def _normalise(shape: np.ndarray) -> np.ndarray:
    centred = shape - float(np.mean(shape))
    scale = float(np.std(centred))
    if scale <= 0.0:
        return np.zeros_like(centred)
    return centred / scale


def pulse_template(count: int, fs: float, hr: float) -> np.ndarray:
    """Constant-rate template, kept for callers that only need the shape."""
    time_axis = np.arange(count, dtype=float) / fs
    return _normalise(pulse_shape((time_axis * hr / 60.0) % 1.0))


def _hold_envelope(
    time_axis: np.ndarray,
    hold_start: Optional[float],
    hold_duration: float,
    ramp_s: float = 3.0,
) -> np.ndarray:
    if hold_start is None or hold_duration <= 0.0:
        return np.zeros_like(time_axis)
    rising = np.tanh((time_axis - hold_start) / ramp_s)
    falling = np.tanh((time_axis - (hold_start + hold_duration)) / ramp_s)
    return 0.5 * (rising - falling)


def _spo2_trajectory(
    time_axis: np.ndarray,
    hold_start: Optional[float],
    hold_duration: float,
    spo2: float,
    hold_spo2_drop: float,
) -> np.ndarray:
    if hold_start is None or hold_duration <= 0.0 or hold_spo2_drop <= 0.0:
        return np.full_like(time_axis, spo2)

    elapsed = time_axis - hold_start
    during = (time_axis >= hold_start) & (time_axis < hold_start + hold_duration)
    after = time_axis >= hold_start + hold_duration

    drop = np.zeros_like(time_axis)
    drop[during] = np.clip((elapsed[during] - 18.0) / 15.0, 0.0, 1.0)
    end_value = float(np.clip((hold_duration - 18.0) / 15.0, 0.0, 1.0))
    drop[after] = end_value * np.exp(
        -(time_axis[after] - (hold_start + hold_duration)) / 12.0
    )
    return np.clip(spo2 - hold_spo2_drop * drop, 70.0, 99.9)


def _r_from_spo2_array(spo2_values: np.ndarray) -> np.ndarray:
    a, b, c = dsp.DEFAULT_COEFFS
    discriminant = b * b - 4.0 * a * (c - spo2_values)
    root = np.sqrt(np.maximum(discriminant, 0.0))
    first = (-b + root) / (2.0 * a)
    second = (-b - root) / (2.0 * a)
    return np.maximum(first, second)


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
    resp_hz: float = 0.25,
    resp_depth: float = 0.012,
    rsa_bpm: float = 3.5,
    pwa_depth: float = 0.15,
    hold_start: Optional[float] = None,
    hold_duration: float = 0.0,
    hold_hr_drop: float = 4.0,
    hold_spo2_drop: float = 0.0,
) -> Dict[str, object]:
    """Generate a raw (red, ir) recording with known ground truth."""
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    if not 30.0 <= hr <= 240.0:
        raise ValueError("hr must be within [30, 240]")
    if not 70.0 <= spo2 <= 100.0:
        raise ValueError("spo2 must be within [70, 100]")

    rng = np.random.default_rng(seed)
    count = int(round(duration_s * fs))
    time_axis = np.arange(count, dtype=float) / fs

    hold_env = _hold_envelope(time_axis, hold_start, hold_duration)
    respiratory_scale = 1.0 - 0.95 * hold_env

    instantaneous_hr = (
        hr
        + rsa_bpm * np.sin(2.0 * math.pi * resp_hz * time_axis) * respiratory_scale
        - hold_hr_drop * hold_env
    )
    instantaneous_hr = np.clip(instantaneous_hr, 30.0, 240.0)

    phase = np.cumsum(instantaneous_hr / 60.0) / fs
    cardiac = _normalise(pulse_shape(phase))

    amplitude = (
        1.0
        + pwa_depth
        * np.sin(2.0 * math.pi * resp_hz * time_axis + 0.8)
        * respiratory_scale
    )
    level = (
        1.0
        + resp_depth
        * np.sin(2.0 * math.pi * resp_hz * time_axis)
        * respiratory_scale
    )

    spo2_track = _spo2_trajectory(
        time_axis, hold_start, hold_duration, spo2, hold_spo2_drop
    )
    ratio_track = _r_from_spo2_array(spo2_track)

    if finger:
        ir_signal = dc_ir * level * (1.0 + pi_ir * amplitude * cardiac)
        red_signal = dc_red * level * (1.0 + pi_ir * ratio_track * amplitude * cardiac)
        ir_noise = rng.normal(0.0, noise_counts, count)
        red_noise = rng.normal(0.0, noise_counts * 1.2, count)
    else:
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
        "r": float(np.mean(ratio_track)),
        "pi_ir": pi_ir,
        "finger": finger,
        "duration_s": duration_s,
        "hold_start": hold_start,
        "hold_duration": hold_duration,
        "resp_hz": resp_hz,
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
    return generate(**kwargs)  # type: ignore[arg-type]
