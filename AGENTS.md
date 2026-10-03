# Antigravity Session Vault: Engineering & Verification Rules

This project enforces strict autonomous quality assurance standards to prevent self-grading confirmation bias, presentation-layer regressions, and silent data loss.

---

## 1. Dual-Layer Verification Mandate

Every feature, refactor, or bug fix modifying user-facing functionality MUST verify both layers:
- **Backend Layer**: Unit tests in `tests/test_vault.py` testing database transactions, sync idempotency, and data models.
- **Presentation Layer**: Headless UI tests in `tests/test_ui_apptest.py` utilizing Streamlit's official `st.testing.v1.AppTest` framework.
- **Rule**: Never declare a UI feature or bug fix verified based solely on backend unit test passes. Interactive elements (buttons, filters, search inputs, data grids) must be programmatically clicked and asserted in-process.

---

## 2. Streamlit Widget Lifecycle & Cross-Tab Navigation Contract

- **NEVER EVENT**: Never modify `st.session_state[widget_key]` inside an inline conditional block (e.g. `if st.button(...): st.session_state["main_nav_tab"] = ...`) after that widget has been instantiated. This causes a fatal `StreamlitWidgetAlreadyInstantiatedError`.
- **Mandatory Pattern**: All programmatic cross-tab transitions and widget state changes MUST execute via `on_click` callbacks defined at the module top level (e.g., `navigate_to_explorer`, `open_conversation_viewer`). Callbacks execute before the script rerun starts and before widget re-instantiation.
- **Null Safety**: Streamlit widgets (such as `st.segmented_control`) return `None` when a user clicks the active option to deselect it. All filter logic must provide a null-safe fallback (e.g. `active_filter = selected_filter or default_key`).

---

## 3. Mathematical Invariant Property Testing

- **Rule**: Tests must assert immutable conservation laws and properties rather than transient static integers that break as database contents mutate.
  - **Conservation Law**: `total_discovered == matching_evicted + brand_new + missing_assets + newer_content + already_archived`.
  - **Actionable Sum**: `actionable_count == matching_evicted + brand_new + missing_assets + newer_content`.
  - **Ordering Contract**: All actionable items strictly precede non-actionable items in rendered datasets.
  - **Monotonicity**: Ingesting offsite data can upgrade an evicted session (`metadata_only_evicted` -> `imported_from_offsite`), but can NEVER downgrade an active session (`complete_active`).

---

## 4. Zero Silent Truncation

- **Rule**: Never silently slice dataframes or collections (e.g., `items[:50]`) without explicit user-facing pagination controls and total count indicators. Rendered items must equal source items unless the user explicitly filters or paginates.

---

## 5. SQLite WAL Concurrency & Safe File Operations

- **Concurrency**: All database connections must specify `sqlite3.connect(..., timeout=10.0)` and handle `sqlite3.OperationalError: database is locked` gracefully.
- **Windows Read-Only Files**: All file writes, copies, and directory sweeps must use `copy_file_robust`, `sync_directory_tree`, and `rmtree_robust` from `core/vault_sync.py` to prevent `PermissionError` when operating on Git loose objects or read-only files.

---

## 6. Pre-Delivery Verification Gate

Before concluding any turn:
1. Compile modified Python files: `python -m py_compile app.py core/*.py`.
2. Run the unified test suite: `python -m unittest discover tests -v` (or execute `run_tests.bat`).
3. Ensure **0 failures and 0 errors** across all backend and headless UI tests.
4. Record objective, root cause, artifacts, and verification evidence in `docs/JOURNAL.md`.
