#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flasher_web_server.py - High-Performance Web UI Server for Parallel ESP32 Flashing
with Interactive Serial Terminal and Persistent Local Flashing Database.

Pure Python standard library implementation: Zero extra pip dependencies required!
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    print("WARNING: pyserial not installed. Serial communication will be limited.")
    serial = None
    list_ports = None

# Base directories
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
FIRMWARE_DIR = PROJECT_ROOT
UI_DIR = SCRIPT_DIR / "flasher-ui"
HISTORY_FILE = SCRIPT_DIR / "flasher_history.json"

# Import dedicated Printer & Provisioning Services
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from printer_service import (
    list_printers,
    get_default_printer,
    get_printer_status,
    print_twin_label,
    print_single_label,
    print_calibration_test,
    calibrate_gap,
    feed_label
)
from provisioning_service import (
    send_set_id,
    send_get_id,
    generate_node_uuid_hex,
    format_as_uuid,
    insert_node_to_supabase,
    fetch_levels_from_supabase,
    create_level_in_supabase,
    fetch_nodes_from_supabase,
    fetch_edges_from_supabase,
    insert_edges_to_supabase,
    link_destinations_to_supabase,
    history_store as prov_history_store
)


def load_supabase_env():
    """Reads default Supabase credentials from .env in workspace root."""
    env_file = PROJECT_ROOT.parent / ".env"
    cfg = {"supabase_url": "", "supabase_anon_key": ""}
    if env_file.exists():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SUPABASE_URL="):
                        cfg["supabase_url"] = line.split("=", 1)[1].strip()
                    elif line.startswith("SUPABASE_ANON_KEY="):
                        cfg["supabase_anon_key"] = line.split("=", 1)[1].strip()
        except Exception:
            pass
    return cfg

# Find ESP-IDF Python and esptool
def find_esp_tools():
    direct_candidates = [
        "C:/Espressif/tools/python/v5.5.5/venv/Scripts/python.exe",
        "C:/Espressif/tools/python/v5.5.5/venv/Scripts/esptool.exe",
    ]
    python_exe = sys.executable
    esptool_exe = None

    if Path(direct_candidates[0]).exists():
        python_exe = direct_candidates[0]

    if Path(direct_candidates[1]).exists():
        esptool_exe = direct_candidates[1]

    idf_env = os.environ.get("IDF_PYTHON_ENV_PATH")
    if idf_env:
        p = Path(idf_env) / "Scripts" / "python.exe"
        e = Path(idf_env) / "Scripts" / "esptool.exe"
        if p.exists():
            python_exe = str(p)
        if e.exists():
            esptool_exe = str(e)

    return python_exe, esptool_exe

PYTHON_EXE, ESPTOOL_EXE = find_esp_tools()

ESP_PORT_HINTS = (
    "cp210", "ch340", "ch9102", "ftdi", "usb-serial", "usb serial",
    "silicon labs", "usb2.0-serial", "uart", "espressif", "jtag"
)

