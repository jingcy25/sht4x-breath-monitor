"""Pure signal processing for reflective PPG.

Everything here operates on numpy arrays only, so it can be exercised by the
simulator and the replay path without touching I2C.

The SpO2 estimate uses the classic ratio-of-ratios method:

    R    = (AC_red / DC_red) / (AC_ir / DC_ir)
    SpO2 = a*R^2 + b*R + c

with the empirical Maxim AN6409 coefficients. Those coefficients were fitted on
Maxim's own evaluation hardware and healthy volunteers; they are NOT a
calibrated clinical curve and the MAX30102 datasheet specifies no SpO2 accuracy.
Treat the absolute value as a teaching/trend number, not a diagnosis.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np
from scipy.signal import butter, find_peaks, sosfiltfilt

ADC_MAX = 0x03FFFF  # 18-bit ADC, values at or above this are saturated

# Maxim AN6409: SpO2 = a*R^2 + b*R + c
DEFAULT_COEFFS: Tuple[float, float, float] = (-45.060, 30.354, 94.845)


def rms(values: Sequence[float]) -> float:
    """Root-mean-square of a sequence (0.0 for empty input)."""
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(array * array)))


def bandpass(
    x: Sequence[float],
    fs: float,
    lo: float = 0.5,
    hi: float = 5.0,
    order: int = 4,
) -> Optional[np.ndarray]:
    """Zero-phase Butterworth band-pass.

    Returns ``None`` when the window is too short to filter meaningfully rather
    than raising, because the monitor calls this on a growing buffer during the
    first few seconds of a session.
    """
    array = np.asarray(x, dtype=float)
    if array.size < 2 * order + 3 or fs <= 0:
        return None

    nyquist = fs / 2.0
    low = max(float(lo), 1e-4)
    high = min(float(hi), nyquist * 0.95)
    if low >= high:
        return None

    sos = butter(order, [low, high], btype="bandpass", fs=fs, output="sos")
    padlen = min(3 * (2 * len(sos) + 1), array.size - 1)
    if padlen < 1:
        return None
    try:
        return sosfiltfilt(sos, array, padlen=padlen)
    except ValueError:
        return None


def spo2_from_r(r: float, coeffs: Tuple[float, float, float] = DEFAULT_COEFFS) -> float:
    """Apply the empirical R -> SpO2 curve, clamped to a physically sane range."""
    a, b, c = coeffs
    value = a * r * r + b * r + c
    return float(min(100.0, max(70.0, value)))


def r_from_spo2(
    spo2: float, coeffs: Tuple[float, float, float] = DEFAULT_COEFFS
) -> float:
    """Invert the R -> SpO2 curve on its physically meaningful branch.

    The curve is a downward parabola with its vertex near R = 0.337, so every
    SpO2 below ~99.96 has two roots. Real tissue sits on the right-hand branch
    (R grows as saturation falls), hence ``max(roots)``.
    """
    a, b, c = coeffs
    discriminant = b * b - 4.0 * a * (c - spo2)
    if discriminant < 0.0:
        raise ValueError(
            "SpO2 %.2f%% is above the maximum of the empirical curve (99.96%%)" % spo2
        )
    root = math.sqrt(discriminant)
    roots = [(-b + root) / (2.0 * a), (-b - root) / (2.0 * a)]
    candidates = [value for value in roots if 0.0 < value < 3.0]
    if not candidates:
        raise ValueError("no physical R value for SpO2 %.2f%%" % spo2)
    return float(max(candidates))


def estimate_spo2(
    red: Sequence[float],
    ir: Sequence[float],
    fs: float,
    coeffs: Tuple[float, float, float] = DEFAULT_COEFFS,
) -> Dict[str, Optional[float]]:
    """Estimate SpO2, R and the perfusion index from a window of raw PPG.

    Returns a dict whose values are ``None`` when the window is unusable, so a
    caller can distinguish "no reading" from "reading of zero".
    """
    empty: Dict[str, Optional[float]] = {
        "spo2": None,
        "r": None,
        "pi_ir": None,
        "pi_red": None,
        "ac_red": None,
        "ac_ir": None,
        "dc_red": None,
        "dc_ir": None,
    }

    red_array = np.asarray(red, dtype=float)
    ir_array = np.asarray(ir, dtype=float)
    if red_array.size != ir_array.size or red_array.size < 32:
        return empty

    dc_red = float(np.mean(red_array))
    dc_ir = float(np.mean(ir_array))
    if dc_red <= 0.0 or dc_ir <= 0.0:
        return empty

    filtered_red = bandpass(red_array, fs)
    filtered_ir = bandpass(ir_array, fs)
    if filtered_red is None or filtered_ir is None:
        return empty

    ac_red = rms(filtered_red)
    ac_ir = rms(filtered_ir)
    if ac_red <= 0.0 or ac_ir <= 0.0:
        return empty

    ratio = (ac_red / dc_red) / (ac_ir / dc_ir)
    return {
        "spo2": spo2_from_r(ratio, coeffs),
        "r": float(ratio),
        "pi_ir": 100.0 * ac_ir / dc_ir,
        "pi_red": 100.0 * ac_red / dc_red,
        "ac_red": ac_red,
        "ac_ir": ac_ir,
        "dc_red": dc_red,
        "dc_ir": dc_ir,
    }


def estimate_hr(
    ir: Sequence[float],
    fs: float,
    hr_min: float = 30.0,
    hr_max: float = 240.0,
) -> Tuple[Optional[float], Optional[float]]:
    """Heart rate from the IR channel via autocorrelation.

    Peak counting on a filtered PPG is fragile: the dicrotic wave and the
    diastolic trough both look like candidates, and a wrong ``distance``
    parameter silently doubles or halves the rate. Autocorrelation asks the
    right question instead -- "after how many samples does this waveform repeat
    itself?" -- and the answer is the beat period.

    Returns ``(bpm, periodicity)`` where ``periodicity`` is the normalised
    autocorrelation at the chosen lag (0..1) and doubles as a rhythm-quality
    signal. Both are ``None`` when no periodic component is found.
    """
    filtered = bandpass(ir, fs)
    if filtered is None:
        return None, None

    centred = filtered - float(np.mean(filtered))
    length = centred.size
    if length < int(0.5 * fs):
        return None, None

    autocorr = np.correlate(centred, centred, mode="full")[length - 1 :]
    if autocorr[0] <= 0.0:
        return None, None
    autocorr = autocorr / autocorr[0]

    min_lag = max(2, int(round(60.0 * fs / hr_max)))
    max_lag = min(length - 1, int(round(60.0 * fs / hr_min)))
    if max_lag <= min_lag + 2:
        return None, None

    segment = autocorr[min_lag:max_lag]
    peaks, _ = find_peaks(segment, prominence=0.02)
    if peaks.size == 0:
        return None, None

    best_local = int(peaks[int(np.argmax(segment[peaks]))])
    periodicity = float(segment[best_local])
    lag = float(best_local + min_lag)

    # Sub-sample refinement so high heart rates are not quantised to 0.01 s.
    index = best_local + min_lag
    if 0 < index < length - 1:
        y_before = float(autocorr[index - 1])
        y_peak = float(autocorr[index])
        y_after = float(autocorr[index + 1])
        denominator = y_before - 2.0 * y_peak + y_after
        if denominator != 0.0:
            lag += 0.5 * (y_before - y_after) / denominator

    if lag <= 0.0:
        return None, None
    bpm = 60.0 * fs / lag
    if not hr_min <= bpm <= hr_max:
        return None, periodicity
    return float(bpm), periodicity


def waveform_correlation(
    red: Sequence[float], ir: Sequence[float], fs: float
) -> Optional[float]:
    """Pearson correlation of the band-passed red and IR channels.

    Real pulsatile tissue produces the same cardiac waveform in both channels,
    so the correlation is close to 1. Two independent noise sources land near 0.
    """
    filtered_red = bandpass(red, fs)
    filtered_ir = bandpass(ir, fs)
    if filtered_red is None or filtered_ir is None:
        return None
    denominator = float(np.std(filtered_red) * np.std(filtered_ir))
    if denominator <= 0.0:
        return None
    return float(np.corrcoef(filtered_red, filtered_ir)[0, 1])


def signal_quality(
    red: Sequence[float],
    ir: Sequence[float],
    fs: float,
    spo2_info: Dict[str, Optional[float]],
    periodicity: Optional[float],
    min_pi: float = 0.02,
    max_pi: float = 5.0,
    correlation: Optional[float] = None,
) -> float:
    """Composite 0..1 quality score.

    Four independent checks, weighted so that a missing pulse waveform cannot
    hide behind a good-looking perfusion index:

    * 0.35 perfusion index inside a plausible range
    * 0.30 rhythm: normalised autocorrelation at the beat period
    * 0.20 red/IR waveform correlation (both channels must see the same pulse)
    * 0.15 no ADC saturation
    """
    if not spo2_info or spo2_info.get("r") is None:
        return 0.0

    pi = spo2_info.get("pi_ir")
    score_pi = 1.0 if pi is not None and min_pi <= pi <= max_pi else 0.0

    if periodicity is None:
        score_rhythm = 0.0
    else:
        score_rhythm = float(np.clip((periodicity - 0.4) / 0.4, 0.0, 1.0))

    if correlation is None:
        correlation = waveform_correlation(red, ir, fs)
    if correlation is None:
        score_corr = 0.0
    else:
        score_corr = float(np.clip((correlation - 0.6) / 0.3, 0.0, 1.0))

    red_array = np.asarray(red, dtype=float)
    ir_array = np.asarray(ir, dtype=float)
    saturated = bool(red_array.size and (red_array.max() >= ADC_MAX or ir_array.max() >= ADC_MAX))
    score_sat = 0.0 if saturated else 1.0

    return 0.35 * score_pi + 0.30 * score_rhythm + 0.20 * score_corr + 0.15 * score_sat


def finger_present(dc_ir: float, threshold: float = 5000.0) -> bool:
    """Crude but effective finger detector based on the IR DC level.

    The threshold depends on the ADC range and LED current; it is a config value
    for exactly that reason. With 4096 nA range and 7.2 mA LEDs a finger gives
    tens of thousands of counts while open air sits near the dark level.
    """
    return float(dc_ir) > float(threshold)


def analyze_window(
    red: Sequence[float],
    ir: Sequence[float],
    fs: float,
    coeffs: Tuple[float, float, float] = DEFAULT_COEFFS,
    min_sqi: float = 0.55,
    finger_dc_ir: float = 20000.0,
    min_pi: float = 0.02,
    max_pi: float = 5.0,
    hr_min: float = 30.0,
    hr_max: float = 240.0,
) -> Dict[str, Any]:
    """Run the whole per-window pipeline. Shared by monitor and replay."""
    red_array = np.asarray(red, dtype=float)
    ir_array = np.asarray(ir, dtype=float)

    info = estimate_spo2(red_array, ir_array, fs, coeffs)
    hr, periodicity = estimate_hr(ir_array, fs, hr_min=hr_min, hr_max=hr_max)
    correlation = waveform_correlation(red_array, ir_array, fs)
    sqi = signal_quality(
        red_array,
        ir_array,
        fs,
        info,
        periodicity,
        min_pi=min_pi,
        max_pi=max_pi,
        correlation=correlation,
    )

    dc_ir = float(np.mean(ir_array)) if ir_array.size else 0.0
    dc_red = float(np.mean(red_array)) if red_array.size else 0.0
    present = finger_present(dc_ir, finger_dc_ir)

    valid = bool(
        present
        and info.get("spo2") is not None
        and hr is not None
        and sqi >= min_sqi
        and correlation is not None
        and correlation >= 0.6
        and periodicity is not None
        and periodicity >= 0.45
    )

    return {
        "hr_bpm": hr if valid else None,
        "spo2_pct": info.get("spo2") if valid else None,
        "r_ratio": info.get("r"),
        "pi_pct": info.get("pi_ir"),
        "sqi": sqi,
        "finger": present,
        "valid": valid,
        "red_dc": dc_red,
        "ir_dc": dc_ir,
        "window_samples": int(red_array.size),
    }
