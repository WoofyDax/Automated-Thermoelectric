#!/usr/bin/env python3
"""
TestEquity TEC1 Thermoelectric Chamber — Automated Test Runner

Cycles the chamber through a configurable list of target temperatures.
At each step it:
  1. Sets the setpoint
  2. Waits for the temperature to stabilize within tolerance
  3. Records data and checks alarm status
  4. Logs everything to CSV and console

Usage:
    python3 test_runner.py              # use config.json defaults
    python3 test_runner.py --help       # see all options
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from ezzone import EZZoneClient, EZZoneError

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_dir: str, console: bool = True) -> logging.Logger:
    """Configure logging to both console and a timestamped log file."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"test_{timestamp}.log")

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    logger = logging.getLogger("testequity")
    logger.setLevel(logging.DEBUG)

    # File handler — full debug
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    logger.info("Log file: %s", log_file)
    return logger


# ---------------------------------------------------------------------------
# CSV logging
# ---------------------------------------------------------------------------

class CSVLogger:
    """Appends rows to a timestamped CSV file."""

    FIELDS = [
        "timestamp_iso", "elapsed_s", "step", "target_c",
        "pv_c", "setpoint_c", "mode", "heat_pct", "cool_pct",
        "alarm", "stable", "note"
    ]

    def __init__(self, log_dir: str):
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(log_dir, f"data_{timestamp}.csv")
        self.file = open(self.path, "w", newline="")
        self.writer = csv.DictWriter(self.file, fieldnames=self.FIELDS)
        self.writer.writeheader()
        self.file.flush()

    def log(self, **kwargs):
        row = {f: kwargs.get(f, "") for f in self.FIELDS}
        self.writer.writerow(row)
        self.file.flush()

    def close(self):
        self.file.close()


# ---------------------------------------------------------------------------
# Stabilisation monitor
# ---------------------------------------------------------------------------

def wait_for_stabilization(
    client: EZZoneClient,
    target: float,
    tolerance: float,
    min_stable_time: float,
    max_wait: float,
    poll_interval: float,
    csv_log: CSVLogger,
    step_label: str,
    log: logging.Logger,
    start_time: float,
) -> Tuple[bool, float]:
    """
    Poll the chamber until temperature is within tolerance for min_stable_time
    consecutive seconds. Returns (stabilized, elapsed_seconds).
    """
    stable_start: Optional[float] = None

    while True:
        elapsed = time.monotonic() - start_time
        pv = client.get_process_value()
        alarm = client.get_alarm_status()
        mode = client.get_control_mode()
        heat = client.get_heat_output()
        cool = client.get_cool_output()
        sp = client.get_active_setpoint()

        # Determine stability
        delta = abs(pv - target) if pv is not None else float("inf")
        is_stable_now = (pv is not None and delta <= tolerance)

        note = ""
        if pv is None:
            note = "READ_ERROR"
        elif is_stable_now:
            note = "stable"
        else:
            note = "soaking"

        csv_log.log(
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            elapsed_s=round(elapsed, 1),
            step=step_label,
            target_c=target,
            pv_c=pv,
            setpoint_c=sp,
            mode=mode,
            heat_pct=heat,
            cool_pct=cool,
            alarm=alarm,
            stable=str(is_stable_now).lower(),
            note=note,
        )

        if pv is not None:
            log.info(
                "%s  |  PV: %.2f °C  |  target: %.2f °C  |  Δ: %.2f °C  |  heat: %s%%  cool: %s%%  |  %s",
                step_label, pv, target, delta, heat, cool, note.upper()
            )

        # Stabilisation state machine
        if is_stable_now:
            if stable_start is None:
                stable_start = time.monotonic()
            stable_duration = time.monotonic() - stable_start
            if stable_duration >= min_stable_time:
                log.info("✓ Stabilized at %.2f °C (held for %.1f s)", pv, stable_duration)
                return True, elapsed
        else:
            stable_start = None

        # Timeout check
        if elapsed >= max_wait:
            log.error("✗ Stabilization timeout after %.0f s. Last PV: %s °C", elapsed, pv)
            return False, elapsed

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Main test sequence
# ---------------------------------------------------------------------------