# Persistent Flashing Database Manager
class HistoryStore:
    def __init__(self, filepath):
        self.filepath = filepath
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "stats": {
                "total_flashed_success": 7,
                "total_flashed_failed": 0,
                "total_attempts": 7,
                "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
            },
            "history": [
                {
                    "id": 1,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "project": "IPS_Mesh_Node",
                    "port": "Initial Batch (7 Boards)",
                    "status": "success",
                    "elapsed": 15.0,
                    "details": "تم حرق الدفعة الأولى (7 بوردات) بنجاح"
                }
            ]
        }

    def _save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving history file: {e}")

    def record_flash(self, port, project, success, elapsed, details=""):
        with self.lock:
            stats = self.data.setdefault("stats", {})
            history = self.data.setdefault("history", [])

            if success:
                stats["total_flashed_success"] = stats.get("total_flashed_success", 0) + 1
            else:
                stats["total_flashed_failed"] = stats.get("total_flashed_failed", 0) + 1

            stats["total_attempts"] = stats.get("total_attempts", 0) + 1
            stats["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")

            item = {
                "id": len(history) + 1,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "project": project,
                "port": port,
                "status": "success" if success else "failed",
                "elapsed": elapsed,
                "details": details
            }
            history.append(item)
            if len(history) > 500:
                self.data["history"] = history[-500:]

            self._save()

    def get_data(self):
        with self.lock:
            return {
                "stats": self.data.get("stats", {}),
                "history": list(reversed(self.data.get("history", [])))[:100]
            }

    def get_stats(self):
        with self.lock:
            return dict(self.data.get("stats", {}))

    def set_success_count(self, count):
        with self.lock:
            stats = self.data.setdefault("stats", {})
            stats["total_flashed_success"] = int(count)
            stats["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save()
            return stats

history_store = HistoryStore(HISTORY_FILE)

# Serial Terminal Manager for Interactive Per-Port Consoles
class SerialTerminalManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}  # port -> {ser, baud, running, buffer, next_id}

    def open_port(self, port, baud=115200):
        if not serial:
            return False, "pyserial library is not installed"
        self.close_port(port)
        with self.lock:
            try:
                ser = serial.Serial()
                ser.port = port
                ser.baudrate = baud
                ser.dtr = False
                ser.rts = False
                ser.timeout = 0.1
                ser.open()
            except Exception as e:
                return False, str(e)

            session = {
                "serial": ser,
                "baud": baud,
                "running": True,
                "buffer": [],
                "next_id": 1,
            }
            self.sessions[port] = session

            t = threading.Thread(
                target=self._reader_thread,
                args=(port, session),
                daemon=True
            )
            session["thread"] = t
            t.start()
            return True, "Connected"

    def _reader_thread(self, port, session):
        ser = session["serial"]
        line_buf = ""
        while session["running"]:
            try:
                data = ser.read(ser.in_waiting or 1)
                if data:
                    text = data.decode("utf-8", errors="replace")
                    with self.lock:
                        for ch in text:
                            if ch == "\n":
                                session["buffer"].append((session["next_id"], line_buf.strip("\r")))
                                session["next_id"] += 1
                                if len(session["buffer"]) > 500:
                                    session["buffer"].pop(0)
                                line_buf = ""
                            else:
                                line_buf += ch
                else:
                    time.sleep(0.015)
            except Exception:
                break
        session["running"] = False

    def poll_lines(self, port, since_id=0):
        with self.lock:
            session = self.sessions.get(port)
            if not session:
                return {"connected": False, "lines": [], "last_id": since_id}
            
            new_lines = [
                {"id": item[0], "text": item[1]}
                for item in session["buffer"]
                if item[0] > since_id
            ]
            last_id = session["buffer"][-1][0] if session["buffer"] else since_id
            return {
                "connected": session["running"],
                "lines": new_lines,
                "last_id": last_id,
                "baud": session["baud"]
            }

    def write_line(self, port, text):
        with self.lock:
            session = self.sessions.get(port)
            if not session or not session["running"]:
                return False, "Port is not open"
            try:
                if not text.endswith("\n"):
                    text += "\n"
                session["serial"].write(text.encode("utf-8"))
                return True, "Sent"
            except Exception as e:
                return False, str(e)

    def reset_esp(self, port):
        with self.lock:
            session = self.sessions.get(port)
            if not session or not session["running"]:
                return False, "Port is not open"
            try:
                ser = session["serial"]
                ser.dtr = False
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                return True, "Reset pulse sent"
            except Exception as e:
                return False, str(e)

    def close_port(self, port):
        with self.lock:
            session = self.sessions.pop(port, None)
            if session:
                session["running"] = False
                try:
                    session["serial"].close()
                except Exception:
                    pass
                return True
            return False

terminal_manager = SerialTerminalManager()

