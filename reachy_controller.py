#!/usr/bin/env python3
"""
reachy_controller.py — Flask backend for Reachy Care web controller.

Runs on the Pi as a standalone service (port 8090), independent of main.py.
Provides HTTP endpoints for power lifecycle, deploy, config, persons, logs, etc.
"""

import glob
import hmac
import json
import logging
import os
import secrets
import signal
import subprocess
import threading
import time
import urllib.request
from functools import lru_cache
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

# ---------------------------------------------------------------------------
# Constants & paths
# ---------------------------------------------------------------------------

CARE_DIR = Path(__file__).resolve().parent
KNOWN_FACES = CARE_DIR / "known_faces"
LOGS_DIR = CARE_DIR / "logs"
CMD_DIR = Path("/tmp/reachy_care_cmds")
STATUS_FILE = Path("/tmp/reachy_care_status.json")
PID_FILE = Path("/tmp/reachy_care.pid")
CONV_PID_FILE = Path("/tmp/conv_app.pid")
STARTUP_LOG = LOGS_DIR / "start_all.log"
PORT = 8090
# Port 8090 serves the Eiffel AI dashboard assets from resources/dashboard/.
# It stays up even when main.py crashes, so the user can still power-cycle
# the full stack from the web UI.
RESOURCES = CARE_DIR / "resources" / "dashboard"

VENV_ACTIVATE = "source /venvs/apps_venv/bin/activate"

# Keys exposed via /api/settings (with secret masking)
CONFIG_KEYS = {
    "LOCATION": "str",
    "TIMEZONE": "str",
    "WAKE_WORD_FALLBACK": "str",
    "TELEGRAM_BOT_TOKEN": "secret",
    "TELEGRAM_CHAT_ID": "str",
    "TELEGRAM_ENABLED": "bool",
    "ALERT_EMAIL_FROM": "str",
    "ALERT_EMAIL_PASSWORD": "secret",
    "ALERT_EMAIL_TO": "str",
    "ALERT_EMAIL_ENABLED": "bool",
    "GROQ_API_KEY": "secret",
    "AUDD_API_TOKEN": "secret",
    "JOURNAL_EMAIL_HOUR": "int",
    "DASHBOARD_REMOTE_CODE": "secret",
}

# Remote auth allowed origins (CORS) and session TTL
CORS_ALLOWED_ORIGINS = {
    "https://eiffelai.io",
    "https://www.eiffelai.io",
    "https://reachy-care.eiffelai.io",
}
AUTH_COOKIE_NAME = "reachy_auth"
AUTH_SESSION_TTL = 24 * 3600  # 24 h rolling
AUTH_PUBLIC_PATHS = {
    "/api/health", "/api/auth", "/wifi-setup.html", "/hotspot-detect.html",
    # MJPEG / snapshot : <img crossorigin> n'envoie pas le cookie d'auth depuis
    # eiffelai.io, or Tailscale restreint déjà l'accès au tailnet → public OK.
    "/api/video", "/api/snapshot",
    # SSE EventSource idem : pas de cookie cross-origin. Tailscale protège.
    "/api/logs/stream",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [controller] %(levelname)s  %(message)s",
)
log = logging.getLogger("controller")

# ---------------------------------------------------------------------------
# Background operation tracking
# ---------------------------------------------------------------------------

