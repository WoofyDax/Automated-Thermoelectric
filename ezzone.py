"""
EZ-Zone PM Temperature Controller — Modbus RTU Client
TestEquity TEC1 Thermoelectric Chamber

Communicates via RS-232C (FTDI USB adapter) using 32-bit Modbus RTU.
Null-modem cable, DB-9 connector.

Register Map (verified against TEC1 manual, TC-3400 comms PDF,
and Sample Modbus Packet PDF):

    Read (FC 0x03 or 0x04):
      360–361  – Analog Input 1 / Process Value (32-bit float LE, °C)
      370–371  – Analog Input 2 (32-bit float LE, °C)

    Write (FC 0x10 — Multiple Write Registers REQUIRED for floats):
      2160–2161 – Closed Loop Set Point (32-bit float LE, °C)

    Word order: Low register = LSW, High register = MSW (Low-High).
    Float byte order: little-endian.

IMPORTANT: RS-232 only sets registers — it does NOT control the
chamber. Both POWER and TEMP switches must be physically ON for
the thermoelectric system to operate. Serial just sets the target
and reads back values.
"""

import time
import struct
import logging
from typing import Optional

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

logger = logging.getLogger(__name__)

# ---------- Verified Register Map ----------
REG_PROCESS_VALUE    = 360    # R   float  – Analog Input 1 (chamber temp, °C)
REG_ANALOG_INPUT_2   = 370    # R   float  – Analog Input 2 (°C)
REG_SETPOINT         = 2160   # R/W float  – Closed Loop Set Point (°C)


class EZZoneError(Exception):
    """Base exception for EZ-Zone communication errors."""
    pass


class EZZoneClient:
    """Modbus RTU client for the TEC1 EZ-Zone PM temperature controller.

    - 32-bit floats in low-word-first (LE) order
    - FC 0x10 (Write Multiple Registers) for all float writes
    - FC 0x03 (Read Holding Registers) for reads
    - Temperatures in °C (TEC1 factory configuration)
    """

    def __init__(self, port: str = "/dev/ttyUSB0",
                 slave_id: int = 1,
                 baudrate: int = 9600,
                 timeout: float = 1.0,
                 retries: int = 3,
                 retry_delay: float = 1.0):
        self.slave_id = slave_id
        self.retries = retries
        self.retry_delay = retry_delay

        self.client = ModbusSerialClient(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Open the serial Modbus connection."""
        port_name = getattr(self.client.comm_params, 'host', 'unknown')
        logger.info("Connecting to EZ-Zone on %s (slave %d) …",
                    port_name, self.slave_id)
        connected = self.client.connect()
        if connected:
            logger.info("Connected to TEC1 EZ-Zone PM.")
        else:
            logger.error("Failed to connect.")
        return connected

    def disconnect(self):
        """Close the serial connection."""
        self.client.close()
        logger.info("Disconnected.")

    @property
    def port_name(self) -> str:
        return getattr(self.client.comm_params, 'host', 'unknown')

    @property
    def is_connected(self) -> bool:
        return self.client.connected if hasattr(self.client, 'connected') else False

    # ------------------------------------------------------------------
    # Low-level — 32-bit float, LE word order, LE float
    # ------------------------------------------------------------------

    def _read_float(self, register: int) -> Optional[float]:
        """Read IEEE 754 float from two consecutive holding registers.
        Register N = LSW, Register N+1 = MSW. Little-endian float."""
        for attempt in range(1, self.retries + 1):
            try:
                rr = self.client.read_holding_registers(
                    address=register, count=2, device_id=self.slave_id
                )
                if rr.isError():
                    raise ModbusException(f"Modbus error: {rr}")
                # registers[0] = low word, registers[1] = high word
                raw = struct.pack("<HH", rr.registers[0], rr.registers[1])
                return round(struct.unpack("<f", raw)[0], 2)
            except (ModbusException, struct.error, IndexError) as e:
                logger.warning("Read float reg %d attempt %d/%d: %s",
                               register, attempt, self.retries, e)
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
        logger.error("Failed to read float register %d.", register)
        return None

    def _write_float(self, register: int, value: float) -> bool:
        """Write IEEE 754 float to two consecutive registers using FC 0x10.
        Register N = LSW, Register N+1 = MSW. Little-endian float."""
        raw = struct.pack("<f", float(value))
        regs = list(struct.unpack("<HH", raw))  # [low_word, high_word]
        for attempt in range(1, self.retries + 1):
            try:
                rr = self.client.write_registers(
                    address=register, values=regs, device_id=self.slave_id
                )
                if rr.isError():
                    raise ModbusException(f"Modbus error: {rr}")
                return True
            except ModbusException as e:
                logger.warning("Write float reg %d = %.2f attempt %d/%d: %s",
                               register, value, attempt, self.retries, e)
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
        logger.error("Failed to write float register %d.", register)
        return False

    # ------------------------------------------------------------------
    # High-level API — the only two registers you should touch
    # ------------------------------------------------------------------

    def get_process_value(self) -> Optional[float]:
        """Read current chamber temperature from Analog Input 1 (°C)."""
        return self._read_float(REG_PROCESS_VALUE)

    def get_analog_input_2(self) -> Optional[float]:
        """Read Analog Input 2 value (°C)."""
        return self._read_float(REG_ANALOG_INPUT_2)

    def get_setpoint(self) -> Optional[float]:
        """Read the current closed-loop set point (°C)."""
        return self._read_float(REG_SETPOINT)

    def set_setpoint(self, temperature_c: float) -> bool:
        """Set the closed-loop set point (°C). Uses FC 0x10 (Write Multiple Registers)."""
        return self._write_float(REG_SETPOINT, temperature_c)

    def get_status(self) -> dict:
        """Read current chamber status."""
        return {
            "process_value_c":  self.get_process_value(),
            "analog_input_2_c": self.get_analog_input_2(),
            "setpoint_c":       self.get_setpoint(),
        }