# Global Flash Job State Manager
class FlashJobManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"  # idle, running, finished, aborted
        self.active_processes = {}  # port -> Popen
        self.start_time = 0
        self.end_time = 0
        self.project_name = ""
        self.ports_data = {}  # port -> {status, progress, stage, log, elapsed, error}

    def reset(self, project_name, ports):
        with self.lock:
            self.status = "running"
            self.project_name = project_name
            self.start_time = time.time()
            self.end_time = 0
            self.active_processes = {}
            self.ports_data = {}
            for p in ports:
                self.ports_data[p] = {
                    "status": "pending",
                    "progress": 0,
                    "stage": "Waiting to start...",
                    "log": [],
                    "elapsed": 0.0,
                    "error": None
                }

    def update_port(self, port, **kwargs):
        with self.lock:
            if port in self.ports_data:
                for k, v in kwargs.items():
                    if k == "log_append":
                        self.ports_data[port]["log"].append(v)
                        if len(self.ports_data[port]["log"]) > 150:
                            self.ports_data[port]["log"].pop(0)
                    else:
                        self.ports_data[port][k] = v

    def stop_all(self):
        with self.lock:
            self.status = "aborted"
            for p, proc in list(self.active_processes.items()):
                try:
                    proc.terminate()
                except Exception:
                    pass
            for p in self.ports_data:
                if self.ports_data[p]["status"] in ("pending", "connecting", "erasing", "writing"):
                    self.ports_data[p]["status"] = "failed"
                    self.ports_data[p]["stage"] = "Aborted by user"
                    self.ports_data[p]["error"] = "Operation cancelled"

    def get_snapshot(self):
        with self.lock:
            now = time.time()
            elapsed_total = (self.end_time - self.start_time) if self.end_time else (now - self.start_time if self.start_time else 0.0)
            
            succeeded = sum(1 for p, d in self.ports_data.items() if d["status"] == "success")
            failed = sum(1 for p, d in self.ports_data.items() if d["status"] == "failed")
            in_progress = sum(1 for p, d in self.ports_data.items() if d["status"] in ("pending", "connecting", "erasing", "writing"))

            if self.status == "running" and in_progress == 0 and len(self.ports_data) > 0:
                self.status = "finished"
                self.end_time = now

            return {
                "status": self.status,
                "project": self.project_name,
                "batch_total": len(self.ports_data),
                "batch_succeeded": succeeded,
                "batch_failed": failed,
                "batch_in_progress": in_progress,
                "batch_elapsed_time": round(elapsed_total, 1),
                "lifetime_stats": history_store.get_stats(),
                "ports": self.ports_data
            }

job_manager = FlashJobManager()

def get_connected_ports():
    if not list_ports:
        return []
    ports = []
    for p in list_ports.comports():
        haystack = f"{p.description or ''} {p.manufacturer or ''} {p.hwid or ''}".lower()
        is_esp = any(hint in haystack for hint in ESP_PORT_HINTS)
        is_open_in_term = p.device in terminal_manager.sessions
        ports.append({
            "port": p.device,
            "description": p.description or "Serial Port",
            "desc": p.description or "Serial Port",
            "manufacturer": p.manufacturer or "",
            "hwid": p.hwid or "",
            "is_esp": is_esp,
            "terminal_open": is_open_in_term
        })
    return sorted(ports, key=lambda x: (not x["is_esp"], x["port"]))

