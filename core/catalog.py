import sqlite3
from typing import List, Dict, Any, Optional
from pathlib import Path
import json

from config import BACKUP_CATALOG_DB, BACKUP_CONVERSATIONS_DIR, BACKUP_BRAIN_DIR

class CatalogManager:
    def __init__(self, db_path: Path = BACKUP_CATALOG_DB):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=10000;")
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
            # Reconcile any existing records that were complete_active but evicted from live
            conn.execute("""
            UPDATE backed_up_conversations
            SET retention_status = 'safeguarded_evicted'
            WHERE retention_status = 'complete_active' AND is_evicted_from_live = 1;
            """)
            conn.commit()

    def get_summary_metrics(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM backed_up_conversations")
            total = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM backed_up_conversations WHERE retention_status = 'complete_active' AND is_evicted_from_live = 0")
            complete_active = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM backed_up_conversations WHERE retention_status = 'safeguarded_evicted' OR (retention_status = 'complete_active' AND is_evicted_from_live = 1)")
            safeguarded_evicted = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM backed_up_conversations WHERE retention_status = 'metadata_only_evicted'")
            metadata_only = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM backed_up_conversations WHERE retention_status = 'imported_from_offsite'")
            offsite_imported = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM backed_up_conversations WHERE is_pinned = 1")
            pinned = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM backed_up_conversations WHERE category = 'interactive'")
            interactive = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM backed_up_conversations WHERE category = 'automated_queue'")
            queue = cur.fetchone()[0]

            return {
                "total": total,
                "complete_active": complete_active,
                "safeguarded_evicted": safeguarded_evicted,
                "metadata_only_evicted": metadata_only,
                "imported_from_offsite": offsite_imported,
                "pinned": pinned,
                "workspaces": len(self.get_workspace_counts()),
                "interactive": interactive,
                "automated_queue": queue,
            }

    def get_unique_workspaces(self) -> List[str]:
        return [item["workspace"] for item in self.get_workspace_counts()]

    def get_workspace_counts(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT workspace_uris, COUNT(*) FROM backed_up_conversations WHERE workspace_uris IS NOT NULL AND workspace_uris != '' GROUP BY workspace_uris")
            counts = {}
            for raw, cnt in cur.fetchall():
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        for item in parsed:
                            counts[item] = counts.get(item, 0) + cnt
                    else:
                        counts[str(parsed)] = counts.get(str(parsed), 0) + cnt
                except Exception:
                    counts[raw] = counts.get(raw, 0) + cnt
            sorted_ws = sorted(counts.items(), key=lambda x: x[1], reverse=True)
            return [{"workspace": ws, "count": cnt} for ws, cnt in sorted_ws if ws]

    def get_category_counts(self) -> Dict[str, int]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT category, COUNT(*) FROM backed_up_conversations GROUP BY category")
            cat_counts = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM backed_up_conversations WHERE is_pinned = 1")
            cat_counts["pinned"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM backed_up_conversations")
            cat_counts["total"] = cur.fetchone()[0]
            return cat_counts

    def get_retention_counts(self) -> Dict[str, int]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT retention_status, COUNT(*) FROM backed_up_conversations GROUP BY retention_status")
            ret_counts = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM backed_up_conversations")
            ret_counts["total"] = cur.fetchone()[0]
            # Ensure all expected keys exist
            for k in ["complete_active", "safeguarded_evicted", "imported_from_offsite", "metadata_only_evicted"]:
                ret_counts.setdefault(k, 0)
            return ret_counts

    def query_conversations(
        self,
        retention_filter: Optional[str] = None,
        category_filter: Optional[str] = None,
        workspace_filter: Optional[str] = None,
        search_query: Optional[str] = None,
        order_by: str = "last_modified_time DESC",
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        conditions = []
        params = []

        if retention_filter and retention_filter != "All":
            conditions.append("retention_status = ?")
            params.append(retention_filter)

        if category_filter and category_filter != "All":
            if category_filter == "pinned":
                conditions.append("is_pinned = 1")
            else:
                conditions.append("category = ?")
                params.append(category_filter)

        if workspace_filter and workspace_filter != "All":
            conditions.append("workspace_uris LIKE ?")
            params.append(f"%{workspace_filter}%")

        if search_query and search_query.strip():
            sq = f"%{search_query.strip()}%"
            conditions.append("(conversation_id LIKE ? OR title LIKE ? OR preview LIKE ? OR user_notes LIKE ?)")
            params.extend([sq, sq, sq, sq])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"""
        SELECT conversation_id, title, preview, step_count, last_modified_time,
               last_user_input_time, workspace_uris, project_id, status,
               category, retention_status, has_db_file, has_brain_folder,
               is_pinned, is_evicted_from_live, user_notes
        FROM backed_up_conversations
        {where_clause}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def count_conversations(
        self,
        retention_filter: Optional[str] = None,
        category_filter: Optional[str] = None,
        workspace_filter: Optional[str] = None,
        search_query: Optional[str] = None,
    ) -> int:
        conditions = []
        params = []

        if retention_filter and retention_filter != "All":
            conditions.append("retention_status = ?")
            params.append(retention_filter)

        if category_filter and category_filter != "All":
            if category_filter == "pinned":
                conditions.append("is_pinned = 1")
            else:
                conditions.append("category = ?")
                params.append(category_filter)

        if workspace_filter and workspace_filter != "All":
            conditions.append("workspace_uris LIKE ?")
            params.append(f"%{workspace_filter}%")

        if search_query and search_query.strip():
            sq = f"%{search_query.strip()}%"
            conditions.append("(conversation_id LIKE ? OR title LIKE ? OR preview LIKE ? OR user_notes LIKE ?)")
            params.extend([sq, sq, sq, sq])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT COUNT(*) FROM backed_up_conversations {where_clause}"

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return cur.fetchone()[0]

    def get_conversation_by_id(self, cid: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM backed_up_conversations WHERE conversation_id = ?", (cid,))
            row = cur.fetchone()
            return dict(row) if row else None

    def update_user_notes(self, cid: str, notes: str):
        with self._get_connection() as conn:
            conn.execute("UPDATE backed_up_conversations SET user_notes = ? WHERE conversation_id = ?", (notes, cid))
            conn.commit()

    def update_pinned_status(self, cid: str, is_pinned: bool):
        with self._get_connection() as conn:
            conn.execute("UPDATE backed_up_conversations SET is_pinned = ? WHERE conversation_id = ?", (int(is_pinned), cid))
            conn.commit()

    def upgrade_to_imported(self, cid: str, has_db: bool, has_brain: bool):
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT retention_status, has_db_file, has_brain_folder FROM backed_up_conversations WHERE conversation_id = ?", (cid,))
            row = cur.fetchone()
            if row:
                curr_ret = row[0]
                existing_db = bool(row[1])
                existing_brain = bool(row[2])
                target_ret = "imported_from_offsite" if curr_ret in ("metadata_only_evicted", None, "") else curr_ret
                final_db = existing_db or has_db
                final_brain = existing_brain or has_brain
                conn.execute("""
                UPDATE backed_up_conversations 
                SET retention_status = ?,
                    has_db_file = ?,
                    has_brain_folder = ?
                WHERE conversation_id = ?
                """, (target_ret, int(final_db), int(final_brain), cid))
                conn.commit()

    def record_restored(
        self,
        cid: str,
        restored_at: str,
        target_workspace_uri: Optional[str] = None
    ) -> None:
        """
        Marks an archived or evicted session as restored and active in live Antigravity.
        Upgrades retention_status to complete_active, clears eviction flag, updates
        last_modified_time to ensure slot #1 ordering, and optionally updates workspace_uris.
        """
        with self._get_connection() as conn:
            if target_workspace_uri:
                conn.execute("""
                UPDATE backed_up_conversations
                SET is_evicted_from_live = 0,
                    retention_status = 'complete_active',
                    has_db_file = 1,
                    last_modified_time = ?,
                    last_synced_time = ?,
                    workspace_uris = ?
                WHERE conversation_id = ?
                """, (restored_at, restored_at, target_workspace_uri, cid))
            else:
                conn.execute("""
                UPDATE backed_up_conversations
                SET is_evicted_from_live = 0,
                    retention_status = 'complete_active',
                    has_db_file = 1,
                    last_modified_time = ?,
                    last_synced_time = ?
                WHERE conversation_id = ?
                """, (restored_at, restored_at, cid))
            conn.commit()
