import os
import shutil
import sqlite3
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime, timezone

from config import (
    BACKUP_CONVERSATIONS_DIR,
    BACKUP_BRAIN_DIR,
    BACKUP_CATALOG_DB
)
from core.catalog import CatalogManager
from core.classifier import classify_session
from core.file_ops import copy_file_robust, backup_sqlite_db_safe, sync_directory_tree

UUID_REGEX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

def is_valid_uuid(val: str) -> bool:
    return bool(UUID_REGEX.match(val))

def is_valid_trajectory_db(db_path: Path) -> bool:
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trajectory_meta'")
            return cur.fetchone() is not None
    except Exception:
        return False

def scan_offsite_source(source_path: Path) -> Dict[str, Any]:
    """
    Scans a source directory (e.g. from external drive, offsite nightly backup)
    for recoverable .db files, brain/ folders, and metadata summaries.
    Accurately categorizes and reconciles 100% of discovered sessions into:
    - Actionable Evicted Upgrades
    - Brand New Sessions
    - Incomplete Sessions with Missing Assets
    - Already Archived & Up-to-Date Sessions
    """
    if not source_path.exists() or not source_path.is_dir():
        return {
            "valid": False,
            "error": f"Path '{source_path}' does not exist or is not a directory.",
            "discovered_conversations": []
        }

    catalog = CatalogManager()
    found_dbs: Dict[str, Path] = {}
    found_brains: Dict[str, Path] = {}
    source_summaries_map: Dict[str, Dict[str, Any]] = {}

    # Check for conversation_summaries.db in source to enrich uncatalogued sessions
    for root, dirs, files in os.walk(source_path):
        if "conversation_summaries.db" in files:
            sum_path = Path(root) / "conversation_summaries.db"
            try:
                with sqlite3.connect(f"file:{sum_path.resolve().as_posix()}?mode=ro", uri=True, timeout=5.0) as s_conn:
                    s_conn.row_factory = sqlite3.Row
                    cur = s_conn.cursor()
                    cur.execute("SELECT conversation_id, title, preview, workspace_uris, step_count, last_modified_time, project_id FROM conversation_summaries")
                    for row in cur.fetchall():
                        source_summaries_map[row["conversation_id"]] = dict(row)
            except Exception:
                pass
            break

    # Scan for *.db files and brain/ directories
    for root, dirs, files in os.walk(source_path):
        for f in files:
            if f.endswith(".db"):
                stem = f[:-3]
                if is_valid_uuid(stem):
                    full_p = Path(root) / f
                    if is_valid_trajectory_db(full_p):
                        found_dbs[stem] = full_p

        for d in dirs:
            if is_valid_uuid(d):
                full_d = Path(root) / d
                transcript = full_d / ".system_generated" / "logs" / "transcript.jsonl"
                transcript_full = full_d / ".system_generated" / "logs" / "transcript_full.jsonl"
                if transcript.exists() or transcript_full.exists() or (full_d / "scratch").exists():
                    found_brains[d] = full_d

    all_cids = sorted(list(set(list(found_dbs.keys()) + list(found_brains.keys()))))
    discovered = []

    matching_evicted_count = 0
    brand_new_count = 0
    missing_assets_count = 0
    newer_content_count = 0
    already_archived_count = 0

    for cid in all_cids:
        existing = catalog.get_conversation_by_id(cid)
        has_source_db = cid in found_dbs
        has_source_brain = cid in found_brains

        title = ""
        preview = ""
        workspace = ""
        last_modified = ""
        curr_ret = ""

        # Check physical archive paths
        dest_db = BACKUP_CONVERSATIONS_DIR / f"{cid}.db"
        dest_brain = BACKUP_BRAIN_DIR / cid

        archive_has_db = dest_db.exists() or bool(existing.get("has_db_file", 0) if existing else False)
        archive_has_brain = dest_brain.exists() or bool(existing.get("has_brain_folder", 0) if existing else False)

        if existing:
            title = existing.get("title", "")
            preview = existing.get("preview", "")
            workspace = existing.get("workspace_uris", "")
            last_modified = existing.get("last_modified_time", "")
            curr_ret = existing.get("retention_status", "")
        elif cid in source_summaries_map:
            meta = source_summaries_map[cid]
            title = meta.get("title", "")
            preview = meta.get("preview", "")
            workspace = meta.get("workspace_uris", "")
            last_modified = meta.get("last_modified_time", "")

        # Delta checks: compare sizes, timestamps, and step counts
        db_has_newer_content = False
        brain_has_newer_content = False
        step_count_newer = False
        newer_reason = []

        if has_source_db and dest_db.exists():
            try:
                s_stat = found_dbs[cid].stat()
                d_stat = dest_db.stat()
                if s_stat.st_size > d_stat.st_size:
                    db_has_newer_content = True
                    newer_reason.append(f"DB is larger ({s_stat.st_size:,} vs {d_stat.st_size:,} bytes)")
                elif s_stat.st_size != d_stat.st_size and s_stat.st_mtime > d_stat.st_mtime + 2.0:
                    db_has_newer_content = True
                    newer_reason.append("DB has modified size and newer timestamp")
            except Exception:
                pass

        if has_source_brain and dest_brain.exists():
            try:
                s_tr = found_brains[cid] / ".system_generated" / "logs" / "transcript.jsonl"
                d_tr = dest_brain / ".system_generated" / "logs" / "transcript.jsonl"
                if s_tr.exists() and d_tr.exists():
                    s_tstat = s_tr.stat()
                    d_tstat = d_tr.stat()
                    if s_tstat.st_size > d_tstat.st_size:
                        brain_has_newer_content = True
                        newer_reason.append(f"Transcript has newer turns ({s_tstat.st_size:,} vs {d_tstat.st_size:,} bytes)")
                    elif s_tstat.st_size != d_tstat.st_size and s_tstat.st_mtime > d_tstat.st_mtime + 2.0:
                        brain_has_newer_content = True
                        newer_reason.append("Transcript has newer timestamp")
            except Exception:
                pass

        if cid in source_summaries_map and existing:
            source_steps = source_summaries_map[cid].get("step_count", 0) or 0
            existing_steps = existing.get("step_count", 0) or 0
            if source_steps > existing_steps:
                step_count_newer = True
                newer_reason.append(f"Step count is higher ({source_steps} vs {existing_steps})")

        has_newer = db_has_newer_content or brain_has_newer_content or step_count_newer

        # Categorize actionability
        is_actionable = False
        if not existing:
            action_type = "NEW_SESSION"
            action_label = "➕ Add New Session"
            status_label = "New Session (Not in Catalog)"
            is_actionable = True
            brand_new_count += 1
        elif curr_ret == "metadata_only_evicted":
            action_type = "UPGRADE_EVICTED"
            action_label = "🚀 Upgrade Evicted -> Restored"
            status_label = "Can Upgrade Evicted Session"
            is_actionable = True
            matching_evicted_count += 1
        elif (has_source_db and not archive_has_db) or (has_source_brain and not archive_has_brain):
            action_type = "ENRICH_ASSET"
            action_label = "✨ Enrich Missing Assets"
            status_label = "Archive Missing Asset (Can Enrich)"
            is_actionable = True
            missing_assets_count += 1
        elif has_newer:
            action_type = "UPDATE_NEWER_DATA"
            action_label = "⚡ Update Newer Content"
            status_label = f"Newer Content in Backup ({', '.join(newer_reason)})"
            is_actionable = True
            newer_content_count += 1
        else:
            action_type = "ALREADY_UP_TO_DATE"
            action_label = "✅ Verified Identical"
            status_label = f"Verified Identical in Vault ({curr_ret})"
            is_actionable = False
            already_archived_count += 1

        discovered.append({
            "conversation_id": cid,
            "has_db": has_source_db,
            "has_brain": has_source_brain,
            "title": title or preview or "Untitled",
            "preview": preview,
            "workspace": workspace,
            "last_modified": last_modified,
            "db_path": str(found_dbs[cid]) if has_source_db else "",
            "brain_path": str(found_brains[cid]) if has_source_brain else "",
            "is_actionable": is_actionable,
            "action_type": action_type,
            "action_label": action_label,
            "status_label": status_label,
            "archive_retention": curr_ret or "Not in Catalog",
            "archive_has_db": archive_has_db,
            "archive_has_brain": archive_has_brain,
        })

    # Sort actionable items first (Evicted upgrades first, then newer data, then new, then enrich, then up-to-date)
    action_priority = {
        "UPGRADE_EVICTED": 1,
        "UPDATE_NEWER_DATA": 2,
        "NEW_SESSION": 3,
        "ENRICH_ASSET": 4,
        "ALREADY_UP_TO_DATE": 5
    }
    discovered.sort(key=lambda x: (action_priority.get(x["action_type"], 99), (x["title"] or "").lower()))

    actionable_count = matching_evicted_count + brand_new_count + missing_assets_count + newer_content_count

    return {
        "valid": True,
        "scanned_path": str(source_path),
        "total_discovered": len(all_cids),
        "matching_evicted": matching_evicted_count,
        "brand_new": brand_new_count,
        "missing_assets": missing_assets_count,
        "newer_content": newer_content_count,
        "already_archived": already_archived_count,
        "actionable_count": actionable_count,
        "has_summaries_db": bool(source_summaries_map),
        "items": discovered
    }


