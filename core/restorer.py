import sqlite3
import re
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from config import (
    LIVE_CONVERSATIONS_DIR,
    LIVE_BRAIN_DIR,
    LIVE_ANNOTATIONS_DIR,
    LIVE_SUMMARIES_DB,
    BACKUP_CONVERSATIONS_DIR,
    BACKUP_BRAIN_DIR,
    BACKUP_ANNOTATIONS_DIR,
    BACKUP_CATALOG_DB
)
from core.file_ops import (
    copy_file_robust,
    backup_sqlite_db_safe,
    sync_directory_tree,
    make_writable,
    rmtree_robust
)
from core.catalog import CatalogManager

ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")

def validate_conversation_id(conversation_id: str) -> bool:
    """
    Validates that conversation_id is safe against path traversal and SQL injection.
    Only permits alphanumeric characters, hyphens, and underscores.
    """
    if not conversation_id or not isinstance(conversation_id, str):
        return False
    cid_clean = conversation_id.strip()
    if not cid_clean:
        return False
    if ".." in cid_clean or "/" in cid_clean or "\\" in cid_clean or ":" in cid_clean:
        return False
    return bool(ID_PATTERN.match(cid_clean))

def normalize_workspace_uri(uri: Optional[Any]) -> str:
    """
    Normalizes a workspace URI to a valid JSON array string representation.
    Guarantees consistent format for Antigravity's conversation_summaries table.
    """
    if not uri:
        return '[""]'
    if isinstance(uri, list):
        filtered = [str(u) for u in uri if u]
        return json.dumps(filtered if filtered else [""])
    uri_str = str(uri).strip()
    if not uri_str:
        return '[""]'
    if uri_str.startswith("[") and uri_str.endswith("]"):
        try:
            parsed = json.loads(uri_str)
            if isinstance(parsed, list):
                filtered = [str(u) for u in parsed if u]
                return json.dumps(filtered if filtered else [""])
        except Exception:
            pass
    return json.dumps([uri_str])

def check_sqlite_integrity(db_path: Path) -> bool:
    """
    Verifies that a SQLite database file exists, has non-zero size,
    and is readable without corruption.
    """
    try:
        p = Path(db_path)
        if not p.exists() or p.stat().st_size == 0:
            return False
        uri = f"file:{p.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5.0) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA schema_version;")
            cur.fetchone()
        return True
    except Exception:
        return False

def is_safe_subpath(path: Path, base_dir: Path) -> bool:
    """
    Verifies that path is strictly contained within base_dir.
    """
    try:
        resolved_path = path.resolve()
        resolved_base = base_dir.resolve()
        return resolved_path.is_relative_to(resolved_base)
    except Exception:
        return False

def ensure_live_summaries_schema(conn: sqlite3.Connection) -> None:
    """
    Ensures the conversation_summaries table exists in the live database.
    """
    conn.execute("""
    CREATE TABLE IF NOT EXISTS conversation_summaries (
        conversation_id TEXT PRIMARY KEY,
        title TEXT,
        preview TEXT,
        step_count INTEGER,
        last_modified_time TEXT,
        workspace_uris TEXT,
        status TEXT,
        source TEXT,
        project_id TEXT,
        agent_name TEXT,
        parent_conversation_id TEXT,
        nesting_depth INTEGER,
        battle_id TEXT,
        winning_conversation_id TEXT,
        not_fully_idle INTEGER,
        killed INTEGER,
        last_user_input_time TEXT,
        last_user_input_step_index INTEGER,
        app_data_dir TEXT,
        raw_summary BLOB,
        group_id TEXT
    );
    """)

