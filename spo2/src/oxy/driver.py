"""Minimal driver for the MAX30102 reflective PPG sensor.

Only the registers needed for SpO2 mode are implemented. ``smbus2`` is imported
lazily so the rest of the package (simulator, replay, self-test) works on a
machine with no hardware library installed.
"""

from __future__ import annotations

import time
from typing import Dict, List, Tuple

REG_INTR_STATUS_1 = 0x00
REG_INTR_STATUS_2 = 0x01
REG_INTR_ENABLE_1 = 0x02
REG_INTR_ENABLE_2 = 0x03
REG_FIFO_WR_PTR = 0x04
REG_FIFO_OVF = 0x05
REG_FIFO_RD_PTR = 0x06
REG_FIFO_DATA = 0x07
REG_FIFO_CONFIG = 0x08
REG_MODE_CONFIG = 0x09
REG_SPO2_CONFIG = 0x0A
REG_LED1_PA = 0x0C  # red
REG_LED2_PA = 0x0D  # infrared
REG_MULTI_LED_CTRL1 = 0x11
REG_MULTI_LED_CTRL2 = 0x12
REG_TEMP_INT = 0x1F
REG_TEMP_FRAC = 0x20
REG_TEMP_CONFIG = 0x21
REG_PART_ID = 0xFF

PART_ID_EXPECTED = 0x15
ADC_MASK = 0x03FFFF

SAMPLE_RATE_CODES = {
    50: 0,
    100: 1,
    200: 2,
    400: 3,
    800: 4,
    1000: 5,
    1600: 6,
    3200: 7,
}

# SPO2_CONFIG bit fields
_ADC_RANGE_4096NA = 0x01 << 5
_LED_PW_411US_18BIT = 0x03
_MODE_SPO2 = 0x03
_RESET_BIT = 0x40


