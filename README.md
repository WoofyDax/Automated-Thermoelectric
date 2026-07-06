# TestEquity TEC1 — Automated Thermoelectric Test Suite

Web dashboard and automated temperature sweep for the **TestEquity TEC1**
thermoelectric chamber with **Watlow EZ-Zone PM** controller over RS-232C
Modbus RTU. Optimized for **Raspberry Pi 3 Model B**.

---

## 🔧 Hardware Setup

```
Raspberry Pi ── USB ── FTDI USB-to-Serial ── RS-232C null-modem ── EZ-Zone PM (DB-9)
                                                         │
                                                    TEC1 Chamber
```

- Both **POWER** and **TEMP** switches must be physically ON
- RS-232 only sets the target temperature and reads values back
- FTDI adapter appears as `/dev/ttyUSB0`

---

## 📥 Download & Install (Raspberry Pi)

### One-command install:

```bash
git clone https://github.com/WoofyDax/Automated-Thermoelectric.git
cd Automated-Thermoelectric
chmod +x setup.sh
./setup.sh
```

That's it. The script installs all dependencies, sets up serial permissions,
and creates a systemd service to auto-start on boot.

### Start the dashboard:

```bash
sudo systemctl start tequity-dashboard
```

### Open in your browser:

```
http://<raspberry-pi-ip>:5000
```

Find your Pi's IP with: `hostname -I`

---

## 🖥️ Manual Start (without systemd)

```bash
cd ~/Automated-Thermoelectric
python3 dashboard.py --pi
```

The `--pi` flag enables Raspberry Pi optimizations (lower memory, longer poll interval).

---

## ⚙️ Configuration

Edit `config.json` to adjust:

| Setting | Default | Description |
|---------|---------|-------------|
| `serial.port` | `/dev/ttyUSB0` | FTDI serial port |
| `serial.baudrate` | `9600` | Modbus baud rate |
| `test.temperature_range_c` | `[10,15,20,25,30,35,40,45,50]` | Test sequence |
| `test.dwell_time_seconds` | `30` | Hold time per step |
| `test.safety_high_limit_c` | `50` | Maximum allowed setpoint |
| `test.safety_low_limit_c` | `10` | Minimum allowed setpoint |
| `web.port` | `5000` | Dashboard port |

---

## 📡 Modbus Register Map

| Register | Type | Format | Description |
|----------|------|--------|-------------|
| 360–361 | Read | float LE | Analog Input 1 — Chamber temperature (°C) |
| 370–371 | Read | float LE | Analog Input 2 |
| 2160–2161 | Write | float LE (FC 0x10) | Closed Loop Set Point (°C) |

- **Word order**: Low register = LSW, High register = MSW
- **Float**: IEEE 754 little-endian
- **Writes**: Must use Function Code 0x10 (Write Multiple Registers)

---

## 🔧 Commands

| Task | Command |
|------|---------|
| Start dashboard | `sudo systemctl start tequity-dashboard` |
| Stop dashboard | `sudo systemctl stop tequity-dashboard` |
| View logs | `journalctl -u tequity-dashboard -f` |
| List serial ports | `python3 test_runner.py --list-ports` |
| Run CLI test | `python3 test_runner.py` |
| Set temp via API | `curl -X POST localhost:5000/api/setpoint -H 'Content-Type: application/json' -d '{"value":30}'` |
| Get status | `curl localhost:5000/api/status` |

---

## 📁 Files

| File | Purpose |
|------|---------|
| `dashboard.py` | Flask + SocketIO web server |
| `ezzone.py` | Modbus RTU client library |
| `test_runner.py` | CLI automated temperature sweep |
| `templates/index.html` | Web dashboard UI (Chart.js) |
| `config.json` | All settings |
| `setup.sh` | One-command Pi installer |
| `docs/` | TEC1 manual, comms PDF, Modbus packet samples |
