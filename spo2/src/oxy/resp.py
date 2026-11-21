"""PPG-only breath-hold detection.

A voluntary breath hold leaves three fingerprints in a finger PPG:

* the respiratory baseline modulation (~0.25 Hz) disappears;
* respiratory sinus arrhythmia disappears, so beat-to-beat intervals flatten;
* pulse amplitude modulation disappears.

None of the three is decisive on its own -- sitting still also reduces RSA -- but
together, against a personal baseline recorded during normal breathing, they form
a usable score. SpO2 is deliberately *not* part of the score: a 30-45 s hold
typically moves it by less than 2 %, which is inside the noise.

The detector emits a stable per-second record (state, score, confidence, raw
features and per-feature scores) so a later fusion layer can weight this device
against a microphone, camera or airflow sensor without knowing its internals.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.signal import find_peaks

from . import dsp

FEATURE_WEIGHTS: Dict[str, float] = {"resp_mod": 0.40, "rsa": 0.25, "pwa": 0.15, "hr_drop": 0.20}
HR_DROP_FULL_BPM = 8.0

# Minimum feature magnitude used as the denominator. Without it a subject who
# barely modulated during calibration would produce huge "drops" from noise.
FEATURE_FLOOR: Dict[str, float] = {"resp_mod": 0.20, "rsa": 1.0, "pwa": 2.0}


def _band_amplitude(
    series: Sequence[float], fs: float, lo: float, hi: float
) -> Optional[float]:
    """Peak spectral amplitude inside [lo, hi], in percent of the mean.

    An FFT with a Hann window is used instead of a low-order band-pass because
    a 0.15 Hz high-pass on a 7 s window has a transient longer than the window
    itself, which made the classic filter output jitter by an order of magnitude
    inside a real breath hold.
    """
    values = np.asarray(series, dtype=float)
    count = values.size
    if count < 24 or fs <= 0:
        return None

    level = float(np.mean(values))
    if level <= 0.0 or float(np.std(values)) <= 0.0:
        return None

    time_axis = np.arange(count, dtype=float) / fs
    slope, intercept = np.polyfit(time_axis, values, 1)
    detrended = values - (slope * time_axis + intercept)

    window = np.hanning(count)
    spectrum = np.fft.rfft(detrended * window)
    freqs = np.fft.rfftfreq(count, 1.0 / fs)
    band = (freqs >= lo) & (freqs <= hi)
    if not band.any():
        return None

    amplitude = 2.0 * np.abs(spectrum[band]) / float(np.sum(window))
    return 100.0 * float(np.max(amplitude)) / level


def respiratory_modulation(ir: Sequence[float], fs: float) -> Optional[float]:
    """Respiratory baseline modulation in percent of the DC level."""
    return _band_amplitude(ir, fs, 0.15, 0.40)


def beat_series(
    ir: Sequence[float], fs: float
) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """Systolic peak times (s) and per-beat pulse amplitudes."""
    filtered = dsp.bandpass(ir, fs, 0.5, 5.0)
    if filtered is None:
        return None

    std = float(np.std(filtered))
    if std <= 0.0:
        return None

    peaks, _ = find_peaks(
        filtered,
        distance=max(1, int(0.30 * fs)),
        prominence=0.4 * std,
    )
    if peaks.size < 4:
        return None

    times = peaks.astype(float) / fs
    amplitudes = np.empty(peaks.size, dtype=float)
    for index, peak in enumerate(peaks):
        stop = min(filtered.size, peak + int(0.40 * fs))
        trough = float(np.min(filtered[peak:stop])) if stop > peak else 0.0
        amplitudes[index] = float(filtered[peak]) - trough
    return times, amplitudes


def _modulation_index(
    times: Sequence[float],
    values: Sequence[float],
    fs_resample: float = 4.0,
    lo: float = 0.15,
    hi: float = 0.40,
) -> Optional[float]:
    """Band-limited relative modulation of an unevenly sampled series."""
    time_array = np.asarray(times, dtype=float)
    value_array = np.asarray(values, dtype=float)
    if time_array.size < 6:
        return None
    if float(time_array[-1] - time_array[0]) < 6.0:
        return None

    level = float(np.mean(value_array))
    if level <= 0.0:
        return None

    grid = np.arange(float(time_array[0]), float(time_array[-1]), 1.0 / fs_resample)
    if grid.size < 24:
        return None
    series = np.interp(grid, time_array, value_array)
    return _band_amplitude(series, fs_resample, lo, hi)


def respiratory_features(ir: Sequence[float], fs: float) -> Dict[str, Any]:
    """All PPG respiratory features for one window."""
    result: Dict[str, Any] = {
        "resp_mod": respiratory_modulation(ir, fs),
        "rsa": None,
        "pwa": None,
        "hr_bpm": None,
        "beats": 0,
    }

    beats = beat_series(ir, fs)
    if beats is not None:
        times, amplitudes = beats
        result["beats"] = int(times.size)
        if times.size >= 6:
            midpoints = 0.5 * (times[1:] + times[:-1])
            intervals = np.diff(times)
            mean_amplitudes = 0.5 * (amplitudes[1:] + amplitudes[:-1])
            usable = (intervals >= 0.30) & (intervals <= 2.0)
            if int(usable.sum()) >= 5:
                result["rsa"] = _modulation_index(midpoints[usable], intervals[usable])
                result["pwa"] = _modulation_index(
                    midpoints[usable], mean_amplitudes[usable]
                )

    hr, _ = dsp.estimate_hr(ir, fs)
    result["hr_bpm"] = hr
    return result


class PPGBreathHoldDetector:
    """Stateful detector: feed it one window per update, once a second."""

    def __init__(
        self,
        fs: float = 100.0,
        fast_window_s: float = 7.0,
        slow_window_s: float = 16.0,
        calib_s: float = 20.0,
        baseline_min: int = 12,
        min_sqi: float = 0.55,
        enter_score: float = 0.55,
        enter_hold_s: int = 2,
        exit_score: float = 0.40,
        exit_hold_s: int = 4,
        rearm_s: int = 4,
        finger_dc_ir: float = 20000.0,
        min_pi: float = 0.02,
        max_pi: float = 5.0,
        coeffs: Tuple[float, float, float] = dsp.DEFAULT_COEFFS,
    ) -> None:
        self.fs = float(fs)
        self.fast_window_s = float(fast_window_s)
        self.slow_window_s = float(slow_window_s)
        self.calib_s = int(calib_s)
        self.baseline_min = int(baseline_min)
        self.min_sqi = float(min_sqi)
        self.enter_score = float(enter_score)
        self.enter_hold_s = int(enter_hold_s)
        self.exit_score = float(exit_score)
        self.exit_hold_s = int(exit_hold_s)
        self.rearm_s = int(rearm_s)
        self.finger_dc_ir = float(finger_dc_ir)
        self.min_pi = float(min_pi)
        self.max_pi = float(max_pi)
        self.coeffs = coeffs

        self._slow_samples = max(64, int(round(self.slow_window_s * self.fs)))
        self._fast_samples = max(64, int(round(self.fast_window_s * self.fs)))
        self._slow_min_samples = max(32, int(round(0.5 * self._slow_samples)))

        self._baseline_samples: Dict[str, List[float]] = {
            "resp_mod": [],
            "rsa": [],
            "pwa": [],
            "hr": [],
        }
        self.baseline: Optional[Dict[str, float]] = None
        self.baseline_hr: Optional[float] = None

        self.state = "warming"
        self._enter_count = 0
        self._exit_count = 0
        self._valid_streak = 0
        self._calib_count = 0
        self.hold_started_t: Optional[float] = None
        self.hold_ended_t: Optional[float] = None

    # ------------------------------------------------------------------ input
    def reset_baseline(self) -> None:
        self._baseline_samples = {"resp_mod": [], "rsa": [], "pwa": [], "hr": []}
        self.baseline = None
        self.baseline_hr = None
        self.state = "warming"
        self._enter_count = 0
        self._exit_count = 0
        self._calib_count = 0

    def update(
        self,
        red: Sequence[float],
        ir: Sequence[float],
        t: float,
    ) -> Dict[str, Any]:
        """Run one analysis step and return the fusion-ready record."""
        red_array = np.asarray(red, dtype=float)
        ir_array = np.asarray(ir, dtype=float)

        if red_array.size < self._slow_min_samples or ir_array.size < self._slow_min_samples:
            return self._record(t, "warming", 0.0, 0.0, {}, {}, None)

        analysis = dsp.analyze_window(
            red_array[-self._slow_samples :],
            ir_array[-self._slow_samples :],
            self.fs,
            coeffs=self.coeffs,
            min_sqi=self.min_sqi,
            finger_dc_ir=self.finger_dc_ir,
            min_pi=self.min_pi,
            max_pi=self.max_pi,
        )

        if not analysis["valid"]:
            self.state = "no_signal"
            self._enter_count = 0
            self._exit_count = 0
            self._valid_streak = 0
            return self._record(t, "no_signal", 0.0, 0.0, {}, {}, analysis)

        self._valid_streak += 1

        features = respiratory_features(ir_array[-self._slow_samples :], self.fs)
        features["resp_mod"] = respiratory_modulation(
            ir_array[-self._fast_samples :], self.fs
        )
        features["hr_delta"] = (
            None
            if features["hr_bpm"] is None or self.baseline_hr is None
            else float(features["hr_bpm"] - self.baseline_hr)
        )

        if self.baseline is None:
            return self._calibrate(t, features, analysis)

        drops = self._feature_drops(features)
        weights = {name: FEATURE_WEIGHTS[name] for name in drops if drops[name] is not None}
        if weights:
            score = sum(FEATURE_WEIGHTS[n] * drops[n] for n in weights) / sum(
                weights.values()
            )
        else:
            score = 0.0

        confidence = self._confidence(analysis, drops)
        self._update_state(float(score), t)
        return self._record(t, self.state, float(score), confidence, features, drops, analysis)

    # --------------------------------------------------------------- internals
    def _calibrate(
        self, t: float, features: Dict[str, Any], analysis: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.state = "calibrating"
        if self._valid_streak >= 3:
            self._calib_count += 1
            for name in ("resp_mod", "rsa", "pwa"):
                value = features.get(name)
                if value is not None:
                    self._baseline_samples[name].append(float(value))
            if features.get("hr_bpm") is not None:
                self._baseline_samples["hr"].append(float(features["hr_bpm"]))

        ready = (
            self._calib_count >= self.calib_s
            and len(self._baseline_samples["resp_mod"]) >= self.baseline_min
            and len(self._baseline_samples["rsa"]) >= self.baseline_min
            and len(self._baseline_samples["pwa"]) >= self.baseline_min
        )
        if ready:
            self.baseline = {
                name: float(np.median(self._baseline_samples[name]))
                for name in ("resp_mod", "rsa", "pwa")
            }
            if self._baseline_samples["hr"]:
                self.baseline_hr = float(np.median(self._baseline_samples["hr"]))
            self.state = "normal"

        confidence = 0.0 if self.baseline is None else self._baseline_strength()
        return self._record(
            t,
            self.state,
            0.0,
            confidence,
            features,
            {},
            analysis,
            progress=min(1.0, self._calib_count / max(1, self.calib_s)),
        )

    def _feature_drops(self, features: Dict[str, Any]) -> Dict[str, Optional[float]]:
        drops: Dict[str, Optional[float]] = {}
        if self.baseline is None:
            return {name: None for name in FEATURE_WEIGHTS}

        for name in ("resp_mod", "rsa", "pwa"):
            value = features.get(name)
            if value is None:
                drops[name] = None
                continue
            denominator = max(self.baseline[name], FEATURE_FLOOR[name])
            drops[name] = float(np.clip(1.0 - value / denominator, 0.0, 1.0))

        hr = features.get("hr_bpm")
        if self.baseline_hr is None or hr is None:
            drops["hr_drop"] = None
        else:
            drops["hr_drop"] = float(
                np.clip((self.baseline_hr - float(hr)) / HR_DROP_FULL_BPM, 0.0, 1.0)
            )
        return drops

    def _baseline_strength(self) -> float:
        if self.baseline is None:
            return 0.0
        ratios = [
            self.baseline[name] / FEATURE_FLOOR[name]
            for name in ("resp_mod", "rsa", "pwa")
        ]
        return float(np.clip(min(ratios), 0.0, 1.0))

    def _confidence(
        self, analysis: Dict[str, Any], drops: Dict[str, Optional[float]]
    ) -> float:
        available = sum(1 for value in drops.values() if value is not None)
        coverage = available / len(FEATURE_WEIGHTS)
        sqi = float(analysis.get("sqi") or 0.0)
        return float(np.clip(sqi * coverage * self._baseline_strength(), 0.0, 1.0))

    def _update_state(self, score: float, t: float) -> None:
        if score >= self.enter_score:
            self._enter_count += 1
        else:
            self._enter_count = 0
        if score <= self.exit_score:
            self._exit_count += 1
        else:
            self._exit_count = 0

        if self.state != "hold":
            if self._enter_count >= self.enter_hold_s and self._valid_streak >= self.rearm_s:
                self.state = "hold"
                self.hold_started_t = t
        else:
            if self._exit_count >= self.exit_hold_s:
                self.state = "normal"
                self.hold_ended_t = t

    def _record(
        self,
        t: float,
        state: str,
        score: float,
        confidence: float,
        features: Dict[str, Any],
        drops: Dict[str, Optional[float]],
        analysis: Optional[Dict[str, Any]],
        progress: Optional[float] = None,
    ) -> Dict[str, Any]:
        def rounded(value: Any, digits: int) -> Optional[float]:
            return None if value is None else round(float(value), digits)

        payload: Dict[str, Any] = {
            "t": round(float(t), 3),
            "device": "ppg",
            "state": state,
            "score": round(float(score), 3),
            "confidence": round(float(confidence), 3),
            "features": {
                "resp_mod": rounded(features.get("resp_mod"), 4),
                "rsa": rounded(features.get("rsa"), 3),
                "pwa": rounded(features.get("pwa"), 3),
                "hr_bpm": rounded(features.get("hr_bpm"), 1),
                "hr_delta": rounded(features.get("hr_delta"), 1),
                "spo2": rounded((analysis or {}).get("spo2_pct"), 1),
                "sqi": rounded((analysis or {}).get("sqi"), 3),
                "finger": bool((analysis or {}).get("finger")),
            },
            "drops": {name: rounded(value, 3) for name, value in drops.items()},
            "baseline": self.baseline,
            "baseline_hr": self.baseline_hr,
        }
        if progress is not None:
            payload["calib_progress"] = round(float(progress), 3)
        return payload