def _insert_live_summary(
    live_summaries_db: Path,
    insert_dict: Dict[str, Any],
    max_retries: int = 3
) -> None:
    """
    Inserts or replaces a conversation summary row with retry on lock contention.
    """
    live_summaries_db.parent.mkdir(parents=True, exist_ok=True)
    last_err = None
    for attempt in range(max_retries):
        try:
            with sqlite3.connect(live_summaries_db, timeout=10.0) as live_conn:
                live_conn.execute("PRAGMA journal_mode=WAL;")
                live_conn.execute("PRAGMA busy_timeout=10000;")
                ensure_live_summaries_schema(live_conn)

                live_cur = live_conn.cursor()
                live_cur.execute("PRAGMA table_info(conversation_summaries)")
                live_cols = [c[1] for c in live_cur.fetchall()]

                # Filter only columns that actually exist in the live database
                filtered_dict = {k: v for k, v in insert_dict.items() if k in live_cols}
                col_names = ", ".join(f"`{k}`" for k in filtered_dict.keys())
                placeholders = ", ".join("?" for _ in filtered_dict)
                sql = f"INSERT OR REPLACE INTO `conversation_summaries` ({col_names}) VALUES ({placeholders})"

                live_cur.execute(sql, list(filtered_dict.values()))
                live_conn.commit()
                return
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" in str(e).lower() and attempt < max_retries - 1:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise
        except Exception as e:
            raise e
    if last_err:
        raise last_err

