import os
import sys
import sqlite3
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    BACKUP_BASE_DIR,
    BACKUP_CATALOG_DB,
    BACKUP_CONVERSATIONS_DIR,
    BACKUP_BRAIN_DIR,
    LIVE_SUMMARIES_DB
)
from core.catalog import CatalogManager
from core.classifier import classify_session
from core.db_extractor import extract_file_payloads, extract_chat_timeline
from core.importer import scan_offsite_source, execute_import
from core.backup_engine import sync_live_to_backup
from core.search_engine import search_backup_conversations, highlight_snippet
from core.file_ops import copy_file_robust, backup_sqlite_db_safe, sync_directory_tree, rmtree_robust
from core.daemon import acquire_pid_lock, release_pid_lock, is_pid_running, run_pin_sentry
from core.restorer import (
    restore_conversation,
    validate_conversation_id,
    normalize_workspace_uri,
    check_sqlite_integrity
)

class TestAntigravityVault(unittest.TestCase):

    def setUp(self):
        self.catalog = CatalogManager()

    def test_01_catalog_metrics(self):
        """Test that the catalog has catalogued the live and legacy conversations."""
        metrics = self.catalog.get_summary_metrics()
        print("\nCatalog Metrics:", metrics)
        self.assertGreater(metrics["total"], 2500, "Total catalog records should exceed 2500")
        self.assertGreaterEqual(metrics["metadata_only_evicted"] + metrics.get("imported_from_offsite", 0), 2000, "Evicted + imported records should exceed 2000")
        self.assertGreaterEqual(metrics["pinned"], 10, "Should have >= 10 pinned records")
        self.assertIn("workspaces", metrics)
        self.assertIsInstance(metrics["workspaces"], int)
        self.assertGreater(metrics["workspaces"], 0, "Discovered workspaces should be > 0")
        # Invariant: Conservation law across 4 distinct retention categories
        self.assertEqual(
            metrics["total"],
            metrics["complete_active"] + metrics.get("safeguarded_evicted", 0) + metrics.get("imported_from_offsite", 0) + metrics["metadata_only_evicted"],
            "Total catalogued must equal sum of all four distinct retention states"
        )

    def test_02_classifier(self):
        """Test classifier categorizes queue tasks vs interactive sessions."""
        self.assertEqual(classify_session("Process Journal Abstract Queue", "", 1, False), "automated_queue")
        self.assertEqual(classify_session("Remediate Flash Article", "", 2, False), "automated_queue")
        self.assertEqual(classify_session("Building Content Generator Strategy", "", 45, False), "interactive")
        self.assertEqual(classify_session("Random Title", "", 5, True), "pinned")

    def test_03_query_and_filter(self):
        """Test querying with filters and pagination."""
        # Query complete active only
        active = self.catalog.query_conversations(retention_filter="complete_active", limit=10)
        self.assertEqual(len(active), 10)
        for a in active:
            self.assertEqual(a["retention_status"], "complete_active")
            self.assertEqual(a["has_db_file"], 1)

        # Query metadata only
        evicted = self.catalog.query_conversations(retention_filter="metadata_only_evicted", limit=10)
        self.assertGreater(len(evicted), 0, "Should have at least one evicted session")
        self.assertLessEqual(len(evicted), 10, "Should not exceed limit of 10")
        for e in evicted:
            self.assertEqual(e["retention_status"], "metadata_only_evicted")
            self.assertEqual(e["has_db_file"], 0)

    def test_04_file_payload_extraction(self):
        """Test extracting write_to_file payloads directly from SQLite db."""
        # Find any conversation db file in backup
        db_files = list(BACKUP_CONVERSATIONS_DIR.glob("*.db"))
        self.assertTrue(len(db_files) > 0, "Backup should contain .db files")

        found_extracted_files = False
        for db_file in db_files[:10]:
            files = extract_file_payloads(db_file)
            if files:
                found_extracted_files = True
                print(f"\nExtracted {len(files)} files from {db_file.name}: {[f['filename'] for f in files]}")
                for f in files:
                    self.assertTrue(len(f["content"]) > 0, "Extracted content should not be empty")
                break

        self.assertTrue(found_extracted_files, "Should successfully extract file payloads from at least one db")

    def test_05_chat_timeline_extraction(self):
        """Test extracting chat timeline from backup databases."""
        db_files = list(BACKUP_CONVERSATIONS_DIR.glob("*.db"))
        sample_db = None
        timeline = []
        for f in db_files:
            tl = extract_chat_timeline(db_path=f)
            if len(tl) > 0:
                sample_db = f
                timeline = tl
                break
        self.assertIsNotNone(sample_db, "Should find at least one valid database with chat timeline")
        self.assertTrue(len(timeline) > 0, f"Timeline should be extracted from {sample_db.name}")
        print(f"\nExtracted {len(timeline)} timeline steps from {sample_db.name}")

    def test_06_importer_scan(self):
        """Test scanner detects valid conversation databases."""
        # Test scanning the backup folder itself as a simulated offsite source
        scan_res = scan_offsite_source(BACKUP_CONVERSATIONS_DIR)
        self.assertTrue(scan_res["valid"])
        self.assertGreater(scan_res["total_discovered"], 500)
        print(f"\nImporter scan discovered: {scan_res['total_discovered']} items")

    def test_07_sync_engine(self):
        """Test that incremental sync engine runs smoothly."""
        res = sync_live_to_backup()
        self.assertTrue(res["success"], f"Sync failed: {res.get('error')}")
        print("\nSync Engine run completed successfully:", res)

    def test_08_dropdown_counts(self):
        """Test that filter dropdown count helpers return accurate values."""
        ret_counts = self.catalog.get_retention_counts()
        self.assertIn("total", ret_counts)
        self.assertIn("complete_active", ret_counts)
        self.assertIn("safeguarded_evicted", ret_counts)
        self.assertIn("metadata_only_evicted", ret_counts)
        self.assertIn("imported_from_offsite", ret_counts)
        self.assertEqual(
            ret_counts["total"],
            ret_counts.get("complete_active", 0) +
            ret_counts.get("safeguarded_evicted", 0) +
            ret_counts.get("metadata_only_evicted", 0) +
            ret_counts.get("imported_from_offsite", 0)
        )

        cat_counts = self.catalog.get_category_counts()
        self.assertIn("total", cat_counts)
        self.assertIn("interactive", cat_counts)
        self.assertIn("automated_queue", cat_counts)
        self.assertIn("pinned", cat_counts)

        ws_counts = self.catalog.get_workspace_counts()
        self.assertIsInstance(ws_counts, list)
        self.assertGreater(len(ws_counts), 0)
        self.assertIn("workspace", ws_counts[0])
        self.assertIn("count", ws_counts[0])
        print(f"\nDropdown Counts: Ret={ret_counts}, Cat={cat_counts}, Top WS={ws_counts[0]}")

    def test_09_global_search_transcripts_and_artifacts(self):
        """Test global search finds hits in both transcripts and markdown artifacts with snippet highlighting."""
        # Search for a generic term present in both artifacts and transcripts
        res = search_backup_conversations(
            query="implementation_plan",
            catalog=self.catalog,
            scope="All Content",
            max_results=5
        )
        self.assertGreater(len(res["results"]), 0, "Should find matching conversations for implementation_plan")
        self.assertGreater(res["scanned_count"], 0)
        self.assertIn("elapsed_time", res)

        first_match = res["results"][0]
        self.assertIn("conversation_id", first_match)
        self.assertIn("title", first_match)
        self.assertIn("workspace", first_match)
        self.assertIn("date", first_match)
        self.assertIn("hits", first_match)
        self.assertGreater(len(first_match["hits"]), 0)

        # Verify snippet highlight
        first_hit = first_match["hits"][0]
        self.assertIn("<mark", first_hit["snippet"])
        clean_label = first_hit['label'].encode('ascii', 'ignore').decode()
        print(f"\nSearch test: Found {len(res['results'])} convs in {res['elapsed_time']:.2f}s. First hit: {clean_label}")

    def test_10_global_search_workspace_and_limit(self):
        """Test global search respects workspace filtering and max_results limit."""
        # 1. Limit check
        res_limit = search_backup_conversations(
            query="error",
            catalog=self.catalog,
            max_results=3
        )
        self.assertLessEqual(len(res_limit["results"]), 3)
        self.assertTrue(res_limit["stopped_early"])

        # 2. Workspace filter check dynamically using first available workspace
        ws_counts = self.catalog.get_workspace_counts()
        if ws_counts:
            target_ws = ws_counts[0]["workspace"]
            # Clean protocol to match display / search behavior
            ws_kw = target_ws.replace("file:///", "").replace("%3A", ":").split("/")[-1]
            res_ws = search_backup_conversations(
                query="the",
                catalog=self.catalog,
                workspace_filter=ws_kw,
                max_results=10
            )
            for r in res_ws["results"]:
                self.assertIn(ws_kw.lower(), r["workspace"].lower())
            print(f"\nWorkspace filter test: Found {len(res_ws['results'])} convs in {ws_kw} workspace")

    def test_11_archive_only_file_extraction_fidelity(self):
        """Test hermetic file extraction reconstructing complete files without 0-byte entries from backup archive."""
        test_cid = "be8c334a-1c51-47bf-8055-11a28b10a8cf"
        db_path = BACKUP_CONVERSATIONS_DIR / f"{test_cid}.db"
        if not db_path.exists():
            dbs = list(BACKUP_CONVERSATIONS_DIR.glob("*.db"))
            if not dbs:
                self.skipTest("No conversation database found in backup conversations directory.")
            test_cid = dbs[0].stem

        files = extract_file_payloads(db_path=db_path, brain_dir=BACKUP_BRAIN_DIR / test_cid)
        self.assertIsInstance(files, list)
        self.assertGreater(len(files), 0, "Should extract files from the conversation trajectory/brain")

        file_map = {f["filename"]: f for f in files}
        if "app.py" in file_map:
            app_f = file_map["app.py"]
            self.assertGreater(app_f["size_bytes"], 25000, "app.py should have substantial reconstructed size")
            self.assertIn("import streamlit", app_f["content"])
            self.assertTrue(app_f["filename"].endswith(".py"))
            self.assertFalse(app_f["is_artifact"])

        for f in files:
            if f.get("content"):
                self.assertEqual(f["size_bytes"], len(f["content"].encode("utf-8")), "size_bytes must match content length")
                self.assertGreater(f["size_bytes"], 0)

        print(f"\nArchive extraction fidelity test: Verified {len(files)} files for {test_cid[:8]}... strictly from archive (zero 0-byte entries)")

    def test_12_robust_file_ops_readonly_overwrite(self):
        """Test that copy_file_robust successfully overwrites Windows read-only files."""
        temp_dir = Path(__file__).parent / "temp_file_ops_test"
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            src = temp_dir / "src.txt"
            dst = temp_dir / "dst.txt"
            src.write_text("v2 content", encoding="utf-8")
            dst.write_text("v1 content", encoding="utf-8")

            # Set destination to read-only (Windows 0o444)
            import stat
            os.chmod(dst, stat.S_IREAD)

            # Robust copy should clear read-only and overwrite cleanly
            copied = copy_file_robust(src, dst)
            self.assertTrue(copied)
            self.assertEqual(dst.read_text(encoding="utf-8"), "v2 content")
        finally:
            rmtree_robust(temp_dir)

    def test_13_git_tree_sync_idempotency_and_readonly(self):
        """Test that sync_directory_tree handles nested read-only Git objects without crashing."""
        temp_dir = Path(__file__).parent / "temp_git_sync_test"
        src_dir = temp_dir / "src_brain"
        dst_dir = temp_dir / "dst_brain"

        git_obj = src_dir / ".git" / "objects" / "00" / "5dcd780f8a8c3dfbea9264d003848b62e124ce"
        git_obj.parent.mkdir(parents=True, exist_ok=True)
        git_obj.write_text("git loose object dummy content", encoding="utf-8")

        import stat
        os.chmod(git_obj, stat.S_IREAD)

        try:
            # 1. First sync - copies successfully
            res1 = sync_directory_tree(src_dir, dst_dir)
            self.assertTrue(res1["success"])
            self.assertEqual(res1["copied"], 1)
            self.assertEqual(len(res1["errors"]), 0)

            # 2. Second sync - idempotent skip, zero PermissionError
            res2 = sync_directory_tree(src_dir, dst_dir)
            self.assertTrue(res2["success"])
            self.assertEqual(res2["copied"], 0)
            self.assertEqual(res2["skipped"], 1)
            self.assertEqual(len(res2["errors"]), 0)

            # 3. Third sync with updated file - overwrites read-only cleanly
            os.chmod(git_obj, stat.S_IWRITE | stat.S_IREAD)
            git_obj.write_text("git loose object updated content", encoding="utf-8")
            os.chmod(git_obj, stat.S_IREAD)

            res3 = sync_directory_tree(src_dir, dst_dir)
            self.assertTrue(res3["success"])
            self.assertEqual(res3["copied"], 1)
            self.assertEqual(len(res3["errors"]), 0)
            dst_git_obj = dst_dir / ".git" / "objects" / "00" / "5dcd780f8a8c3dfbea9264d003848b62e124ce"
            self.assertEqual(dst_git_obj.read_text(encoding="utf-8"), "git loose object updated content")
        finally:
            rmtree_robust(temp_dir)

    def test_14_sqlite_wal_safe_backup(self):
        """Test that backup_sqlite_db_safe safely snapshots active databases in WAL mode."""
        import sqlite3
        temp_dir = Path(__file__).parent / "temp_sqlite_wal_test"
        rmtree_robust(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        src_db = temp_dir / "live_active.db"
        dst_db = temp_dir / "backup_archive.db"

        try:
            with sqlite3.connect(src_db) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, msg TEXT);")
                conn.execute("INSERT INTO test_data (msg) VALUES ('turn 1'), ('turn 2');")
                conn.commit()

            success = backup_sqlite_db_safe(src_db, dst_db)
            self.assertTrue(success)
            self.assertTrue(dst_db.exists())

            # Verify backup has the data intact
            with sqlite3.connect(dst_db) as b_conn:
                cur = b_conn.cursor()
                cur.execute("SELECT COUNT(*) FROM test_data")
                self.assertEqual(cur.fetchone()[0], 2)
        finally:
            rmtree_robust(temp_dir)

    def test_15_importer_repeated_import_safe(self):
        """Test that execute_import survives second import on existing read-only brain folders."""
        import sqlite3
        import stat
        test_cid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        temp_dir = Path(__file__).parent / "temp_importer_test"
        rmtree_robust(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)

        fake_src_db = temp_dir / f"{test_cid}.db"
        with sqlite3.connect(fake_src_db) as conn:
            conn.execute("CREATE TABLE trajectory_meta (id INTEGER PRIMARY KEY);")
            conn.commit()

        fake_src_brain = temp_dir / test_cid
        fake_git = fake_src_brain / ".git" / "objects" / "00" / "fakeobject"
        fake_git.parent.mkdir(parents=True, exist_ok=True)
        fake_git.write_text("sample git hash payload", encoding="utf-8")
        os.chmod(fake_git, stat.S_IREAD)

        item = {
            "conversation_id": test_cid,
            "has_db": True,
            "has_brain": True,
            "title": "Adversarial Import Test Session",
            "preview": "Testing repeat import resilience",
            "workspace": "file:///d:/Developer/test",
            "db_path": str(fake_src_db),
            "brain_path": str(fake_src_brain),
            "status_label": "Testing"
        }

        try:
            # First import run
            res1 = execute_import([item])
            self.assertTrue(res1["success"], f"First import failed: {res1.get('failed_items')}")

            # Second import run (reproducing the exact user incident!)
            res2 = execute_import([item])
            self.assertTrue(res2["success"], f"Second import failed on read-only objects: {res2.get('failed_items')}")
            self.assertEqual(len(res2["failed_items"]), 0)
        finally:
            rmtree_robust(temp_dir)
            # Cleanup test conversation from catalog and backup
            try:
                with sqlite3.connect(BACKUP_CATALOG_DB, timeout=10.0) as conn:
                    conn.execute("DELETE FROM backed_up_conversations WHERE conversation_id = ?", (test_cid,))
                    conn.commit()
                (BACKUP_CONVERSATIONS_DIR / f"{test_cid}.db").unlink(missing_ok=True)
                rmtree_robust(BACKUP_BRAIN_DIR / test_cid)
            except Exception:
                pass

    def test_16_daemon_pid_lock(self):
        """Test daemon PID lock acquisition and release with hermetically isolated PID file."""
        self.assertTrue(is_pid_running(os.getpid()))
        self.assertFalse(is_pid_running(99999999))

        from unittest.mock import patch
        test_pid_file = Path(__file__).parent / "temp_daemon.pid"
        try:
            with patch("core.daemon.DAEMON_PID_FILE", test_pid_file):
                test_pid_file.unlink(missing_ok=True)
                acquired = acquire_pid_lock()
                self.assertTrue(acquired)
                self.assertTrue(test_pid_file.exists())
                self.assertEqual(int(test_pid_file.read_text(encoding="utf-8").strip()), os.getpid())
                release_pid_lock()
                self.assertFalse(test_pid_file.exists())
        finally:
            test_pid_file.unlink(missing_ok=True)

    def test_17_importer_reconciliation_and_actionable_filtering(self):
        """Test scanner 4-tier reconciliation and actionable-first sorting on real backup data."""
        acronis_path = Path("D:/Acronis/antigravity-2026-09-20")
        if not acronis_path.exists():
            self.skipTest("Acronis backup folder not found on D: drive")

        res = scan_offsite_source(acronis_path)
        self.assertTrue(res["valid"])
        self.assertGreater(res["total_discovered"], 0)

        # Mathematical reconciliation invariant: total must equal the exact sum of all 5 buckets
        reconciled_sum = (
            res["matching_evicted"]
            + res["brand_new"]
            + res["missing_assets"]
            + res["newer_content"]
            + res["already_archived"]
        )
        self.assertEqual(res["total_discovered"], reconciled_sum, "100% of discovered sessions must be accounted for")
        self.assertEqual(
            res["actionable_count"],
            res["matching_evicted"] + res["brand_new"] + res["missing_assets"] + res["newer_content"]
        )

        # Actionable items must be sorted first invariant
        items = res["items"]
        self.assertEqual(len(items), res["total_discovered"])
        saw_non_actionable = False
        actionable_after_non_actionable = False
        for item in items:
            if not item["is_actionable"]:
                saw_non_actionable = True
            elif saw_non_actionable:
                actionable_after_non_actionable = True
                break
        self.assertFalse(actionable_after_non_actionable, "All actionable items must precede non-actionable items in the list")

        # Test progress callback support with a dummy test CID
        progress_calls = []
        def mock_progress(cur, tot, cid, title):
            progress_calls.append((cur, tot, cid))

        dummy_cid = "00000000-0000-0000-0000-000000000000"
        fake_item = {
            "conversation_id": dummy_cid,
            "title": "Mock Progress Test",
            "preview": "Mock",
            "db_path": "",
            "brain_path": "",
            "has_db": False,
            "has_brain": False,
            "workspace": ""
        }
        try:
            import_res = execute_import([fake_item], progress_callback=mock_progress)
            self.assertEqual(len(progress_calls), 1)
            self.assertEqual(progress_calls[0][0], 1)
            self.assertEqual(progress_calls[0][1], 1)
        finally:
            with sqlite3.connect(BACKUP_CATALOG_DB, timeout=10.0) as conn:
                conn.execute("DELETE FROM backed_up_conversations WHERE conversation_id = ?", (dummy_cid,))
                conn.commit()

    def test_18_restore_validation_and_path_traversal(self):
        """Verify strict adversarial rejection of malicious IDs and path traversal tokens."""
        # 1. Traversal and injection attempts must fail validation
        self.assertFalse(validate_conversation_id("../malicious"))
        self.assertFalse(validate_conversation_id("..\\malicious"))
        self.assertFalse(validate_conversation_id("../../etc/passwd"))
        self.assertFalse(validate_conversation_id("cid/with/slash"))
        self.assertFalse(validate_conversation_id("cid\\with\\backslash"))
        self.assertFalse(validate_conversation_id("cid:with:colon"))
        self.assertFalse(validate_conversation_id(""))
        self.assertFalse(validate_conversation_id("   "))
        self.assertFalse(validate_conversation_id(None))

        # 2. Legitimate IDs must pass
        self.assertTrue(validate_conversation_id("002e4349-8e0e-4335-a158-6d067966f2c8"))
        self.assertTrue(validate_conversation_id("session_123_abc-XYZ"))

        # 3. restore_conversation must reject traversal cleanly
        res = restore_conversation("../evil_path")
        self.assertFalse(res["success"])
        self.assertIn("Invalid conversation ID format", res["error"])

        # 4. Non-existent ID must fail pre-flight validation
        res_nonexistent = restore_conversation("ffffffff-ffff-ffff-ffff-ffffffffffff")
        self.assertFalse(res_nonexistent["success"])
        self.assertIn("does not exist in backup archive", res_nonexistent["error"])

    def test_19_restore_conversation_end_to_end_and_monotonicity(self):
        """Verify complete hermetic restoration, catalog upgrade, and monotonicity."""
        test_dir = Path(__file__).parent / "temp_restore_test"
        rmtree_robust(test_dir)
        test_dir.mkdir(parents=True, exist_ok=True)

        mock_backup_conv = test_dir / "backup_conversations"
        mock_backup_brain = test_dir / "backup_brain"
        mock_backup_ann = test_dir / "backup_ann"
        mock_backup_cat = test_dir / "backup_catalog.db"

        mock_live_conv = test_dir / "live_conversations"
        mock_live_brain = test_dir / "live_brain"
        mock_live_ann = test_dir / "live_ann"
        mock_live_sum = test_dir / "live_summaries.db"

        for p in [mock_backup_conv, mock_backup_brain, mock_backup_ann, mock_live_conv, mock_live_brain, mock_live_ann]:
            p.mkdir(parents=True, exist_ok=True)

        cat_mgr = CatalogManager(db_path=mock_backup_cat)
        test_cid = "11111111-2222-3333-4444-555555555555"

        # Create valid source trajectory DB
        src_db = mock_backup_conv / f"{test_cid}.db"
        with sqlite3.connect(src_db) as conn:
            conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, content TEXT);")
            conn.execute("INSERT INTO steps VALUES (1, 'initial prompt');")
            conn.commit()

        # Create source brain folder
        src_brain_dir = mock_backup_brain / test_cid
        src_brain_dir.mkdir(parents=True, exist_ok=True)
        (src_brain_dir / "task.md").write_text("Test Task Content", encoding="utf-8")

        # Create source annotation
        src_ann = mock_backup_ann / f"{test_cid}.pbtxt"
        src_ann.write_text("pinned: false", encoding="utf-8")

        # Insert metadata row into backup catalog with evicted status
        with sqlite3.connect(mock_backup_cat) as conn:
            conn.execute("""
            INSERT INTO backed_up_conversations (
                conversation_id, title, preview, step_count, last_modified_time,
                retention_status, has_db_file, has_brain_folder, is_evicted_from_live, workspace_uris
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                test_cid, "Original Evicted Session", "Evicted Preview", 10,
                "2026-09-01T00:00:00+00:00", "imported_from_offsite", 1, 1, 1, '["file:///d:/OldWorkspace"]'
            ))
            conn.commit()

        from unittest.mock import patch
        try:
            with patch("core.restorer.BACKUP_CONVERSATIONS_DIR", mock_backup_conv), \
                 patch("core.restorer.BACKUP_BRAIN_DIR", mock_backup_brain), \
                 patch("core.restorer.BACKUP_ANNOTATIONS_DIR", mock_backup_ann), \
                 patch("core.restorer.BACKUP_CATALOG_DB", mock_backup_cat), \
                 patch("core.restorer.LIVE_CONVERSATIONS_DIR", mock_live_conv), \
                 patch("core.restorer.LIVE_BRAIN_DIR", mock_live_brain), \
                 patch("core.restorer.LIVE_ANNOTATIONS_DIR", mock_live_ann), \
                 patch("core.restorer.LIVE_SUMMARIES_DB", mock_live_sum):

                # Perform restoration
                target_ws = "file:///d:/NewWorkspace"
                res = restore_conversation(test_cid, target_workspace_uri=target_ws, catalog_manager=cat_mgr)
                self.assertTrue(res["success"], f"Restoration failed: {res.get('error')}")

                # 1. Verify live DB copied and intact
                live_db = mock_live_conv / f"{test_cid}.db"
                self.assertTrue(live_db.exists())
                self.assertTrue(check_sqlite_integrity(live_db))

                # 2. Verify live brain synced
                live_brain = mock_live_brain / test_cid
                self.assertTrue(live_brain.exists())
                self.assertTrue((live_brain / "task.md").exists())
                self.assertEqual((live_brain / "task.md").read_text(encoding="utf-8"), "Test Task Content")

                # 3. Verify live annotations copied
                live_ann = mock_live_ann / f"{test_cid}.pbtxt"
                self.assertTrue(live_ann.exists())

                # 4. Verify live conversation_summaries.db updated
                with sqlite3.connect(mock_live_sum) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT title, workspace_uris, step_count FROM conversation_summaries WHERE conversation_id = ?", (test_cid,))
                    sum_row = cur.fetchone()
                    self.assertIsNotNone(sum_row)
                    self.assertEqual(sum_row[0], "Original Evicted Session")
                    self.assertEqual(sum_row[1], '["file:///d:/NewWorkspace"]')
                    self.assertEqual(sum_row[2], 10)

                # 5. Verify catalog upgraded to complete_active (Monotonicity Law)
                cat_row = cat_mgr.get_conversation_by_id(test_cid)
                self.assertIsNotNone(cat_row)
                self.assertEqual(cat_row["retention_status"], "complete_active", "Restored session must upgrade to complete_active")
                self.assertEqual(cat_row["is_evicted_from_live"], 0, "Restored session must clear is_evicted_from_live flag")
                self.assertEqual(cat_row["workspace_uris"], '["file:///d:/NewWorkspace"]')

        finally:
            rmtree_robust(test_dir)

    def test_20_restore_preserves_live_active_without_overwrite(self):
        """Verify that restoring an already-live session does not clobber active live data when overwrite_live=False."""
        test_dir = Path(__file__).parent / "temp_preserve_test"
        rmtree_robust(test_dir)
        test_dir.mkdir(parents=True, exist_ok=True)

        mock_backup_conv = test_dir / "backup_conversations"
        mock_backup_cat = test_dir / "backup_catalog.db"
        mock_live_conv = test_dir / "live_conversations"
        mock_live_sum = test_dir / "live_summaries.db"

        for p in [mock_backup_conv, mock_live_conv]:
            p.mkdir(parents=True, exist_ok=True)

        cat_mgr = CatalogManager(db_path=mock_backup_cat)
        test_cid = "22222222-3333-4444-5555-666666666666"

        # Backup DB: has 1 step
        src_db = mock_backup_conv / f"{test_cid}.db"
        with sqlite3.connect(src_db) as conn:
            conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, content TEXT);")
            conn.execute("INSERT INTO steps VALUES (1, 'backup version');")
            conn.commit()

        # Live DB: already has 2 steps (active live work)
        dst_db = mock_live_conv / f"{test_cid}.db"
        with sqlite3.connect(dst_db) as conn:
            conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, content TEXT);")
            conn.execute("INSERT INTO steps VALUES (1, 'backup version');")
            conn.execute("INSERT INTO steps VALUES (2, 'new live work step');")
            conn.commit()

        # Catalog has complete_active
        with sqlite3.connect(mock_backup_cat) as conn:
            conn.execute("""
            INSERT INTO backed_up_conversations (
                conversation_id, title, preview, step_count, last_modified_time,
                retention_status, has_db_file, has_brain_folder, is_evicted_from_live, workspace_uris
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                test_cid, "Active Session", "Preview", 2,
                "2026-09-01T00:00:00+00:00", "complete_active", 1, 0, 0, '["file:///d:/WS"]'
            ))
            conn.commit()

        from unittest.mock import patch
        try:
            with patch("core.restorer.BACKUP_CONVERSATIONS_DIR", mock_backup_conv), \
                 patch("core.restorer.BACKUP_BRAIN_DIR", test_dir / "empty_brain"), \
                 patch("core.restorer.BACKUP_ANNOTATIONS_DIR", test_dir / "empty_ann"), \
                 patch("core.restorer.BACKUP_CATALOG_DB", mock_backup_cat), \
                 patch("core.restorer.LIVE_CONVERSATIONS_DIR", mock_live_conv), \
                 patch("core.restorer.LIVE_BRAIN_DIR", test_dir / "live_brain"), \
                 patch("core.restorer.LIVE_ANNOTATIONS_DIR", test_dir / "live_ann"), \
                 patch("core.restorer.LIVE_SUMMARIES_DB", mock_live_sum):

                # 1. Restore with overwrite_live=False (default) on active session
                res = restore_conversation(test_cid, overwrite_live=False, catalog_manager=cat_mgr)
                self.assertTrue(res["success"])
                self.assertTrue(res.get("already_active", False), "Active session must report already_active without clobbering")

                # Verify live DB was NOT overwritten (still has 2 steps)
                with sqlite3.connect(dst_db) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM steps;")
                    self.assertEqual(cur.fetchone()[0], 2, "Live active steps must NOT be clobbered by older backup")

        finally:
            rmtree_robust(test_dir)

    def test_21_restore_rollback_on_failure(self):
        """Verify atomic rollback removes newly created files if restore encounters a fatal write failure."""
        test_dir = Path(__file__).parent / "temp_rollback_test"
        rmtree_robust(test_dir)
        test_dir.mkdir(parents=True, exist_ok=True)

        mock_backup_conv = test_dir / "backup_conversations"
        mock_backup_cat = test_dir / "backup_catalog.db"
        mock_live_conv = test_dir / "live_conversations"
        mock_live_sum = test_dir / "live_summaries.db"

        for p in [mock_backup_conv, mock_live_conv]:
            p.mkdir(parents=True, exist_ok=True)

        cat_mgr = CatalogManager(db_path=mock_backup_cat)
        test_cid = "33333333-4444-5555-6666-777777777777"

        src_db = mock_backup_conv / f"{test_cid}.db"
        with sqlite3.connect(src_db) as conn:
            conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY);")
            conn.commit()

        with sqlite3.connect(mock_backup_cat) as conn:
            conn.execute("""
            INSERT INTO backed_up_conversations (
                conversation_id, title, preview, step_count, last_modified_time,
                retention_status, has_db_file, has_brain_folder, is_evicted_from_live, workspace_uris
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                test_cid, "Rollback Test", "Preview", 1,
                "2026-09-01T00:00:00+00:00", "metadata_only_evicted", 1, 0, 1, '[""]'
            ))
            conn.commit()

        from unittest.mock import patch
        try:
            with patch("core.restorer.BACKUP_CONVERSATIONS_DIR", mock_backup_conv), \
                 patch("core.restorer.BACKUP_BRAIN_DIR", test_dir / "empty_brain"), \
                 patch("core.restorer.BACKUP_ANNOTATIONS_DIR", test_dir / "empty_ann"), \
                 patch("core.restorer.BACKUP_CATALOG_DB", mock_backup_cat), \
                 patch("core.restorer.LIVE_CONVERSATIONS_DIR", mock_live_conv), \
                 patch("core.restorer.LIVE_BRAIN_DIR", test_dir / "live_brain"), \
                 patch("core.restorer.LIVE_ANNOTATIONS_DIR", test_dir / "live_ann"), \
                 patch("core.restorer.LIVE_SUMMARIES_DB", mock_live_sum):

                # Simulate fatal failure during _insert_live_summary
                with patch("core.restorer._insert_live_summary", side_effect=sqlite3.OperationalError("Fatal write error")):
                    res = restore_conversation(test_cid, catalog_manager=cat_mgr)
                    self.assertFalse(res["success"])
                    self.assertIn("Restoration failed", res["error"])

                    # Assert that destination DB was rolled back and deleted
                    live_db = mock_live_conv / f"{test_cid}.db"
                    self.assertFalse(live_db.exists(), "Partially copied live DB must be rolled back on failure")

        finally:
            rmtree_robust(test_dir)

    def test_22_safeguarded_evicted_taxonomy_and_conservation_law(self):
        """Verify that safeguarded_evicted taxonomy strictly partitions live vs vault-only sessions."""
        metrics = self.catalog.get_summary_metrics()
        
        # 1. Conservation law must hold strictly
        self.assertEqual(
            metrics["total"],
            metrics["complete_active"] + metrics["safeguarded_evicted"] + metrics["imported_from_offsite"] + metrics["metadata_only_evicted"]
        )

        # 2. Complete active must strictly match un-evicted live DB count
        active_sessions = self.catalog.query_conversations(retention_filter="complete_active", limit=50)
        self.assertGreater(len(active_sessions), 0)
        for s in active_sessions:
            self.assertEqual(s["is_evicted_from_live"], 0, "complete_active sessions must have is_evicted_from_live = 0")
            self.assertEqual(s["retention_status"], "complete_active")

        # 3. Safeguarded evicted must have physical backup DB but is_evicted_from_live = 1
        safeguarded = self.catalog.query_conversations(retention_filter="safeguarded_evicted", limit=50)
        self.assertGreater(len(safeguarded), 0, "Should have safeguarded evicted sessions")
        for s in safeguarded:
            self.assertEqual(s["is_evicted_from_live"], 1, "safeguarded_evicted sessions must have is_evicted_from_live = 1")
            self.assertEqual(s["has_db_file"], 1, "safeguarded_evicted sessions must possess physical backup DB")
            self.assertEqual(s["retention_status"], "safeguarded_evicted")

    def test_23_pin_sentry_keep_alive_and_auto_resurrection(self):
        """Verify Pin Sentry performs both preventive keep-alive touches and self-healing auto-resurrections."""
        test_dir = Path(__file__).parent / "temp_pin_sentry_test"
        rmtree_robust(test_dir)
        test_dir.mkdir(parents=True, exist_ok=True)

        mock_backup_conv = test_dir / "backup_conversations"
        mock_backup_brain = test_dir / "backup_brain"
        mock_backup_cat = test_dir / "backup_catalog.db"
        mock_live_conv = test_dir / "live_conversations"
        mock_live_brain = test_dir / "live_brain"
        mock_live_sum = test_dir / "live_summaries.db"

        for p in [mock_backup_conv, mock_backup_brain, mock_live_conv, mock_live_brain]:
            p.mkdir(parents=True, exist_ok=True)

        cat_mgr = CatalogManager(db_path=mock_backup_cat)
        cid_active = "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee"
        cid_evicted = "ffff2222-bbbb-cccc-dddd-eeeeeeeeeeee"

        # Create dummy trajectory databases
        for cid in [cid_active, cid_evicted]:
            src_db = mock_backup_conv / f"{cid}.db"
            with sqlite3.connect(src_db) as conn:
                conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY);")
                conn.commit()

        # Place cid_active in live conversations directory with an old timestamp
        live_active_db = mock_live_conv / f"{cid_active}.db"
        copy_file_robust(mock_backup_conv / f"{cid_active}.db", live_active_db)

        # Initialize mock live_summaries.db
        with sqlite3.connect(mock_live_sum) as conn:
            conn.execute("""
            CREATE TABLE conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                preview TEXT,
                step_count INTEGER,
                last_modified_time TEXT,
                workspace_uris TEXT,
                status TEXT,
                last_user_input_time TEXT,
                app_data_dir TEXT
            );
            """)
            # Insert cid_active with an old timestamp (e.g., 48 hours ago)
            conn.execute("""
            INSERT INTO conversation_summaries (
                conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, last_user_input_time, app_data_dir
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cid_active, "Active Strategy Session", "Preview", 10,
                "2026-10-01 10:00:00.000000+00:00", '[""]', "CASCADE_RUN_STATUS_IDLE",
                "2026-10-01 10:00:00.000000+00:00", "antigravity"
            ))
            # Insert 400 dummy newer sessions to simulate rank #401
            for i in range(400):
                conn.execute("""
                INSERT INTO conversation_summaries (
                    conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, last_user_input_time, app_data_dir
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    f"dummy-{i:04d}", f"Queue Job {i}", "", 1,
                    f"2026-10-02 {i//60:02d}:{i%60:02d}:00.000000+00:00", '[""]', "CASCADE_RUN_STATUS_IDLE",
                    f"2026-10-02 {i//60:02d}:{i%60:02d}:00.000000+00:00", "antigravity"
                ))
            conn.commit()

        # Seed mock backup_catalog.db
        with sqlite3.connect(mock_backup_cat) as conn:
            # cid_active: marked is_pinned=1, is_evicted=0
            conn.execute("""
            INSERT INTO backed_up_conversations (
                conversation_id, title, preview, step_count, last_modified_time,
                retention_status, has_db_file, has_brain_folder, is_pinned, is_evicted_from_live, workspace_uris
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cid_active, "Active Strategy Session", "Preview", 10,
                "2026-10-01 10:00:00.000000+00:00", "complete_active", 1, 0, 1, 0, '[""]'
            ))
            # cid_evicted: marked is_pinned=1, is_evicted=1 (evicted from live)
            conn.execute("""
            INSERT INTO backed_up_conversations (
                conversation_id, title, preview, step_count, last_modified_time,
                retention_status, has_db_file, has_brain_folder, is_pinned, is_evicted_from_live, workspace_uris
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cid_evicted, "Evicted Strategy Session", "Preview", 25,
                "2026-09-20 12:00:00.000000+00:00", "safeguarded_evicted", 1, 0, 1, 1, '[""]'
            ))
            conn.commit()

        from unittest.mock import patch
        try:
            with patch("core.restorer.BACKUP_CONVERSATIONS_DIR", mock_backup_conv), \
                 patch("core.restorer.BACKUP_BRAIN_DIR", mock_backup_brain), \
                 patch("core.restorer.BACKUP_ANNOTATIONS_DIR", test_dir / "ann_src"), \
                 patch("core.restorer.BACKUP_CATALOG_DB", mock_backup_cat), \
                 patch("core.restorer.LIVE_CONVERSATIONS_DIR", mock_live_conv), \
                 patch("core.restorer.LIVE_BRAIN_DIR", mock_live_brain), \
                 patch("core.restorer.LIVE_ANNOTATIONS_DIR", test_dir / "ann_dst"), \
                 patch("core.restorer.LIVE_SUMMARIES_DB", mock_live_sum):

                # Run Pin Sentry
                sentry_res = run_pin_sentry(
                    max_rank_threshold=350,
                    max_age_hours=12.0,
                    catalog_db_path=mock_backup_cat,
                    live_summaries_path=mock_live_sum,
                    live_conv_dir=mock_live_conv
                )

                self.assertEqual(sentry_res["protected_count"], 2)
                self.assertEqual(sentry_res["refreshed"], 1, "Should have refreshed cid_active keep-alive timestamp")
                self.assertEqual(sentry_res["auto_resurrected"], 1, "Should have auto-resurrected cid_evicted into live")

                # Verify cid_active timestamp was bumped in live_summaries.db
                with sqlite3.connect(mock_live_sum) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT last_modified_time FROM conversation_summaries WHERE conversation_id = ?", (cid_active,))
                    new_lmt = cur.fetchone()[0]
                    self.assertIn("2026-10-03", new_lmt, "Active session timestamp must be refreshed to today")

                # Verify cid_evicted was resurrected into mock_live_conv
                resurrected_live_db = mock_live_conv / f"{cid_evicted}.db"
                self.assertTrue(resurrected_live_db.exists(), "Evicted pinned session must be physically restored into live conversations")

                # Verify cid_evicted status upgraded in catalog
                with sqlite3.connect(mock_backup_cat) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT retention_status, is_evicted_from_live FROM backed_up_conversations WHERE conversation_id = ?", (cid_evicted,))
                    ret_stat, is_ev = cur.fetchone()
                    self.assertEqual(ret_stat, "complete_active")
                    self.assertEqual(is_ev, 0)

        finally:
            rmtree_robust(test_dir)

    def test_24_sync_live_to_backup_auto_protects_pinned(self):
        """Verify that sync_live_to_backup(auto_protect_pinned=True) auto-resurrects evicted pinned sessions."""
        test_dir = Path("D:/test_agy_sync_protect_pinned")
        if test_dir.exists():
            rmtree_robust(test_dir)
        test_dir.mkdir(parents=True, exist_ok=True)
        try:
            mock_live_sum = test_dir / "conversation_summaries.db"
            mock_live_conv = test_dir / "live_conv"
            mock_live_brain = test_dir / "live_brain"
            mock_backup_conv = test_dir / "backup_conv"
            mock_backup_brain = test_dir / "backup_brain"
            mock_backup_cat = test_dir / "backup_catalog.db"

            mock_live_conv.mkdir(parents=True, exist_ok=True)
            mock_live_brain.mkdir(parents=True, exist_ok=True)
            mock_backup_conv.mkdir(parents=True, exist_ok=True)
            mock_backup_brain.mkdir(parents=True, exist_ok=True)

            CatalogManager(mock_backup_cat)._init_db()

            cid_pinned = "c8901234-5678-4abc-def0-123456789abc"
            # Populate in backup
            b_db = mock_backup_conv / f"{cid_pinned}.db"
            with sqlite3.connect(b_db) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT, value TEXT);")
                conn.execute("INSERT INTO metadata VALUES ('test', 'pinned');")

            # Insert as evicted pinned in catalog
            with sqlite3.connect(mock_backup_cat) as conn:
                conn.execute("""
                INSERT INTO backed_up_conversations (
                    conversation_id, title, preview, step_count, last_modified_time,
                    retention_status, has_db_file, has_brain_folder, is_pinned, is_evicted_from_live, workspace_uris
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cid_pinned, "Pinned Evicted Session", "Preview", 12,
                    "2026-09-15 12:00:00.000000+00:00", "safeguarded_evicted", 1, 0, 1, 1, '[""]'
                ))
                conn.commit()

            # Init mock_live_sum
            with sqlite3.connect(mock_live_sum) as conn:
                conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT,
                    last_modified_time TEXT,
                    workspace_uris TEXT,
                    step_count INTEGER,
                    preview TEXT
                );
                """)
                conn.commit()

            from unittest.mock import patch
            with patch("core.backup_engine.LIVE_SUMMARIES_DB", mock_live_sum), \
                 patch("core.backup_engine.LIVE_CONVERSATIONS_DIR", mock_live_conv), \
                 patch("core.backup_engine.LIVE_BRAIN_DIR", mock_live_brain), \
                 patch("core.backup_engine.BACKUP_CONVERSATIONS_DIR", mock_backup_conv), \
                 patch("core.backup_engine.BACKUP_BRAIN_DIR", mock_backup_brain), \
                 patch("core.backup_engine.BACKUP_CATALOG_DB", mock_backup_cat), \
                 patch("core.pin_sentry.BACKUP_CATALOG_DB", mock_backup_cat), \
                 patch("core.pin_sentry.LIVE_SUMMARIES_DB", mock_live_sum), \
                 patch("core.pin_sentry.LIVE_CONVERSATIONS_DIR", mock_live_conv), \
                 patch("core.restorer.BACKUP_CONVERSATIONS_DIR", mock_backup_conv), \
                 patch("core.restorer.BACKUP_BRAIN_DIR", mock_backup_brain), \
                 patch("core.restorer.BACKUP_CATALOG_DB", mock_backup_cat), \
                 patch("core.restorer.LIVE_CONVERSATIONS_DIR", mock_live_conv), \
                 patch("core.restorer.LIVE_BRAIN_DIR", mock_live_brain), \
                 patch("core.restorer.LIVE_SUMMARIES_DB", mock_live_sum):

                res = sync_live_to_backup(auto_protect_pinned=True)
                self.assertTrue(res.get("success"))
                self.assertIn("pin_sentry", res)
                self.assertEqual(res["pin_sentry"]["auto_resurrected"], 1)

                # Verify resurrected into live
                self.assertTrue((mock_live_conv / f"{cid_pinned}.db").exists())

                # Verify upgraded in catalog
                with sqlite3.connect(mock_backup_cat) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT retention_status, is_evicted_from_live FROM backed_up_conversations WHERE conversation_id = ?", (cid_pinned,))
                    ret, is_ev = cur.fetchone()
                    self.assertEqual(ret, "complete_active")
                    self.assertEqual(is_ev, 0)
        finally:
            rmtree_robust(test_dir)

    def test_25_daemon_live_status_reporting(self):
        """Verify get_daemon_live_status accurately reflects process state without false positives."""
        from core.daemon import get_daemon_live_status
        status, detail, hb = get_daemon_live_status()
        self.assertIsInstance(status, str)
        self.assertIsInstance(detail, str)
        self.assertIsInstance(hb, dict)

    def test_26_daemon_subprocess_spawning_and_survival(self):
        """Verify detached background daemon subprocess spawns cleanly, survives import, and stops cleanly."""
        from core.daemon import (
            start_daemon_process,
            stop_daemon_process,
            is_pid_running,
            get_daemon_live_status
        )
        import time

        # Ensure no prior daemon is lingering
        stop_daemon_process()
        time.sleep(0.5)

        # Launch detached daemon subprocess with 10s interval
        success, res = start_daemon_process(interval=10)
        self.assertTrue(success, f"start_daemon_process failed: {res}")
        self.assertIsInstance(res, int)
        self.assertGreater(res, 0)
        daemon_pid = res

        try:
            # Verify PID is actively running at OS level
            self.assertTrue(is_pid_running(daemon_pid), f"Daemon process {daemon_pid} should be active")

            # Check status reporting reflects active daemon
            status, detail, hb = get_daemon_live_status()
            self.assertTrue(status.startswith("🟢"), f"Expected 🟢 Active status, got: {status}")

            # Verify idempotency: calling start_daemon_process again returns the same running PID
            success_again, res_again = start_daemon_process(interval=10)
            self.assertTrue(success_again)
            self.assertEqual(res_again, daemon_pid)
        finally:
            # Cleanly terminate the daemon
            stopped = stop_daemon_process()
            self.assertTrue(stopped)
            time.sleep(0.5)
            self.assertFalse(is_pid_running(daemon_pid), f"Daemon process {daemon_pid} should have stopped")
            status_after, _, _ = get_daemon_live_status()
            self.assertEqual(status_after, "⚪ Inactive")

if __name__ == "__main__":
    unittest.main()