def run_tests(config_path: str) -> bool:
    """Run the full temperature test sequence. Returns True if all passed."""
    # Load config
    with open(config_path) as f:
        cfg = json.load(f)

    serial_cfg   = cfg["serial"]
    modbus_cfg   = cfg["modbus"]
    test_cfg     = cfg["test"]
    output_cfg   = cfg["output"]

    log = setup_logging(output_cfg["log_dir"], console=output_cfg["console_log"])

    log.info("=" * 60)
    log.info("TestEquity TEC1 Automated Temperature Test")
    log.info("=" * 60)

    # Validate temperatures against safety limits
    temps = test_cfg["temperature_range_c"]
    hi_lim = test_cfg["safety_high_limit_c"]
    lo_lim = test_cfg["safety_low_limit_c"]
    for t in temps:
        if t > hi_lim or t < lo_lim:
            log.error("Temperature %.1f °C exceeds safety limits [%.1f, %.1f]. Aborting.", t, lo_lim, hi_lim)
            return False

    log.info("Test sequence: %s °C", temps)
    log.info("Dwell time: %d s  |  Tolerance: ±%.2f °C  |  Min stable: %d s  |  Max wait: %d s",
             test_cfg["dwell_time_seconds"],
             test_cfg["stabilization_tolerance_c"],
             test_cfg["stabilization_min_time_seconds"],
             test_cfg["max_stabilization_wait_seconds"])

    # --- Connect ---
    client = EZZoneClient(
        port=serial_cfg["port"],
        slave_id=modbus_cfg["slave_id"],
        baudrate=serial_cfg["baudrate"],
        timeout=serial_cfg["timeout"],
        retries=modbus_cfg["retries"],
        retry_delay=modbus_cfg["retry_delay_seconds"],
    )

    if not client.connect():
        log.error("Cannot connect to EZ-Zone controller. Aborting.")
        return False

    csv_log = CSVLogger(output_cfg["log_dir"])

    overall_success = True

    try:
        # Initial status
        status = client.get_status()
        log.info("Initial status: PV=%.2f °C  SP=%.2f °C  Mode=%s  Heat=%.1f%%  Cool=%.1f%%",
                 status["process_value_c"],
                 status["active_setpoint_c"],
                 client.control_mode_name(status["control_mode"]),
                 status["heat_output_pct"],
                 status["cool_output_pct"])

        # Enable control
        if not client.enable_control():
            log.error("Failed to set controller to auto mode.")
            overall_success = False
            return False
        log.info("Controller set to AUTO mode.")

        test_start = time.monotonic()

        # ------------------------------------------------------------------
        # Temperature sweep
        # ------------------------------------------------------------------
        for idx, target in enumerate(temps, start=1):
            step_label = f"[{idx}/{len(temps)}]"
            log.info("-" * 50)
            log.info("%s  Target: %.2f °C", step_label, target)

            # Set setpoint
            if not client.set_setpoint(target):
                log.error("%s  Failed to write setpoint. Skipping.", step_label)
                overall_success = False
                continue

            log.info("%s  Setpoint written. Waiting for stabilization …", step_label)
            step_start = time.monotonic()

            stable, _ = wait_for_stabilization(
                client=client,
                target=target,
                tolerance=test_cfg["stabilization_tolerance_c"],
                min_stable_time=test_cfg["stabilization_min_time_seconds"],
                max_wait=test_cfg["max_stabilization_wait_seconds"],
                poll_interval=test_cfg["poll_interval_seconds"],
                csv_log=csv_log,
                step_label=step_label,
                log=log,
                start_time=step_start,
            )

            if not stable:
                log.error("%s  FAILED to stabilize at %.2f °C", step_label, target)
                overall_success = False
            else:
                # Dwell at temperature
                dwell = test_cfg["dwell_time_seconds"]
                log.info("%s  DWELLING for %d s at %.2f °C …", step_label, dwell, target)
                dwell_start = time.monotonic()
                while (time.monotonic() - dwell_start) < dwell:
                    pv = client.get_process_value()
                    alarm = client.get_alarm_status()
                    csv_log.log(
                        timestamp_iso=datetime.now(timezone.utc).isoformat(),
                        elapsed_s=round(time.monotonic() - test_start, 1),
                        step=f"{step_label}-dwell",
                        target_c=target,
                        pv_c=pv,
                        setpoint_c=client.get_active_setpoint(),
                        mode=client.get_control_mode(),
                        heat_pct=client.get_heat_output(),
                        cool_pct=client.get_cool_output(),
                        alarm=alarm,
                        stable="true",
                        note="dwell",
                    )
                    if alarm and alarm != 0:
                        log.warning("%s  ALARM active during dwell: %d", step_label, alarm)
                    time.sleep(test_cfg["poll_interval_seconds"])

                log.info("%s  ✓ Dwell complete at %.2f °C", step_label, target)

        # ------------------------------------------------------------------
        # Cool-down (if enabled)
        # ------------------------------------------------------------------
        if test_cfg.get("cooldown_enabled", False):
            cooldown_target = test_cfg.get("cooldown_target_c", 25.0)
            cooldown_timeout = test_cfg.get("cooldown_timeout_seconds", 600)
            log.info("-" * 50)
            log.info("COOLDOWN: returning to %.2f °C", cooldown_target)

            if not client.set_setpoint(cooldown_target):
                log.warning("Failed to write cooldown setpoint.")
            else:
                cd_start = time.monotonic()
                stable, _ = wait_for_stabilization(
                    client=client,
                    target=cooldown_target,
                    tolerance=test_cfg["stabilization_tolerance_c"] * 2,  # looser for cooldown
                    min_stable_time=5,
                    max_wait=cooldown_timeout,
                    poll_interval=test_cfg["poll_interval_seconds"],
                    csv_log=csv_log,
                    step_label="cooldown",
                    log=log,
                    start_time=cd_start,
                )
                if stable:
                    log.info("✓ Cooldown complete at %.2f °C", cooldown_target)
                else:
                    log.warning("Cooldown did not reach %.2f °C within timeout.", cooldown_target)

        # ------------------------------------------------------------------
        # Final status
        # ------------------------------------------------------------------
        status = client.get_status()
        log.info("=" * 60)
        log.info("Final status: PV=%.2f °C  SP=%.2f °C  Mode=%s  Heat=%.1f%%  Cool=%.1f%%",
                 status["process_value_c"],
                 status["active_setpoint_c"],
                 client.control_mode_name(status["control_mode"]),
                 status["heat_output_pct"],
                 status["cool_output_pct"])

        total_elapsed = time.monotonic() - test_start
        log.info("Total test duration: %.0f s (%.1f min)", total_elapsed, total_elapsed / 60)

        if overall_success:
            log.info("RESULT: ALL TEMPERATURE STEPS PASSED ✓")
        else:
            log.warning("RESULT: ONE OR MORE STEPS FAILED ✗")

        # Write summary report
        report_path = os.path.join(output_cfg["log_dir"], output_cfg["report_file"])
        with open(report_path, "w") as rf:
            rf.write(f"TestEquity TEC1 Test Report\n")
            rf.write(f"Date: {datetime.now().isoformat()}\n")
            rf.write(f"Result: {'PASS' if overall_success else 'FAIL'}\n")
            rf.write(f"Duration: {total_elapsed:.0f} s\n")
            rf.write(f"Temperatures tested: {temps}\n")
            rf.write(f"Data log: {csv_log.path}\n")
        log.info("Report written to %s", report_path)

    except KeyboardInterrupt:
        log.warning("Test interrupted by user.")
        overall_success = False
    except Exception as e:
        log.exception("Unexpected error: %s", e)
        overall_success = False
    finally:
        csv_log.close()
        client.disconnect()

    return overall_success


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="TestEquity TEC1 — Automated Temperature Test Runner"
    )
    parser.add_argument(
        "-c", "--config", default="config.json",
        help="Path to JSON config file (default: config.json)"
    )
    parser.add_argument(
        "--list-ports", action="store_true",
        help="List available serial ports and exit (no Modbus traffic)."
    )
    parser.add_argument(
        "--port", default=None,
        help="Override serial port (e.g. /dev/ttyUSB0)"
    )
    parser.add_argument(
        "--temps", default=None,
        help="Override temperature list as comma-separated values (e.g. -20,0,25,80)"
    )
    args = parser.parse_args()

    if args.list_ports:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        if not ports:
            print("No serial ports found.")
            sys.exit(2)
        for p in ports:
            print(f"  {p.device}  –  {p.description}")
        sys.exit(0)

    # Load base config, apply overrides, write temp config for run_tests
    with open(args.config) as f:
        cfg = json.load(f)

    if args.port:
        cfg["serial"]["port"] = args.port
    if args.temps:
        cfg["test"]["temperature_range_c"] = [float(t.strip()) for t in args.temps.split(",")]

    # Write a merged config so run_tests sees the overrides
    merged_path = "/tmp/testequity_merged_config.json"
    with open(merged_path, "w") as f:
        json.dump(cfg, f, indent=2)

    success = run_tests(merged_path)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