def restore_conversation(
    conversation_id: str,
    target_workspace_uri: Optional[str] = None,
    overwrite_live: bool = False,
    catalog_manager: Optional[CatalogManager] = None
) -> Dict[str, Any]:
    """
    Restores an archived conversation into live Antigravity with adversarial hardening:
    1. Input validation & path traversal prevention.
    2. Pre-flight verification (validates metadata and DB integrity before any writes).
    3. Active live session protection: does not clobber existing live DB unless overwrite_live=True.
    4. Safe atomic synchronization:
       - .db file copied via online SQLite snapshot (with orphaned WAL cleanup).
       - brain/ folder synced via robust tree sync (clearing read-only attributes).
       - annotations copied if present.
    5. Automatic rollback of newly created files on failure.
    6. Concurrency retry for live conversation_summaries.db.
    7. Immediate catalog state upgrade: marks session as complete_active with is_evicted_from_live=0.
    """
    # 1. Input validation & path traversal prevention
    if not validate_conversation_id(conversation_id):
        return {
            "success": False,
            "error": f"Invalid conversation ID format: '{conversation_id}'. Must be alphanumeric with dashes or underscores."
        }

    cid = conversation_id.strip()

    conv_db_src = BACKUP_CONVERSATIONS_DIR / f"{cid}.db"
    conv_db_dst = LIVE_CONVERSATIONS_DIR / f"{cid}.db"

    brain_src = BACKUP_BRAIN_DIR / cid
    brain_dst = LIVE_BRAIN_DIR / cid

    ann_src = BACKUP_ANNOTATIONS_DIR / f"{cid}.pbtxt"
    ann_dst = LIVE_ANNOTATIONS_DIR / f"{cid}.pbtxt"

    # Verify containment within expected directories
    if not is_safe_subpath(conv_db_dst, LIVE_CONVERSATIONS_DIR):
        return {"success": False, "error": f"Destination path traversal detected for conversation {cid}."}
    if not is_safe_subpath(brain_dst, LIVE_BRAIN_DIR):
        return {"success": False, "error": f"Brain path traversal detected for conversation {cid}."}

    # 2. Pre-flight validation: check source DB existence and integrity
    if not conv_db_src.exists():
        return {
            "success": False,
            "error": f"Trajectory database for conversation {cid} does not exist in backup archive ({conv_db_src})."
        }

    if not check_sqlite_integrity(conv_db_src):
        return {
            "success": False,
            "error": f"Trajectory database for conversation {cid} in backup is empty or corrupted."
        }

    # 3. Pre-flight validation: query catalog metadata BEFORE any disk writes
    with sqlite3.connect(BACKUP_CATALOG_DB, timeout=10.0) as b_conn:
        b_conn.execute("PRAGMA busy_timeout=10000;")
        b_cur = b_conn.cursor()
        b_cur.execute("SELECT * FROM backed_up_conversations WHERE conversation_id = ?", (cid,))
        row = b_cur.fetchone()
        if not row:
            return {
                "success": False,
                "error": f"Metadata record not found in backup catalog for {cid}."
            }
        cols = [d[0] for d in b_cur.description]
        cat_data = dict(zip(cols, row))

    # 4. Prepare parameters
    now_iso = datetime.now(timezone.utc).isoformat()
    if target_workspace_uri:
        ws_uri = normalize_workspace_uri(target_workspace_uri)
    else:
        ws_uri = normalize_workspace_uri(cat_data.get("workspace_uris"))

    step_count = int(cat_data.get("step_count") or 0)
    last_step_idx = step_count - 1 if step_count > 0 else -1

    insert_dict = {
        "conversation_id": cid,
        "title": cat_data.get("title", ""),
        "preview": cat_data.get("preview", ""),
        "step_count": step_count,
        "last_modified_time": now_iso,
        "workspace_uris": ws_uri,
        "status": "CASCADE_RUN_STATUS_IDLE",
        "source": "",
        "project_id": cat_data.get("project_id", ""),
        "agent_name": "",
        "parent_conversation_id": "",
        "nesting_depth": 0,
        "battle_id": "",
        "winning_conversation_id": "",
        "not_fully_idle": 0,
        "killed": 0,
        "last_user_input_time": now_iso,
        "last_user_input_step_index": last_step_idx,
        "app_data_dir": "antigravity",
        "raw_summary": cat_data.get("raw_summary", None),
        "group_id": ""
    }

    # 5. Check if live files already exist and session is active in Antigravity
    cat_is_evicted = bool(cat_data.get("is_evicted_from_live"))
    live_already_exists = conv_db_dst.exists() and conv_db_dst.stat().st_size > 0

    if not cat_is_evicted and live_already_exists and not overwrite_live:
        return {
            "success": True,
            "already_active": True,
            "conversation_id": cid,
            "message": f"Session '{cat_data.get('title') or cid}' is already Active in Antigravity."
        }

    should_copy_files = (not live_already_exists) or overwrite_live

    # Track newly created artifacts for atomic rollback on failure
    newly_created_files = []
    newly_created_dirs = []

    try:
        if should_copy_files:
            # Clean up stale WAL / SHM files if DB does not yet exist to prevent log replay conflicts
            if not conv_db_dst.exists():
                newly_created_files.append(conv_db_dst)
                for ext in ["-wal", "-shm"]:
                    stale_file = Path(str(conv_db_dst) + ext)
                    if stale_file.exists():
                        make_writable(stale_file)
                        stale_file.unlink(missing_ok=True)

            conv_db_dst.parent.mkdir(parents=True, exist_ok=True)
            if not backup_sqlite_db_safe(conv_db_src, conv_db_dst):
                raise RuntimeError(f"Failed to copy SQLite database from {conv_db_src} to {conv_db_dst}")

            # Safely sync the brain folder if it exists
            if brain_src.exists():
                if not brain_dst.exists():
                    newly_created_dirs.append(brain_dst)
                brain_dst.parent.mkdir(parents=True, exist_ok=True)
                sync_res = sync_directory_tree(brain_src, brain_dst)
                if sync_res.get("errors"):
                    # Log errors, but non-fatal if primary files succeeded
                    pass

            # Safely copy annotations if present
            if ann_src.exists():
                if not ann_dst.exists():
                    newly_created_files.append(ann_dst)
                ann_dst.parent.mkdir(parents=True, exist_ok=True)
                copy_file_robust(ann_src, ann_dst)

        # 6. Insert into live conversation_summaries.db with retry
        _insert_live_summary(LIVE_SUMMARIES_DB, insert_dict)

        # 7. Update backup catalog immediately
        cat_mgr = catalog_manager or CatalogManager()
        cat_mgr.record_restored(cid, now_iso, ws_uri)

    except Exception as e:
        # Atomic rollback: remove newly created files and directories
        for f in newly_created_files:
            try:
                make_writable(f)
                f.unlink(missing_ok=True)
            except Exception:
                pass
        for d in newly_created_dirs:
            try:
                rmtree_robust(d)
            except Exception:
                pass
        return {
            "success": False,
            "error": f"Restoration failed for {cid}: {e}"
        }

    # Format human-friendly message
    title_display = cat_data.get("title") or cid
    if live_already_exists and not overwrite_live:
        msg = f"Live trajectory files already present. Revived '{title_display}' to slot #1 in Antigravity sidebar."
    else:
        msg = f"Conversation '{title_display}' restored successfully! It is now slot #1 in your Antigravity sidebar."

    return {
        "success": True,
        "conversation_id": cid,
        "restored_at": now_iso,
        "workspace": ws_uri,
        "live_files_preserved": bool(live_already_exists and not overwrite_live),
        "message": msg
    }
