import unittest
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from core.importer import scan_offsite_source

class TestStreamlitUIAppTest(unittest.TestCase):
    """
    Automated Headless UI Integration Tests using Streamlit's official AppTest framework.
    Ensures UI components, segmented filters, and reactive data tables actively respond
    to user interactions without relying on fragile string matching or manual clicking.
    """

    def test_importer_tab_filter_reactivity(self):
        """Verify that selecting segmented filters in Tab 4 actively updates the rendered dataframe."""
        acronis_path = Path("D:/Acronis/antigravity-2026-09-20")
        if not acronis_path.exists():
            self.skipTest("Acronis backup folder not found on D: drive")

        scan_data = scan_offsite_source(acronis_path)
        total_discovered = scan_data["total_discovered"]
        actionable_count = scan_data["actionable_count"]
        identical_count = scan_data["already_archived"]

        self.assertGreater(total_discovered, 0)
        self.assertEqual(total_discovered, actionable_count + identical_count + scan_data["brand_new"])

        # Initialize headless AppTest from app.py
        at = AppTest.from_file("../app.py", default_timeout=20)
        at.session_state["main_nav_tab"] = "📥 Offsite Backup Importer"
        at.session_state["importer_source_input"] = str(acronis_path)
        at.session_state["scan_result"] = scan_data
        at.run()

        self.assertEqual(len(at.exception), 0, f"AppTest raised unexpected exceptions: {at.exception}")

        # 1. Default Filter Check: defaults to 'actionable' if actionable_count > 0, else 'all'
        expected_default = actionable_count if actionable_count > 0 else total_discovered
        if expected_default > 0:
            initial_df = at.dataframe[0].value
            self.assertEqual(len(initial_df), expected_default, f"Default filter should render {expected_default} rows")
        else:
            self.assertGreater(len(at.info), 0, "Empty state should render st.info message")

        # 2. Select 'all': should reactively update to total_discovered rows
        at.segmented_control(key="importer_segmented_filter_view").set_value("all").run()
        self.assertEqual(len(at.exception), 0)
        self.assertGreater(len(at.dataframe), 0)
        all_df = at.dataframe[0].value
        self.assertEqual(len(all_df), total_discovered, f"'All Discovered' filter must render {total_discovered} rows")

        # 3. Select 'identical': should reactively update to identical_count rows
        at.segmented_control(key="importer_segmented_filter_view").set_value("identical").run()
        self.assertEqual(len(at.exception), 0)
        if identical_count > 0:
            identical_df = at.dataframe[0].value
            self.assertEqual(len(identical_df), identical_count, f"'Verified Identical' filter must render {identical_count} rows")
        else:
            self.assertGreater(len(at.info), 0)

        # 4. Select 'actionable': should render actionable_count rows or info banner if 0
        at.segmented_control(key="importer_segmented_filter_view").set_value("actionable").run()
        self.assertEqual(len(at.exception), 0)
        if actionable_count > 0:
            actionable_df = at.dataframe[0].value
            self.assertEqual(len(actionable_df), actionable_count, f"'Actionable Only' filter must render {actionable_count} rows")
        else:
            self.assertGreater(len(at.info), 0, "Zero actionable items should render info banner")

        # 5. In-table Search Check: search for a specific title keyword
        if len(all_df) > 0:
            first_title_word = str(all_df.iloc[0]["Title"]).split()[0]
            at.segmented_control(key="importer_segmented_filter_view").set_value("all").run()
            at.text_input(key="importer_table_search_input").set_value(first_title_word).run()
            self.assertEqual(len(at.exception), 0)
            self.assertGreater(len(at.dataframe), 0)
            searched_df = at.dataframe[0].value
            self.assertGreater(len(searched_df), 0, f"Searching for '{first_title_word}' should return matching sessions")
            self.assertLessEqual(len(searched_df), total_discovered, "Search filter must narrow or match the full list")

    def test_importer_ui_reconciliation_cards(self):
        """Verify that the 4 summary metric cards render the mathematically reconciled counts."""
        # Use synthetic scan data to ensure 100% deterministic assertion of all 4 metric cards
        synthetic_scan = {
            "valid": True,
            "total_discovered": 100,
            "matching_evicted": 15,
            "missing_assets": 5,
            "newer_content": 10,
            "brand_new": 20,
            "already_archived": 50,
            "actionable_count": 50,
            "items": []
        }

        at = AppTest.from_file("../app.py", default_timeout=20)
        at.session_state["main_nav_tab"] = "📥 Offsite Backup Importer"
        at.session_state["importer_source_input"] = "D:/MockBackup"
        at.session_state["scan_result"] = synthetic_scan
        at.run()

        # Find metrics in the Importer tab
        metric_values = [m.value for m in at.metric]
        self.assertIn("100", metric_values, "Total Discovered 100 must appear in metric cards")
        self.assertIn("15", metric_values, "Evicted upgrades count 15 must appear in metric cards")
        self.assertIn("50", metric_values, "Verified Identical 50 must appear in metric cards")

    def test_synthetic_importer_filter_reactivity(self):
        """Hermetic test of filter reactivity using synthetic items (runs anywhere without disk dependency)."""
        items = [
            {"conversation_id": f"act-{i}", "title": f"Actionable Item {i}", "is_actionable": True, "action_type": "UPGRADE_EVICTED", "action_label": "Restores evicted DB", "has_db": True, "has_brain": True, "archive_retention": "metadata_only_evicted", "workspace": "test"}
            for i in range(5)
        ] + [
            {"conversation_id": f"ident-{i}", "title": f"Identical Item {i}", "is_actionable": False, "action_type": "ALREADY_UP_TO_DATE", "action_label": "Verified Identical", "has_db": True, "has_brain": True, "archive_retention": "imported_from_offsite", "workspace": "test"}
            for i in range(10)
        ]
        synthetic_scan = {
            "valid": True,
            "total_discovered": 15,
            "matching_evicted": 5,
            "missing_assets": 0,
            "newer_content": 0,
            "brand_new": 0,
            "already_archived": 10,
            "actionable_count": 5,
            "items": items
        }

        at = AppTest.from_file("../app.py", default_timeout=20)
        at.session_state["main_nav_tab"] = "📥 Offsite Backup Importer"
        at.session_state["importer_source_input"] = "D:/MockBackup"
        at.session_state["scan_result"] = synthetic_scan
        at.run()

        # 1. Default should be 'actionable' (5 rows)
        self.assertEqual(len(at.dataframe[0].value), 5)

        # 2. Switch to 'all' (15 rows)
        at.segmented_control(key="importer_segmented_filter_view").set_value("all").run()
        self.assertEqual(len(at.dataframe[0].value), 15)

        # 3. Switch to 'identical' (10 rows)
        at.segmented_control(key="importer_segmented_filter_view").set_value("identical").run()
        self.assertEqual(len(at.dataframe[0].value), 10)

        # 4. Search in table
        at.text_input(key="importer_table_search_input").set_value("Item 3").run()
        self.assertEqual(len(at.dataframe[0].value), 1)

    def test_import_receipt_navigation_and_dismiss(self):
        """Verify that clicking 'View Upgraded Sessions in Explorer' switches tabs safely without StreamlitWidgetAlreadyInstantiatedError."""
        receipt = {
            "upgraded_records": 10,
            "new_records": 2,
            "imported_dbs": 10,
            "imported_brains": 10,
            "failed_items": []
        }

        at = AppTest.from_file("../app.py", default_timeout=20)
        at.session_state["main_nav_tab"] = "📥 Offsite Backup Importer"
        at.session_state["import_receipt"] = receipt
        at.run()

        self.assertEqual(len(at.exception), 0, f"Initial render with receipt raised: {at.exception}")

        # 1. Click 'View Upgraded Sessions in Explorer' button
        btn = at.button(key="btn_view_upgraded_explorer")
        self.assertIsNotNone(btn, "btn_view_upgraded_explorer must exist when receipt is active")
        btn.click().run()

        # 2. Must not throw StreamlitWidgetAlreadyInstantiatedError
        self.assertEqual(len(at.exception), 0, f"Clicking View in Explorer raised widget conflict: {at.exception}")

        # 3. Session state and navigation must switch to Conversation Explorer
        self.assertEqual(at.session_state["main_nav_tab"], "📂 Conversation Explorer")

        # 4. Return to Importer tab and test Dismiss Receipt button
        at.session_state["main_nav_tab"] = "📥 Offsite Backup Importer"
        at.session_state["import_receipt"] = receipt
        at.run()
        self.assertIn("import_receipt", at.session_state)

        dismiss_btn = at.button(key="btn_dismiss_receipt")
        dismiss_btn.click().run()
        self.assertEqual(len(at.exception), 0)
        self.assertNotIn("import_receipt", at.session_state, "Dismissing receipt must clear import_receipt from session state")

    def test_explorer_restore_button_interaction(self):
        """Verify that active sessions show 'Active in Antigravity' and restorable evicted sessions have 'Restore to Live'."""
        def fake_restore(cid, target_workspace_uri=None, overwrite_live=False, catalog_manager=None):
            return {
                "success": True,
                "conversation_id": cid,
                "restored_at": "2026-10-03T00:00:00Z",
                "workspace": target_workspace_uri,
                "message": f"Conversation '{cid}' restored successfully! It is now slot #1 in your Antigravity sidebar."
            }

        with patch("core.restorer.restore_conversation", side_effect=fake_restore):
            at = AppTest.from_file("../app.py", default_timeout=20)
            at.session_state["main_nav_tab"] = "📂 Conversation Explorer"
            at.run()

            self.assertEqual(len(at.exception), 0, f"Initial render raised: {at.exception}")

            # 1. Assert active sessions render 'Active in Antigravity' disabled button
            active_btns = [b for b in at.button if b.key and b.key.startswith("act_")]
            self.assertGreater(len(active_btns), 0, "Explorer must render Active in Antigravity indicators for active sessions")
            for ab in active_btns[:3]:
                self.assertTrue(ab.disabled)
                self.assertIn("Active in AGY", ab.label)

            # 2. Select 'imported_from_offsite' filter to locate restorable evicted sessions
            ret_sb = at.selectbox(key="explorer_ret_filter")
            self.assertIsNotNone(ret_sb, "Retention status selectbox must be found in Explorer")
            target_opt = next((opt for opt in ret_sb.options if "Recovered from Offsite" in opt or "imported_from_offsite" in opt), ret_sb.options[-2])
            ret_sb.set_value(target_opt).run()
            self.assertEqual(len(at.exception), 0)

            restore_btns = [b for b in at.button if b.key and b.key.startswith("res_") and not b.disabled]
            self.assertGreater(len(restore_btns), 0, "Explorer must render at least one enabled 'Restore to Live' button for evicted sessions")

            target_btn = restore_btns[0]
            self.assertIn("Restore to Live", target_btn.label)
            target_btn.click().run()

            # Must not raise StreamlitWidgetAlreadyInstantiatedError or any other exception
            self.assertEqual(len(at.exception), 0, f"Clicking restore button raised: {at.exception}")

            # Assert feedback banner or success is rendered
            success_texts = [s.value for s in at.success]
            self.assertTrue(
                any("Restored to Slot #1" in s for s in success_texts),
                f"Expected restore confirmation banner, got: {success_texts}"
            )

    def test_viewer_restore_button_interaction(self):
        """Verify that Viewer renders 'Active in Antigravity' for active sessions and autopopulates workspace selector for evicted sessions."""
        from core.catalog import CatalogManager
        cat = CatalogManager()

        def fake_restore(cid, target_workspace_uri=None, overwrite_live=False, catalog_manager=None):
            return {
                "success": True,
                "conversation_id": cid,
                "restored_at": "2026-10-03T00:00:00Z",
                "workspace": target_workspace_uri,
                "message": f"Conversation '{cid}' restored successfully! It is now slot #1 in your Antigravity sidebar."
            }

        with patch("core.restorer.restore_conversation", side_effect=fake_restore):
            # 1. Active session test: must display 'Active in Antigravity' and omit restore button
            active_convs = cat.query_conversations(retention_filter="complete_active", limit=1)
            self.assertTrue(len(active_convs) > 0, "Must have at least one active conversation in catalog")
            active_cid = active_convs[0]["conversation_id"]

            at_active = AppTest.from_file("../app.py", default_timeout=20)
            at_active.session_state["main_nav_tab"] = "📖 Transcript & Plan Viewer"
            at_active.session_state["selected_cid"] = active_cid
            at_active.run()

            self.assertEqual(len(at_active.exception), 0, f"Active viewer render raised: {at_active.exception}")
            act_btn = at_active.button(key="viewer_act_btn")
            self.assertIsNotNone(act_btn, "Active session in Viewer must render viewer_act_btn")
            self.assertTrue(act_btn.disabled)
            self.assertIn("Active in AGY", act_btn.label)

            # 2. Evicted session test: must render 'Restore to Sidebar #1' and autopopulated workspace selector
            evicted_convs = cat.query_conversations(retention_filter="imported_from_offsite", limit=1)
            self.assertTrue(len(evicted_convs) > 0, "Must have at least one restorable evicted conversation")
            evicted_cid = evicted_convs[0]["conversation_id"]

            at_evicted = AppTest.from_file("../app.py", default_timeout=20)
            at_evicted.session_state["main_nav_tab"] = "📖 Transcript & Plan Viewer"
            at_evicted.session_state["selected_cid"] = evicted_cid
            at_evicted.run()

            self.assertEqual(len(at_evicted.exception), 0, f"Evicted viewer render raised: {at_evicted.exception}")

            # Assert target workspace selector exists and is autopopulated
            ws_select = at_evicted.selectbox(key="viewer_target_ws")
            self.assertIsNotNone(ws_select, "Target Workspace selectbox must exist in Viewer for evicted session")
            self.assertGreater(len(ws_select.options), 0)

            # Find and click viewer restore button
            btn = at_evicted.button(key="viewer_restore_btn")
            self.assertIsNotNone(btn, "viewer_restore_btn must exist in Viewer for restorable evicted conversation")
            btn.click().run()

            # Must execute cleanly with 0 exceptions
            self.assertEqual(len(at_evicted.exception), 0, f"Clicking viewer restore button raised: {at_evicted.exception}")

            # Assert feedback banner
            success_texts = [s.value for s in at_evicted.success]
            self.assertTrue(
                any("Restored to Slot #1" in s for s in success_texts),
                f"Expected restore confirmation in Viewer, got: {success_texts}"
            )

    def test_sidebar_appearance_removed_and_archive_path_rendered(self):
        """Verify that the Appearance selector is removed from the sidebar and Archive Path caption is rendered."""
        at = AppTest.from_file("../app.py", default_timeout=20)
        at.run()

        self.assertEqual(len(at.exception), 0, f"App execution raised: {at.exception}")

        # 1. Appearance selectbox (vault_theme_override) must NOT exist
        theme_selectboxes = [sb for sb in at.selectbox if sb.key == "vault_theme_override"]
        self.assertEqual(len(theme_selectboxes), 0, "Appearance / template selectbox must be removed from the sidebar")

        # 2. Archive Path must be present in captions
        caption_texts = [c.value for c in at.caption]
        has_archive_path = any("Archive Path" in c for c in caption_texts)
        self.assertTrue(has_archive_path, f"Archive Path caption must be present in sidebar. Captions found: {caption_texts}")

    def test_safeguarded_evicted_ui_filtering_and_sidebar_metrics(self):
        """Verify sidebar metrics render new labels and Explorer filters safeguarded_evicted sessions with restore buttons."""
        def fake_restore(cid, target_workspace_uri=None, overwrite_live=False, catalog_manager=None):
            return {
                "success": True,
                "conversation_id": cid,
                "restored_at": "2026-10-03T00:00:00Z",
                "workspace": target_workspace_uri,
                "message": f"Conversation '{cid}' restored successfully! It is now slot #1 in your Antigravity sidebar."
            }

        with patch("core.restorer.restore_conversation", side_effect=fake_restore):
            at = AppTest.from_file("../app.py", default_timeout=20)
            at.session_state["main_nav_tab"] = "📂 Conversation Explorer"
            at.run()

            self.assertEqual(len(at.exception), 0, f"AppTest raised unexpected exception: {at.exception}")

            # 1. Assert sidebar metrics contain the new labels
            metric_labels = [m.label for m in at.metric if m.label]
            self.assertIn("🟢 Active in AGY", metric_labels, f"Expected '🟢 Active in AGY' in sidebar metrics, got: {metric_labels}")
            self.assertIn("🛡️ Safeguarded", metric_labels, f"Expected '🛡️ Safeguarded' in sidebar metrics, got: {metric_labels}")
            self.assertIn("📥 Offsite Restored", metric_labels, f"Expected '📥 Offsite Restored' in sidebar metrics, got: {metric_labels}")
            self.assertIn("📋 Metadata Only", metric_labels, f"Expected '📋 Metadata Only' in sidebar metrics, got: {metric_labels}")

            # 2. Select 'safeguarded_evicted' in retention filter
            ret_sb = at.selectbox(key="explorer_ret_filter")
            self.assertIsNotNone(ret_sb)
            target_opt = next((opt for opt in ret_sb.options if "Safeguarded in Vault" in opt), None)
            self.assertIsNotNone(target_opt, f"Expected 'Safeguarded in Vault' option in {ret_sb.options}")
            ret_sb.set_value(target_opt).run()

            self.assertEqual(len(at.exception), 0)

            # 3. Assert safeguarded badge is rendered in the HTML/markdown cards
            markdown_texts = [m.value for m in at.markdown]
            has_safeguarded_badge = any("badge-safeguarded" in text or "🛡️ Safeguarded (Evicted)" in text for text in markdown_texts)
            self.assertTrue(has_safeguarded_badge, "Expected '🛡️ Safeguarded (Evicted)' badge in rendered conversation cards")

            # 4. Assert enabled '⚡ Restore to Live' buttons exist for safeguarded sessions
            restore_btns = [b for b in at.button if b.key and b.key.startswith("res_") and not b.disabled]
            self.assertGreater(len(restore_btns), 0, "Safeguarded sessions must provide an enabled '⚡ Restore to Live' button")
            self.assertIn("Restore to Live", restore_btns[0].label)

            # 5. Click restore button and verify reactive feedback
            restore_btns[0].click().run()
            self.assertEqual(len(at.exception), 0)
            success_texts = [s.value for s in at.success]
            self.assertTrue(
                any("Restored to Slot #1" in s for s in success_texts),
                f"Expected restore confirmation banner, got: {success_texts}"
            )

    def test_sidebar_sync_live_now_and_tab5_pin_sentry(self):
        """Verify sidebar Sync Live Now executes cleanly, renders full-width message, and Tab 5 renders Pin Sentry protection."""
        def fake_sync(auto_protect_pinned=True):
            return {
                "success": True,
                "synced_at": "2026-10-03T00:00:00Z",
                "synced_dbs": 2,
                "synced_brains": 2,
                "pin_sentry": {"protected_count": 11, "auto_resurrected": 0, "refreshed": 1, "errors": []},
                "errors": []
            }

        with patch("core.backup_engine.sync_live_to_backup", side_effect=fake_sync):
            at = AppTest.from_file("../app.py", default_timeout=20)
            at.run()

            self.assertEqual(len(at.exception), 0, f"App execution raised: {at.exception}")

            # 1. Verify '💼 Workspaces' and '⭐ Pinned' metrics exist in sidebar
            sidebar_metric_labels = [m.label for m in at.metric if m.label]
            self.assertIn("💼 Workspaces", sidebar_metric_labels, "Expected '💼 Workspaces' metric in sidebar")
            self.assertIn("⭐ Pinned", sidebar_metric_labels, "Expected '⭐ Pinned' metric in sidebar")

            # 2. Verify '🔄 Sync Live Now' button exists in sidebar and can be clicked
            sync_btn = next((b for b in at.button if "Sync Live Now" in b.label), None)
            self.assertIsNotNone(sync_btn, "Sync Live Now button must exist in sidebar")
            sync_btn.click().run()

            self.assertEqual(len(at.exception), 0, f"Clicking Sync Live Now raised: {at.exception}")
            # Assert full-width success banner renders
            self.assertTrue(
                any("Synced 2 DBs" in s.value for s in at.success),
                f"Expected sync success banner, got: {[s.value for s in at.success]}"
            )

            # 3. Switch to Tab 5 (Health & Settings) and assert Pin Sentry controls
            at.session_state["main_nav_tab"] = "⚙️ Health & Settings"
            at.run()
            self.assertEqual(len(at.exception), 0)

            # Assert Pinned Sessions and Active in Antigravity metrics
            metric_labels = [m.label for m in at.metric if m.label]
            self.assertIn("⭐ Protected Pinned Sessions", metric_labels)
            self.assertIn("🟢 Active in Antigravity", metric_labels)

            # Assert Run Pin Sentry button exists
            sentry_btn = at.button(key="btn_run_sentry_now")
            self.assertIsNotNone(sentry_btn, "btn_run_sentry_now must exist in Tab 5")

    def test_sidebar_daemon_start_and_stop_lifecycle(self):
        """Verify sidebar Start and Stop daemon buttons interact cleanly and render full-width status feedback."""
        with patch("core.daemon.get_daemon_live_status", return_value=("⚪ Inactive", "Daemon not running", {})), \
             patch("core.daemon.start_daemon_process", return_value=(True, 4242)):
            at = AppTest.from_file("../app.py", default_timeout=20)
            at.run()
            self.assertEqual(len(at.exception), 0)

            # Start button should be visible when Inactive
            start_btn = at.button(key="sidebar_start_daemon_btn")
            self.assertIsNotNone(start_btn, "Start daemon button must exist when daemon is inactive")
            start_btn.click().run()
            self.assertEqual(len(at.exception), 0)
            self.assertTrue(
                any("Sync daemon started (PID 4242)" in s.value for s in at.success),
                f"Expected start confirmation banner, got: {[s.value for s in at.success]}"
            )

        with patch("core.daemon.get_daemon_live_status", return_value=("🟢 Active", "PID 4242 • Synced 5s ago", {})), \
             patch("core.daemon.stop_daemon_process", return_value=True):
            at = AppTest.from_file("../app.py", default_timeout=20)
            at.run()
            self.assertEqual(len(at.exception), 0)

            # Stop button should be visible when Active
            stop_btn = at.button(key="sidebar_stop_daemon_btn")
            self.assertIsNotNone(stop_btn, "Stop daemon button must exist when daemon is active")
            stop_btn.click().run()
            self.assertEqual(len(at.exception), 0)
            self.assertTrue(
                any("Sync daemon stopped cleanly" in i.value for i in at.info),
                f"Expected stop confirmation banner, got: {[i.value for i in at.info]}"
            )

    def test_pinned_badge_display_and_filter(self):
        """Verify that pinned conversations render the ⭐ Pinned badge and filter correctly by Category."""
        at = AppTest.from_file("../app.py", default_timeout=20)
        at.session_state["main_nav_tab"] = "📂 Conversation Explorer"
        at.run()
        self.assertEqual(len(at.exception), 0, f"App execution raised: {at.exception}")

        # Filter by Category = Pinned
        cat_sb = at.selectbox(key="explorer_cat_filter")
        self.assertIsNotNone(cat_sb)
        pinned_opt = next((opt for opt in cat_sb.options if "Pinned" in opt), None)
        self.assertIsNotNone(pinned_opt, f"Expected 'Pinned' option in category filter, got: {cat_sb.options}")

        cat_sb.set_value(pinned_opt).run()
        self.assertEqual(len(at.exception), 0)

        # Assert markdown contains ⭐ Pinned badge
        markdown_texts = [m.value for m in at.markdown]
        self.assertTrue(
            any("badge-pinned" in m or "⭐ Pinned" in m for m in markdown_texts),
            "Filtered pinned list must render ⭐ Pinned badge in HTML"
        )

if __name__ == "__main__":
    unittest.main()


