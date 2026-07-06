#!/usr/bin/env python3
"""
TestEquity TEC1 — Web Dashboard

Flask + SocketIO dashboard for the TEC1 thermoelectric chamber.
Reads PV (register 360) and writes setpoint (register 2160) via Modbus RTU.

IMPORTANT: POWER and TEMP switches must be physically ON.
RS-232 only sets the target temperature and reads back values.
"""

import argparse
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit

from ezzone import EZZoneClient

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = "testequity-tec1"
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

client: Optional[EZZoneClient] = None
config: dict = {}

temp_history: deque = deque(maxlen=300)

poller_running = False
poller_thread: Optional[threading.Thread] = None

# Active test state
test_running = False
test_thread: Optional[threading.Thread] = None
test_results: list = []
test_current_step: str = "idle"

logger = logging.getLogger("dashboard")


# ---------------------------------------------------------------------------
# Background Poller
# ---------------------------------------------------------------------------

def poller_loop():
    global poller_running, client, temp_history
    interval = config.get("web", {}).get("poll_interval_seconds", 2.0)

    while poller_running:
        if client and client.is_connected:
            try:
                pv = client.get_process_value()
                sp = client.get_setpoint()

                now = datetime.now(timezone.utc).isoformat()

                status = {
                    "timestamp": now,
                    "pv_c": pv,
                    "setpoint_c": sp,
                    "connected": True,
                    "test_running": test_running,
                    "test_step": test_current_step,
                }

                if pv is not None:
                    temp_history.append({"t": now, "pv": pv, "sp": sp})

                socketio.emit("status", status)
            except Exception as e:
                logger.error("Poller error: %s", e)
                socketio.emit("status", {"connected": False, "error": str(e)})
        else:
            socketio.emit("status", {"connected": False, "test_running": test_running})

        time.sleep(interval)


def start_poller():
    global poller_running, poller_thread
    if poller_running:
        return
    poller_running = True
    poller_thread = threading.Thread(target=poller_loop, daemon=True)
    poller_thread.start()
    logger.info("Poller started.")


def stop_poller():
    global poller_running
    poller_running = False


# ---------------------------------------------------------------------------
# Background Test Runner
# ---------------------------------------------------------------------------

def background_test_runner():
    global test_running, test_current_step, test_results, client, config

    test_cfg = config["test"]
    temps = test_cfg["temperature_range_c"]
    tolerance = test_cfg["stabilization_tolerance_c"]
    min_stable = test_cfg["stabilization_min_time_seconds"]
    max_wait = test_cfg["max_stabilization_wait_seconds"]
    poll_interval = test_cfg["poll_interval_seconds"]
    dwell_time = test_cfg["dwell_time_seconds"]

    test_results = []
    overall_success = True

    try:
        for idx, target in enumerate(temps, start=1):
            if not test_running:
                socketio.emit("test_event", {"type": "aborted", "message": "Stopped."})
                return

            test_current_step = f"Step {idx}/{len(temps)}: {target}°C"
            socketio.emit("test_event", {
                "type": "step_start", "step": idx, "total": len(temps),
                "target": target, "message": f"Setting {target}°C",
            })

            client.set_setpoint(target)

            # Wait for stabilization
            stable_start = None
            stable = False
            step_start = time.monotonic()

            while test_running:
                elapsed = time.monotonic() - step_start
                pv = client.get_process_value()
                if elapsed >= max_wait:
                    break
                if pv is not None and abs(pv - target) <= tolerance:
                    if stable_start is None:
                        stable_start = time.monotonic()
                    if (time.monotonic() - stable_start) >= min_stable:
                        stable = True
                        break
                else:
                    stable_start = None
                time.sleep(poll_interval)

            if not test_running:
                return

            step_result = {"step": idx, "target": target, "stable": stable,
                           "pv": client.get_process_value()}
            test_results.append(step_result)

            if stable:
                socketio.emit("test_event", {
                    "type": "step_stable", "step": idx,
                    "message": f"Stable at {target}°C — dwelling {dwell_time}s",
                })
                dwell_start = time.monotonic()
                while (time.monotonic() - dwell_start) < dwell_time and test_running:
                    time.sleep(poll_interval)

                socketio.emit("test_event", {
                    "type": "step_complete", "step": idx, "target": target,
                    "message": f"✓ Step {idx} done",
                })
            else:
                overall_success = False
                socketio.emit("test_event", {
                    "type": "step_failed", "step": idx, "target": target,
                    "message": f"✗ Step {idx} FAILED",
                })

        # Cooldown
        if test_running and test_cfg.get("cooldown_enabled", False):
            cd_target = test_cfg.get("cooldown_target_c", 25.0)
            test_current_step = f"Cooldown to {cd_target}°C"
            socketio.emit("test_event", {"type": "cooldown", "target": cd_target})
            client.set_setpoint(cd_target)
            cd_start = time.monotonic()
            cd_timeout = test_cfg.get("cooldown_timeout_seconds", 600)
            while (time.monotonic() - cd_start) < cd_timeout and test_running:
                time.sleep(poll_interval)

        test_current_step = f"{'PASS' if overall_success else 'FAIL'}"
        socketio.emit("test_event", {
            "type": "complete", "success": overall_success,
            "results": test_results,
            "message": f"{'✓ PASS' if overall_success else '✗ FAIL'}",
        })

    except Exception as e:
        logger.exception("Test error")
        socketio.emit("test_event", {"type": "error", "message": str(e)})
    finally:
        test_running = False
        test_current_step = "idle"


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    global client
    if not client or not client.is_connected:
        return jsonify({"connected": False})
    try:
        return jsonify({
            "connected": True,
            "pv_c": client.get_process_value(),
            "setpoint_c": client.get_setpoint(),
            "test_running": test_running,
            "test_step": test_current_step,
        })
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)})