def get_available_targets():
    targets = []
    for proj_dir in [FIRMWARE_DIR / "IPS_Mesh_Node", FIRMWARE_DIR / "IPS_Mesh_Root"]:
        if not proj_dir.is_dir():
            continue
        flasher_args_path = proj_dir / "build" / "flasher_args.json"
        has_build = flasher_args_path.exists()
        info = {
            "name": proj_dir.name,
            "path": str(proj_dir),
            "has_build": has_build,
            "chip": "esp32",
            "files": [],
            "total_size": 0,
            "build_time": ""
        }
        if has_build:
            try:
                with open(flasher_args_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    info["chip"] = data.get("extra_esptool_args", {}).get("chip", "esp32")
                    files = data.get("flash_files", {})
                    total_size = 0
                    for offset, rel in files.items():
                        full_p = proj_dir / "build" / rel
                        sz = full_p.stat().st_size if full_p.exists() else 0
                        total_size += sz
                        info["files"].append({"offset": offset, "file": rel, "size": sz})
                    info["total_size"] = total_size
                mtime = flasher_args_path.stat().st_mtime
                info["build_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
            except Exception as e:
                info["build_error"] = str(e)
        targets.append(info)
    return targets

def execute_flash_worker(port, project_dir, baud, erase_only):
    start_t = time.time()
    terminal_manager.close_port(port)

    job_manager.update_port(port, status="connecting", stage="Connecting to ESP32...")

    # Load args
    if not erase_only:
        args_file = project_dir / "build" / "flasher_args.json"
        if not args_file.exists():
            job_manager.update_port(port, status="failed", stage="Missing build files", error="flasher_args.json not found")
            history_store.record_flash(port, project_dir.name, False, 0.0, "Missing flasher_args.json")
            return
        with open(args_file, "r", encoding="utf-8") as f:
            flasher_args = json.load(f)

        chip = flasher_args.get("extra_esptool_args", {}).get("chip", "esp32")
        write_flash_args = flasher_args.get("write_flash_args", [])
        flash_files = flasher_args.get("flash_files", {})

        cmd = [
            PYTHON_EXE, "-m", "esptool",
            "--chip", chip,
            "--port", port,
            "--baud", str(baud),
            "--before", "default_reset",
            "--after", "hard_reset",
            "write_flash"
        ] + write_flash_args

        for offset, rel_path in flash_files.items():
            cmd += [offset, str(project_dir / "build" / rel_path)]
    else:
        cmd = [
            PYTHON_EXE, "-m", "esptool",
            "--chip", "esp32",
            "--port", port,
            "--baud", str(baud),
            "erase_flash"
        ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        with job_manager.lock:
            job_manager.active_processes[port] = proc

        pct_regex = re.compile(r"\((\d+)\s*%\)")
        curr_progress = 0
        buf = ""

        while True:
            ch = proc.stdout.read(1)
            if not ch:
                if proc.poll() is not None:
                    break
                time.sleep(0.01)
                continue

            if ch in ("\r", "\n"):
                line = buf.strip()
                buf = ""
                if not line:
                    continue

                job_manager.update_port(port, log_append=line)

                if "Connecting" in line:
                    job_manager.update_port(port, status="connecting", stage="Connecting...")
                elif "Erasing flash" in line:
                    job_manager.update_port(port, status="erasing", stage="Erasing flash chip...", progress=15)
                elif "Writing at" in line:
                    m = pct_regex.search(line)
                    if m:
                        pct = int(m.group(1))
                        curr_progress = pct
                        job_manager.update_port(port, status="writing", stage=f"Writing Flash ({pct}%)", progress=pct)
                    else:
                        job_manager.update_port(port, status="writing", stage="Writing Flash...")
                elif "Hash of data verified" in line:
                    job_manager.update_port(port, stage="Hash verified!", progress=99)
                elif "Hard resetting" in line:
                    job_manager.update_port(port, stage="Resetting board...", progress=100)
            else:
                buf += ch

        ret = proc.wait()
        elapsed = round(time.time() - start_t, 1)

        if ret == 0:
            job_manager.update_port(
                port,
                status="success",
                stage=f"Completed in {elapsed}s",
                progress=100,
                elapsed=elapsed
            )
            # Record in permanent local history
            history_store.record_flash(port, project_dir.name, True, elapsed, f"Completed successfully in {elapsed}s")
        else:
            job_manager.update_port(
                port,
                status="failed",
                stage="Flash failed",
                error=f"esptool exited with code {ret}",
                elapsed=elapsed
            )
            # Record failed in permanent history
            history_store.record_flash(port, project_dir.name, False, elapsed, f"esptool error code {ret}")

    except Exception as e:
        elapsed = round(time.time() - start_t, 1)
        job_manager.update_port(
            port,
            status="failed",
            stage="Execution error",
            error=str(e),
            elapsed=elapsed,
            log_append=f"Exception: {e}"
        )
        history_store.record_flash(port, project_dir.name, False, elapsed, str(e))
    finally:
        with job_manager.lock:
            if port in job_manager.active_processes:
                del job_manager.active_processes[port]


class FlasherHTTPRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, apikey")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/ports":
            ports = get_connected_ports()
            self._send_json({"ports": ports})
        elif path == "/api/targets":
            targets = get_available_targets()
            self._send_json({"targets": targets})
        elif path == "/api/progress":
            snapshot = job_manager.get_snapshot()
            self._send_json(snapshot)
        elif path == "/api/history":
            hist = history_store.get_data()
            self._send_json(hist)
        elif path == "/api/terminal/poll":
            qs = parse_qs(parsed.query)
            port = qs.get("port", [""])[0]
            since = int(qs.get("since", [0])[0])
            res = terminal_manager.poll_lines(port, since_id=since)
            self._send_json(res)
        elif path == "/api/printers":
            printers = list_printers()
            default_p = get_default_printer()
            self._send_json({"printers": printers, "default_printer": default_p})
        elif path == "/api/printer/status":
            qs = parse_qs(parsed.query)
            p_name = qs.get("printer", [""])[0] or None
            status = get_printer_status(p_name)
            self._send_json(status)
        elif path == "/api/provision/history":
            hist = prov_history_store.get_data()
            self._send_json(hist)
        elif path == "/api/supabase/config":
            cfg = load_supabase_env()
            self._send_json(cfg)
        elif path == "/api/supabase/levels":
            sb_cfg = load_supabase_env()
            ok, levels, msg = fetch_levels_from_supabase(sb_cfg.get("supabase_url"), sb_cfg.get("supabase_anon_key"))
            self._send_json({"ok": ok, "levels": levels, "message": msg}, status=200 if ok else 500)
        elif path == "/api/supabase/nodes":
            qs = parse_qs(parsed.query)
            level_id = qs.get("level", [""])[0] or None
            sb_cfg = load_supabase_env()
            ok, nodes, msg = fetch_nodes_from_supabase(sb_cfg.get("supabase_url"), sb_cfg.get("supabase_anon_key"), level_id=level_id)
            self._send_json({"ok": ok, "nodes": nodes, "message": msg}, status=200 if ok else 500)
        elif path == "/api/supabase/edges":
            sb_cfg = load_supabase_env()
            ok, edges, msg = fetch_edges_from_supabase(sb_cfg.get("supabase_url"), sb_cfg.get("supabase_anon_key"))
            self._send_json({"ok": ok, "edges": edges, "message": msg}, status=200 if ok else 500)
        elif path == "/favicon.ico":

            self.send_response(204)
            self.end_headers()
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}

        if path == "/api/flash":
            project_name = data.get("project", "IPS_Mesh_Node")
            ports = data.get("ports", [])
            baud = data.get("baud", 460800)
            erase_only = bool(data.get("erase", False))

            if not ports:
                self._send_json({"error": "No ports specified"}, status=400)
                return

            if job_manager.status == "running":
                self._send_json({"error": "A flashing job is already running"}, status=409)
                return

            project_dir = FIRMWARE_DIR / project_name
            job_manager.reset(project_name, ports)

            for p in ports:
                t = threading.Thread(
                    target=execute_flash_worker,
                    args=(p, project_dir, baud, erase_only),
                    daemon=True
                )
                t.start()

            self._send_json({"status": "started", "ports": ports})

        elif path == "/api/stop":
            job_manager.stop_all()
            self._send_json({"status": "stopped"})

        elif path == "/api/history/set_count":
            count = data.get("count", 7)
            stats = history_store.set_success_count(count)
            self._send_json({"ok": True, "stats": stats})

        elif path == "/api/terminal/open":
            port = data.get("port", "")
            baud = int(data.get("baud", 115200))
            ok, msg = terminal_manager.open_port(port, baud)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)

        elif path == "/api/terminal/send":
            port = data.get("port", "")
            text = data.get("text", "")
            ok, msg = terminal_manager.write_line(port, text)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)

        elif path == "/api/terminal/reset":
            port = data.get("port", "")
            ok, msg = terminal_manager.reset_esp(port)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)

        elif path == "/api/terminal/close":
            port = data.get("port", "")
            terminal_manager.close_port(port)
            self._send_json({"ok": True, "message": "Closed"})

        elif path == "/api/printer/print":
            node_id = str(data.get("node_id", "101")).strip()
            floor = str(data.get("floor", "GF")).strip()
            p_name = data.get("printer_name") or None
            mode = data.get("mode", "twin")
            copies = int(data.get("copies", 1))
            height_mm = float(data.get("height_mm", 24.0))
            gap_mm = float(data.get("gap_mm", 3.0))
            offset_y = int(data.get("offset_y", 0))
            if "offset_mm" in data:
                offset_y = int(round(float(data["offset_mm"]) * 8))
            inter_gap_dots = 8
            if "inter_gap_mm" in data:
                inter_gap_dots = int(round(float(data["inter_gap_mm"]) * 8))

            if mode == "single":
                ok, msg = print_single_label(node_id, floor, printer_name=p_name, copies=copies, height_mm=height_mm, gap_mm=gap_mm, offset_y=offset_y)
            else:
                ok, msg = print_twin_label(node_id, floor, printer_name=p_name, copies=copies, height_mm=height_mm, gap_mm=gap_mm, offset_y=offset_y, inter_gap_dots=inter_gap_dots)

            if ok:
                prov_history_store.record_print_event(node_id)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 500)

        elif path == "/api/printer/calibrate":
            p_name = data.get("printer_name") or None
            ok, msg = calibrate_gap(p_name)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 500)

        elif path == "/api/printer/feed":
            p_name = data.get("printer_name") or None
            height_mm = float(data.get("height_mm", 24.0))
            gap_mm = float(data.get("gap_mm", 3.0))
            ok, msg = feed_label(p_name, height_mm=height_mm, gap_mm=gap_mm)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 500)

        elif path == "/api/printer/calibrate_ruler":
            p_name = data.get("printer_name") or None
            height_mm = float(data.get("height_mm", 24.0))
            gap_mm = float(data.get("gap_mm", 3.0))
            copies = int(data.get("copies", 1))
            ok, msg = print_calibration_test(p_name, height_mm=height_mm, gap_mm=gap_mm, copies=copies)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 500)

        elif path == "/api/printer/test":
            p_name = data.get("printer_name") or None
            height_mm = float(data.get("height_mm", 24.0))
            gap_mm = float(data.get("gap_mm", 3.0))
            copies = int(data.get("copies", 1))
            ok, msg = print_twin_label("999", "TEST", printer_name=p_name, copies=copies, height_mm=height_mm, gap_mm=gap_mm)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 500)

        elif path == "/api/provision/send_id":
            port = data.get("port", "")
            uid = data.get("uid", "")
            baud = int(data.get("baud", 115200))
            if not port:
                self._send_json({"error": "No COM port specified"}, status=400)
                return
            if not uid:
                uid = generate_node_uuid_hex()
            terminal_manager.close_port(port)
            ok, msg, raw = send_set_id(port, uid, baud)
            self._send_json({"ok": ok, "message": msg, "uid": uid, "raw": raw}, status=200 if ok else 400)

        elif path == "/api/provision/get_id":
            port = data.get("port", "")
            baud = int(data.get("baud", 115200))
            if not port:
                self._send_json({"error": "No COM port specified"}, status=400)
                return
            terminal_manager.close_port(port)
            ok, uid, raw = send_get_id(port, baud)
            self._send_json({"ok": ok, "uid": uid, "raw": raw}, status=200 if ok else 400)

        elif path == "/api/provision/history/save":
            record = data.get("record", data)
            saved = prov_history_store.add_record(record)
            self._send_json({"ok": True, "record": saved})

        elif path == "/api/provision/history/delete":
            rec_id = data.get("id")
            if rec_id is not None:
                prov_history_store.delete_record(rec_id)
            self._send_json({"ok": True})

        elif path == "/api/provision/history/clear":
            prov_history_store.clear_all()
            self._send_json({"ok": True})

        elif path == "/api/provision/supabase/insert":
            payload = data.get("payload", {})
            edges = data.get("edges", [])
            destinations = data.get("destinations", [])
            sb_cfg = load_supabase_env()
            sb_url = data.get("supabase_url") or sb_cfg.get("supabase_url")
            sb_key = data.get("supabase_key") or sb_cfg.get("supabase_anon_key")
            
            # 1. Insert Node
            ok, res = insert_node_to_supabase(payload, sb_url, sb_key)
            if not ok:
                self._send_json({"ok": False, "result": res}, status=400)
                return

            # 2. Insert Edges if provided
            edge_res = None
            if edges:
                _, edge_res = insert_edges_to_supabase(edges, sb_url, sb_key)

            # 3. Link Destinations if provided
            dest_res = None
            node_id = payload.get("node_id") if isinstance(payload, dict) else None
            if destinations and node_id:
                _, dest_res = link_destinations_to_supabase(node_id, destinations, sb_url, sb_key)

            self._send_json({
                "ok": True,
                "result": res,
                "edges_result": edge_res,
                "destinations_result": dest_res
            }, status=200)

        elif path == "/api/supabase/edges/insert":
            edges = data.get("edges", [])
            sb_cfg = load_supabase_env()
            sb_url = data.get("supabase_url") or sb_cfg.get("supabase_url")
            sb_key = data.get("supabase_key") or sb_cfg.get("supabase_anon_key")
            ok, res = insert_edges_to_supabase(edges, sb_url, sb_key)
            self._send_json({"ok": ok, "result": res}, status=200 if ok else 400)

        elif path == "/api/supabase/destinations/link":
            node_id = data.get("node_id")
            destinations = data.get("destinations", [])
            sb_cfg = load_supabase_env()
            sb_url = data.get("supabase_url") or sb_cfg.get("supabase_url")
            sb_key = data.get("supabase_key") or sb_cfg.get("supabase_anon_key")
            ok, res = link_destinations_to_supabase(node_id, destinations, sb_url, sb_key)
            self._send_json({"ok": ok, "result": res}, status=200 if ok else 400)

        elif path == "/api/supabase/levels/create":
            payload = data.get("payload", data)
            sb_cfg = load_supabase_env()
            sb_url = data.get("supabase_url") or sb_cfg.get("supabase_url")
            sb_key = data.get("supabase_key") or sb_cfg.get("supabase_anon_key")
            ok, res = create_level_in_supabase(payload, sb_url, sb_key)
            self._send_json({"ok": ok, "result": res}, status=200 if ok else 400)


        else:
            self._send_json({"error": "Not found"}, status=404)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if args and isinstance(args[0], str) and ("/api/progress" in args[0] or "/api/ports" in args[0] or "/api/terminal" in args[0]):
            return
        super().log_message(format, *args)


