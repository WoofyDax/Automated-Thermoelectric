"""
EZ-Zone PM Temperature Controller — Modbus RTU Client
TestEquity TEC1 Thermoelectric Chamber

Communicates via RS-232C (FTDI USB adapter) using 32-bit Modbus RTU.
Null-modem cable, DB-9 connector.

Register Map (verified against TEC1 manual, TC-3400 comms PDF,
and Sample Modbus Packet PDF):

    Read (FC 0x03 or 0x04):
      360–361  – Analog Input 1 / Process Value (32-bit float LE, °C)
      1882     – Control Mode Active (integer: 10=Auto, 54=Manual, 62=Off)
      1904–1905 – Heat Power (32-bit float LE, 0..100%)
      1906–1907 – Cool Power (32-bit float LE, -100..0%)
      1908–1909 – Control Loop Output Power (32-bit float LE, -100..100%)
      2172–2173 – Closed Loop Active Set Point (32-bit float LE, °C)

    Write (FC 0x10 — Multiple Write Registers REQUIRED for floats):
      2160–2161 – Closed Loop Set Point (32-bit float LE, °C)

    Write (FC 0x06 — Single Write Register for integer setup values):
      1880     – Loop Control Mode (10=Auto, 54=Manual, 62=Off)
      1886     – Cool Algorithm (71=PID, 64=On-Off, 62=Off)
      918      – Digital Output 2 Function (20=Cool, 36=Heat, 62=Off)

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
REG_SETPOINT         = 2160   # R/W float  – Closed Loop Set Point (°C)
REG_ACTIVE_SETPOINT  = 2172   # R   float  – Closed Loop Active Set Point (°C)
REG_IDLE_SETPOINT    = 2176   # R/W float  – Idle Set Point (event-triggered)
REG_RAMP_ACTIVE_SETPOINT = 2190 # R float  – Ramp Active Set Point (°C)

REG_CONTROL_MODE     = 1880   # R/W int    – 10=Auto, 54=Manual, 62=Off
REG_CONTROL_MODE_ACTIVE = 1882 # R int      – active control mode
REG_HEAT_POWER       = 1904   # R   float  – Heat output, 0..100%
REG_COOL_POWER       = 1906   # R   float  – Cool output, -100..0%
REG_LOOP_OUTPUT_POWER = 1908  # R   float  – Net output, -100..100%

REG_COOL_ALGORITHM   = 1886   # R/W int    – 71=PID, 64=On-Off, 62=Off
REG_OUTPUT_1_FUNCTION = 888   # R/W int    – Digital output 1 function
REG_OUTPUT_2_FUNCTION = 918   # R/W int    – Digital output 2 function
REG_OUTPUT_1_POWER   = 894    # R   float  – Digital output 1 power, 0..100%
REG_OUTPUT_2_POWER   = 924    # R   float  – Digital output 2 power, 0..100%

WATLOW_AUTO = 10
WATLOW_MANUAL = 54
WATLOW_OFF = 62
WATLOW_ON = 63
WATLOW_COOL = 20
WATLOW_HEAT = 36
WATLOW_PID = 71


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

    def _read_int(self, register: int) -> Optional[int]:
        """Read one unsigned 16-bit integer register."""
        for attempt in range(1, self.retries + 1):
            try:
                rr = self.client.read_holding_registers(
                    address=register, count=1, device_id=self.slave_id
                )
                if rr.isError():
                    raise ModbusException(f"Modbus error: {rr}")
                return int(rr.registers[0])
            except (ModbusException, IndexError) as e:
                logger.warning("Read int reg %d attempt %d/%d: %s",
                               register, attempt, self.retries, e)
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
        logger.error("Failed to read integer register %d.", register)
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

    def _write_int(self, register: int, value: int) -> bool:
        """Write one unsigned 16-bit integer register using FC 0x06."""
        for attempt in range(1, self.retries + 1):
            try:
                rr = self.client.write_register(
                    address=register, value=int(value), device_id=self.slave_id
                )
                if rr.isError():
                    raise ModbusException(f"Modbus error: {rr}")
                return True
            except ModbusException as e:
                logger.warning("Write int reg %d = %d attempt %d/%d: %s",
                               register, value, attempt, self.retries, e)
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
        logger.error("Failed to write integer register %d.", register)
        return False

    # ------------------------------------------------------------------
    # High-level API — the only two registers you should touch
    # ------------------------------------------------------------------

    def get_process_value(self) -> Optional[float]:
        """Read current chamber temperature from Analog Input 1 (°C)."""
        return self._read_float(REG_PROCESS_VALUE)

    def get_setpoint(self) -> Optional[float]:
        """Read the current closed-loop set point (°C)."""
        return self._read_float(REG_SETPOINT)

    def set_setpoint(self, temperature_c: float) -> bool:
        """Set the closed-loop set point (°C). Uses FC 0x10 (Write Multiple Registers)."""
        return self._write_float(REG_SETPOINT, temperature_c)

    def enable_control(self) -> bool:
        """Enable closed-loop AUTO control mode."""
        return self._write_int(REG_CONTROL_MODE, WATLOW_AUTO)

    def disable_control(self) -> bool:
        """Turn loop control off."""
        return self._write_int(REG_CONTROL_MODE, WATLOW_OFF)

    def get_control_mode(self) -> Optional[int]:
        """Read active control mode (10=Auto, 54=Manual, 62=Off)."""
        return self._read_int(REG_CONTROL_MODE_ACTIVE)

    def get_heat_output(self) -> Optional[float]:
        """Read heat output percentage (0–100%)."""
        return self._read_float(REG_HEAT_POWER)

    def get_cool_output(self) -> Optional[float]:
        """Read cool output percentage (-100–0%)."""
        return self._read_float(REG_COOL_POWER)

    def get_loop_output(self) -> Optional[float]:
        """Read net loop output percentage (-100=cooling, +100=heating)."""
        return self._read_float(REG_LOOP_OUTPUT_POWER)

    def get_alarm_status(self) -> Optional[int]:
        """Read alarm status register.

        NOTE: Alarm register is controller-model dependent.
        On EZ-Zone PM this may be Alarm Status 1 (register 1240)
        or Profile Event Status. Not yet implemented — always
        returns None until the correct register is confirmed.
        """
        return None

    def get_active_setpoint(self) -> Optional[float]:
        """Read the active setpoint (may differ from SP1 in manual mode)."""
        return self._read_float(REG_ACTIVE_SETPOINT)

    def get_idle_setpoint(self) -> Optional[float]:
        """Read the idle setpoint, which only controls when an idle event is active."""
        return self._read_float(REG_IDLE_SETPOINT)

    def set_idle_setpoint(self, temperature_c: float) -> bool:
        """Set the idle setpoint. This is not the normal closed-loop setpoint."""
        return self._write_float(REG_IDLE_SETPOINT, temperature_c)

    def get_cooling_config(self) -> dict:
        """Read the TEC1 cooling-related setup and output state registers."""
        return {
            "control_mode_active": self.get_control_mode(),
            "cool_algorithm": self._read_int(REG_COOL_ALGORITHM),
            "output_1_function": self._read_int(REG_OUTPUT_1_FUNCTION),
            "output_2_function": self._read_int(REG_OUTPUT_2_FUNCTION),
            "output_1_power_pct": self._read_float(REG_OUTPUT_1_POWER),
            "output_2_power_pct": self._read_float(REG_OUTPUT_2_POWER),
        }

    def configure_tec1_cooling(self) -> dict:
        """Restore the TestEquity TEC1 heat/cool setup from the manual.

        This writes EEPROM-backed setup values. Use only for repair, not in a
        fast polling loop.
        """
        results = {
            "control_mode_auto": self._write_int(REG_CONTROL_MODE, WATLOW_AUTO),
            "cool_algorithm_pid": self._write_int(REG_COOL_ALGORITHM, WATLOW_PID),
            "output_1_heat": self._write_int(REG_OUTPUT_1_FUNCTION, WATLOW_HEAT),
            "output_2_cool": self._write_int(REG_OUTPUT_2_FUNCTION, WATLOW_COOL),
        }
        return results

    @staticmethod
    def control_mode_name(mode: Optional[int]) -> str:
        """Return human-readable name for control mode."""
        if mode == WATLOW_AUTO:
            return "Auto"
        if mode == WATLOW_MANUAL:
            return "Manual"
        if mode == WATLOW_OFF:
            return "Off"
        return "Unknown"

    def get_status(self) -> dict:
        """Read current chamber status."""
        mode = self.get_control_mode()
        return {
            "process_value_c":  self.get_process_value(),
            "setpoint_c":       self.get_setpoint(),
            "active_setpoint_c": self.get_active_setpoint(),
            "idle_setpoint_c":  self.get_idle_setpoint(),
            "control_mode":     mode,
            "control_mode_name": self.control_mode_name(mode),
            "heat_output_pct":  self.get_heat_output(),
            "cool_output_pct":  self.get_cool_output(),
            "loop_output_pct":  self.get_loop_output(),
        }
