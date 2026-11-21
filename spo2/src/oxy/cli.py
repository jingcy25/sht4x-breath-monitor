"""Command line entry point.

    PYTHONPATH=src python3 -m oxy.cli <command> [options]

Commands
--------
diag       inspect the I2C bus and the sensor registers
monitor    live acquisition to CSV
simulate   run the whole pipeline on synthetic data (no hardware)
replay     re-run the DSP on a raw CSV recorded earlier
selftest   assert the pipeline recovers known HR / SpO2 values
plot       render a summary CSV to PNG
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import __version__, dsp, plotting, simulate
from .config import load_config
from .monitor import Monitor
from .resp import PPGBreathHoldDetector

EXIT_OK = 0
EXIT_HARDWARE = 2
EXIT_NO_DATA = 3


# ---------------------------------------------------------------- utilities
def _load_coeffs(config: Dict[str, Any]) -> Tuple[Tuple[float, float, float], Optional[str]]:
    path = config["spo2"].get("calibration_path")
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            coeffs = (
                float(payload["a"]),
                float(payload["b"]),
                float(payload["c"]),
            )
            return coeffs, path
        except (OSError, KeyError, ValueError) as exc:
            print("warning: ignoring unreadable calibration file %s (%s)" % (path, exc))
    return dsp.DEFAULT_COEFFS, None


def _default_session_path(config: Dict[str, Any]) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(config["output"]["dir"], "session_%s.csv" % stamp)


def _derive_raw_path(summary_path: str) -> str:
    root, extension = os.path.splitext(summary_path)
    return root + "_raw" + (extension or ".csv")


def _read_raw(path: str) -> Tuple[np.ndarray, np.ndarray, float]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("raw CSV has no data rows: %s" % path)
    if "red" not in rows[0] or "ir" not in rows[0]:
        raise ValueError(
            "%s looks like a 1 Hz summary file; replay needs the raw "
            "file written alongside it (suffix _raw.csv)" % path
        )
    red = np.asarray([float(row["red"]) for row in rows], dtype=float)
    ir = np.asarray([float(row["ir"]) for row in rows], dtype=float)
    times = np.asarray([float(row["monotonic_s"]) for row in rows], dtype=float)
    deltas = np.diff(times)
    deltas = deltas[deltas > 0]
    if deltas.size == 0:
        raise ValueError("raw CSV has no usable timestamps: %s" % path)
    fs = 1.0 / float(np.median(deltas))
    return red, ir, round(fs, 3)


def _summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid_rows = [row for row in rows if row["valid"]]
    count = len(rows)
    valid = len(valid_rows)
    hrs = [row["hr_bpm"] for row in valid_rows if row["hr_bpm"] is not None]
    spo2s = [row["spo2_pct"] for row in valid_rows if row["spo2_pct"] is not None]
    return {
        "windows": count,
        "valid_windows": valid,
        "valid_ratio": (valid / count) if count else 0.0,
        "mean_hr": float(np.mean(hrs)) if hrs else None,
        "mean_spo2": float(np.mean(spo2s)) if spo2s else None,
        "std_hr": float(np.std(hrs)) if hrs else None,
        "std_spo2": float(np.std(spo2s)) if spo2s else None,
    }


def _print_stats(stats: Dict[str, Any]) -> None:
    print(
        "emitted=%s valid=%s (%.0f%%) mean_HR=%s mean_SpO2=%s overflows=%s"
        % (
            stats.get("emitted", stats.get("windows")),
            stats.get("valid", stats.get("valid_windows")),
            100.0 * float(stats.get("valid_ratio", 0.0)),
            "%.1f bpm" % stats["mean_hr"] if stats.get("mean_hr") is not None else "--",
            "%.1f%%" % stats["mean_spo2"] if stats.get("mean_spo2") is not None else "--",
            stats.get("overflow_events", 0),
        )
    )


# ------------------------------------------------------------------ commands
def cmd_diag(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    bus = int(config["i2c"]["bus"])

    print("I2C bus %d scan" % bus)
    scanner = shutil.which("i2cdetect") or (
        "/usr/sbin/i2cdetect" if os.path.exists("/usr/sbin/i2cdetect") else None
    )
    if scanner:
        try:
            subprocess.run([scanner, "-y", str(bus)], check=False)
        except OSError as exc:
            print("  could not run i2cdetect: %s" % exc)
    else:
        print("  i2cdetect not installed (sudo apt install i2c-tools)")

    from .driver import MAX30102

    try:
        device = MAX30102(
            bus=bus,
            address=int(config["i2c"]["address"]),
            sample_rate=int(config["acq"]["sample_rate"]),
            led_red=int(config["led"]["current_red"]),
            led_ir=int(config["led"]["current_ir"]),
        )
    except RuntimeError as exc:
        print("\nERROR: %s" % exc)
        return EXIT_HARDWARE

    try:
        print("\nPART_ID = 0x%02X" % device.part_id)
        print("registers:")
        for name, value in device.dump_registers().items():
            print("  %-14s 0x%02X  (%d)" % (name, value, value))
        print("die temperature: %.2f C" % device.read_temperature_c())

        print("\nFIFO samples (5 reads, 0.2 s apart):")
        for _ in range(5):
            samples, overflowed = device.read_fifo()
            flag = " OVERFLOW" if overflowed else ""
            if samples:
                last = samples[-1]
                print(
                    "  n=%2d  red=%6d ir=%6d%s"
                    % (len(samples), last[0], last[1], flag)
                )
            else:
                print("  n= 0  (no samples yet)%s" % flag)
            time.sleep(0.2)
        print("\nIf ir is a few hundred, nothing is on the sensor. A finger gives tens of thousands.")
    finally:
        device.close()
    return EXIT_OK


def cmd_monitor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    coeffs, calibration_path = _load_coeffs(config)

    from .driver import MAX30102

    try:
        device = MAX30102(
            bus=int(config["i2c"]["bus"]),
            address=int(config["i2c"]["address"]),
            sample_rate=int(config["acq"]["sample_rate"]),
            led_red=int(config["led"]["current_red"]),
            led_ir=int(config["led"]["current_ir"]),
        )
    except RuntimeError as exc:
        print("ERROR: %s" % exc)
        return EXIT_HARDWARE

    summary_path = args.out or _default_session_path(config)
    raw_path: Optional[str] = None
    if config["output"]["log_raw"] and not args.no_raw:
        raw_path = args.raw_out or _derive_raw_path(summary_path)

    if calibration_path:
        print("using calibration curve from %s" % calibration_path)
    else:
        print("using default Maxim AN6409 curve (uncalibrated)")

    monitor = Monitor(
        device,
        fs=float(config["acq"]["sample_rate"]),
        window_s=float(config["dsp"]["window_s"]),
        poll_interval_s=float(config["acq"]["poll_interval_s"]),
        emit_interval_s=float(config["output"]["emit_interval_s"]),
        coeffs=coeffs,
        min_sqi=float(config["dsp"]["min_sqi"]),
        finger_dc_ir=float(config["dsp"]["finger_dc_ir"]),
        min_pi=float(config["dsp"]["min_pi_pct"]),
        max_pi=float(config["dsp"]["max_pi_pct"]),
        hr_min=float(config["dsp"]["hr_min"]),
        hr_max=float(config["dsp"]["hr_max"]),
        summary_path=summary_path,
        raw_path=raw_path,
        quiet=args.quiet,
    )

    print("summary -> %s" % summary_path)
    if raw_path:
        print("raw     -> %s" % raw_path)
    print("Ctrl-C to stop.\n")

    try:
        stats = monitor.run(args.duration)
    finally:
        device.close()

    print("\nsession finished")
    _print_stats(stats)
    print("elapsed %.1f s" % stats.get("elapsed_s", 0.0))
    return EXIT_OK if stats["valid"] else EXIT_NO_DATA


def cmd_simulate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    coeffs, _ = _load_coeffs(config)

    recording = simulate.generate(
        duration_s=args.duration,
        fs=float(config["acq"]["sample_rate"]),
        hr=args.hr,
        spo2=args.spo2,
        pi_ir=args.pi / 100.0,
        noise_counts=args.noise,
        seed=args.seed,
        finger=not args.no_finger,
    )

    samples_per_poll = max(
        1,
        int(round(float(config["acq"]["poll_interval_s"]) * recording["fs"])),
    )
    device = simulate.FakeDevice(recording, samples_per_poll)
    clock = simulate.VirtualClock()

    summary_path = args.out or os.path.join(config["output"]["dir"], "simulation.csv")
    raw_path = None if args.no_raw else _derive_raw_path(summary_path)

    monitor = Monitor(
        device,
        fs=float(recording["fs"]),
        window_s=float(config["dsp"]["window_s"]),
        poll_interval_s=float(config["acq"]["poll_interval_s"]),
        emit_interval_s=float(config["output"]["emit_interval_s"]),
        clock=clock,
        coeffs=coeffs,
        min_sqi=float(config["dsp"]["min_sqi"]),
        finger_dc_ir=float(config["dsp"]["finger_dc_ir"]),
        min_pi=float(config["dsp"]["min_pi_pct"]),
        max_pi=float(config["dsp"]["max_pi_pct"]),
        summary_path=summary_path,
        raw_path=raw_path,
        quiet=args.quiet,
    )

    print(
        "simulating %.1f s at HR=%.0f bpm, SpO2=%.1f%%, R=%.3f, finger=%s"
        % (
            args.duration,
            recording["hr"],
            recording["spo2"],
            recording["r"],
            "no" if args.no_finger else "yes",
        )
    )
    stats = monitor.run(args.duration)

    print("\nsummary -> %s" % summary_path)
    if raw_path:
        print("raw     -> %s" % raw_path)
    _print_stats(stats)

    if stats["mean_hr"] is not None:
        print(
            "recovered HR=%.1f bpm (target %.0f), SpO2=%.1f%% (target %.1f)"
            % (stats["mean_hr"], args.hr, stats["mean_spo2"], args.spo2)
        )
    if args.no_finger:
        return EXIT_OK if stats["valid"] == 0 else EXIT_NO_DATA
    return EXIT_OK if stats["valid"] else EXIT_NO_DATA


def cmd_hold(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    coeffs, calibration_path = _load_coeffs(config)
    fs = float(config["acq"]["sample_rate"])

    from .driver import MAX30102

    try:
        device = MAX30102(
            bus=int(config["i2c"]["bus"]),
            address=int(config["i2c"]["address"]),
            sample_rate=int(config["acq"]["sample_rate"]),
            led_red=int(config["led"]["current_red"]),
            led_ir=int(config["led"]["current_ir"]),
        )
    except RuntimeError as exc:
        print("ERROR: %s" % exc)
        return EXIT_HARDWARE

    detector = PPGBreathHoldDetector(
        fs=fs,
        calib_s=args.calib,
        min_sqi=float(config["dsp"]["min_sqi"]),
        finger_dc_ir=float(config["dsp"]["finger_dc_ir"]),
        min_pi=float(config["dsp"]["min_pi_pct"]),
        max_pi=float(config["dsp"]["max_pi_pct"]),
        coeffs=coeffs,
    )

    summary_path = args.out or os.path.join(
        config["output"]["dir"], "hold_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    root, _ = os.path.splitext(summary_path)
    jsonl_path = args.jsonl or (root + ".jsonl")
    raw_path = None if args.no_raw else _derive_raw_path(summary_path)

    monitor = Monitor(
        device,
        fs=fs,
        window_s=float(config["dsp"]["window_s"]),
        poll_interval_s=float(config["acq"]["poll_interval_s"]),
        emit_interval_s=float(config["output"]["emit_interval_s"]),
        coeffs=coeffs,
        min_sqi=float(config["dsp"]["min_sqi"]),
        finger_dc_ir=float(config["dsp"]["finger_dc_ir"]),
        min_pi=float(config["dsp"]["min_pi_pct"]),
        max_pi=float(config["dsp"]["max_pi_pct"]),
        hr_min=float(config["dsp"]["hr_min"]),
        hr_max=float(config["dsp"]["hr_max"]),
        summary_path=summary_path,
        raw_path=raw_path,
        quiet=args.quiet,
        hold_detector=detector,
        jsonl_path=jsonl_path,
    )

    print("PPG 单独憋气识别（仅 MAX30102）")
    if calibration_path:
        print("using calibration curve from %s" % calibration_path)
    print("步骤：先正常呼吸约 %d 秒建立基线，看到 normal 后再憋气。" % args.calib)
    print("检测延迟约 10-15 秒；注意贴紧手指、保持不动、遮光。")
    print("summary -> %s" % summary_path)
    print("jsonl   -> %s" % jsonl_path)
    if raw_path:
        print("raw     -> %s" % raw_path)
    print("Ctrl-C to stop.\n")

    try:
        stats = monitor.run(args.duration)
    finally:
        device.close()

    print("\nsession finished")
    _print_stats(stats)
    print(
        "hold state active for %d s, transitions: %s"
        % (stats.get("hold_seconds", 0), stats.get("hold_transitions", []))
    )
    return EXIT_OK if stats["valid"] else EXIT_NO_DATA


def cmd_hold_sim(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    coeffs, _ = _load_coeffs(config)
    fs = float(config["acq"]["sample_rate"])

    recording = simulate.generate(
        duration_s=args.duration,
        fs=fs,
        hr=args.hr,
        spo2=args.spo2,
        seed=args.seed,
        hold_start=args.hold_start,
        hold_duration=args.hold_dur,
        hold_hr_drop=args.hr_drop,
        noise_counts=args.noise,
    )

    detector = PPGBreathHoldDetector(
        fs=fs,
        calib_s=args.calib,
        min_sqi=float(config["dsp"]["min_sqi"]),
        finger_dc_ir=float(config["dsp"]["finger_dc_ir"]),
        min_pi=float(config["dsp"]["min_pi_pct"]),
        max_pi=float(config["dsp"]["max_pi_pct"]),
        coeffs=coeffs,
    )

    summary_path = args.out or os.path.join(config["output"]["dir"], "hold_simulation.csv")
    root, _ = os.path.splitext(summary_path)
    jsonl_path = root + ".jsonl"

    monitor = Monitor(
        simulate.FakeDevice(recording, int(round(float(config["acq"]["poll_interval_s"]) * fs))),
        fs=fs,
        window_s=float(config["dsp"]["window_s"]),
        poll_interval_s=float(config["acq"]["poll_interval_s"]),
        emit_interval_s=float(config["output"]["emit_interval_s"]),
        clock=simulate.VirtualClock(),
        coeffs=coeffs,
        min_sqi=float(config["dsp"]["min_sqi"]),
        finger_dc_ir=float(config["dsp"]["finger_dc_ir"]),
        min_pi=float(config["dsp"]["min_pi_pct"]),
        max_pi=float(config["dsp"]["max_pi_pct"]),
        summary_path=summary_path,
        jsonl_path=jsonl_path,
        quiet=args.quiet,
        hold_detector=detector,
    )

    print(
        "simulating %.0f s: normal 0-%.0f, hold %.0f-%.0f, HR %.0f, SpO2 %.0f"
        % (
            args.duration,
            args.hold_start if args.hold_start is not None else args.duration,
            args.hold_start or 0,
            (args.hold_start or 0) + args.hold_dur,
            args.hr,
            args.spo2,
        )
    )
    stats = monitor.run(args.duration)

    transitions = stats.get("hold_transitions", [])
    print("transitions: %s" % transitions)
    if args.hold_start is not None:
        starts = [tr for tr in transitions if tr["to"] == "hold"]
        ends = [tr for tr in transitions if tr["from"] == "hold"]
        if starts:
            print("detected hold at t=%.1f s (latency %.1f s)" % (starts[0]["t"], starts[0]["t"] - args.hold_start))
        else:
            print("hold was NOT detected")
        if ends:
            print("hold ended at t=%.1f s" % ends[0]["t"])
    print("baseline: %s  hr=%s" % (detector.baseline, detector.baseline_hr))
    print("summary -> %s" % summary_path)
    print("jsonl   -> %s" % jsonl_path)
    _print_stats(stats)
    return EXIT_OK


def cmd_replay(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    coeffs, _ = _load_coeffs(config)

    red, ir, fs = _read_raw(args.csv)
    window_s = float(config["dsp"]["window_s"])
    window = max(32, int(round(window_s * fs)))
    step = max(1, int(round(fs)))

    rows: List[Dict[str, Any]] = []
    for end in range(window, red.size + 1, step):
        result = dsp.analyze_window(
            red[end - window : end],
            ir[end - window : end],
            fs,
            coeffs=coeffs,
            min_sqi=float(config["dsp"]["min_sqi"]),
            finger_dc_ir=float(config["dsp"]["finger_dc_ir"]),
            min_pi=float(config["dsp"]["min_pi_pct"]),
            max_pi=float(config["dsp"]["max_pi_pct"]),
            hr_min=float(config["dsp"]["hr_min"]),
            hr_max=float(config["dsp"]["hr_max"]),
        )
        rows.append(
            {
                "hr_bpm": result["hr_bpm"],
                "spo2_pct": result["spo2_pct"] if result["valid"] else None,
                "pi_pct": result["pi_pct"],
                "r_ratio": result["r_ratio"],
                "sqi": result["sqi"],
                "valid": result["valid"],
            }
        )

    summary = _summarise(rows)
    print(
        "replay %s: %.1f s at %.1f Hz, %d windows"
        % (args.csv, red.size / fs, fs, summary["windows"])
    )
    _print_stats(summary)

    if args.json:
        print(json.dumps(summary, indent=2))
    return EXIT_OK if summary["valid_windows"] else EXIT_NO_DATA


def cmd_selftest(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    coeffs, _ = _load_coeffs(config)
    failures: List[str] = []

    def check(name: str, condition: bool, detail: str) -> None:
        status = "PASS" if condition else "FAIL"
        print("[%s] %s: %s" % (status, name, detail))
        if not condition:
            failures.append(name)

    fs = float(config["acq"]["sample_rate"])
    poll = float(config["acq"]["poll_interval_s"])

    # 1. R <-> SpO2 round trip on the empirical curve
    worst = 0.0
    for target in (90.0, 95.0, 97.0, 99.0):
        recovered = dsp.spo2_from_r(dsp.r_from_spo2(target), coeffs)
        worst = max(worst, abs(recovered - target))
    check("curve round-trip", worst < 0.05, "max error %.4f%%" % worst)

    # 2. Full pipeline: known HR / SpO2 must be recovered
    def run_case(target_hr: float, target_spo2: float, seed: int = 11) -> Dict[str, Any]:
        recording = simulate.generate(
            duration_s=30.0, fs=fs, hr=target_hr, spo2=target_spo2, seed=seed
        )
        monitor = Monitor(
            simulate.FakeDevice(recording, int(round(poll * fs))),
            fs=fs,
            window_s=float(config["dsp"]["window_s"]),
            poll_interval_s=poll,
            emit_interval_s=float(config["output"]["emit_interval_s"]),
            clock=simulate.VirtualClock(),
            coeffs=coeffs,
            min_sqi=float(config["dsp"]["min_sqi"]),
            finger_dc_ir=float(config["dsp"]["finger_dc_ir"]),
            min_pi=float(config["dsp"]["min_pi_pct"]),
            max_pi=float(config["dsp"]["max_pi_pct"]),
            quiet=True,
        )
        return monitor.run(30.0)

    reference_stats: Optional[Dict[str, Any]] = None
    for target_hr, target_spo2 in ((60.0, 98.0), (72.0, 97.0), (110.0, 95.0)):
        stats = run_case(target_hr, target_spo2)
        if target_hr == 72.0:
            reference_stats = stats
        hr_ok = stats["mean_hr"] is not None and abs(stats["mean_hr"] - target_hr) <= 3.0
        spo2_ok = (
            stats["mean_spo2"] is not None
            and abs(stats["mean_spo2"] - target_spo2) <= 2.0
        )
        check(
            "recover %.0f bpm / %.0f%%" % (target_hr, target_spo2),
            hr_ok and spo2_ok,
            "HR %s (target %.0f +/-3), SpO2 %s (target %.0f +/-2)"
            % (
                "--" if stats["mean_hr"] is None else "%.1f bpm" % stats["mean_hr"],
                target_hr,
                "--" if stats["mean_spo2"] is None else "%.1f%%" % stats["mean_spo2"],
                target_spo2,
            ),
        )

    check(
        "valid windows",
        reference_stats is not None and reference_stats["valid_ratio"] > 0.5,
        "%.0f%% of windows valid"
        % (100.0 * (reference_stats["valid_ratio"] if reference_stats else 0.0)),
    )

    # 3. No finger must never produce a reading
    empty = simulate.generate(duration_s=15.0, fs=fs, seed=3, finger=False)
    empty_monitor = Monitor(
        simulate.FakeDevice(empty, int(round(poll * fs))),
        fs=fs,
        window_s=float(config["dsp"]["window_s"]),
        poll_interval_s=poll,
        emit_interval_s=float(config["output"]["emit_interval_s"]),
        clock=simulate.VirtualClock(),
        coeffs=coeffs,
        min_sqi=float(config["dsp"]["min_sqi"]),
        finger_dc_ir=float(config["dsp"]["finger_dc_ir"]),
        quiet=True,
    )
    empty_stats = empty_monitor.run(15.0)
    check(
        "no-finger rejection",
        empty_stats["valid"] == 0,
        "%d valid windows (expected 0)" % empty_stats["valid"],
    )

    # 4. PPG-only breath-hold detection
    def run_hold_case(hold_start, hold_duration, duration, seed):
        recording = simulate.generate(
            duration_s=duration,
            fs=fs,
            hr=72.0,
            spo2=97.0,
            seed=seed,
            hold_start=hold_start,
            hold_duration=hold_duration,
        )
        detector = PPGBreathHoldDetector(
            fs=fs,
            calib_s=20.0,
            min_sqi=float(config["dsp"]["min_sqi"]),
            finger_dc_ir=float(config["dsp"]["finger_dc_ir"]),
            min_pi=float(config["dsp"]["min_pi_pct"]),
            max_pi=float(config["dsp"]["max_pi_pct"]),
            coeffs=coeffs,
        )
        monitor = Monitor(
            simulate.FakeDevice(recording, int(round(poll * fs))),
            fs=fs,
            window_s=float(config["dsp"]["window_s"]),
            poll_interval_s=poll,
            emit_interval_s=float(config["output"]["emit_interval_s"]),
            clock=simulate.VirtualClock(),
            coeffs=coeffs,
            min_sqi=float(config["dsp"]["min_sqi"]),
            finger_dc_ir=float(config["dsp"]["finger_dc_ir"]),
            min_pi=float(config["dsp"]["min_pi_pct"]),
            max_pi=float(config["dsp"]["max_pi_pct"]),
            quiet=True,
            hold_detector=detector,
        )
        return monitor.run(duration)

    hold_stats = run_hold_case(40.0, 25.0, 95.0, 5)
    transitions = hold_stats.get("hold_transitions", [])
    starts = [item for item in transitions if item["to"] == "hold"]
    ends = [item for item in transitions if item["from"] == "hold"]
    detected_at = starts[0]["t"] if starts else None
    ended_at = ends[0]["t"] if ends else None
    check(
        "PPG hold detection",
        detected_at is not None and 40.0 <= detected_at <= 62.0,
        "first hold at %s, end at %s"
        % ("--" if detected_at is None else "t=%.0fs" % detected_at,
           "--" if ended_at is None else "t=%.0fs" % ended_at),
    )
    check(
        "PPG hold release",
        ended_at is not None and ended_at <= 80.0,
        "returned to non-hold at %s"
        % ("--" if ended_at is None else "t=%.0fs" % ended_at),
    )

    quiet_stats = run_hold_case(None, 0.0, 70.0, 9)
    quiet_hold_events = [
        item for item in quiet_stats.get("hold_transitions", []) if item["to"] == "hold"
    ]
    check(
        "PPG no false hold",
        len(quiet_hold_events) == 0,
        "%d false hold entries in %d state changes"
        % (len(quiet_hold_events), len(quiet_stats.get("hold_transitions", []))),
    )

    print("")
    if failures:
        print("SELFTEST FAILED: %s" % ", ".join(failures))
        return 1
    print("SELFTEST PASS")
    return EXIT_OK


def cmd_plot(args: argparse.Namespace) -> int:
    output = plotting.plot_summary(args.csv, args.out, title=args.title)
    print("wrote %s" % output)
    return EXIT_OK


def cmd_version(_args: argparse.Namespace) -> int:
    print("oxy %s" % __version__)
    return EXIT_OK


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oxy",
        description="MAX30102 pulse oximetry monitor for Raspberry Pi.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="path to config.yaml (defaults to built-in values)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    diag = subparsers.add_parser("diag", help="inspect I2C bus and sensor registers")
    diag.set_defaults(func=cmd_diag)

    monitor = subparsers.add_parser("monitor", help="live acquisition to CSV")
    monitor.add_argument("--out", help="summary CSV path")
    monitor.add_argument("--raw-out", help="raw 100 Hz CSV path")
    monitor.add_argument("--no-raw", action="store_true", help="do not log raw samples")
    monitor.add_argument(
        "--duration", type=float, default=0.0, help="stop after N seconds (0 = forever)"
    )
    monitor.add_argument("--quiet", action="store_true", help="suppress the status line")
    monitor.set_defaults(func=cmd_monitor)

    sim = subparsers.add_parser("simulate", help="run the pipeline on synthetic data")
    sim.add_argument("--duration", type=float, default=30.0)
    sim.add_argument("--hr", type=float, default=72.0)
    sim.add_argument("--spo2", type=float, default=97.0)
    sim.add_argument("--pi", type=float, default=1.0, help="perfusion index in percent")
    sim.add_argument("--noise", type=float, default=40.0, help="ADC counts")
    sim.add_argument("--seed", type=int, default=7)
    sim.add_argument("--no-finger", action="store_true")
    sim.add_argument("--out", help="summary CSV path")
    sim.add_argument("--no-raw", action="store_true")
    sim.add_argument("--quiet", action="store_true")
    sim.set_defaults(func=cmd_simulate)

    replay = subparsers.add_parser("replay", help="re-run the DSP on a raw CSV")
    replay.add_argument("csv", help="raw CSV written by monitor/simulate")
    replay.add_argument("--json", action="store_true", help="print the summary as JSON")
    replay.set_defaults(func=cmd_replay)

    selftest = subparsers.add_parser("selftest", help="verify the pipeline without hardware")
    selftest.set_defaults(func=cmd_selftest)

    hold = subparsers.add_parser("hold", help="PPG-only breath-hold detection (live)")
    hold.add_argument("--out", help="summary CSV path")
    hold.add_argument("--jsonl", help="per-second JSONL stream for fusion")
    hold.add_argument("--no-raw", action="store_true")
    hold.add_argument("--duration", type=float, default=0.0)
    hold.add_argument("--calib", type=float, default=20.0, help="baseline seconds")
    hold.add_argument("--quiet", action="store_true")
    hold.set_defaults(func=cmd_hold)

    hold_sim = subparsers.add_parser("hold-sim", help="verify breath-hold detection offline")
    hold_sim.add_argument("--duration", type=float, default=95.0)
    hold_sim.add_argument("--hold-start", type=float, default=40.0)
    hold_sim.add_argument("--hold-dur", type=float, default=30.0)
    hold_sim.add_argument("--hr", type=float, default=72.0)
    hold_sim.add_argument("--spo2", type=float, default=97.0)
    hold_sim.add_argument("--hr-drop", type=float, default=4.0)
    hold_sim.add_argument("--noise", type=float, default=40.0)
    hold_sim.add_argument("--calib", type=float, default=20.0)
    hold_sim.add_argument("--seed", type=int, default=5)
    hold_sim.add_argument("--out", default=None)
    hold_sim.add_argument("--quiet", action="store_true")
    hold_sim.set_defaults(func=cmd_hold_sim)

    plot = subparsers.add_parser("plot", help="render a summary CSV to PNG")
    plot.add_argument("csv")
    plot.add_argument("out")
    plot.add_argument("--title", default=None)
    plot.set_defaults(func=cmd_plot)

    version = subparsers.add_parser("version", help="print the version")
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    def _terminate(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError):
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except FileNotFoundError as exc:
        print("ERROR: %s" % exc)
        return EXIT_HARDWARE
    except ValueError as exc:
        print("ERROR: %s" % exc)
        return EXIT_HARDWARE


if __name__ == "__main__":
    sys.exit(main())
