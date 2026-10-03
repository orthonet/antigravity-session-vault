import sys
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from config import (
    BACKUP_CATALOG_DB,
    LIVE_SUMMARIES_DB,
    LIVE_CONVERSATIONS_DIR
)
from core.catalog import CatalogManager
from core.restorer import restore_conversation

# Ensure Windows stdout handles emojis gracefully without crashing on cp1252
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def safe_print(msg: str):
    """Prints safely to stdout, falling back to ASCII if the terminal codec fails."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        try:
            ascii_msg = msg.encode("ascii", errors="replace").decode("ascii")
            print(ascii_msg, flush=True)
        except Exception:
            pass
    except Exception:
        pass

def run_pin_sentry(
    max_rank_threshold: int = 350,
    max_age_hours: float = 12.0,
    catalog_db_path: Path = BACKUP_CATALOG_DB,
    live_summaries_path: Path = LIVE_SUMMARIES_DB,
    live_conv_dir: Path = LIVE_CONVERSATIONS_DIR
) -> Dict[str, Any]:
    """
    Vault Pin Sentry Subsystem:
    Defends pinned high-value conversations against Antigravity's rolling eviction cap:
    1. Self-Healing Auto-Resurrection: If a pinned session has backup files in the Vault
       but is evicted, physically missing from live Antigravity, or pruned from
       conversation_summaries.db, automatically resurrects it.
    2. Preventive Keep-Alive Touch: If an active pinned session slides down past
       max_rank_threshold (default #350) or is older than max_age_hours (default 12h),
       updates its last_modified_time in conversation_summaries.db and touches its
       .db file mtime, keeping it pinned in the top 'Today' tier of the Antigravity sidebar.
    """
    result = {
        "protected_count": 0,
        "auto_resurrected": 0,
        "refreshed": 0,
        "errors": []
    }

    if not catalog_db_path.exists() or not live_summaries_path.exists():
        return result

    # 1. Query all pinned conversations from catalog
    try:
        with sqlite3.connect(catalog_db_path, timeout=10.0) as cat_conn:
            cat_conn.execute("PRAGMA busy_timeout=10000;")
            cur = cat_conn.cursor()
            cur.execute("""
            SELECT conversation_id, title, has_db_file, is_evicted_from_live, last_modified_time
            FROM backed_up_conversations
            WHERE is_pinned = 1
            """)
            pinned_sessions = cur.fetchall()
    except Exception as e:
        result["errors"].append(f"Catalog query failed: {e}")
        return result

    result["protected_count"] = len(pinned_sessions)
    if not pinned_sessions:
        return result

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    now_native = now_utc.strftime("%Y-%m-%d %H:%M:%S.%f+00:00")
    now_ts = now_utc.timestamp()

    cat_mgr = CatalogManager(catalog_db_path)

    for cid, title, has_db_file, is_evicted, lmt in pinned_sessions:
        title_str = title or cid
        live_db_file = live_conv_dir / f"{cid}.db"
        live_file_exists = live_db_file.exists() and live_db_file.stat().st_size > 0

        # Check existence in live conversation_summaries.db
        in_summaries = False
        live_lmt_str = None
        try:
            with sqlite3.connect(live_summaries_path, timeout=5.0) as sum_conn:
                sum_conn.execute("PRAGMA busy_timeout=5000;")
                s_cur = sum_conn.cursor()
                s_cur.execute(
                    "SELECT last_modified_time FROM conversation_summaries WHERE conversation_id = ?",
                    (cid,)
                )
                s_row = s_cur.fetchone()
                if s_row:
                    in_summaries = True
                    live_lmt_str = s_row[0]
        except Exception as e:
            # If summary table locked, assume present to avoid false-positive duplicate restores
            in_summaries = True

        # Scenario A: Session is evicted, missing from disk, or missing from summaries -> Auto-Resurrect
        needs_resurrection = (is_evicted or not live_file_exists or not in_summaries) and bool(has_db_file)
        if needs_resurrection:
            try:
                res_res = restore_conversation(cid, overwrite_live=True, catalog_manager=cat_mgr)
                if res_res.get("success"):
                    result["auto_resurrected"] += 1
                    safe_print(f"[{now_iso}] [Pin Sentry] 🛡️ Auto-resurrected pinned session '{title_str}' into live Antigravity.")
                else:
                    result["errors"].append(f"Failed to auto-resurrect {cid}: {res_res.get('error')}")
            except Exception as e:
                result["errors"].append(f"Auto-resurrect exception for {cid}: {e}")
            continue

        # Scenario B: Session is currently active and summarized in live Antigravity -> Preventive Keep-Alive
        if live_file_exists and in_summaries and live_lmt_str:
            try:
                with sqlite3.connect(live_summaries_path, timeout=10.0) as sum_conn:
                    sum_conn.execute("PRAGMA busy_timeout=10000;")
                    sum_cur = sum_conn.cursor()

                    # Calculate rank (how many sessions in live summaries are newer)
                    sum_cur.execute(
                        "SELECT COUNT(*) FROM conversation_summaries WHERE last_modified_time > ?",
                        (live_lmt_str,)
                    )
                    rank = sum_cur.fetchone()[0]

                    needs_refresh = rank >= max_rank_threshold
                    if not needs_refresh:
                        try:
                            clean_ts = live_lmt_str.replace(" ", "T")
                            parsed_dt = datetime.fromisoformat(clean_ts)
                            if parsed_dt.tzinfo is None:
                                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
                            age_hours = (now_utc - parsed_dt).total_seconds() / 3600.0
                            if age_hours >= max_age_hours:
                                needs_refresh = True
                        except Exception:
                            needs_refresh = True

                    if needs_refresh:
                        sum_cur.execute(
                            "UPDATE conversation_summaries SET last_modified_time = ? WHERE conversation_id = ?",
                            (now_native, cid)
                        )
                        sum_conn.commit()

                        try:
                            os.utime(live_db_file, (now_ts, now_ts))
                        except Exception:
                            pass

                        with sqlite3.connect(catalog_db_path, timeout=10.0) as cat_conn:
                            cat_conn.execute("PRAGMA busy_timeout=10000;")
                            cat_conn.execute(
                                "UPDATE backed_up_conversations SET last_modified_time = ?, last_synced_time = ?, is_evicted_from_live = 0, retention_status = 'complete_active' WHERE conversation_id = ?",
                                (now_native, now_iso, cid)
                            )
                            cat_conn.commit()

                        result["refreshed"] += 1
                        safe_print(f"[{now_iso}] [Pin Sentry] 🛡️ Refreshed keep-alive timestamp for pinned session '{title_str}' (rank was #{rank+1}).")
            except Exception as e:
                result["errors"].append(f"Keep-alive check failed for {cid}: {e}")

    return result
