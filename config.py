import os
from pathlib import Path

# Paths to Live Antigravity environment
LIVE_BASE_DIR = Path.home() / ".gemini" / "antigravity"
LIVE_CONVERSATIONS_DIR = LIVE_BASE_DIR / "conversations"
LIVE_BRAIN_DIR = LIVE_BASE_DIR / "brain"
LIVE_ANNOTATIONS_DIR = LIVE_BASE_DIR / "annotations"
LIVE_SUMMARIES_DB = LIVE_BASE_DIR / "conversation_summaries.db"

# Paths to Backup Archive
BACKUP_BASE_DIR = Path(os.environ.get("AGY_BACKUP_DIR", "d:/Antigravity-Backup"))
BACKUP_CONVERSATIONS_DIR = BACKUP_BASE_DIR / "conversations"
BACKUP_BRAIN_DIR = BACKUP_BASE_DIR / "brain"
BACKUP_ANNOTATIONS_DIR = BACKUP_BASE_DIR / "annotations"
BACKUP_STATE_DIR = Path(os.environ.get("AGY_STATE_DIR", str(BACKUP_BASE_DIR / "state")))
BACKUP_CATALOG_DB = BACKUP_BASE_DIR / "backup_catalog.db"
DAEMON_HEARTBEAT_FILE = BACKUP_STATE_DIR / "daemon_heartbeat.json"
DAEMON_PID_FILE = BACKUP_STATE_DIR / "daemon.pid"
DAEMON_LOG_FILE = BACKUP_STATE_DIR / "daemon.log"

# App settings
POLL_INTERVAL_SECONDS = 30
PAGE_SIZE_DEFAULT = 25
LOGO_PATH = Path(__file__).parent / "antigravity_logo.svg"

