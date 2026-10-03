import os
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
import json

LIVE_DIR = Path.home() / ".gemini" / "antigravity"
BACKUP_DIR = Path(os.environ.get("AGY_BACKUP_DIR", "d:/Antigravity-Backup"))

# Subdirectories
CONVS_SRC = LIVE_DIR / "conversations"
BRAIN_SRC = LIVE_DIR / "brain"
ANN_SRC = LIVE_DIR / "annotations"
SUMMARIES_SRC = LIVE_DIR / "conversation_summaries.db"

CONVS_DST = BACKUP_DIR / "conversations"
BRAIN_DST = BACKUP_DIR / "brain"
ANN_DST = BACKUP_DIR / "annotations"
STATE_DST = BACKUP_DIR / "state"
CATALOG_DB = BACKUP_DIR / "backup_catalog.db"

def init_catalog_db():
    CATALOG_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(CATALOG_DB) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS backed_up_conversations (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            preview TEXT,
            step_count INTEGER,
            last_modified_time DATETIME,
            last_user_input_time DATETIME,
            workspace_uris TEXT,
            project_id TEXT,
            status TEXT,
            category TEXT,
            retention_status TEXT,
            has_db_file BOOLEAN DEFAULT 0,
            has_brain_folder BOOLEAN DEFAULT 0,
            is_pinned BOOLEAN DEFAULT 0,
            is_evicted_from_live BOOLEAN DEFAULT 0,
            first_backed_up_time DATETIME,
            last_synced_time DATETIME,
            raw_summary BLOB,
            user_notes TEXT DEFAULT ""
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cat_last_mod ON backed_up_conversations(last_modified_time);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cat_retention ON backed_up_conversations(retention_status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cat_category ON backed_up_conversations(category);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cat_workspace ON backed_up_conversations(workspace_uris);")
        conn.commit()

def classify_session(title: str, preview: str, step_count: int, is_pinned: bool) -> str:
    if is_pinned:
        return "pinned"
    
    t = (title or "").lower()
    p = (preview or "").lower()
    combined = t + " " + p
    
    queue_keywords = ["queue", "remediate", "abstract", "flash", "who's who", "pending", "cron", "scheduled"]
    if any(k in combined for k in queue_keywords):
        return "automated_queue"
        
    if step_count > 15:
        return "interactive"
        
    return "general"

def run_snapshot():
    start_time = datetime.now()
    print(f"[{start_time.isoformat()}] Starting initial snapshot...")
    print(f"Source: {LIVE_DIR}")
    print(f"Destination: {BACKUP_DIR}")
    
    # 1. Create target directories
    for d in [CONVS_DST, BRAIN_DST, ANN_DST, STATE_DST]:
        d.mkdir(parents=True, exist_ok=True)
        
    init_catalog_db()
    
    # 2. Check pinned files in annotations
    pinned_ids = set()
    if ANN_SRC.exists():
        for f in ANN_SRC.glob("*.pbtxt"):
            cid = f.stem
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                if "pinned:true" in content or "pinned: true" in content:
                    pinned_ids.add(cid)
                # Copy annotation file
                shutil.copy2(f, ANN_DST / f.name)
            except Exception as e:
                pass
    print(f"Found {len(pinned_ids)} pinned conversation IDs in annotations/.")
    
    # 3. Snapshot all active conversation .db files
    conv_files = list(CONVS_SRC.glob("*.db")) if CONVS_SRC.exists() else []
    print(f"Copying {len(conv_files)} conversation .db files to {CONVS_DST}...")
    copied_dbs = 0
    for cf in conv_files:
        try:
            shutil.copy2(cf, CONVS_DST / cf.name)
            copied_dbs += 1
        except Exception as e:
            print(f"Error copying {cf.name}: {e}")
    print(f"Successfully copied {copied_dbs} .db files.")
    
    # 4. Snapshot all active brain directories
    brain_subdirs = [d for d in BRAIN_SRC.iterdir() if d.is_dir() and d.name != "tempmediaStorage"] if BRAIN_SRC.exists() else []
    print(f"Copying {len(brain_subdirs)} brain/ subdirectories to {BRAIN_DST}...")
    copied_brains = 0
    for bd in brain_subdirs:
        target_sub = BRAIN_DST / bd.name
        try:
            shutil.copytree(bd, target_sub, dirs_exist_ok=True)
            copied_brains += 1
        except Exception as e:
            print(f"Error copying brain/{bd.name}: {e}")
    print(f"Successfully copied {copied_brains} brain directories.")
    
    # 5. Mirror metadata into backup_catalog.db
    now_utc = datetime.now(timezone.utc).isoformat()
    active_cids = set(f.stem for f in conv_files)
    
    print(f"Reading conversation_summaries.db...")
    with sqlite3.connect(SUMMARIES_SRC) as s_conn, sqlite3.connect(CATALOG_DB) as c_conn:
        s_cur = s_conn.cursor()
        s_cur.execute("SELECT * FROM conversation_summaries")
        cols = [d[0] for d in s_cur.description]
        rows = s_cur.fetchall()
        print(f"Total catalog records in live DB: {len(rows)}")
        
        c_cur = c_conn.cursor()
        
        complete_active_count = 0
        metadata_only_count = 0
        
        for r in rows:
            row_dict = dict(zip(cols, r))
            cid = row_dict["conversation_id"]
            title = row_dict.get("title", "")
            preview = row_dict.get("preview", "")
            step_count = row_dict.get("step_count", 0)
            lmt = row_dict.get("last_modified_time", "")
            luit = row_dict.get("last_user_input_time", "")
            ws = row_dict.get("workspace_uris", "")
            proj = row_dict.get("project_id", "")
            status = row_dict.get("status", "")
            raw_summary = row_dict.get("raw_summary", None)
            
            is_pinned = cid in pinned_ids
            has_db = cid in active_cids
            has_brain = (BRAIN_DST / cid).exists()
            
            if has_db:
                retention_status = "complete_active"
                is_evicted = 0
                complete_active_count += 1
            else:
                retention_status = "metadata_only_evicted"
                is_evicted = 1
                metadata_only_count += 1
                
            category = classify_session(title, preview, step_count, is_pinned)
            
            c_cur.execute("""
            INSERT OR REPLACE INTO backed_up_conversations (
                conversation_id, title, preview, step_count, last_modified_time,
                last_user_input_time, workspace_uris, project_id, status,
                category, retention_status, has_db_file, has_brain_folder,
                is_pinned, is_evicted_from_live, first_backed_up_time,
                last_synced_time, raw_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cid, title, preview, step_count, lmt,
                luit, ws, proj, status,
                category, retention_status, int(has_db), int(has_brain),
                int(is_pinned), is_evicted, now_utc,
                now_utc, raw_summary
            ))
            
        c_conn.commit()
        
    # Write sync state
    state_file = STATE_DST / "last_snapshot.json"
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({
            "snapshot_time": now_utc,
            "copied_dbs": copied_dbs,
            "copied_brains": copied_brains,
            "complete_active_records": complete_active_count,
            "metadata_only_records": metadata_only_count,
            "total_records": len(rows),
            "pinned_count": len(pinned_ids)
        }, f, indent=2)
        
    duration = (datetime.now() - start_time).total_seconds()
    print(f"\n=== Snapshot Complete in {duration:.1f}s ===")
    print(f"Total Records in Catalog: {len(rows)}")
    print(f"  Complete Active (Full .db + brain/): {complete_active_count}")
    print(f"  Metadata Only (Evicted Prior to Backup): {metadata_only_count}")
    print(f"  Pinned Conversations Protected: {len(pinned_ids)}")
    print(f"Archive Catalog DB: {CATALOG_DB}")

if __name__ == "__main__":
    run_snapshot()
