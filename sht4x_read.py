#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHT4x 冒烟测试 —— 先确认接线和 I2C 通信没问题。

接线（按模块丝印为准）：
    SHT4x VIN  -> Pin 1  (3V3)
    SHT4x GND  -> Pin 9  (GND)
    SHT4x SCL  -> Pin 5  (GPIO3 / SCL)
    SHT4x SDA  -> Pin 3  (GPIO2 / SDA)

运行：
    python3 sht4x_read.py

读不到时排查：
    1) 已启用 I2C： sudo raspi-config -> Interface Options -> I2C -> Enable
    2) 设备在位： i2cdetect -y 1   （应看到 0x44）
    3) 接线是否接触不良 / 是否误接 5V（SHT4x 必须 3.3V）
"""
import time

import board
import busio
import adafruit_sht4x


def main():
    i2c = busio.I2C(board.SCL, board.SDA)
    sensor = adafruit_sht4x.SHT4x(i2c)
    sensor.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION  # 高精度、不加热

    try:
        print(f"检测到 SHT4x，序列号 = 0x{sensor.serial_number:04X}")
    except Exception:
        print("检测到 SHT4x（序列号读取失败，不影响使用）")

    print("时间        温度(°C)   湿度(%RH)")
    print("-" * 34)
    while True:
        t = sensor.temperature
        h = sensor.relative_humidity
        print(f"{time.strftime('%H:%M:%S')}   {t:6.2f}     {h:6.2f}")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
