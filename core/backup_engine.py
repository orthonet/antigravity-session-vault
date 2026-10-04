import sqlite3
import json
import logging
import re
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

def sync_live_to_backup(auto_protect_pinned: bool = True) -> Dict[str, Any]:
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

    # 1. Mirror pinned and unpinned annotations
    pinned_ids = set()
    unpinned_ids = set()
    if LIVE_ANNOTATIONS_DIR.exists():
        BACKUP_ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)
        for f in LIVE_ANNOTATIONS_DIR.glob("*.pbtxt"):
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
                if re.search(r'\bpinned\s*:\s*true\b', txt, re.IGNORECASE):
                    pinned_ids.add(f.stem)
                elif re.search(r'\bpinned\s*:\s*false\b', txt, re.IGNORECASE):
                    unpinned_ids.add(f.stem)
                copy_file_robust(f, BACKUP_ANNOTATIONS_DIR / f.name)
            except Exception as e:
                logger.debug(f"Annotation copy warning for {f.name}: {e}")

    # Preserve pinned status for archived/evicted sessions whose annotations only exist in backup
    if BACKUP_ANNOTATIONS_DIR.exists():
        for f in BACKUP_ANNOTATIONS_DIR.glob("*.pbtxt"):
            if f.stem not in pinned_ids and f.stem not in unpinned_ids:
                live_counterpart = LIVE_ANNOTATIONS_DIR / f.name
                if not live_counterpart.exists():
                    try:
                        txt = f.read_text(encoding="utf-8", errors="ignore")
                        if re.search(r'\bpinned\s*:\s*true\b', txt, re.IGNORECASE):
                            pinned_ids.add(f.stem)
                        elif re.search(r'\bpinned\s*:\s*false\b', txt, re.IGNORECASE):
                            unpinned_ids.add(f.stem)
                    except Exception:
                        pass

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

        cat_cur.execute("SELECT conversation_id, last_modified_time, has_db_file, has_brain_folder, retention_status, is_pinned FROM backed_up_conversations")
        cat_map = {r[0]: (r[1], bool(r[2]), bool(r[3]), r[4], bool(r[5])) for r in cat_cur.fetchall()}

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

            cached = cat_map.get(cid)
            has_backup_db_cached = cached[1] if cached else False
            has_backup_brain_cached = cached[2] if cached else False
            cached_ret = cached[3] if cached and len(cached) > 3 else None
            cached_pinned = cached[4] if cached and len(cached) > 4 else False

            if cid in pinned_ids:
                is_pinned = True
            elif cid in unpinned_ids:
                is_pinned = False
            else:
                is_pinned = cached_pinned

            has_live_db = cid in live_db_files
            src_brain = LIVE_BRAIN_DIR / cid
            src_brain_exists = src_brain.exists()

            lmt_changed = (cached is None) or (cached[0] != live_lmt)
            pin_changed = (cached is None) or (is_pinned != cached_pinned)
            db_missing = has_live_db and not has_backup_db_cached
            brain_missing = src_brain_exists and not has_backup_brain_cached

            needs_sync = lmt_changed or pin_changed or db_missing or brain_missing

            if needs_sync:
                has_backup_db = False
                has_backup_brain = False

                # 3a. Backup .db if exists in live (handles SQLite WAL mode safely)
                if has_live_db:
                    if lmt_changed or db_missing:
                        src_db = LIVE_CONVERSATIONS_DIR / f"{cid}.db"
                        dst_db = BACKUP_CONVERSATIONS_DIR / f"{cid}.db"
                        try:
                            if backup_sqlite_db_safe(src_db, dst_db):
                                synced_dbs += 1
                                has_backup_db = True
                        except Exception as e:
                            errors.append(f"DB backup failed for {cid}: {e}")
                    else:
                        has_backup_db = has_backup_db_cached
                elif cached and cached[1]:
                    has_backup_db = True

                # 3b. Sync brain folder if exists in live (handles Git read-only objects safely)
                dst_brain = BACKUP_BRAIN_DIR / cid
                if src_brain_exists:
                    if lmt_changed or brain_missing:
                        try:
                            sync_res = sync_directory_tree(src_brain, dst_brain)
                            if sync_res.get("copied", 0) > 0:
                                synced_brains += 1
                            has_backup_brain = dst_brain.exists()
                            if sync_res.get("errors"):
                                errors.extend(sync_res["errors"])
                        except Exception as e:
                            errors.append(f"Brain sync failed for {cid}: {e}")
                    else:
                        has_backup_brain = has_backup_brain_cached
                elif dst_brain.exists():
                    has_backup_brain = True

                category = classify_session(title, preview, step_count, is_pinned)
                if has_live_db:
                    retention_status = "complete_active"
                    is_evicted = 0
                elif cached_ret == "imported_from_offsite":
                    retention_status = "imported_from_offsite"
                    is_evicted = 1
                elif has_backup_db:
                    retention_status = "safeguarded_evicted"
                    is_evicted = 1
                else:
                    retention_status = "metadata_only_evicted"
                    is_evicted = 1

                cat_cur.execute("""
                INSERT INTO backed_up_conversations (
                    conversation_id, title, preview, step_count, last_modified_time,
                    last_user_input_time, workspace_uris, project_id, status,
                    category, retention_status, has_db_file, has_brain_folder,
                    is_pinned, is_evicted_from_live, first_backed_up_time,
                    last_synced_time, raw_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    title = excluded.title,
                    preview = excluded.preview,
                    step_count = excluded.step_count,
                    last_modified_time = excluded.last_modified_time,
                    last_user_input_time = excluded.last_user_input_time,
                    workspace_uris = excluded.workspace_uris,
                    project_id = excluded.project_id,
                    status = excluded.status,
                    category = excluded.category,
                    retention_status = excluded.retention_status,
                    has_db_file = excluded.has_db_file,
                    has_brain_folder = excluded.has_brain_folder,
                    is_pinned = excluded.is_pinned,
                    is_evicted_from_live = excluded.is_evicted_from_live,
                    last_synced_time = excluded.last_synced_time,
                    raw_summary = excluded.raw_summary
                """, (
                    cid, title, preview, step_count, live_lmt,
                    row_dict.get("last_user_input_time", ""), ws, proj, status,
                    category, retention_status, int(has_backup_db), int(has_backup_brain),
                    int(is_pinned), is_evicted, now_utc,
                    now_utc, raw_summary
                ))

        # 4. Check for sessions that were live before but now evicted, or evicted sessions whose pin changed
        live_cids = set(r[live_cols.index("conversation_id")] for r in live_rows) if live_rows and "conversation_id" in live_cols else set()
        for cid, cached_val in cat_map.items():
            last_lmt, had_db, had_brain = cached_val[:3]
            cached_pinned = cached_val[4] if len(cached_val) > 4 else False
            if had_db and cid not in live_db_files:
                cat_cur.execute("""
                UPDATE backed_up_conversations
                SET is_evicted_from_live = 1,
                    retention_status = CASE 
                        WHEN retention_status = 'complete_active' THEN 'safeguarded_evicted'
                        ELSE retention_status 
                    END
                WHERE conversation_id = ?
                """, (cid,))

            if cid not in live_cids:
                if cid in pinned_ids:
                    is_pinned_now = True
                elif cid in unpinned_ids:
                    is_pinned_now = False
                else:
                    is_pinned_now = cached_pinned

                if is_pinned_now != cached_pinned:
                    cat_cur.execute("""
                    UPDATE backed_up_conversations
                    SET is_pinned = ?,
                        category = CASE WHEN ? = 1 THEN 'pinned' ELSE category END,
                        last_synced_time = ?
                    WHERE conversation_id = ?
                    """, (int(is_pinned_now), int(is_pinned_now), now_utc, cid))

        cat_conn.commit()
    finally:
        cat_conn.close()

    # 5. Vault Pin Sentry Protection: Auto-resurrect evicted pinned sessions & keep alive
    pin_sentry_res = {}
    if auto_protect_pinned:
        try:
            from core.pin_sentry import run_pin_sentry
            pin_sentry_res = run_pin_sentry(
                catalog_db_path=BACKUP_CATALOG_DB,
                live_summaries_path=LIVE_SUMMARIES_DB,
                live_conv_dir=LIVE_CONVERSATIONS_DIR
            )
        except Exception as pe:
            errors.append(f"Pin Sentry execution notice: {pe}")

    return {
        "success": True,
        "synced_at": now_utc,
        "synced_dbs": synced_dbs,
        "synced_brains": synced_brains,
        "pin_sentry": pin_sentry_res,
        "errors": errors
    }
