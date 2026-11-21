#!/usr/bin/env python3
"""Fit a custom R -> SpO2 curve against a commercial oximeter.

Two steps, because the two data sources are recorded separately:

1. Merge a monitor log with reference readings taken by hand:

       python3 scripts/calibrate.py merge data/session.csv data/reference.csv \\
           --out data/calibration_points.csv

   ``data/reference.csv`` must have ``timestamp_iso,ref_spo2`` rows. Each
   reference reading is matched to the nearest valid log row within
   ``--tolerance`` seconds (default 3 s).

2. Fit a quadratic and write the calibration file the monitor loads:

       python3 scripts/calibrate.py fit data/calibration_points.csv \\
           --out data/calibration.json

Honest caveat: without controlled hypoxia you can only cover roughly 95-100%
SpO2, so the fitted curve is an interpolation near normal saturation, not a
clinical calibration.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Tuple

import numpy as np


def _epoch(iso_text: str) -> float:
    text = iso_text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def _read_csv(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def cmd_merge(args: argparse.Namespace) -> int:
    log_rows = _read_csv(args.log)
    reference_rows = _read_csv(args.reference)

    candidates: List[Tuple[float, float]] = []
    for row in log_rows:
        if (row.get("valid") or "").strip() != "1":
            continue
        ratio = (row.get("r_ratio") or "").strip()
        stamp = (row.get("timestamp_iso") or "").strip()
        if not ratio or not stamp:
            continue
        try:
            candidates.append((_epoch(stamp), float(ratio)))
        except ValueError:
            continue

    if not candidates:
        print("ERROR: no valid rows with r_ratio in %s" % args.log)
        return 2

    candidates.sort()
    times = np.asarray([item[0] for item in candidates])

    pairs: List[Tuple[float, float]] = []
    skipped = 0
    for row in reference_rows:
        stamp = (row.get("timestamp_iso") or "").strip()
        value = (row.get("ref_spo2") or row.get("spo2") or "").strip()
        if not stamp or not value:
            skipped += 1
            continue
        try:
            moment = _epoch(stamp)
            reference = float(value)
        except ValueError:
            skipped += 1
            continue
        index = int(np.argmin(np.abs(times - moment)))
        gap = abs(times[index] - moment)
        if gap > args.tolerance:
            skipped += 1
            continue
        pairs.append((candidates[index][1], reference))

    if not pairs:
        print("ERROR: reference readings did not match any log row (tolerance %.1f s)" % args.tolerance)
        return 2

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["r_ratio", "ref_spo2"])
        for ratio, value in pairs:
            writer.writerow(["%.5f" % ratio, "%.1f" % value])

    print(
        "wrote %d calibration points to %s (%d reference rows skipped)"
        % (len(pairs), args.out, skipped)
    )
    return 0


def cmd_fit(args: argparse.Namespace) -> int:
    rows = _read_csv(args.points)
    ratios: List[float] = []
    targets: List[float] = []
    for row in rows:
        try:
            ratios.append(float(row["r_ratio"]))
            targets.append(float(row["ref_spo2"]))
        except (KeyError, ValueError):
            continue

    if len(ratios) < 5:
        print("ERROR: need at least 5 points, got %d" % len(ratios))
        return 2

    x = np.asarray(ratios)
    y = np.asarray(targets)
    a, b, c = (float(value) for value in np.polyfit(x, y, 2))
    predicted = a * x * x + b * x + c
    residuals = predicted - y

    rmse = float(np.sqrt(np.mean(residuals ** 2)))
    bias = float(np.mean(residuals))
    sd = float(np.std(residuals, ddof=1)) if residuals.size > 1 else 0.0
    loa_low = bias - 1.96 * sd
    loa_high = bias + 1.96 * sd

    print("points        : %d" % len(ratios))
    print("R range       : %.3f .. %.3f" % (x.min(), x.max()))
    print("reference range: %.1f .. %.1f %%" % (y.min(), y.max()))
    print("curve         : SpO2 = %.4f*R^2 + %.4f*R + %.4f" % (a, b, c))
    print("RMSE          : %.2f %%" % rmse)
    print("bias          : %+.2f %%" % bias)
    print("95%% LoA      : %+.2f .. %+.2f %%" % (loa_low, loa_high))

    if x.max() - x.min() < 0.1:
        print("WARNING: R range is very narrow; the fit will not generalise.")

    payload = {
        "a": a,
        "b": b,
        "c": c,
        "n_points": len(ratios),
        "r_min": float(x.min()),
        "r_max": float(x.max()),
        "rmse": rmse,
        "bias": bias,
        "loa_low": loa_low,
        "loa_high": loa_high,
        "source": os.path.abspath(args.points),
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    print("wrote %s" % args.out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    merge = subparsers.add_parser("merge", help="match reference readings to log rows")
    merge.add_argument("log", help="summary CSV from oxy monitor")
    merge.add_argument("reference", help="CSV with timestamp_iso,ref_spo2")
    merge.add_argument("--out", default="data/calibration_points.csv")
    merge.add_argument("--tolerance", type=float, default=3.0)
    merge.set_defaults(func=cmd_merge)

    fit = subparsers.add_parser("fit", help="fit and write calibration.json")
    fit.add_argument("points", help="CSV with r_ratio,ref_spo2")
    fit.add_argument("--out", default="data/calibration.json")
    fit.set_defaults(func=cmd_fit)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
