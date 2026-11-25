#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHT4x 呼吸检测 / 呼吸暂停监测  ——  蜂鸣器部分留到最后（当前为占位）。

原理：
    呼气时湿度快速上升、吸气时回落。用慢速指数滑动平均估计"基线"，
    湿度偏离基线的幅度就是"呼吸信号"。信号向上越过阈值记为一次呼吸；
    连续 APNEA_SECONDS 秒没有呼吸 -> 触发报警（当前只打印，蜂鸣器待接）。

用法：
    python3 sht4x_breath.py                  # 普通状态输出
    python3 sht4x_breath.py --csv out.csv    # 同时记录 CSV，用于标定
    python3 sht4x_breath.py --spark          # 终端实时波形（标定阈值最直观）
    python3 sht4x_breath.py --th-up 1.0      # 调高呼气判定阈值 (%RH)
    python3 sht4x_breath.py --apnea 30       # 把暂停报警改成 30 秒

常用参数：
    --apnea     暂停报警秒数        默认 20
    --th-up     呼气判定阈值 %RH    默认 1.2（已实测标定）
    --th-down   迟滞阈值 %RH        默认 0.1
    --tau       基线时间常数(秒)    默认 2.0
    --rate      采样率 Hz           默认 20
"""
import argparse
import math
import time
from collections import deque

import board
import busio
import adafruit_sht4x


class BreathDetector:
    """基于湿度偏离基线的呼吸检测器（带迟滞，避免一次呼吸被重复计数）。"""

    def __init__(self, tau=2.0, th_up=0.5, th_down=0.1):
        self.tau = tau          # 基线时间常数（秒），越大基线越迟钝
        self.th_up = th_up      # 呼气判定阈值：信号向上越过
        self.th_down = th_down  # 迟滞阈值：信号向下越过才允许下一次计数
        self.baseline = None
        self.state = "low"      # low = 吸气等待回落；high = 刚呼过气
        self._last_t = None

    def update(self, t, h):
        if self.baseline is None:
            self.baseline = h
        dt = (t - self._last_t) if self._last_t is not None else 0.05
        self._last_t = t
        alpha = math.exp(-dt / self.tau)
        self.baseline = alpha * self.baseline + (1.0 - alpha) * h
        signal = h - self.baseline

        breath = False
        if self.state == "low" and signal >= self.th_up:
            self.state = "high"
            breath = True
        elif self.state == "high" and signal <= self.th_down:
            self.state = "low"
        return signal, breath


def sparkline(vals, width=64, lo=-2.0, hi=2.0):
    """把一串信号值画成单行 ASCII 波形，中间的 '|' 是基线(0)。"""
    row = ["·"] * width
    row[width // 2] = "|"
    for v in vals:
        pos = (v - lo) / (hi - lo)
        pos = int(round(max(0.0, min(1.0, pos)) * (width - 1)))
        row[pos] = "*"
    return "".join(row)


def main():
    ap = argparse.ArgumentParser(description="SHT4x 呼吸检测 / 呼吸暂停监测")
    ap.add_argument("--apnea", type=float, default=20.0, help="呼吸暂停报警秒数，默认 20")
    ap.add_argument("--th-up", type=float, default=1.2, help="呼气判定阈值 %%RH，默认 1.2（实测标定）")
    ap.add_argument("--th-down", type=float, default=0.1, help="迟滞阈值 %%RH，默认 0.1")
    ap.add_argument("--tau", type=float, default=2.0, help="基线时间常数（秒），默认 2.0")
    ap.add_argument("--rate", type=float, default=20.0, help="采样率 Hz，默认 20")
    ap.add_argument("--csv", type=str, default="", help="保存 CSV 的路径，例如 out.csv")
    ap.add_argument("--spark", action="store_true", help="终端实时波形显示")
    args = ap.parse_args()

    i2c = busio.I2C(board.SCL, board.SDA)
    sensor = adafruit_sht4x.SHT4x(i2c)
    sensor.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION

    det = BreathDetector(tau=args.tau, th_up=args.th_up, th_down=args.th_down)
    interval = 1.0 / args.rate
    half = max(4.0 * args.th_up, 1.0)  # 波形显示范围：阈值上下各 4 倍

    last_breath = time.monotonic()
    breath_times = deque(maxlen=8)  # 用于估算呼吸频率（次/分）
    signal_hist = deque(maxlen=64)  # 波形历史

    csv_f = open(args.csv, "w", newline="") if args.csv else None
    if csv_f:
        csv_f.write("t_s,humidity,signal,breath,apnea\n")

    # TODO(alarm): 蜂鸣器留到最后，这里先只打印；最后在这里接入 GPIO17 驱动。
    def set_alarm(on):
        if on and not set_alarm.state:
            print(f"  !! [ALARM] 呼吸暂停超过 {args.apnea:.0f} 秒 —— 蜂鸣器待接 !!")
        elif not on and set_alarm.state:
            print("  -- 检测到呼吸，报警解除 --")
        set_alarm.state = on

    set_alarm.state = False

    print(f"SHT4x 呼吸监测启动：采样 {args.rate:.0f}Hz，暂停阈值 {args.apnea:.0f}s，"
          f"呼气阈值 {args.th_up:.2f}%RH")
    print("按 Ctrl+C 退出。\n")

    next_status = 0.0
    try:
        while True:
            t = time.monotonic()
            h = sensor.relative_humidity
            signal, breath = det.update(t, h)
            signal_hist.append(signal)

            if breath:
                last_breath = t
                breath_times.append(t)

            apnea = (t - last_breath) > args.apnea
            set_alarm(apnea)

            # 呼吸频率：仅当最近一次呼吸在 5 秒内有效
            if len(breath_times) >= 2 and (t - breath_times[-1]) < 5.0:
                bpm = 60.0 * (len(breath_times) - 1) / (breath_times[-1] - breath_times[0])
            else:
                bpm = 0.0

            if csv_f:
                csv_f.write(f"{t:.3f},{h:.3f},{signal:.3f},"
                            f"{1 if breath else 0},{1 if apnea else 0}\n")
                csv_f.flush()

            if args.spark:
                line = (f"\r{sparkline(signal_hist, lo=-half, hi=half)}  "
                        f"湿度 {h:5.2f}%RH  呼吸 {bpm:4.1f}次/分  "
                        f"{'[APNEA]' if apnea else '      '}")
                print(line, end="", flush=True)
            elif t >= next_status:
                print(f"[{time.strftime('%H:%M:%S')}] 湿度 {h:5.2f}%RH  "
                      f"信号 {signal:+5.2f}  呼吸 {bpm:4.1f}次/分  "
                      f"{'[APNEA] 呼吸暂停' if apnea else '正常'}")
                next_status = t + 0.5

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        if csv_f:
            csv_f.close()
            print(f"CSV 已保存：{args.csv}")


if __name__ == "__main__":
    main()
