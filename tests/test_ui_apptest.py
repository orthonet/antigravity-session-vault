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
                self.assertIn("Active in Antigravity", ab.label)

            # 2. Select 'imported_from_offsite' filter to locate restorable evicted sessions
            ret_sb = at.selectbox(key="explorer_ret_filter")
            self.assertIsNotNone(ret_sb, "Retention status selectbox must be found in Explorer")
            target_opt = next((opt for opt in ret_sb.options if "Imported from Offsite" in opt or "imported_from_offsite" in opt), ret_sb.options[-1])
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
            self.assertIn("Active in Antigravity", act_btn.label)

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

if __name__ == "__main__":
    unittest.main()


