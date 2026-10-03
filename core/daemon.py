import time
import sys
import json
import os
import atexit
import signal
import ctypes
import subprocess
from typing import Dict, Any, Optional, Tuple, Union
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is in sys.path when executed directly as a script
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    POLL_INTERVAL_SECONDS,
    DAEMON_HEARTBEAT_FILE,
    DAEMON_PID_FILE,
    DAEMON_LOG_FILE,
    BACKUP_CATALOG_DB,
    LIVE_SUMMARIES_DB,
    LIVE_CONVERSATIONS_DIR
)
from core.backup_engine import sync_live_to_backup
from core.pin_sentry import run_pin_sentry, safe_print

running = True

def is_pid_running(pid: int) -> bool:
    """
    Checks whether a process with the given PID is currently active on Windows.
    """
    if pid <= 0:
        return False
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

def acquire_pid_lock() -> bool:
    """
    Ensures that only one daemon instance runs at a time.
    Returns True if lock acquired, False if another instance is active.
    """
    DAEMON_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DAEMON_PID_FILE.exists():
        try:
            old_pid = int(DAEMON_PID_FILE.read_text(encoding="utf-8").strip())
            if is_pid_running(old_pid):
                safe_print(f"[{datetime.now().isoformat()}] Another daemon instance is already active (PID: {old_pid}). Exiting.")
                return False
            else:
                safe_print(f"[{datetime.now().isoformat()}] Removing stale daemon PID file ({old_pid}).")
        except Exception:
            pass

    try:
        DAEMON_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception as e:
        safe_print(f"[{datetime.now().isoformat()}] Warning: Could not write PID file: {e}")
        return True

def release_pid_lock():
    """Removes the PID file upon daemon shutdown."""
    try:
        if DAEMON_PID_FILE.exists():
            current_pid = str(os.getpid())
            stored_pid = DAEMON_PID_FILE.read_text(encoding="utf-8").strip()
            if stored_pid == current_pid:
                DAEMON_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

def mark_heartbeat_stopped():
    """Updates the heartbeat file to 'stopped' state."""
    if DAEMON_HEARTBEAT_FILE.exists():
        try:
            with open(DAEMON_HEARTBEAT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["status"] = "stopped"
            with open(DAEMON_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

def signal_handler(signum, frame):
    global running
    safe_print(f"\n[{datetime.now().isoformat()}] Shutdown signal received (signal {signum}). Gracefully stopping daemon...")
    running = False

def start_daemon_process(interval: int = 30) -> Tuple[bool, Union[int, str]]:
    """
    Launches the background daemon as a detached subprocess on Windows.
    Returns (True, pid) on success, or (False, error_message) on failure.
    """
    # Check if already running
    if DAEMON_PID_FILE.exists():
        try:
            cur_pid = int(DAEMON_PID_FILE.read_text(encoding="utf-8").strip())
            if is_pid_running(cur_pid):
                return True, cur_pid
        except Exception:
            pass

    daemon_script = Path(__file__).resolve()
    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP = 0x00000200, DETACHED_PROCESS = 0x00000008
        creationflags = 0x00000200 | 0x00000008

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"

    DAEMON_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    try:
        log_f = open(DAEMON_LOG_FILE, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(daemon_script), str(interval)],
            cwd=str(PROJECT_ROOT),
            env=env,
            creationflags=creationflags,
            close_fds=True,
            stdout=log_f,
            stderr=subprocess.STDOUT
        )
        log_f.close()

        # Verification check: wait for process to initialize and record its first heartbeat
        start_wait = time.time()
        while time.time() - start_wait < 2.5:
            time.sleep(0.25)
            if not is_pid_running(proc.pid):
                break
            if DAEMON_HEARTBEAT_FILE.exists():
                try:
                    with open(DAEMON_HEARTBEAT_FILE, "r", encoding="utf-8") as f:
                        hb_data = json.load(f)
                    if hb_data.get("pid") == proc.pid and hb_data.get("status") == "active":
                        break
                except Exception:
                    pass

        if not is_pid_running(proc.pid):
            err_detail = "Process terminated immediately."
            if DAEMON_LOG_FILE.exists():
                try:
                    lines = DAEMON_LOG_FILE.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                    if lines:
                        err_detail = " ".join(lines[-3:])
                except Exception:
                    pass
            safe_print(f"Daemon process {proc.pid} died immediately: {err_detail}")
            return False, f"Daemon exited immediately: {err_detail}"

        # Disarm Popen.__del__ ResourceWarning for intentionally detached daemon
        proc.returncode = 0
        return True, proc.pid
    except Exception as e:
        safe_print(f"Failed to launch background daemon: {e}")
        return False, str(e)

def stop_daemon_process() -> bool:
    """
    Terminates the active background daemon process cleanly.
    """
    if not DAEMON_PID_FILE.exists():
        mark_heartbeat_stopped()
        return True

    try:
        pid = int(DAEMON_PID_FILE.read_text(encoding="utf-8").strip())
        if is_pid_running(pid):
            if sys.platform == "win32":
                # Use taskkill /PID /T /F for reliable termination on Windows
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
            else:
                os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)

        DAEMON_PID_FILE.unlink(missing_ok=True)
        mark_heartbeat_stopped()
        return True
    except Exception as e:
        safe_print(f"Error stopping daemon process: {e}")
        return False

