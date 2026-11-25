"""Offline plotting for the 1 Hz summary CSV.

matplotlib is imported inside the function so that ``selftest`` / ``simulate`` /
``replay`` never pay for it (and never fail when it is absent).
"""

from __future__ import annotations

import csv
from typing import Dict, List, Optional

import numpy as np


def _read_summary(path: str) -> Dict[str, np.ndarray]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("no data rows in %s" % path)

    def column(name: str) -> np.ndarray:
        values: List[float] = []
        for row in rows:
            raw = (row.get(name) or "").strip()
            try:
                values.append(float(raw) if raw else float("nan"))
            except ValueError:
                values.append(float("nan"))
        return np.asarray(values, dtype=float)

    return {
        "t": column("monotonic_s"),
        "hr": column("hr_bpm"),
        "spo2": column("spo2_pct"),
        "r": column("r_ratio"),
        "pi": column("pi_pct"),
        "sqi": column("sqi"),
        "valid": column("valid"),
    }


def plot_summary(
    csv_path: str,
    out_path: str,
    title: Optional[str] = None,
) -> str:
    """Render four stacked panels and return the output path."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = _read_summary(csv_path)
    time_axis = data["t"]
    if not np.all(np.isfinite(time_axis)):
        time_axis = np.arange(len(data["hr"]), dtype=float)

    figure, axes = plt.subplots(4, 1, figsize=(11, 9.5), sharex=True)

    axes[0].plot(time_axis, data["spo2"], color="#b2182b", linewidth=1.4)
    axes[0].set_ylabel("SpO2 [%]")
    axes[0].set_ylim(85, 101)
    axes[0].grid(alpha=0.25)

    axes[1].plot(time_axis, data["hr"], color="#2166ac", linewidth=1.4)
    axes[1].set_ylabel("HR [bpm]")
    axes[1].grid(alpha=0.25)

    axes[2].plot(time_axis, data["pi"], color="#1b7837", linewidth=1.2, label="PI [%]")
    axes[2].set_ylabel("PI [%]")
    axes[2].grid(alpha=0.25)

    axes[3].plot(time_axis, data["r"], color="#762a83", linewidth=1.2, label="R ratio")
    axes[3].set_ylabel("R ratio")
    axes[3].set_xlabel("time [s]")
    axes[3].grid(alpha=0.25)

    quality_axis = axes[2].twinx()
    quality_axis.plot(time_axis, data["sqi"], color="#e08214", linewidth=1.0, alpha=0.7)
    quality_axis.set_ylabel("SQI")
    quality_axis.set_ylim(0, 1.05)

    axes[0].set_title(title or ("MAX30102 session: %s" % csv_path))
    figure.tight_layout()
    figure.savefig(out_path, dpi=140)
    plt.close(figure)
    return out_path