_op_status: dict = {}  # {action: {state, message, ts}}
_op_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _pid_alive(pid_path: Path) -> bool:
    """Check if the process recorded in *pid_path* is alive."""
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def _read_status() -> dict:
    """Read the status JSON written periodically by main.py."""
    try:
        return json.loads(STATUS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_state() -> str:
    """
    Return the high-level state of Reachy Care.

    * "starting" - start_all.sh or restart op currently running
    * "running"  - main.py alive and not sleeping
    * "sleeping" - main.py alive, sleep mode active
    * "off"      - main.py not alive
    * "error"    - PID present but status file missing / stale
    """
    with _op_lock:
        for action in ("start", "restart"):
            if _op_status.get(action, {}).get("state") == "running":
                return "starting"
    alive = _pid_alive(PID_FILE)
    if not alive:
        return "off"
    status = _read_status()
    if not status:
        return "error"
    if status.get("sleeping"):
        return "sleeping"
    return "running"


def _send_cmd(cmd_dict: dict) -> None:
    """
    Write a command JSON into the commands directory.

    Uses atomic write (tmp + os.replace) to avoid partial reads.
    """
    CMD_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.time_ns()
    target = CMD_DIR / f"{ts}.json"
    tmp = CMD_DIR / f".tmp_{ts}.json"
    tmp.write_text(json.dumps(cmd_dict))
    os.replace(str(tmp), str(target))
    log.info("Sent command: %s", cmd_dict)


def _run_local(cmd: str, timeout: int = 60) -> tuple[bool, str]:
    """Run a shell command locally. Return (ok, output)."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        output = (r.stdout + r.stderr).strip()
        return r.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Timeout after {timeout}s"
    except Exception as exc:
        return False, str(exc)


def _background_op(action: str, cmd: str, timeout: int = 120, log_file: Path | None = None) -> None:
    """Run a long operation in a background thread, tracked in _op_status.

    If *log_file* is provided, stdout/stderr are redirected to that file
    (truncated at start) so a SSE endpoint can stream the output live.
    """

    def _run():
        with _op_lock:
            _op_status[action] = {
                "state": "running",
                "message": f"Started {action}",
                "ts": time.time(),
            }
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("")
            full = f"{cmd} > {log_file} 2>&1"
            ok, _ = _run_local(full, timeout=timeout)
            try:
                output = log_file.read_text()[-2000:]
            except Exception:
                output = ""
        else:
            ok, output = _run_local(cmd, timeout=timeout)
        state = "done" if ok else "error"
        _op_status[action] = {"state": state, "message": output[:2000], "ts": time.time()}
        log.info("Background op %s finished: %s", action, state)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _read_config() -> dict:
    """
    Read config.py + config_local.py and return whitelisted keys.

    Secrets are masked to show only the last 4 characters.
    """
    ns: dict = {}
    config_path = CARE_DIR / "config.py"
    config_local_path = CARE_DIR / "config_local.py"

    try:
        exec(compile(config_path.read_text(), str(config_path), "exec"), ns)
    except Exception as exc:
        log.warning("Failed to read config.py: %s", exc)

    try:
        exec(compile(config_local_path.read_text(), str(config_local_path), "exec"), ns)
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("Failed to read config_local.py: %s", exc)

    result = {}
    for key, kind in CONFIG_KEYS.items():
        val = ns.get(key, "")
        if kind == "secret" and val:
            masked = "*" * max(0, len(str(val)) - 4) + str(val)[-4:]
            result[key] = masked
        else:
            result[key] = val
    return result


def _write_config(key: str, value) -> tuple[bool, str]:
    """
    Write a single key to config_local.py (the override file).

    Handles str, bool, and int types based on CONFIG_KEYS.
    """
    if key not in CONFIG_KEYS:
        return False, f"Unknown config key: {key}"

    kind = CONFIG_KEYS[key]
    config_local_path = CARE_DIR / "config_local.py"

    # Parse value to correct type
    if kind == "bool":
        if isinstance(value, str):
            value = value.lower() in ("true", "1", "yes")
        py_val = repr(bool(value))
    elif kind == "int":
        try:
            py_val = str(int(value))
        except (ValueError, TypeError):
            return False, f"Invalid integer value for {key}"
    else:
        # str or secret
        py_val = repr(str(value))

    # Read existing config_local.py lines
    lines: list[str] = []
    try:
        lines = config_local_path.read_text().splitlines()
    except FileNotFoundError:
        pass

    # Replace or append the key
    new_line = f"{key} = {py_val}"
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}") and "=" in stripped:
            lhs = stripped.split("=", 1)[0].strip()
            if lhs == key:
                lines[i] = new_line
                found = True
                break
    if not found:
        lines.append(new_line)

    config_local_path.write_text("\n".join(lines) + "\n")
    log.info("Updated config_local.py: %s", key)
    return True, "OK"


def _list_persons() -> list[dict]:
    """List known persons from *_memory.json files in known_faces/."""
    persons = []
    pattern = str(KNOWN_FACES / "*_memory.json")
    for path in sorted(glob.glob(pattern)):
        name = Path(path).stem.replace("_memory", "")
        try:
            data = json.loads(Path(path).read_text())
            persons.append(
                {
                    "name": name,
                    "display_name": data.get("display_name", name),
                    "visits": data.get("visit_count", 0),
                    "last_seen": data.get("last_seen", ""),
                }
            )
        except (json.JSONDecodeError, OSError):
            persons.append({"name": name, "display_name": name, "visits": 0, "last_seen": ""})
    return persons


def _person_data(name: str) -> dict | None:
    """Load full memory + today's journal for a person."""
    mem_path = KNOWN_FACES / f"{name}_memory.json"
    if not mem_path.exists():
        return None
    try:
        memory = json.loads(mem_path.read_text())
    except (json.JSONDecodeError, OSError):
        memory = {}

    # Today's journal entry
    today_str = time.strftime("%Y-%m-%d")
    journal_path = CARE_DIR / "journals" / f"{name}_{today_str}.json"
    journal = {}
    try:
        journal = json.loads(journal_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    return {"name": name, "memory": memory, "today_journal": journal}


# ---------------------------------------------------------------------------
# Bluetooth helpers
# ---------------------------------------------------------------------------

_bt_scan_results: list[dict] = []
_bt_scanning = False


def _bt_run(cmd: str, timeout: int = 15) -> tuple[bool, str]:
    """Run a bluetoothctl command. Return (ok, output)."""
    return _run_local(f"bluetoothctl {cmd}", timeout=timeout)


def _bt_connected_device() -> dict | None:
    """Return info about the currently connected BT audio device, or None."""
    ok, out = _bt_run("info", timeout=5)
    if not ok or "Missing device address" in out:
        return None
    info = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Device "):
            info["address"] = line.split()[1]
        elif line.startswith("Alias:"):
            info["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Connected:"):
            info["connected"] = "yes" in line.lower()
        elif line.startswith("Paired:"):
            info["paired"] = "yes" in line.lower()
    if info.get("connected"):
        return info
    return None


def _bt_paired_devices() -> list[dict]:
    """List all paired BT devices."""
    ok, out = _bt_run("paired-devices", timeout=5)
    if not ok:
        return []
    devices = []
    for line in out.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) >= 3 and parts[0] == "Device":
            devices.append({"address": parts[1], "name": parts[2]})
    return devices


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=None)


# ---- SPA pages ------------------------------------------------------------

@app.route("/")
@app.route("/control")
@app.route("/persons")
@app.route("/logs")
@app.route("/dashboard.html")
def serve_spa():
    """On the Pi, `/` serves dashboard.html directly — the LAN user is
    already in front of the robot, no need for the eiffelai.io landing
    poll page. index.html (landing) is only useful on the CDN deployment.
    `/dashboard.html` explicit alias for the back-link from settings.html."""
    return send_from_directory(str(RESOURCES), "dashboard.html")


@app.route("/index.html")
@app.route("/landing")
def serve_landing():
    """Exposed for parity with the CDN, not used by default LAN flow."""
    return send_from_directory(str(RESOURCES), "index.html")


@app.route("/settings")
@app.route("/settings.html")
def serve_settings():
    return send_from_directory(str(RESOURCES), "settings.html")


@app.route("/static/<path:filename>")
def serve_static(filename):
    """Serve static assets (JS, CSS, images)."""
    return send_from_directory(str(RESOURCES), filename)


# ---- API: Status -----------------------------------------------------------

@app.route("/api/status")
def api_status():
    state = _get_state()
    status = _read_status()
    return jsonify(
        {
            "state": state,
            "mode": status.get("mode", ""),
            # main.py écrit "person" (pas "current_person") et "uptime" (pas
            # "uptime_sec"). Expose aussi "persons" (liste) pour le sélecteur
            # Journal du dashboard.
            "person": status.get("person", ""),
            "persons": status.get("persons", []),
            "sleeping": status.get("sleeping", False),
            "uptime": status.get("uptime", 0),
            "modules": status.get("modules", {}),
            "op_status": dict(_op_status),
            # Live AttenLabs state (SILENT / TO_HUMAN / TO_COMPUTER) plus the
            # reachy_speaking flag. Empty until main.py adds these fields to
            # _write_status_file.
            "attention_state": status.get("attention_state", ""),
            "reachy_speaking": status.get("reachy_speaking", False),
        }
    )


# ---- API: Power lifecycle --------------------------------------------------

