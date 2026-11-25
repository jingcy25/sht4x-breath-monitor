"""Direct smbus2 access to the sensors used by the breath monitor.

Deliberately avoids Adafruit Blinka: on a fresh Raspberry Pi OS image Blinka
pulls RPi.GPIO/board/busio, which is fragile on Bookworm and unnecessary here.
Everything is plain I2C register access.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

import smbus2


class SHT4x:
    """Temperature / relative humidity.

    Address is fixed at 0x44, so two SHT4x sensors can only coexist on two
    different I2C buses (see the i2c-gpio overlay in the wiring notes).
    """

    def __init__(self, bus_num: int = 1, addr: int = 0x44) -> None:
        self.bus = smbus2.SMBus(bus_num)
        self.addr = addr

    def read(self) -> Tuple[float, float]:
        command = smbus2.i2c_msg.write(self.addr, bytes((0xFD,)))
        self.bus.i2c_rdwr(command)
        time.sleep(0.02)
        reply = smbus2.i2c_msg.read(self.addr, 6)
        self.bus.i2c_rdwr(reply)
        data = list(reply)
        temperature = -45.0 + 175.0 * ((data[0] * 256 + data[1]) / 65535.0)
        humidity = -6.0 + 125.0 * ((data[3] * 256 + data[4]) / 65535.0)
        return temperature, humidity

    def close(self) -> None:
        self.bus.close()


class VEML7700:
    """Ambient light. Gain 1/8, 100 ms integration, 0.5376 lx per count."""

    def __init__(self, bus_num: int = 1, addr: int = 0x10) -> None:
        self.bus = smbus2.SMBus(bus_num)
        self.addr = addr
        self.bus.write_i2c_block_data(self.addr, 0x00, [0x00, 0x10])

    def read(self) -> Tuple[int, float]:
        data = self.bus.read_i2c_block_data(self.addr, 0x04, 2)
        raw = data[0] | (data[1] << 8)
        return raw, raw * 0.5376

    def close(self) -> None:
        self.bus.close()


class ADS1115:
    """16-bit ADC, single-ended channel against GND, PGA +/-2.048 V."""

    def __init__(self, bus_num: int = 1, addr: int = 0x48) -> None:
        self.bus = smbus2.SMBus(bus_num)
        self.addr = addr

    def read_voltage(self, channel: int = 0) -> Tuple[float, int]:
        config = 0xC283 | ((channel & 0x03) << 12)
        self.bus.write_i2c_block_data(
            self.addr, 0x01, [(config >> 8) & 0xFF, config & 0xFF]
        )
        time.sleep(0.02)
        data = self.bus.read_i2c_block_data(self.addr, 0x00, 2)
        raw = (data[0] << 8) | data[1]
        if raw > 32767:
            raw -= 65536
        return raw * 2.048 / 32768.0, raw

    def close(self) -> None:
        self.bus.close()
