# TestEquity TEC1 — Automated Temperature Test Suite

Tests a TestEquity TEC1 thermoelectric chamber across a configurable
temperature range to verify the device-under-test (DUT) inside the
chamber remains functional.

Communicates with a **Watlow EZ-Zone PM** temperature controller over
**RS-232C** (DB-9, null-modem cable) using **32-bit Modbus RTU**.

## Hardware Setup

```
PC (USB/Serial) ─── RS-232C null-modem cable ─── EZ-Zone PM (DB-9 rear panel)
                                                       │
                                                       └── TEC1 Chamber
```

- DB-9 connector on the EZ-Zone PM rear panel
- **Null-modem cable** required (RX/TX crossed)
- Default Modbus slave ID: **1**
- Default baud rate: **9600 8N1**

## Installation

```bash
pip3 install pymodbus --break-system-packages
# or in a venv:
python3 -m venv venv && source venv/bin/activate && pip install pymodbus
```

## Configuration

Edit `config.json` before running:

| Section | Key | Description |
|---------|-----|-------------|
| `serial` | `port` | Serial device (e.g. `/dev/ttyACM0`, `/dev/ttyUSB0`, `COM3`) |
| `serial` | `baudrate` | Baud rate (default 9600) |
| `modbus` | `slave_id` | Modbus slave address (default 1) |
| `test` | `temperature_range_c` | List of target temperatures to test |
| `test` | `dwell_time_seconds` | How long to hold at each temperature after stabilization |
| `test` | `stabilization_tolerance_c` | ± tolerance for "stable" |
| `test` | `stabilization_min_time_seconds` | Minimum time within tolerance before dwell starts |
| `test` | `max_stabilization_wait_seconds` | Timeout for reaching a temperature |
| `test` | `safety_high_limit_c` | Abort if any target exceeds this |
| `test` | `safety_low_limit_c` | Abort if any target goes below this |

## Usage

### List available serial ports
```bash
python3 test_runner.py --list-ports
```

### Run with defaults (config.json)
```bash
python3 test_runner.py
```

### Override serial port
```bash
python3 test_runner.py --port /dev/ttyUSB0
```

### Override temperature list
```bash
python3 test_runner.py --temps "-10,0,25,50,80"
```

### Custom config file
```bash
python3 test_runner.py -c my_custom_config.json
```

## How It Works

1. **Connect** to the EZ-Zone PM controller via Modbus RTU
2. **Enable auto mode** (register 2780 = 1)
3. For **each target temperature** in sequence:
   - Write setpoint to register 2160
   - **Poll** process value (register 360) at regular intervals
   - Wait until temperature is **within tolerance for the minimum stable time**
   - **Dwell** for the configured dwell time, continuing to log data
   - Check alarm status (register 3001) throughout
4. Optionally **cooldown** to a safe temperature
5. Write a **summary report**

## Output

All output goes to the `logs/` directory (configurable):

| File | Description |
|------|-------------|
| `data_YYYYMMDD_HHMMSS.csv` | Timestamped CSV with all data points |
| `test_YYYYMMDD_HHMMSS.log` | Full debug log |
| `test_report.txt` | Summary pass/fail report |

### CSV Columns

```
timestamp_iso, elapsed_s, step, target_c, pv_c, setpoint_c,
mode, heat_pct, cool_pct, alarm, stable, note
```

## EZ-Zone PM Register Map (Reference)

| Register | Type | Size | Description |
|----------|------|------|-------------|
| 360 | R | 32-bit float | Analog Input 1 — Process Value (chamber temp) |
| 370 | R | 32-bit float | Analog Input 2 |
| 400 | R | 32-bit float | Loop 1 Heat Output (%) |
| 402 | R | 32-bit float | Loop 1 Cool Output (%) |
| 2160 | R/W | 32-bit float | Loop 1 Set Point 1 |
| 2162 | R/W | 32-bit float | Loop 1 Set Point 2 |
| 2780 | R/W | 16-bit uint | Control Mode (0=off, 1=auto, 2=manual) |
| 2781 | R | 32-bit float | Set Point Closed (active setpoint) |
| 3001 | R | 16-bit uint | Alarm Status (bitmask) |

## Safety Notes

- The script validates all targets against `safety_high_limit_c` and
  `safety_low_limit_c` before starting
- If stabilization times out at any step, that step is marked failed
  but the script continues to the next temperature
- Press `Ctrl+C` to abort the test gracefully at any time
- The optional cooldown phase returns the chamber to a safe ambient
  temperature after the test completes
