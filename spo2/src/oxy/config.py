"""Configuration loading and validation.

The config is a plain nested dict so it is easy to log, diff and dump into a
report appendix. Validation is strict: a wrong LED current or sample rate is a
hard error, not a silent fallback, because silent fallbacks are how you end up
with a 20-minute recording of garbage.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict, Optional

DEFAULTS: Dict[str, Any] = {
    "i2c": {"bus": 1, "address": 0x57},
    "led": {"current_red": 0x24, "current_ir": 0x24},
    "acq": {"sample_rate": 100, "poll_interval_s": 0.2, "resume_gap_s": 2.0},
    "dsp": {
        "window_s": 5.0,
        "min_pi_pct": 0.02,
        "max_pi_pct": 5.0,
        "min_sqi": 0.55,
        "finger_dc_ir": 20000,
        "hr_min": 30,
        "hr_max": 240,
    },
    "spo2": {"curve": "maxim_an6409", "calibration_path": "data/calibration.json"},
    "output": {"dir": "data", "emit_interval_s": 1.0, "log_raw": True},
}

VALID_SAMPLE_RATES = (50, 100, 200, 400, 800, 1000, 1600, 3200)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load ``path`` on top of the defaults and validate the result.

    PyYAML is only imported when a config file is actually requested, so the
    simulator and the self-test keep working on a minimal Pi image.
    """
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        if not os.path.exists(path):
            raise FileNotFoundError("config file not found: %s" % path)
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                "PyYAML is required to read %s (sudo apt install python3-yaml)" % path
            ) from exc
        with open(path, "r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError("config file must contain a YAML mapping: %s" % path)
        _deep_merge(cfg, loaded)
    _validate(cfg)
    return cfg


def _validate(cfg: Dict[str, Any]) -> None:
    addr = int(cfg["i2c"]["address"])
    if not 0x08 <= addr <= 0x77:
        raise ValueError("i2c.address 0x%02X is not a valid 7-bit address" % addr)

    rate = int(cfg["acq"]["sample_rate"])
    if rate not in VALID_SAMPLE_RATES:
        raise ValueError(
            "acq.sample_rate must be one of %s, got %s"
            % (list(VALID_SAMPLE_RATES), rate)
        )

    poll = float(cfg["acq"]["poll_interval_s"])
    if not 0.02 <= poll <= 2.0:
        raise ValueError("acq.poll_interval_s must be within [0.02, 2.0], got %s" % poll)
    # The FIFO is 32 samples deep: polling slower than that guarantees overflow.
    max_poll = 30.0 / rate
    if poll > max_poll:
        raise ValueError(
            "acq.poll_interval_s=%s would overflow the 32-deep FIFO at %s sps "
            "(max %.2f s)" % (poll, rate, max_poll)
        )

    window = float(cfg["dsp"]["window_s"])
    if not 2.0 <= window <= 30.0:
        raise ValueError("dsp.window_s must be within [2, 30], got %s" % window)

    if not 0.0 <= float(cfg["dsp"]["min_sqi"]) <= 1.0:
        raise ValueError("dsp.min_sqi must be within [0, 1]")

    if not 0.0 < float(cfg["dsp"]["min_pi_pct"]) < float(cfg["dsp"]["max_pi_pct"]):
        raise ValueError("dsp.min_pi_pct must be positive and below dsp.max_pi_pct")

    for key in ("current_red", "current_ir"):
        current = int(cfg["led"][key])
        if not 0 <= current <= 0xFF:
            raise ValueError("led.%s must be within 0x00..0xFF, got 0x%X" % (key, current))

    if float(cfg["output"]["emit_interval_s"]) <= 0:
        raise ValueError("output.emit_interval_s must be positive")