class MAX30102:
    """Blocking-read driver: call :meth:`read_fifo` at a steady poll interval."""

    def __init__(
        self,
        bus: int = 1,
        address: int = 0x57,
        sample_rate: int = 100,
        led_red: int = 0x24,
        led_ir: int = 0x24,
    ) -> None:
        if sample_rate not in SAMPLE_RATE_CODES:
            raise ValueError(
                "unsupported sample_rate %s; choose one of %s"
                % (sample_rate, sorted(SAMPLE_RATE_CODES))
            )

        try:
            import smbus2
        except ImportError as exc:  # pragma: no cover - hardware only
            raise RuntimeError(
                "smbus2 is required for hardware access.\n"
                "  sudo apt install -y python3-smbus\n"
                "  pip install smbus2\n"
                "Not on a Raspberry Pi? Use the simulator instead:\n"
                "  PYTHONPATH=src python3 -m oxy.cli simulate --duration 30"
            ) from exc

        self._smbus2 = smbus2
        self.bus_number = int(bus)
        self.address = int(address)
        self.sample_rate = int(sample_rate)
        self.led_red = int(led_red)
        self.led_ir = int(led_ir)
        self._last_ovf = 0

        self._bus = smbus2.SMBus(self.bus_number)
        self._part_id = self._read_byte(REG_PART_ID)
        if self._part_id != PART_ID_EXPECTED:
            self.close()
            raise RuntimeError(
                "unexpected MAX30102 PART_ID 0x%02X (expected 0x%02X).\n"
                "Checklist: SDA=GPIO2/SCL=GPIO3 wired, sensor powered from 3.3 V, "
                "I2C enabled (sudo raspi-config nonint do_i2c 0), and "
                "'i2cdetect -y 1' showing 0x57."
                % (self._part_id, PART_ID_EXPECTED)
            )
        self._configure()

    # ------------------------------------------------------------------ I2C
    def _read_byte(self, register: int) -> int:
        return int(self._bus.read_byte_data(self.address, register))

    def _write_byte(self, register: int, value: int) -> None:
        self._bus.write_byte_data(self.address, register, int(value))

    def _read_block(self, register: int, length: int) -> List[int]:
        """Combined write-then-read so the FIFO pointer auto-increment works.

        ``read_i2c_block_data`` is capped at 32 bytes; 32 samples * 6 bytes is
        192, so the raw i2c_msg path is mandatory for a full FIFO drain.
        """
        write_message = self._smbus2.i2c_msg.write(self.address, [register])
        read_message = self._smbus2.i2c_msg.read(self.address, length)
        self._bus.i2c_rdwr(write_message, read_message)
        return list(read_message)

    # ------------------------------------------------------------ lifecycle
    @property
    def part_id(self) -> int:
        return self._part_id

    def reset(self) -> None:
        self._write_byte(REG_MODE_CONFIG, _RESET_BIT)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if not (self._read_byte(REG_MODE_CONFIG) & _RESET_BIT):
                return
            time.sleep(0.01)
        raise RuntimeError("MAX30102 did not finish its reset within 1 s")

    def _configure(self) -> None:
        self.reset()
        # SMP_AVE=1, rollover disabled, almost-full threshold = 15 remaining
        self._write_byte(REG_FIFO_CONFIG, 0x0F)
        # SpO2 mode: red + IR
        self._write_byte(REG_MODE_CONFIG, _MODE_SPO2)
        # 4096 nA range, 100 sps (default), 411 us / 18-bit pulses
        sr_code = SAMPLE_RATE_CODES[self.sample_rate]
        self._write_byte(
            REG_SPO2_CONFIG,
            _ADC_RANGE_4096NA | (sr_code << 2) | _LED_PW_411US_18BIT,
        )
        self._write_byte(REG_LED1_PA, self.led_red)
        self._write_byte(REG_LED2_PA, self.led_ir)
        self._write_byte(REG_MULTI_LED_CTRL1, 0x00)
        self._write_byte(REG_MULTI_LED_CTRL2, 0x00)

        # Reading the status registers clears any latched interrupt
        self._read_byte(REG_INTR_STATUS_1)
        self._read_byte(REG_INTR_STATUS_2)
        self._last_ovf = self._read_byte(REG_FIFO_OVF) & 0x1F
        self.read_fifo()  # discard whatever was in the FIFO before we started

    # ----------------------------------------------------------------- data
    def read_fifo(self) -> Tuple[List[Tuple[int, int]], bool]:
        """Drain the FIFO.

        Returns ``(samples, overflowed)`` where each sample is ``(red, ir)`` in
        raw 18-bit ADC counts and ``overflowed`` reports data loss since the
        previous call.
        """
        write_pointer = self._read_byte(REG_FIFO_WR_PTR) & 0x1F
        overflow_counter = self._read_byte(REG_FIFO_OVF) & 0x1F
        read_pointer = self._read_byte(REG_FIFO_RD_PTR) & 0x1F

        count = (write_pointer - read_pointer) & 0x1F
        overflowed = overflow_counter != self._last_ovf
        self._last_ovf = overflow_counter

        if count == 0:
            return [], overflowed

        payload = self._read_block(REG_FIFO_DATA, count * 6)
        if len(payload) < count * 6:
            return [], overflowed

        samples: List[Tuple[int, int]] = []
        for index in range(count):
            offset = index * 6
            red = (
                (payload[offset] << 16)
                | (payload[offset + 1] << 8)
                | payload[offset + 2]
            ) & ADC_MASK
            ir = (
                (payload[offset + 3] << 16)
                | (payload[offset + 4] << 8)
                | payload[offset + 5]
            ) & ADC_MASK
            samples.append((int(red), int(ir)))
        return samples, overflowed

    def read_temperature_c(self) -> float:
        """Die temperature, mostly useful to compensate LED drift."""
        self._write_byte(REG_TEMP_CONFIG, 0x01)
        time.sleep(0.04)
        integer = self._read_byte(REG_TEMP_INT)
        fraction = self._read_byte(REG_TEMP_FRAC) & 0x0F
        if integer > 127:
            integer -= 256
        return float(integer) + fraction * 0.0625

    def dump_registers(self) -> Dict[str, int]:
        """Register snapshot for diagnostics."""
        registers = {
            "INTR_STATUS_1": REG_INTR_STATUS_1,
            "INTR_STATUS_2": REG_INTR_STATUS_2,
            "FIFO_CONFIG": REG_FIFO_CONFIG,
            "MODE_CONFIG": REG_MODE_CONFIG,
            "SPO2_CONFIG": REG_SPO2_CONFIG,
            "LED1_PA": REG_LED1_PA,
            "LED2_PA": REG_LED2_PA,
            "TEMP_INT": REG_TEMP_INT,
            "PART_ID": REG_PART_ID,
        }
        return {name: self._read_byte(register) for name, register in registers.items()}

    def close(self) -> None:
        bus = getattr(self, "_bus", None)
        if bus is not None:
            try:
                bus.close()
            except Exception:  # pragma: no cover - best effort on shutdown
                pass
            self._bus = None  # type: ignore[assignment]

    def __enter__(self) -> "MAX30102":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
