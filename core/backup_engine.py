import sqlite3
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List

from config import (
    LIVE_CONVERSATIONS_DIR,
    LIVE_BRAIN_DIR,
    LIVE_ANNOTATIONS_DIR,
    LIVE_SUMMARIES_DB,
    BACKUP_CONVERSATIONS_DIR,
    BACKUP_BRAIN_DIR,
    BACKUP_ANNOTATIONS_DIR,
    BACKUP_CATALOG_DB,
    DAEMON_HEARTBEAT_FILE
)
from core.classifier import classify_session
from core.file_ops import copy_file_robust, backup_sqlite_db_safe, sync_directory_tree

logger = logging.getLogger(__name__)

def sync_live_to_backup() -> Dict[str, Any]:
    """
    Performs a hardened, incremental synchronization:
    1. Detects new or modified conversations in live Antigravity.
    2. Snapshots updated .db files safely using SQLite online backup (handling WAL mode).
    3. Incrementally synchronizes brain/ folders (skipping identical files, clearing read-only Git objects).
    4. Upserts records into backup_catalog.db using WAL mode to prevent locking conflicts.
    5. Detects evicted sessions (files purged from live) and marks them in the catalog.
    6. Writes a heartbeat timestamp for the dashboard status monitor.
    """
    if not LIVE_SUMMARIES_DB.exists():
        return {"success": False, "error": "Live conversation_summaries.db not found."}

    now_utc = datetime.now(timezone.utc).isoformat()
    synced_dbs = 0
    synced_brains = 0
    errors: List[str] = []

    # 1. Mirror pinned annotations
    pinned_ids = set()
    if LIVE_ANNOTATIONS_DIR.exists():
        BACKUP_ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)
        for f in LIVE_ANNOTATIONS_DIR.glob("*.pbtxt"):
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
                if "pinned:true" in txt or "pinned: true" in txt:
                    pinned_ids.add(f.stem)
                copy_file_robust(f, BACKUP_ANNOTATIONS_DIR / f.name)
            except Exception as e:
                logger.debug(f"Annotation copy warning for {f.name}: {e}")

    live_db_files = set(f.stem for f in LIVE_CONVERSATIONS_DIR.glob("*.db")) if LIVE_CONVERSATIONS_DIR.exists() else set()

    # 2. Read live summaries in read-only mode to prevent lock contention with live Antigravity
    try:
        live_uri = f"file:{LIVE_SUMMARIES_DB.resolve().as_posix()}?mode=ro"
        live_conn = sqlite3.connect(live_uri, uri=True, timeout=10.0)
        try:
            live_cur = live_conn.cursor()
            live_cur.execute("SELECT * FROM conversation_summaries")
            live_cols = [d[0] for d in live_cur.description]
            live_rows = live_cur.fetchall()
        finally:
            live_conn.close()
    except Exception as e:
        return {"success": False, "error": f"Failed reading live conversation_summaries.db: {e}"}

    # 3. Read catalog map and perform synchronization
    cat_conn = sqlite3.connect(BACKUP_CATALOG_DB, timeout=10.0)
    try:
        cat_conn.execute("PRAGMA journal_mode=WAL;")
        cat_conn.execute("PRAGMA busy_timeout=10000;")
        cat_cur = cat_conn.cursor()

        cat_cur.execute("SELECT conversation_id, last_modified_time, has_db_file, has_brain_folder FROM backed_up_conversations")
        cat_map = {r[0]: (r[1], bool(r[2]), bool(r[3])) for r in cat_cur.fetchall()}

        for r in live_rows:
            row_dict = dict(zip(live_cols, r))
            cid = row_dict["conversation_id"]
            live_lmt = row_dict.get("last_modified_time", "")
            step_count = row_dict.get("step_count", 0)
            title = row_dict.get("title", "")
            preview = row_dict.get("preview", "")
            ws = row_dict.get("workspace_uris", "")
            proj = row_dict.get("project_id", "")
            status = row_dict.get("status", "")
            raw_summary = row_dict.get("raw_summary", None)

            is_pinned = cid in pinned_ids
            has_live_db = cid in live_db_files
            src_brain = LIVE_BRAIN_DIR / cid
            src_brain_exists = src_brain.exists()

            # Check if backup needs update
            cached = cat_map.get(cid)
            has_backup_db_cached = cached[1] if cached else False
            has_backup_brain_cached = cached[2] if cached else False

            needs_sync = False
            if not cached:
                needs_sync = True
            elif (cached[0] != live_lmt) or (has_live_db and not has_backup_db_cached) or (src_brain_exists and not has_backup_brain_cached):
                needs_sync = True

            if needs_sync:
                has_backup_db = False
                has_backup_brain = False

                # 3a. Backup .db if exists in live (handles SQLite WAL mode safely)
                if has_live_db:
                    src_db = LIVE_CONVERSATIONS_DIR / f"{cid}.db"
                    dst_db = BACKUP_CONVERSATIONS_DIR / f"{cid}.db"
                    try:
                        if backup_sqlite_db_safe(src_db, dst_db):
                            synced_dbs += 1
                            has_backup_db = True
                    except Exception as e:
                        errors.append(f"DB backup failed for {cid}: {e}")
                elif cached and cached[1]:
                    has_backup_db = True

                # 3b. Sync brain folder if exists in live (handles Git read-only objects safely)
                dst_brain = BACKUP_BRAIN_DIR / cid
                if src_brain_exists:
                    try:
                        sync_res = sync_directory_tree(src_brain, dst_brain)
                        if sync_res.get("copied", 0) > 0:
                            synced_brains += 1
                        has_backup_brain = dst_brain.exists()
                        if sync_res.get("errors"):
                            errors.extend(sync_res["errors"])
                    except Exception as e:
                        errors.append(f"Brain sync failed for {cid}: {e}")
                elif dst_brain.exists():
                    has_backup_brain = True

                category = classify_session(title, preview, step_count, is_pinned)
                retention_status = "complete_active" if has_backup_db else "metadata_only_evicted"
                is_evicted = 0 if has_live_db else 1

                cat_cur.execute("""
                INSERT OR REPLACE INTO backed_up_conversations (
                    conversation_id, title, preview, step_count, last_modified_time,
                    last_user_input_time, workspace_uris, project_id, status,
                    category, retention_status, has_db_file, has_brain_folder,
                    is_pinned, is_evicted_from_live, first_backed_up_time,
                    last_synced_time, raw_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cid, title, preview, step_count, live_lmt,
                    row_dict.get("last_user_input_time", ""), ws, proj, status,
                    category, retention_status, int(has_backup_db), int(has_backup_brain),
                    int(is_pinned), is_evicted, now_utc,
                    now_utc, raw_summary
                ))

        # 4. Check for sessions that were live before but now evicted
        for cid, cached_val in cat_map.items():
            last_lmt, had_db, had_brain = cached_val
            if had_db and cid not in live_db_files:
                cat_cur.execute("""
                UPDATE backed_up_conversations
                SET is_evicted_from_live = 1
                WHERE conversation_id = ?
                """, (cid,))

        cat_conn.commit()
    finally:
        cat_conn.close()

    # 5. Update heartbeat file
    DAEMON_HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DAEMON_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "last_heartbeat": now_utc,
            "status": "active",
            "last_synced_dbs": synced_dbs,
            "last_synced_brains": synced_brains,
            "errors": errors[:5] if errors else []
        }, f, indent=2)

    return {
        "success": True,
        "synced_at": now_utc,
        "synced_dbs": synced_dbs,
        "synced_brains": synced_brains,
        "errors": errors
    }