@app.route("/api/setpoint", methods=["POST"])
def api_set_setpoint():
    global client, config
    if not client or not client.is_connected:
        return jsonify({"ok": False, "error": "Not connected"}), 503

    data = request.get_json()
    value = float(data.get("value", 25.0))

    lo = config["test"]["safety_low_limit_c"]
    hi = config["test"]["safety_high_limit_c"]
    if value < lo or value > hi:
        return jsonify({"ok": False, "error": f"Outside safety [{lo}, {hi}]"}), 400

    ok = client.set_setpoint(value)
    return jsonify({"ok": ok, "setpoint": value})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    global client
    data = request.get_json() or {}
    port = data.get("port", config["serial"]["port"])

    if client:
        try: client.disconnect()
        except: pass

    sc = config["serial"]; mc = config["modbus"]
    client = EZZoneClient(port=port, slave_id=mc["slave_id"],
                          baudrate=sc["baudrate"], timeout=sc["timeout"],
                          retries=mc["retries"], retry_delay=mc["retry_delay_seconds"])
    ok = client.connect()
    if ok:
        start_poller()
    return jsonify({"ok": ok, "port": port})


@app.route("/api/test/start", methods=["POST"])
def api_test_start():
    global test_running, test_thread, test_results, client
    if not client or not client.is_connected:
        return jsonify({"ok": False, "error": "Not connected"}), 503
    if test_running:
        return jsonify({"ok": False, "error": "Already running"}), 409
    test_running = True
    test_results = []
    test_thread = threading.Thread(target=background_test_runner, daemon=True)
    test_thread.start()
    return jsonify({"ok": True})


@app.route("/api/test/stop", methods=["POST"])
def api_test_stop():
    global test_running
    test_running = False
    return jsonify({"ok": True})


@app.route("/api/test/results")
def api_test_results():
    return jsonify({"running": test_running, "current_step": test_current_step,
                    "results": test_results})


@app.route("/api/history")
def api_history():
    return jsonify(list(temp_history))


@app.route("/api/logs")
def api_logs():
    log_dir = config["output"]["log_dir"]
    try:
        files = []
        for f in sorted(os.listdir(log_dir), reverse=True):
            fp = os.path.join(log_dir, f)
            if os.path.isfile(fp):
                files.append({"name": f, "size": os.path.getsize(fp),
                              "modified": datetime.fromtimestamp(os.path.getmtime(fp)).isoformat()})
        return jsonify(files)
    except FileNotFoundError:
        return jsonify([])


@app.route("/api/logs/<filename>")
def api_log_download(filename):
    path = os.path.join(config["output"]["log_dir"], filename)
    if not os.path.isfile(path):
        return jsonify({"error": "Not found"}), 404
    return send_file(path, as_attachment=True, download_name=filename)


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    global config
    if request.method == "GET":
        return jsonify({
            "serial": {"port": config["serial"]["port"], "baudrate": config["serial"]["baudrate"]},
            "test": config["test"],
            "web": config["web"],
        })
    data = request.get_json()
    for k in ["temperature_range_c", "dwell_time_seconds",
              "stabilization_tolerance_c", "stabilization_min_time_seconds"]:
        if k in data:
            t = type(config["test"][k])
            config["test"][k] = t(data[k])
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# SocketIO
# ---------------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    emit("history", list(temp_history))


@socketio.on("request_status")
def on_request_status():
    if client and client.is_connected:
        try:
            emit("status", {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "pv_c": client.get_process_value(),
                "setpoint_c": client.get_setpoint(),
                "connected": True,
                "test_running": test_running,
                "test_step": test_current_step,
            })
        except Exception as e:
            emit("status", {"connected": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def connect_on_startup():
    global client, config
    try:
        sc = config["serial"]; mc = config["modbus"]
        client = EZZoneClient(port=sc["port"], slave_id=mc["slave_id"],
                              baudrate=sc["baudrate"], timeout=sc["timeout"],
                              retries=mc["retries"], retry_delay=mc["retry_delay_seconds"])
        if client.connect():
            logger.info("Connected to EZ-Zone on %s", sc["port"])
            start_poller()
        else:
            logger.warning("Could not connect — dashboard will start disconnected.")
    except Exception as e:
        logger.warning("Startup connect failed: %s", e)


def main():
    global config
    parser = argparse.ArgumentParser(description="TEC1 Web Dashboard")
    parser.add_argument("-c", "--config", default="config.json")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--pi", action="store_true",
                        help="Raspberry Pi optimized settings (less RAM/CPU)")
    args = parser.parse_args()

    with open(args.config) as f:
        config.update(json.load(f))

    # Raspberry Pi optimizations
    if args.pi:
        config.setdefault("web", {})["history_points"] = 120
        config.setdefault("web", {})["poll_interval_seconds"] = 3.0
        config.setdefault("test", {})["poll_interval_seconds"] = 3.0
        logger.info("Raspberry Pi mode: history=120, poll=3s")

    history_size = config.get("web", {}).get("history_points", 300)
    global temp_history
    temp_history = deque(maxlen=history_size)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s",
                        datefmt="%H:%M:%S")

    host = args.host or config.get("web", {}).get("host", "0.0.0.0")
    port = args.port or config.get("web", {}).get("port", 5000)

    connect_on_startup()

    logger.info("Dashboard starting on http://%s:%d", host, port)
    socketio.run(app, host=host, port=port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
