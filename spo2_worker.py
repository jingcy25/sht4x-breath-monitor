#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SpO2 / HR 采集子进程封装。

复用 `spo2/` 子项目的 `oxy.cli monitor`（MAX30102 + 既有 DSP），
每秒读一次 summary CSV，把最新一行交给主服务。

设计原则：
- 只在 A 层进入“在床”后启动，离床后停止（同步开启/持续记录）。
- 不参与 20 s 呼吸暂停判定，只做记录/晨报关联。
- 硬件不存在、`spo2/` 不存在或子进程退出时，静默降级为 `--`，不阻塞主服务。
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, Optional


class SpO2Worker:
    def __init__(
        self,
        root: Optional[str] = None,
        on_sample: Optional[Callable[[Dict[str, object]], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        poll_s: float = 0.8,
    ) -> None:
        self.root = os.path.abspath(root or os.path.dirname(__file__))
        self.spo2_dir = os.path.join(self.root, "spo2")
        self.data_dir = os.path.join(self.root, "data")
        self.on_sample = on_sample
        self.on_status = on_status or (lambda msg: None)
        self.poll_s = max(0.3, float(poll_s))

        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._log = None
        self._path: Optional[str] = None
        self._last_row = None
        self._running = False

    # ------------------------------------------------------------------ api
    def start(self) -> bool:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return True
            if not os.path.isdir(os.path.join(self.spo2_dir, "src")):
                self.on_status("SpO2 subproject not found; skipping")
                return False
            os.makedirs(self.data_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            self._path = os.path.join(self.data_dir, "spo2_%s.csv" % stamp)
            log_path = os.path.join(self.data_dir, "spo2.log")
            try:
                self._log = open(log_path, "ab", buffering=0)
            except Exception:
                self._log = subprocess.DEVNULL  # type: ignore[assignment]

            env = os.environ.copy()
            env["PYTHONPATH"] = os.path.join(self.spo2_dir, "src")
            env["PYTHONUNBUFFERED"] = "1"
            cmd = [
                sys.executable,
                "-u",
                "-m",
                "oxy.cli",
                "monitor",
                "--out",
                self._path,
            ]
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=self.spo2_dir,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=self._log,
                    stdin=subprocess.DEVNULL,
                )
            except Exception as exc:
                self.on_status("SpO2 start failed: %s" % exc)
                self._proc = None
                return False

            self._stop.clear()
            self._last_row = None
            self._running = True
            self._thread = threading.Thread(target=self._tail_loop, daemon=True)
            self._thread.start()
            self.on_status("SpO2 started: %s" % self._path)
            return True

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._stop.set()
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except Exception:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        if self._log not in (None, subprocess.DEVNULL):
            try:
                self._log.close()
            except Exception:
                pass
        self._log = None
        self.on_status("SpO2 stopped")

    def close(self) -> None:
        self.stop()

    # --------------------------------------------------------------- tail
    def _tail_loop(self) -> None:
        """Follow the summary CSV as it is appended.

        The oxy monitor writes one row per emitted window and flushes, so this
        is a real tail.  Rows are parsed one at a time with ``csv.reader`` on a
        single line rather than ``csv.DictReader``: a DictReader bound to the
        file stops for good at the first EOF, which made the live tail emit
        nothing while the file was still growing.
        """
        handle = None
        fields = None
        current_path = None
        rows = 0
        try:
            while not self._stop.is_set():
                path = self._path
                if path != current_path:
                    if handle is not None:
                        try:
                            handle.close()
                        except Exception:
                            pass
                    handle = None
                    fields = None
                    current_path = path

                if path and handle is None and os.path.exists(path):
                    try:
                        handle = open(path, "r", newline="", encoding="utf-8")
                        header = handle.readline()
                        if not header.strip():
                            # monitor created the file but has not flushed yet
                            handle.close()
                            handle = None
                        else:
                            fields = next(csv.reader([header]))
                            self.on_status("tail watching %s (cols=%d)"
                                           % (os.path.basename(path), len(fields)))
                    except Exception as exc:
                        self.on_status("SpO2 open failed: %s" % exc)
                        handle = None
                        fields = None

                if handle is not None and fields:
                    try:
                        line = handle.readline()
                        if line and line.strip():
                            values = next(csv.reader([line]))
                            if len(values) == len(fields):
                                row = dict(zip(fields, values))
                                if row != self._last_row:
                                    self._last_row = row
                                    self._emit(row)
                                    rows += 1
                                    if rows % 10 == 0:
                                        self.on_status(
                                            "tail rows=%d spo2=%s valid=%s"
                                            % (rows, row.get("spo2_pct"),
                                               row.get("valid")))
                    except Exception as exc:
                        self.on_status("SpO2 parse failed: %s" % exc)

                proc = self._proc
                if proc is not None and proc.poll() is not None and not self._stop.is_set():
                    self.on_status("SpO2 process exited code=%s" % proc.returncode)
                    self._stop.set()
                    break
                time.sleep(self.poll_s)
            self.on_status("tail stopped after %d rows" % rows)
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    def _emit(self, row: Dict[str, str]) -> None:
        def num(key: str) -> Optional[float]:
            try:
                value = row.get(key, "")
                if value is None or str(value).strip() == "":
                    return None
                return float(value)
            except Exception:
                return None

        payload = {
            "ts": time.time(),
            "hr": num("hr_bpm"),
            "spo2": num("spo2_pct"),
            "pi": num("pi_pct"),
            "sqi": num("sqi"),
            "valid": int(num("valid") or 0),
            "finger": int(num("finger") or 0),
        }
        if self.on_sample is not None:
            try:
                self.on_sample(payload)
            except Exception as exc:
                self.on_status("SpO2 callback failed: %s" % exc)
