import time
import sys
import json
import os
import atexit
import signal
import ctypes
from datetime import datetime, timezone
from pathlib import Path

from config import (
    POLL_INTERVAL_SECONDS,
    DAEMON_HEARTBEAT_FILE,
    DAEMON_PID_FILE
)
from core.backup_engine import sync_live_to_backup

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
        # Fallback to standard os check
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
                print(f"[{datetime.now().isoformat()}] Another daemon instance is already active (PID: {old_pid}). Exiting.")
                return False
            else:
                print(f"[{datetime.now().isoformat()}] Removing stale daemon PID file ({old_pid}).")
        except Exception:
            pass

    try:
        DAEMON_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Warning: Could not write PID file: {e}")
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
    print(f"\n[{datetime.now().isoformat()}] Shutdown signal received (signal {signum}). Gracefully stopping daemon...")
    running = False

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

    print(f"[{datetime.now().isoformat()}] Starting Antigravity continuous backup daemon (PID: {os.getpid()}, Interval: {interval}s)...")

    consecutive_errors = 0

    while running:
        try:
            res = sync_live_to_backup()
            now_iso = datetime.now(timezone.utc).isoformat()
            if res.get("success"):
                consecutive_errors = 0
                dbs = res.get("synced_dbs", 0)
                brains = res.get("synced_brains", 0)
                if dbs > 0 or brains > 0:
                    print(f"[{now_iso}] Synced {dbs} updated DBs and {brains} brain folders.")
                if res.get("errors"):
                    print(f"[{now_iso}] Non-fatal sync notices ({len(res['errors'])} items): {res['errors'][:3]}")
            else:
                consecutive_errors += 1
                print(f"[{now_iso}] Sync warning (attempt {consecutive_errors}): {res.get('error')}")

        except Exception as e:
            consecutive_errors += 1
            now_iso = datetime.now(timezone.utc).isoformat()
            print(f"[{now_iso}] Uncaught error during sync cycle: {e}")
            # Ensure heartbeat reflects warning state
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

    print(f"[{datetime.now().isoformat()}] Antigravity backup daemon stopped cleanly.")

if __name__ == "__main__":
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else POLL_INTERVAL_SECONDS
    run_daemon(interval)