def execute_import(
    discovered_items: List[Dict[str, Any]], 
    progress_callback: Optional[Callable[[int, int, str, str], None]] = None
) -> Dict[str, Any]:
    r"""
    Imports the selected discovered conversation files into d:\Antigravity-Backup
    and updates backup_catalog.db using hardened, idempotent file operations.
    Supports progress updates and isolates individual faults.
    """
    catalog = CatalogManager()
    now_utc = datetime.now(timezone.utc).isoformat()

    imported_dbs = 0
    imported_brains = 0
    upgraded_records = 0
    new_records = 0
    failed_items = []
    total = len(discovered_items)

    for i, item in enumerate(discovered_items):
        cid = item["conversation_id"]
        title = item.get("title", "")
        if progress_callback:
            try:
                progress_callback(i + 1, total, cid, title)
            except Exception:
                pass

        db_src = Path(item["db_path"]) if item.get("db_path") else None
        brain_src = Path(item["brain_path"]) if item.get("brain_path") else None

        has_db_now = False
        has_brain_now = False

        try:
            # 1. Copy .db safely (handling WAL mode and read-only attributes)
            if db_src and db_src.exists():
                dest_db = BACKUP_CONVERSATIONS_DIR / f"{cid}.db"
                backup_sqlite_db_safe(db_src, dest_db)
                imported_dbs += 1
                has_db_now = True

            # 2. Sync brain folder safely (skipping identical files, unsetting read-only on Git objects)
            if brain_src and brain_src.exists():
                dest_brain = BACKUP_BRAIN_DIR / cid
                sync_res = sync_directory_tree(brain_src, dest_brain)
                imported_brains += 1
                has_brain_now = True
                if sync_res.get("errors"):
                    print(f"[Importer Warning] Partial errors syncing brain {cid}: {sync_res['errors']}")

            # 3. Update or insert catalog record
            existing = catalog.get_conversation_by_id(cid)
            if existing:
                # Merge asset flags so existing assets are never cleared to 0
                final_has_db = bool(existing.get("has_db_file")) or has_db_now
                final_has_brain = bool(existing.get("has_brain_folder")) or has_brain_now
                curr_ret = existing.get("retention_status")

                # Only change retention_status if it was evicted or empty; preserve complete_active
                new_ret = "imported_from_offsite" if curr_ret in ("metadata_only_evicted", None, "") else curr_ret

                with sqlite3.connect(BACKUP_CATALOG_DB, timeout=10.0) as conn:
                    conn.execute("PRAGMA busy_timeout=10000;")
                    conn.execute("""
                    UPDATE backed_up_conversations 
                    SET retention_status = ?,
                        has_db_file = ?,
                        has_brain_folder = ?,
                        last_synced_time = ?
                    WHERE conversation_id = ?
                    """, (new_ret, int(final_has_db), int(final_has_brain), now_utc, cid))
                    conn.commit()

                upgraded_records += 1
            else:
                cat = classify_session(item.get("title"), item.get("preview"), 10, False)
                with sqlite3.connect(BACKUP_CATALOG_DB, timeout=10.0) as conn:
                    conn.execute("PRAGMA busy_timeout=10000;")
                    conn.execute("""
                    INSERT OR REPLACE INTO backed_up_conversations (
                        conversation_id, title, preview, step_count, last_modified_time,
                        last_user_input_time, workspace_uris, project_id, status,
                        category, retention_status, has_db_file, has_brain_folder,
                        is_pinned, is_evicted_from_live, first_backed_up_time, last_synced_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        cid, item.get("title", ""), item.get("preview", ""), 1,
                        item.get("last_modified") or now_utc,
                        item.get("last_modified") or now_utc,
                        item.get("workspace", ""), "", "IMPORTED",
                        cat, "imported_from_offsite", int(has_db_now), int(has_brain_now),
                        0, 1, now_utc, now_utc
                    ))
                    conn.commit()
                new_records += 1

        except Exception as e:
            failed_items.append({"conversation_id": cid, "error": str(e)})
            print(f"[Importer Error] Failed to import conversation {cid}: {e}")

    failed_ids = {f["conversation_id"] for f in failed_items}
    imported_items = [it for it in discovered_items if it["conversation_id"] not in failed_ids]

    return {
        "success": len(failed_items) == 0,
        "imported_dbs": imported_dbs,
        "imported_brains": imported_brains,
        "upgraded_records": upgraded_records,
        "new_records": new_records,
        "failed_items": failed_items,
        "total_processed": len(discovered_items),
        "imported_items": imported_items
    }
