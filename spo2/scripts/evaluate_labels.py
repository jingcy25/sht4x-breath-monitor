#!/usr/bin/env python3
"""Score the detector against the manual labels from the web page.

    python3 scripts/evaluate_labels.py \
        --labels data/labels.csv --timeline data/hold_web_YYYYmmdd_HHMMSS.csv

Prints per-interval detection latency, false-positive seconds, and a threshold
sweep so the operator can pick a value that suits their own breathing instead of
using the simulated default.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from typing import Dict, List, Optional, Sequence, Tuple

Timeline = List[Tuple[float, str, float]]


def load_timeline(path: str) -> Timeline:
    if path.endswith(".jsonl"):
        rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
        return [(float(r["t"]), str(r["state"]), float(r["score"])) for r in rows]

    timeline: Timeline = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            state = (row.get("hold_state") or "").strip()
            if not state:
                continue
            try:
                timeline.append(
                    (
                        float(row["monotonic_s"]),
                        state,
                        float(row["hold_score"]),
                    )
                )
            except (KeyError, ValueError):
                continue
    return timeline


def load_labels(path: str) -> List[Tuple[float, str]]:
    labels: List[Tuple[float, str]] = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                labels.append((float(row["monotonic_s"]), row["event"]))
            except (KeyError, ValueError):
                continue
    labels.sort()
    return labels


def pair_intervals(labels: Sequence[Tuple[float, str]]) -> List[Tuple[float, float]]:
    pairs: List[Tuple[float, float]] = []
    start: Optional[float] = None
    for moment, event in labels:
        if event == "hold_start":
            start = moment
        elif event == "hold_end" and start is not None:
            pairs.append((start, moment))
            start = None
    if start is not None:
        pairs.append((start, labels[-1][0] if labels else start))
    return pairs


def replay_states(
    scores: Sequence[float],
    enter: float,
    exit_score: float = 0.40,
    enter_s: int = 2,
    exit_s: int = 4,
) -> List[str]:
    state = "normal"
    enter_count = 0
    exit_count = 0
    states: List[str] = []
    for score in scores:
        enter_count = enter_count + 1 if score >= enter else 0
        exit_count = exit_count + 1 if score <= exit_score else 0
        if state != "hold" and enter_count >= enter_s:
            state = "hold"
        elif state == "hold" and exit_count >= exit_s:
            state = "normal"
        states.append(state)
    return states


def evaluate(
    times: Sequence[float],
    states: Sequence[str],
    intervals: Sequence[Tuple[float, float]],
    grace: float = 10.0,
) -> Dict[str, object]:
    latencies: List[Optional[float]] = []
    for start, end in intervals:
        found: Optional[float] = None
        for moment, state in zip(times, states):
            if start <= moment <= end + 2.0 and state == "hold":
                found = moment - start
                break
        latencies.append(found)

    false_positive = 0
    for moment, state in zip(times, states):
        if state != "hold":
            continue
        near_label = any(start - grace <= moment <= end + grace for start, end in intervals)
        if not near_label:
            false_positive += 1

    detected = [value for value in latencies if value is not None]
    return {
        "misses": sum(1 for value in latencies if value is None),
        "median_latency": statistics.median(detected) if detected else None,
        "max_latency": max(detected) if detected else None,
        "fp_seconds": false_positive,
        "latencies": latencies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", default="data/labels.csv")
    parser.add_argument("--timeline", required=True, help="hold CSV or JSONL")
    parser.add_argument("--no-sweep", action="store_true")
    args = parser.parse_args()

    timeline = load_timeline(args.timeline)
    labels = load_labels(args.labels)
    intervals = pair_intervals(labels)
    if not timeline:
        print("ERROR: empty timeline")
        return 2
    if not intervals:
        print("ERROR: no hold_start/hold_end pairs in %s" % args.labels)
        return 2

    times = [item[0] for item in timeline]
    scores = [item[2] for item in timeline]

    print("timeline : %d points, %.1f s" % (len(times), times[-1] - times[0]))
    print("intervals: %s" % [("%.1f-%.1f" % pair) for pair in intervals])

    result = evaluate(times, [item[1] for item in timeline], intervals)
    print("\nactual state machine")
    for index, (pair, latency) in enumerate(zip(intervals, result["latencies"]), 1):
        print(
            "  #%d %.1f-%.1f s -> %s"
            % (
                index,
                pair[0],
                pair[1],
                "MISS" if latency is None else "detected after %.1f s" % latency,
            )
        )
    print(
        "  misses=%s median=%s max=%s false_positive=%s s"
        % (
            result["misses"],
            "--" if result["median_latency"] is None else "%.1f s" % result["median_latency"],
            "--" if result["max_latency"] is None else "%.1f s" % result["max_latency"],
            result["fp_seconds"],
        )
    )

    if args.no_sweep:
        return 0

    print("\nthreshold sweep (enter_score)")
    print("  enter  misses  median_lat  max_lat  fp_seconds")
    best = None
    for step in range(45, 76, 5):
        enter = step / 100.0
        states = replay_states(scores, enter)
        summary = evaluate(times, states, intervals)
        median = summary["median_latency"]
        print(
            "  %.2f   %-6s  %-9s  %-7s  %s"
            % (
                enter,
                summary["misses"],
                "--" if median is None else "%.1f s" % median,
                "--" if summary["max_latency"] is None else "%.1f s" % summary["max_latency"],
                summary["fp_seconds"],
            )
        )
        key = (summary["misses"], summary["fp_seconds"], median if median is not None else 99.0)
        if best is None or key < best[0]:
            best = (key, enter)

    if best is not None:
        print("\nrecommended enter_score: %.2f" % best[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