@app.route("/api/power/start", methods=["POST"])
def api_power_start():
    """Lance start_all.sh puis envoie wake_motors dès que main.py est up.

    Le front ne doit plus cliquer Réveiller après Allumer — l'enchaînement
    start → wake est atomique côté serveur, trackable via /api/status.
    """
    if _pid_alive(PID_FILE):
        return jsonify({"ok": False, "error": "Already running"}), 409
    with _op_lock:
        if _op_status.get("start", {}).get("state") == "running":
            return jsonify({"ok": False, "error": "Start already in progress"}), 409

    def _start_then_wake():
        with _op_lock:
            _op_status["start"] = {
                "state": "running", "message": "Démarrage...", "ts": time.time(),
            }
        STARTUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        STARTUP_LOG.write_text("")
        ok, _ = _run_local(
            f"bash {CARE_DIR / 'start_all.sh'} > {STARTUP_LOG} 2>&1", timeout=90,
        )
        if not ok:
            try:
                output = STARTUP_LOG.read_text()[-2000:]
            except Exception:
                output = ""
            _op_status["start"] = {"state": "error", "message": output[:2000], "ts": time.time()}
            return
        # Attendre que main.py soit vraiment up (PID + STATUS_FILE présent)
        # avant d'envoyer wake_motors, sinon la cmd arrive dans le vide.
        for _ in range(20):
            if _pid_alive(PID_FILE) and STATUS_FILE.exists():
                break
            time.sleep(0.5)
        try:
            _send_cmd({"cmd": "wake_motors"})
        except Exception as exc:
            log.warning("wake_motors après start a échoué : %s", exc)
        _op_status["start"] = {"state": "done", "message": "Reachy allumé", "ts": time.time()}

    t = threading.Thread(target=_start_then_wake, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Starting..."})


@app.route("/api/power/start/log")
def api_power_start_log():
    """Stream du log de démarrage via SSE.

    Suit STARTUP_LOG tant que l'op start/restart est 'running', puis envoie
    un événement final avec l'état (done/error) et ferme.
    """

    def generate():
        # Attendre jusqu'à 3 s que le fichier apparaisse
        waited = 0
        while not STARTUP_LOG.exists() and waited < 30:
            time.sleep(0.1)
            waited += 1
        if not STARTUP_LOG.exists():
            yield "data: [aucun log de démarrage]\n\n"
            return
        proc = subprocess.Popen(
            ["tail", "-n", "+1", "-F", str(STARTUP_LOG)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        try:
            import select
            while True:
                rlist, _, _ = select.select([proc.stdout], [], [], 0.4)
                if rlist:
                    line = proc.stdout.readline()
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                        continue
                with _op_lock:
                    start_state = _op_status.get("start", {}).get("state")
                    restart_state = _op_status.get("restart", {}).get("state")
                if start_state not in ("running", None) and restart_state != "running":
                    yield f"data: [fin démarrage: {start_state or 'done'}]\n\n"
                    break
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/power/stop", methods=["POST"])
def api_power_stop():
    """Clean shutdown sequence.

    1. Send sleep_mode to main.py via a command file; main.py plays goto_sleep.
    2. Wait 3s for the rest pose to be reached.
    3. ``systemctl stop`` the three services in reverse cascade order
       (main -> conv -> aec). All three have Restart=always, so a plain pkill
       would be respawned by systemd after RestartSec=5s. Earlier attempts
       targeting ``conv_app_v2.service`` failed silently because that unit
       name does not exist; the real units are all named reachy-care-*.
    4. Stop the daemon backend (motors off). The reachy-mini-daemon.service
       stays active so that a subsequent wake does not pay the 40s cold-boot
       cost.
    """
    errors = []

    try:
        _send_cmd({"cmd": "sleep_mode"})
    except Exception as exc:
        errors.append(f"sleep_mode: {exc}")

    time.sleep(3)

    _run_local(
        "sudo systemctl stop reachy-care-main.service "
        "reachy-care-conv.service reachy-care-aec.service",
        timeout=15,
    )

    # Clean legacy PID files (les services cgroup systemd ne les utilisent plus)
    for pf in (PID_FILE, CONV_PID_FILE):
        try:
            pf.unlink()
        except FileNotFoundError:
            pass

    _run_local(
        "curl -s -X POST 'http://localhost:8000/api/daemon/stop?goto_sleep=false' || true",
        timeout=10,
    )

    if errors:
        return jsonify({"ok": False, "errors": errors}), 500
    return jsonify({"ok": True, "message": "Stopped cleanly"})


@app.route("/api/power/wake_smart", methods=["POST"])
def api_power_wake_smart():
    """Wake without restarting the daemon when it is already active.

    Works around a WS /ws/sdk breakage: the previous 'wake' command sent to
    main.py triggered a POST /api/daemon/start?wake_up=true which restarted
    the motor daemon with a new PID and broke every /ws/sdk WebSocket open
    on main.py and conv_app_v2 ("Lost connection with the server" at 95+/s).

    Case 1 (by far the most common; daemon ready + main.py alive): POST
    /api/move/play/wake_up directly on the daemon. Plays the wake animation
    and enables motors, keeps the WS sessions intact, no restart.
    Case 2 (cold boot, main.py down): full cascade via start_all.sh.
    Case 3 (main alive but daemon down): inconsistent state that requires
    an explicit restart from the user, so we return HTTP 409.
    """
    daemon_ready = False
    try:
        resp = urllib.request.urlopen(
            "http://localhost:8000/api/daemon/status", timeout=3
        )
        status = json.loads(resp.read())
        daemon_ready = bool(status.get("backend_status", {}).get("ready", False))
    except Exception as exc:
        log.warning("daemon status check failed: %s", exc)

    main_alive = _pid_alive(PID_FILE)

    if daemon_ready and main_alive:
        try:
            req = urllib.request.Request(
                "http://localhost:8000/api/move/play/wake_up",
                method="POST",
                headers={"Content-Type": "application/json"},
                data=b"{}",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            log.warning("wake_up play failed: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 500
        # Notifier main.py sans passer par son handler 'wake' (qui restart le
        # daemon). Nouvelle cmd 'mark_awake' à implémenter côté main.py pour
        # mettre à jour son flag sleeping=False sans toucher au daemon.
        # Si la cmd n'est pas encore connue de main.py, elle est ignorée.
        try:
            _send_cmd({"cmd": "mark_awake"})
        except Exception:
            pass
        return jsonify({"ok": True, "path": "fast_wake"})

    if main_alive and not daemon_ready:
        return jsonify({
            "ok": False,
            "error": "main.py alive but daemon down — cascade_restart required",
        }), 409

    # Cold boot — pas de main.py : enchaîner start_all.sh (api_power_start)
    return api_power_start()


@app.route("/api/power/sleep", methods=["POST"])
def api_power_sleep():
    if not _pid_alive(PID_FILE):
        return jsonify({"ok": False, "error": "Not running"}), 409
    _send_cmd({"cmd": "sleep_mode"})
    return jsonify({"ok": True, "message": "Sleep command sent"})


@app.route("/api/power/wake", methods=["POST"])
def api_power_wake():
    if not _pid_alive(PID_FILE):
        return jsonify({"ok": False, "error": "Not running"}), 409
    _send_cmd({"cmd": "wake_motors"})
    return jsonify({"ok": True, "message": "Wake command sent"})


# ── LLM mute/unmute (direct IPC to conv_app, instant) ──────────────

CONV_APP_IPC = "http://127.0.0.1:8766"


def _ipc_post(path: str) -> int:
    """POST to conv_app IPC, returns HTTP status or -1 on error."""
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{CONV_APP_IPC}{path}",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=2)
        return resp.status
    except Exception:
        return -1


@app.route("/api/llm/mute", methods=["POST"])
def api_llm_mute():
    """Coupe immédiatement la parole du LLM + mute le bridge."""
    results = {ep: _ipc_post(ep) for ep in ("/cancel", "/sleep")}
    _send_cmd({"cmd": "mute"})
    return jsonify({"ok": True, "muted": True, "ipc": results})


@app.route("/api/llm/unmute", methods=["POST"])
def api_llm_unmute():
    """Restaure l'écoute et la parole du LLM."""
    results = {"/wake": _ipc_post("/wake")}
    _send_cmd({"cmd": "unmute"})
    return jsonify({"ok": True, "muted": False, "ipc": results})


@app.route("/api/power/restart", methods=["POST"])
def api_power_restart():
    with _op_lock:
        running_ops = [
            k for k, v in _op_status.items() if v.get("state") == "running"
        ]
        if running_ops:
            return jsonify({"ok": False, "error": f"Operation in progress: {running_ops}"}), 409

    def _restart():
        _op_status["restart"] = {
            "state": "running",
            "message": "Stopping...",
            "ts": time.time(),
        }
        # Stop (séquence douce — même logique que api_power_stop)
        try:
            _send_cmd({"cmd": "sleep_mode"})
        except Exception:
            pass
        time.sleep(3)
        _run_local(f"pkill -TERM -F {PID_FILE}", timeout=5)
        _run_local(f"pkill -TERM -F {CONV_PID_FILE}", timeout=5)
        time.sleep(1)
        _run_local(f"pkill -KILL -F {PID_FILE} 2>/dev/null || true", timeout=5)
        _run_local(f"pkill -KILL -F {CONV_PID_FILE} 2>/dev/null || true", timeout=5)
        for pf in (PID_FILE, CONV_PID_FILE):
            try:
                pf.unlink()
            except FileNotFoundError:
                pass
        time.sleep(3)
        # Start — redirige vers STARTUP_LOG pour streaming SSE
        _op_status["restart"]["message"] = "Starting..."
        STARTUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        STARTUP_LOG.write_text("")
        ok, _ = _run_local(
            f"bash {CARE_DIR / 'start_all.sh'} > {STARTUP_LOG} 2>&1",
            timeout=90,
        )
        try:
            output = STARTUP_LOG.read_text()[-2000:]
        except Exception:
            output = ""
        state = "done" if ok else "error"
        _op_status["restart"] = {
            "state": state,
            "message": output[:2000],
            "ts": time.time(),
        }

    t = threading.Thread(target=_restart, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Restarting..."})


# ---- API: Deploy -----------------------------------------------------------

@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    with _op_lock:
        if _op_status.get("deploy", {}).get("state") == "running":
            return jsonify({"ok": False, "error": "Deploy already in progress"}), 409

    was_running = _pid_alive(PID_FILE)

    def _deploy():
        _op_status["deploy"] = {
            "state": "running",
            "message": "Pulling...",
            "ts": time.time(),
        }
        # Git pull
        ok, out = _run_local(f"cd {CARE_DIR} && git pull", timeout=60)
        if not ok:
            _op_status["deploy"] = {"state": "error", "message": f"git pull failed: {out}", "ts": time.time()}
            return

        # Patch conv_app
        _op_status["deploy"]["message"] = "Patching conv_app..."
        ok, out = _run_local(
            f"{VENV_ACTIVATE} && cd {CARE_DIR} && python patch_source.py",
            timeout=30,
        )
        if not ok:
            _op_status["deploy"] = {"state": "error", "message": f"patch failed: {out}", "ts": time.time()}
            return

        # Restart if was running
        if was_running:
            _op_status["deploy"]["message"] = "Restarting..."
            # CLAUDE.md: ne JAMAIS kill main.py seul (socket caméra corrompue).
            # Kill atomique conv_app + main.py dans une seule commande.
            _run_local(
                "pkill -f 'reachy.mini.conversation' ; pkill -f 'python.*main\\.py'",
                timeout=10,
            )
            for pf in (PID_FILE, CONV_PID_FILE):
                try:
                    pf.unlink()
                except FileNotFoundError:
                    pass
            time.sleep(3)
            ok, out = _run_local(f"bash {CARE_DIR / 'start_all.sh'}", timeout=90)
            if not ok:
                _op_status["deploy"] = {"state": "error", "message": f"restart failed: {out}", "ts": time.time()}
                return

        _op_status["deploy"] = {"state": "done", "message": "Deploy complete", "ts": time.time()}

    t = threading.Thread(target=_deploy, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Deploying..."})


@app.route("/api/deploy/full", methods=["POST"])
def api_deploy_full():
    with _op_lock:
        if _op_status.get("deploy", {}).get("state") == "running":
            return jsonify({"ok": False, "error": "Deploy already in progress"}), 409

    was_running = _pid_alive(PID_FILE)

    def _deploy_full():
        _op_status["deploy"] = {
            "state": "running",
            "message": "Pulling...",
            "ts": time.time(),
        }
        # Git pull
        ok, out = _run_local(f"cd {CARE_DIR} && git pull", timeout=60)
        if not ok:
            _op_status["deploy"] = {"state": "error", "message": f"git pull failed: {out}", "ts": time.time()}
            return

        # pip install
        _op_status["deploy"]["message"] = "Installing dependencies..."
        ok, out = _run_local(
            f"{VENV_ACTIVATE} && pip install -r {CARE_DIR / 'requirements.txt'}",
            timeout=300,
        )
        if not ok:
            _op_status["deploy"] = {"state": "error", "message": f"pip install failed: {out[:1500]}", "ts": time.time()}
            return

        # Patch conv_app
        _op_status["deploy"]["message"] = "Patching conv_app..."
        ok, out = _run_local(
            f"{VENV_ACTIVATE} && cd {CARE_DIR} && python patch_source.py",
            timeout=30,
        )
        if not ok:
            _op_status["deploy"] = {"state": "error", "message": f"patch failed: {out}", "ts": time.time()}
            return

        # Restart if was running
        if was_running:
            _op_status["deploy"]["message"] = "Restarting..."
            # CLAUDE.md: ne JAMAIS kill main.py seul (socket caméra corrompue).
            # Kill atomique conv_app + main.py dans une seule commande.
            _run_local(
                "pkill -f 'reachy.mini.conversation' ; pkill -f 'python.*main\\.py'",
                timeout=10,
            )
            for pf in (PID_FILE, CONV_PID_FILE):
                try:
                    pf.unlink()
                except FileNotFoundError:
                    pass
            time.sleep(3)
            ok, out = _run_local(f"bash {CARE_DIR / 'start_all.sh'}", timeout=90)
            if not ok:
                _op_status["deploy"] = {"state": "error", "message": f"restart failed: {out}", "ts": time.time()}
                return

        _op_status["deploy"] = {"state": "done", "message": "Full deploy complete", "ts": time.time()}

    t = threading.Thread(target=_deploy_full, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Full deploy starting..."})


@app.route("/api/op/status")
def api_op_status():
    return jsonify(dict(_op_status))


# ---- API: Modules ----------------------------------------------------------

@app.route("/api/module/<name>/toggle", methods=["POST"])
def api_module_toggle(name):
    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled", True)
    _send_cmd({"cmd": "toggle_module", "module": name, "enabled": bool(enabled)})
    return jsonify({"ok": True, "module": name, "enabled": bool(enabled)})


# ---- API: Modes ------------------------------------------------------------

@app.route("/api/mode/<name>", methods=["POST"])
def api_mode_switch(name):
    body = request.get_json(silent=True) or {}
    cmd = {"cmd": "switch_mode", "mode": name}
    if "topic" in body:
        cmd["topic"] = body["topic"]
    _send_cmd(cmd)
    return jsonify({"ok": True, "mode": name})


# ---- API: Persons ----------------------------------------------------------

@app.route("/api/persons")
def api_persons_list():
    """Proxy vers le dashboard embarqué (main.py :8080) qui lit registry.json
    et renvoie la shape attendue par settings.html ({persons, owner} avec
    is_owner/n_photos/enrolled_at). Si main.py est down → 503 main_down."""
    return _proxy_to_embedded("/api/persons")


@app.route("/api/persons/<name>")
def api_person_detail(name):
    data = _person_data(name)
    if data is None:
        return jsonify({"error": "Person not found"}), 404
    return jsonify(data)


@app.route("/api/persons/enroll", methods=["POST"])
def api_person_enroll():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    _send_cmd({"cmd": "enroll", "name": name})
    return jsonify({"ok": True, "message": f"Enroll command sent for {name}"})


@app.route("/api/persons/forget", methods=["POST"])
def api_person_forget():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    # Envoyer la commande a main.py (supprime du registry + .npy)
    _send_cmd({"cmd": "forget", "name": name})
    # Aussi nettoyer les fichiers memory/journal directement (pour les orphelins)
    import glob as g
    for f in g.glob(str(KNOWN_FACES / f"{name}_*")):
        try:
            Path(f).unlink()
            logger.info("Supprime: %s", f)
        except Exception:
            pass
    return jsonify({"ok": True, "message": f"{name} oublie"})


# ---- API: Settings ---------------------------------------------------------

@app.route("/api/settings")
def api_settings_get():
    """Proxy vers le dashboard embarqué (main.py :8080) qui renvoie la shape
    attendue par settings.html (location/timezone en minuscules, + toggles
    telegram/email/wake_word_threshold). Si main.py est down → 503 main_down."""
    return _proxy_to_embedded("/api/settings")


@app.route("/api/settings", methods=["POST"])
def api_settings_update():
    body = request.get_json(silent=True) or {}
    key = body.get("key", "")
    value = body.get("value")
    if not key:
        return jsonify({"ok": False, "error": "key required"}), 400
    ok, msg = _write_config(key, value)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "message": msg})


# ---- API: Video proxy ------------------------------------------------------

@app.route("/api/video")
def api_video():
    """Proxy the MJPEG stream from main.py's dashboard (localhost:8080)."""

    def generate():
        try:
            upstream = urllib.request.urlopen(
                "http://localhost:8080/video_feed", timeout=10
            )
            while True:
                chunk = upstream.read(4096)
                if not chunk:
                    break
                yield chunk
        except Exception as exc:
            log.warning("Video proxy error: %s", exc)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/snapshot")
def api_snapshot():
    """Return a single JPEG frame from the MJPEG stream (lightweight)."""
    try:
        upstream = urllib.request.urlopen(
            "http://localhost:8080/video_feed", timeout=5
        )
        buf = b""
        while len(buf) < 500_000:  # safety limit ~500KB
            chunk = upstream.read(4096)
            if not chunk:
                break
            buf += chunk
            # Find a complete JPEG (SOI=FFD8 ... EOI=FFD9)
            start = buf.find(b"\xff\xd8")
            if start < 0:
                continue
            end = buf.find(b"\xff\xd9", start + 2)
            if end >= 0:
                upstream.close()
                jpeg = buf[start : end + 2]
                return Response(jpeg, mimetype="image/jpeg",
                                headers={"Cache-Control": "no-cache"})
        upstream.close()
    except Exception:
        pass
    # 1x1 transparent pixel fallback
    return Response(status=204)


# ---- API: Log streaming (SSE) ---------------------------------------------

@app.route("/api/logs/stream")
def api_logs_stream():
    """
    Server-Sent Events log streaming.

    Query param: type = main | conv_app | system
    """
    log_type = request.args.get("type", "main")
    log_files = {
        "main": LOGS_DIR / "reachy_care.log",
        "conv_app": LOGS_DIR / "conv_app.log",
        "system": Path("/var/log/syslog"),
    }
    log_path = log_files.get(log_type, log_files["main"])

    def stream():
        try:
            with open(log_path, "r") as f:
                # Seek to last 4KB for initial context
                try:
                    f.seek(0, 2)  # end
                    pos = max(0, f.tell() - 4096)
                    f.seek(pos)
                    if pos > 0:
                        f.readline()  # skip partial line
                except OSError:
                    pass

                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {json.dumps(line.rstrip())}\n\n"
                    else:
                        time.sleep(0.5)
        except FileNotFoundError:
            yield f"data: {json.dumps(f'Log file not found: {log_path}')}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps(f'Error: {exc}')}\n\n"

    return Response(stream(), mimetype="text/event-stream")


# ---- API: Bluetooth --------------------------------------------------------

@app.route("/api/bt/status")
def api_bt_status():
    """Return BT adapter state, connected device, paired devices."""
    ok, out = _bt_run("show", timeout=5)
    powered = False
    if ok:
        for line in out.splitlines():
            if "Powered:" in line:
                powered = "yes" in line.lower()
    connected = _bt_connected_device()
    paired = _bt_paired_devices()
    return jsonify({
        "powered": powered,
        "connected": connected,
        "paired": paired,
        "scanning": _bt_scanning,
    })


@app.route("/api/bt/scan", methods=["POST"])
def api_bt_scan():
    """Scan for nearby BT devices (10 seconds). Returns results."""
    global _bt_scanning, _bt_scan_results

    if _bt_scanning:
        return jsonify({"ok": False, "error": "Scan already in progress"}), 409

    def _do_scan():
        global _bt_scanning, _bt_scan_results
        _bt_scanning = True
        _bt_scan_results = []
        # Scan for 10 seconds
        _run_local("bluetoothctl --timeout 10 scan on", timeout=15)
        # Collect discovered devices
        ok, out = _bt_run("devices", timeout=5)
        if ok:
            for line in out.splitlines():
                parts = line.strip().split(" ", 2)
                if len(parts) >= 3 and parts[0] == "Device":
                    addr = parts[1]
                    name = parts[2]
                    # Skip devices with only MAC as name (unresolved)
                    _bt_scan_results.append({"address": addr, "name": name})
        _bt_scanning = False

    t = threading.Thread(target=_do_scan, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Scanning for 10 seconds..."})


@app.route("/api/bt/devices")
def api_bt_devices():
    """Return last scan results."""
    return jsonify({"devices": _bt_scan_results, "scanning": _bt_scanning})


@app.route("/api/bt/pair", methods=["POST"])
def api_bt_pair():
    """Pair and trust a BT device by address."""
    body = request.get_json(silent=True) or {}
    addr = body.get("address", "").strip()
    if not addr:
        return jsonify({"ok": False, "error": "address required"}), 400

    ok1, out1 = _bt_run(f"pair {addr}", timeout=20)
    ok2, out2 = _bt_run(f"trust {addr}", timeout=10)

    if not ok1 and "already exists" not in out1.lower():
        return jsonify({"ok": False, "error": f"Pair failed: {out1}"}), 500
    return jsonify({"ok": True, "message": f"Paired and trusted {addr}"})


@app.route("/api/bt/connect", methods=["POST"])
def api_bt_connect():
    """Connect to a paired BT device."""
    body = request.get_json(silent=True) or {}
    addr = body.get("address", "").strip()
    if not addr:
        return jsonify({"ok": False, "error": "address required"}), 400

    ok, out = _bt_run(f"connect {addr}", timeout=15)
    if not ok:
        return jsonify({"ok": False, "error": f"Connect failed: {out}"}), 500
    return jsonify({"ok": True, "message": f"Connected to {addr}"})


@app.route("/api/bt/disconnect", methods=["POST"])
def api_bt_disconnect():
    """Disconnect the current BT device."""
    body = request.get_json(silent=True) or {}
    addr = body.get("address", "").strip()
    if not addr:
        # Disconnect whatever is connected
        connected = _bt_connected_device()
        if connected:
            addr = connected["address"]
        else:
            return jsonify({"ok": True, "message": "Nothing connected"})

    ok, out = _bt_run(f"disconnect {addr}", timeout=10)
    return jsonify({"ok": True, "message": f"Disconnected {addr}"})


@app.route("/api/bt/remove", methods=["POST"])
def api_bt_remove():
    """Remove (unpair) a BT device."""
    body = request.get_json(silent=True) or {}
    addr = body.get("address", "").strip()
    if not addr:
        return jsonify({"ok": False, "error": "address required"}), 400

    ok, out = _bt_run(f"remove {addr}", timeout=10)
    if not ok:
        return jsonify({"ok": False, "error": f"Remove failed: {out}"}), 500
    return jsonify({"ok": True, "message": f"Removed {addr}"})


@app.route("/api/bt/audio-output", methods=["GET", "POST"])
def api_bt_audio_output():
    """GET: check BT audio state. POST: switch audio output."""
    if request.method == "GET":
        ok, out = _run_local("pgrep -f bluealsa-aplay", timeout=5)
        active = ok and out.strip() != ""
        return jsonify({"bt_active": active})

    body = request.get_json(silent=True) or {}
    target = body.get("target", "usb")  # "bt" or "usb"

    if target == "bt":
        connected = _bt_connected_device()
        if not connected:
            return jsonify({"ok": False, "error": "No BT device connected"}), 400
        # Signal conv_app to switch to BT mode (high VAD + no interrupt)
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    "http://127.0.0.1:8766/bt_mode_on",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=2,
            )
        except Exception:
            pass
        log.info("Audio output switched to BT")
    else:
        # Signal conv_app to restore USB mode (normal VAD + interrupt)
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    "http://127.0.0.1:8766/bt_mode_off",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=2,
            )
        except Exception:
            pass
        log.info("Audio output switched to USB")

    return jsonify({"ok": True, "target": target})


# ---------------------------------------------------------------------------
# Health / auth / CORS / cascade restart / reboot endpoints.
# ---------------------------------------------------------------------------

_auth_sessions: dict[str, float] = {}
_auth_lock = threading.Lock()


@lru_cache(maxsize=1)
def _pi_wifi_subnet_prefix() -> str:
    """Return the /24 prefix of the Pi's default gateway (wifi). Cached.

    Example: if `ip route` returns 'default via 192.168.1.1 dev wlan0',
    the prefix returned is '192.168.1.'. Clients whose remote_addr starts
    with this prefix are considered same-LAN.
    """
    ok, out = _run_local("ip route show default", timeout=3)
    if not ok:
        return ""
    parts = out.split()
    for i, token in enumerate(parts):
        if token == "via" and i + 1 < len(parts):
            gw = parts[i + 1]
            octets = gw.split(".")
            if len(octets) == 4:
                return ".".join(octets[:3]) + "."
    return ""


def _is_lan_client(remote_ip: str) -> bool:
    # Loopback is always trusted (local processes + Pi-side curl).
    if remote_ip in ("127.0.0.1", "::1", "localhost"):
        return True
    prefix = _pi_wifi_subnet_prefix()
    if not prefix:
        return False
    return remote_ip.startswith(prefix)


def _session_valid(token: str) -> bool:
    if not token:
        return False
    now = time.time()
    with _auth_lock:
        expiry = _auth_sessions.get(token, 0)
        if expiry > now:
            _auth_sessions[token] = now + AUTH_SESSION_TTL
            return True
        _auth_sessions.pop(token, None)
    return False


@app.before_request
def _auth_middleware():
    if request.method == "OPTIONS":
        return None
    if not request.path.startswith("/api/"):
        return None
    if request.path in AUTH_PUBLIC_PATHS:
        return None
    remote = request.remote_addr or ""
    if _is_lan_client(remote):
        return None
    if _session_valid(request.cookies.get(AUTH_COOKIE_NAME, "")):
        return None
    return jsonify({"ok": False, "error": "auth_required"}), 401


@app.after_request
def _add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin in CORS_ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/api/health")
def api_health():
    """Always-on ping. Used by eiffelai.io to detect Pi up + basic state."""
    return jsonify({
        "ok": True,
        "service": "reachy_controller",
        "state": _get_state(),
        "ts": time.time(),
    })


@app.route("/api/auth", methods=["POST"])
def api_auth():
    """Exchange a remote access code for a session cookie (24 h rolling)."""
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip()
    expected = str(_read_config().get("DASHBOARD_REMOTE_CODE", "")).strip()
    if not expected:
        return jsonify({"ok": False, "error": "code_not_configured"}), 503
    if not code or not hmac.compare_digest(code, expected):
        return jsonify({"ok": False, "error": "invalid_code"}), 403
    token = secrets.token_urlsafe(32)
    with _auth_lock:
        _auth_sessions[token] = time.time() + AUTH_SESSION_TTL
    resp = jsonify({"ok": True})
    resp.set_cookie(
        AUTH_COOKIE_NAME, token,
        max_age=AUTH_SESSION_TTL,
        httponly=True, samesite="Lax", secure=True,
    )
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if token:
        with _auth_lock:
            _auth_sessions.pop(token, None)
    resp = jsonify({"ok": True})
    resp.delete_cookie(AUTH_COOKIE_NAME)
    return resp


# Cascade restart sequence. Service names are checked on the Pi at startup;
# a warning is logged if any is missing.
_CASCADE_STEPS: list[tuple[str, int]] = [
    ("reachy-mini-daemon.service", 3),
    ("reachy-care-aec.service", 2),
    ("conv_app_v2.service", 2),
    ("reachy-care-main.service", 0),
]


@app.route("/api/cascade_restart", methods=["POST"])
def api_cascade_restart():
    """Restart daemon → aec → conv → main in order, with mandatory sleeps.

    Returns per-step outcome so the front can render progress. Stops at first
    error and returns 500 with partial results.
    """
    results = []
    for service, sleep_after in _CASCADE_STEPS:
        t0 = time.monotonic()
        ok, out = _run_local(f"sudo systemctl restart {service}", timeout=20)
        duration_ms = int((time.monotonic() - t0) * 1000)
        step = {
            "service": service,
            "status": "ok" if ok else "error",
            "duration_ms": duration_ms,
        }
        if not ok:
            step["error"] = out[:240]
            results.append(step)
            return jsonify({"ok": False, "steps": results}), 500
        results.append(step)
        if sleep_after > 0:
            time.sleep(sleep_after)
    return jsonify({"ok": True, "steps": results})


@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    """Full Pi reboot — last resort when Dynamixel bitmask stuck.

    Reply immediately so the HTTP roundtrip completes, then reboot
    via a detached shell 1 s later.
    """
    _run_local("(sleep 1 && sudo reboot) >/dev/null 2>&1 &", timeout=3)
    return jsonify({"ok": True, "message": "Rebooting in 1 s"})


# ---------------------------------------------------------------------------
# Internal proxy :8090 → :8080 pour les routes live servies par le Dashboard
# embarqué dans main.py. Permet au front unique (resources/dashboard/) de
# consommer toutes les API via une seule origine. Si main.py est mort,
# on retourne 503 + {action: "wake"} pour que le front affiche un banner.
# ---------------------------------------------------------------------------

_EMBEDDED_DASHBOARD_BASE = "http://127.0.0.1:8080"

# Liste des préfixes d'URL qu'on délègue au Dashboard embarqué. Les autres
# routes `/api/*` sont servies nativement par reachy_controller.py.
_PROXY_PREFIXES = (
    "/api/runtime-state",
    "/api/journal/",
    "/api/activities",
    "/api/modes",
    "/api/wake_words",
    "/api/settings/wake_word",
    "/api/settings/profile",
    "/api/settings/location",
    "/api/settings/telegram",
    "/api/settings/email",
    "/api/settings/apikeys",
)


def _proxy_to_embedded(path: str):
    """Forward request to 127.0.0.1:8080 and relay the response.

    Used for endpoints that live in modules/dashboard.py (Dashboard embarqué).
    Returns Flask Response with same status + body + content-type.
    """
    import urllib.error
    import urllib.request
    from flask import Response
    url = _EMBEDDED_DASHBOARD_BASE + path
    method = request.method.upper()
    req_headers = {"Content-Type": request.headers.get("Content-Type", "application/json")}
    data = request.get_data() if method in ("POST", "PUT", "PATCH") else None
    try:
        up = urllib.request.Request(url, data=data, method=method, headers=req_headers)
        with urllib.request.urlopen(up, timeout=10) as resp:
            body = resp.read()
            ct = resp.headers.get("Content-Type", "application/json")
            return Response(body, status=resp.status, mimetype=ct.split(";")[0])
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code, mimetype="application/json")
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError):
        return jsonify({"ok": False, "error": "main_down", "action": "wake"}), 503


@app.route("/api/runtime-state", methods=["GET", "POST"])
def proxy_runtime_state():
    return _proxy_to_embedded("/api/runtime-state")


@app.route("/api/journal/<person>")
def proxy_journal(person):
    return _proxy_to_embedded(f"/api/journal/{person}")


@app.route("/api/journal/<person>/<date>")
def proxy_journal_date(person, date):
    return _proxy_to_embedded(f"/api/journal/{person}/{date}")


@app.route("/api/activities")
def proxy_activities():
    return _proxy_to_embedded("/api/activities")


@app.route("/api/activities/<name>/enable", methods=["POST"])
def proxy_activity_enable(name):
    return _proxy_to_embedded(f"/api/activities/{name}/enable")


@app.route("/api/activities/<name>/disable", methods=["POST"])
def proxy_activity_disable(name):
    return _proxy_to_embedded(f"/api/activities/{name}/disable")


@app.route("/api/activities/<name>/uninstall", methods=["POST"])
def proxy_activity_uninstall(name):
    return _proxy_to_embedded(f"/api/activities/{name}/uninstall")


@app.route("/api/modes")
def proxy_modes():
    return _proxy_to_embedded("/api/modes")


@app.route("/api/wake_words")
def proxy_wake_words():
    return _proxy_to_embedded("/api/wake_words")


@app.route("/api/persons/<name>/profile")
def proxy_person_profile(name):
    return _proxy_to_embedded(f"/api/persons/{name}/profile")


@app.route("/api/persons/<name>/primary", methods=["POST"])
def proxy_person_primary(name):
    return _proxy_to_embedded(f"/api/persons/{name}/primary")


@app.route("/api/settings/location", methods=["POST"])
def proxy_settings_location():
    return _proxy_to_embedded("/api/settings/location")


@app.route("/api/settings/telegram", methods=["POST"])
def proxy_settings_telegram():
    return _proxy_to_embedded("/api/settings/telegram")


@app.route("/api/settings/email", methods=["POST"])
def proxy_settings_email():
    return _proxy_to_embedded("/api/settings/email")


@app.route("/api/settings/apikeys", methods=["POST"])
def proxy_settings_apikeys():
    return _proxy_to_embedded("/api/settings/apikeys")


@app.route("/api/settings/profile", methods=["POST"])
def proxy_settings_profile():
    return _proxy_to_embedded("/api/settings/profile")


@app.route("/api/settings/wake_word", methods=["POST"])
def proxy_settings_wake_word():
    return _proxy_to_embedded("/api/settings/wake_word")


@app.route("/api/cmd", methods=["POST"])
def proxy_cmd():
    return _proxy_to_embedded("/api/cmd")


# ---------------------------------------------------------------------------
# Wifi — NetworkManager nmcli backend. On the target image, NM is active,
# nmcli lives at /usr/bin/nmcli, and the `pollen` user is in the netdev
# group. Hotspot mode is provided by `nmcli device wifi hotspot`, which
# ships its own dnsmasq, so we do not need hostapd.
# ---------------------------------------------------------------------------

import shlex as _shlex


def _nmcli_parse_terse(output: str) -> list[list[str]]:
    """Parse `nmcli -t` output (colon-separated, escaped colons with backslash)."""
    rows: list[list[str]] = []
    for line in output.splitlines():
        if not line:
            continue
        fields: list[str] = []
        cur = ""
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "\\" and i + 1 < len(line):
                cur += line[i + 1]
                i += 2
                continue
            if ch == ":":
                fields.append(cur)
                cur = ""
            else:
                cur += ch
            i += 1
        fields.append(cur)
        rows.append(fields)
    return rows


AUTH_PUBLIC_PATHS.update({
    "/wifi-setup.html",
    "/hotspot-detect.html",
    "/generate_204",          # Android captive portal detection
    "/ncsi.txt",              # Windows captive portal detection
    "/connecttest.txt",
})


@app.route("/api/wifi/scan")
def api_wifi_scan():
    """List wifi networks visible to wlan0. Includes saved + active flags."""
    ok, out = _run_local(
        "nmcli -t -f SSID,SIGNAL,SECURITY,IN-USE device wifi list --rescan yes",
        timeout=20,
    )
    if not ok:
        return jsonify({"ok": False, "error": out[:240]}), 500
    saved_ok, saved_out = _run_local(
        "nmcli -t -f NAME connection show", timeout=5
    )
    saved_names = {row[0] for row in _nmcli_parse_terse(saved_out)} if saved_ok else set()
    seen = set()
    networks = []
    for row in _nmcli_parse_terse(out):
        if len(row) < 4:
            continue
        ssid, signal, security, in_use = row[0], row[1], row[2], row[3]
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        try:
            rssi = int(signal) - 100  # nmcli returns 0-100, approximate to dBm
        except ValueError:
            rssi = None
        networks.append({
            "ssid": ssid,
            "rssi": rssi,
            "signal": int(signal) if signal.isdigit() else None,
            "security": security or "open",
            "saved": ssid in saved_names,
            "active": in_use == "*",
        })
    networks.sort(key=lambda n: (-(n.get("signal") or 0), n["ssid"].lower()))
    return jsonify({"ok": True, "networks": networks})


@app.route("/api/wifi/status")
def api_wifi_status():
    """Current wifi connection state : SSID, RSSI, IPv4, gateway."""
    ok, out = _run_local(
        "nmcli -t -f SSID,SIGNAL,IN-USE device wifi list", timeout=5
    )
    current_ssid = None
    current_signal = None
    if ok:
        for row in _nmcli_parse_terse(out):
            if len(row) >= 3 and row[2] == "*":
                current_ssid = row[0]
                current_signal = int(row[1]) if row[1].isdigit() else None
                break
    ip_ok, ip_out = _run_local("ip -4 addr show wlan0", timeout=3)
    ipv4 = None
    if ip_ok:
        for line in ip_out.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                ipv4 = line.split()[1].split("/")[0]
                break
    hotspot_ok, hotspot_out = _run_local(
        "nmcli -t -f TYPE,ACTIVE connection show --active", timeout=3
    )
    hotspot_active = False
    if hotspot_ok:
        for row in _nmcli_parse_terse(hotspot_out):
            if len(row) >= 1 and "hotspot" in row[0].lower():
                hotspot_active = True
                break
    return jsonify({
        "ok": True,
        "ssid": current_ssid,
        "signal": current_signal,
        "rssi_dbm": (current_signal - 100) if current_signal is not None else None,
        "ipv4": ipv4,
        "hotspot_active": hotspot_active,
    })


@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    """Connect wlan0 to a wifi network. password optional for open networks."""
    body = request.get_json(silent=True) or {}
    ssid = str(body.get("ssid", "")).strip()
    password = str(body.get("password", ""))
    if not ssid:
        return jsonify({"ok": False, "error": "ssid required"}), 400
    if password:
        cmd = f"nmcli device wifi connect {_shlex.quote(ssid)} password {_shlex.quote(password)} ifname wlan0"
    else:
        cmd = f"nmcli device wifi connect {_shlex.quote(ssid)} ifname wlan0"
    ok, out = _run_local(cmd, timeout=30)
    if not ok:
        return jsonify({"ok": False, "error": out[:240]}), 500
    return jsonify({"ok": True, "message": out.strip()[:240]})


@app.route("/api/wifi/forget", methods=["POST"])
def api_wifi_forget():
    """Delete a saved wifi connection profile."""
    body = request.get_json(silent=True) or {}
    ssid = str(body.get("ssid", "")).strip()
    if not ssid:
        return jsonify({"ok": False, "error": "ssid required"}), 400
    ok, out = _run_local(
        f"nmcli connection delete {_shlex.quote(ssid)}", timeout=10
    )
    if not ok:
        return jsonify({"ok": False, "error": out[:240]}), 500
    return jsonify({"ok": True, "message": f"Forgot {ssid}"})


@app.route("/api/wifi/hotspot", methods=["POST"])
def api_wifi_hotspot():
    """Start hotspot on wlan0. Returns the generated SSID + password so the
    frontend can display a QR code or write them down for the user."""
    import hashlib
    body = request.get_json(silent=True) or {}
    hostname_ok, hostname_out = _run_local("hostname -s", timeout=2)
    host_short = hostname_out.strip() if hostname_ok else "reachy"
    seed = hashlib.sha1(f"{host_short}-{time.time()}".encode()).hexdigest()[:8]
    ssid = str(body.get("ssid") or f"Reachy-{host_short[:6]}")
    password = str(body.get("password") or seed)
    cmd = (
        f"nmcli device wifi hotspot ssid {_shlex.quote(ssid)} "
        f"password {_shlex.quote(password)} ifname wlan0"
    )
    ok, out = _run_local(cmd, timeout=15)
    if not ok:
        return jsonify({"ok": False, "error": out[:240]}), 500
    return jsonify({"ok": True, "ssid": ssid, "password": password, "message": out.strip()[:240]})


@app.route("/api/wifi/hotspot/stop", methods=["POST"])
def api_wifi_hotspot_stop():
    """Bring down the hotspot connection profile."""
    ok, out = _run_local("nmcli connection down Hotspot", timeout=10)
    if not ok:
        return jsonify({"ok": False, "error": out[:240]}), 500
    return jsonify({"ok": True, "message": "Hotspot stopped"})


# Captive portal probes — iOS / Android / Windows poll these before showing
# a "no internet" warning. We 302 them to /wifi-setup.html so the user lands
# on the wifi config page automatically when connecting to the Reachy hotspot.
@app.route("/hotspot-detect.html")
@app.route("/generate_204")
@app.route("/ncsi.txt")
@app.route("/connecttest.txt")
def captive_portal_redirect():
    from flask import redirect
    return redirect("/wifi-setup.html", code=302)


@app.route("/wifi-setup.html")
def wifi_setup_page():
    """Minimal standalone wifi setup page served locally on :8090.
    Used when no wifi is configured and the Pi is in hotspot mode.
    Not dependent on eiffelai.io CDN or Tailscale."""
    return send_from_directory(str(CARE_DIR / "resources" / "dashboard"), "wifi-setup.html")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    CMD_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Reachy Care Controller starting on http://0.0.0.0:%d", PORT)
    print(f"\n  Reachy Care Controller: http://0.0.0.0:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)