def run_server(port=8585, open_browser=True):
    UI_DIR.mkdir(parents=True, exist_ok=True)
    server_address = ("127.0.0.1", port)
    
    try:
        httpd = HTTPServer(server_address, FlasherHTTPRequestHandler)
    except OSError:
        port += 1
        server_address = ("127.0.0.1", port)
        httpd = HTTPServer(server_address, FlasherHTTPRequestHandler)

    url = f"http://localhost:{port}"
    print(f"\n=======================================================")
    print(f"  ⚡ IPS Flasher & Provisioning Studio (with Barcode Printer)")
    print(f"  🌐 URL: {url}")
    print(f"  🖨️ Default Printer: {get_default_printer()}")
    print(f"  📁 Flasher DB: {HISTORY_FILE.name}")
    print(f"  🏷️ Provisioning DB: provisioning_history.json")
    print(f"  📊 Flashed Boards: {history_store.get_stats().get('total_flashed_success', 7)}")
    print(f"  🏷️ Provisioned Nodes: {prov_history_store.get_data()['stats'].get('total_provisioned', 0)}")
    print(f"  Press Ctrl+C to stop the server")
    print(f"=======================================================\n")

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        job_manager.stop_all()
        httpd.server_close()

if __name__ == "__main__":
    run_server()