def get_daemon_live_status() -> Tuple[str, str, Dict[str, Any]]:
    """
    Inspects PID file and heartbeat to accurately report daemon status.
    Returns: (status_label, detail_message, heartbeat_dict)
    """
    hb = {}
    if DAEMON_HEARTBEAT_FILE.exists():
        try:
            with open(DAEMON_HEARTBEAT_FILE, "r", encoding="utf-8") as f:
                hb = json.load(f)
        except Exception:
            pass

    hb_time = hb.get("last_heartbeat", "")
    last_sync_desc = ""
    if hb_time:
        try:
            dt = datetime.fromisoformat(hb_time)
            age_s = (datetime.now(timezone.utc) - dt).total_seconds()
            if age_s < 60:
                last_sync_desc = f"Synced {int(age_s)}s ago"
            elif age_s < 3600:
                last_sync_desc = f"Synced {int(age_s // 60)}m ago"
            else:
                last_sync_desc = f"Synced {int(age_s // 3600)}h ago"
        except Exception:
            pass

    if DAEMON_PID_FILE.exists():
        try:
            pid = int(DAEMON_PID_FILE.read_text(encoding="utf-8").strip())
            if is_pid_running(pid):
                hb_pid = hb.get("pid")
                if hb_pid == pid and hb_time:
                    try:
                        dt = datetime.fromisoformat(hb_time)
                        age_s = (datetime.now(timezone.utc) - dt).total_seconds()
                        if age_s < 120 and hb.get("status") in ("active", "starting"):
                            return ("🟢 Active", f"PID {pid} • {last_sync_desc}", hb)
                        else:
                            return ("🟡 Unresponsive", f"PID {pid} • Last heartbeat {int(age_s // 60)}m ago", hb)
                    except Exception:
                        pass
                if last_sync_desc:
                    return ("🟢 Active", f"PID {pid} • {last_sync_desc}", hb)
                return ("🟢 Active", f"PID {pid} • Initial sync in progress...", hb)
            else:
                # Stale PID file
                DAEMON_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    if last_sync_desc:
        return ("⚪ Inactive", f"{last_sync_desc} • Inactive", hb)

    return ("⚪ Inactive", "Daemon not running", hb)

def run_daemon(interval: int = POLL_INTERVAL_SECONDS):
    global running

    if not acquire_pid_lock():
        sys.exit(0)

    atexit.register(release_pid_lock)
    atexit.register(mark_heartbeat_stopped)

    # Register OS signals for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal_handler)

    safe_print(f"[{datetime.now().isoformat()}] Starting Antigravity continuous backup daemon (PID: {os.getpid()}, Interval: {interval}s)...")

    consecutive_errors = 0

    while running:
        try:
            # Run sync and sentry together
            res = sync_live_to_backup(auto_protect_pinned=True)
            sentry_res = res.get("pin_sentry", {})
            now_iso = datetime.now(timezone.utc).isoformat()

            if res.get("success"):
                consecutive_errors = 0
                dbs = res.get("synced_dbs", 0)
                brains = res.get("synced_brains", 0)
                if dbs > 0 or brains > 0:
                    safe_print(f"[{now_iso}] Synced {dbs} updated DBs and {brains} brain folders.")
                if sentry_res.get("auto_resurrected", 0) > 0 or sentry_res.get("refreshed", 0) > 0:
                    safe_print(f"[{now_iso}] Pin Sentry protected {sentry_res['protected_count']} pinned sessions: {sentry_res['auto_resurrected']} resurrected, {sentry_res['refreshed']} refreshed.")
                if res.get("errors"):
                    safe_print(f"[{now_iso}] Non-fatal sync notices ({len(res['errors'])} items): {res['errors'][:3]}")

                # Update heartbeat file
                try:
                    DAEMON_HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
                    with open(DAEMON_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                        json.dump({
                            "last_heartbeat": now_iso,
                            "status": "active",
                            "pid": os.getpid(),
                            "last_synced_dbs": dbs,
                            "last_synced_brains": brains,
                            "pin_sentry": sentry_res,
                            "errors": res.get("errors", [])[:5]
                        }, f, indent=2)
                except Exception:
                    pass
            else:
                consecutive_errors += 1
                safe_print(f"[{now_iso}] Sync warning (attempt {consecutive_errors}): {res.get('error')}")

        except Exception as e:
            consecutive_errors += 1
            now_iso = datetime.now(timezone.utc).isoformat()
            safe_print(f"[{now_iso}] Uncaught error during sync cycle: {e}")
            try:
                if DAEMON_HEARTBEAT_FILE.exists():
                    with open(DAEMON_HEARTBEAT_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["last_error"] = str(e)
                    data["last_error_time"] = now_iso
                    with open(DAEMON_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
            except Exception:
                pass

        # Responsive sleep allowing fast shutdown
        sleep_elapsed = 0
        while running and sleep_elapsed < interval:
            time.sleep(1)
            sleep_elapsed += 1

    safe_print(f"[{datetime.now().isoformat()}] Antigravity backup daemon stopped cleanly.")

if __name__ == "__main__":
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else POLL_INTERVAL_SECONDS
    run_daemon(interval)
