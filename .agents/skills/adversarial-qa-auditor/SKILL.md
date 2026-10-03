---
name: adversarial-qa-auditor
description: >-
  Conducts an adversarial audit and automated headless verification of code changes.
  Use when developing, modifying, or testing user interfaces, data pipelines, stateful
  workflows, or whenever ensuring that unit tests pass without hidden presentation-layer
  blindspots, widget lifecycle collisions, silent data loss, or mutable state flakiness.
---

# Adversarial QA & Automated Verification Runbook

## Overview
When an AI assistant builds and verifies its own code, it naturally suffers from **Self-Grading Confirmation Bias**: testing the happy paths it anticipated while sharing the blindspots that created defects.

This skill provides an actionable methodology to audit implementations adversarially, construct headless presentation tests, and enforce mathematical invariants that prevent regressions.

---

## 1. The Blindspot Taxonomy

Before approving any solution, scan for these six common failure modes:

| Failure Mode | Mechanism | Warning Signs in Code | Remediation |
| :--- | :--- | :--- | :--- |
| **1. Layer Mismatch** | Backend unit tests pass 100%, but UI presentation script is completely unverified. | Tests import `core.*` or `models.*`, but never execute `app.py` or mount UI components. | Implement headless UI integration tests (`st.testing.v1.AppTest`, Playwright, React Testing Library). |
| **2. Widget Lifecycle Collision** | Keyed widget state modified downstream during script rerun. | `if st.button(...): st.session_state["widget_key"] = ...` | Use `on_click` callbacks defined at module scope; never mutate keyed widget states inline. |
| **3. Snapshot Coupling** | Tests assert literal integers that break when data mutates. | `self.assertEqual(count, 22)` | Replace literals with mathematical conservation laws (`total == sum(buckets)`). |
| **4. Silent Truncation** | Data silently dropped without user notification or pagination. | Unconditional slices `items[:50]`, `results[:10]`, unhandled `else` fallbacks. | Assert rendered count equals source count or enforce explicit pagination UI controls. |
| **5. Null-State Deselection** | Toggleable widgets (segmented controls, checkboxes) returning `None`. | Code assumes widget always returns a non-null string. | Implement null-safe fallbacks: `active_key = selected_key or default_key`. |
| **6. Label-Logic Coupling** | Branching logic evaluates human-formatted display strings. | `if "Already in Vault" in label:` | Decouple machine keys from UI labels using `format_func` dictionaries. |

---

## 2. Headless UI Testing Protocol (Streamlit)

Do NOT rely on manual clicking or browser automation tools (Selenium/Playwright) when testing Streamlit apps. Use Streamlit's official in-process headless testing runner:

```python
from streamlit.testing.v1 import AppTest

# 1. Initialize from target script with session state preloaded
at = AppTest.from_file("../app.py", default_timeout=20)
at.session_state["main_nav_tab"] = "📥 Target Tab"
at.run()

# 2. Assert zero exceptions during initial render
self.assertEqual(len(at.exception), 0, f"Render exceptions: {at.exception}")

# 3. Simulate widget interaction (Pills / Segmented Controls)
at.segmented_control(key="target_filter").set_value("all").run()
self.assertEqual(len(at.exception), 0)
self.assertEqual(len(at.dataframe[0].value), total_expected_rows)

# 4. Simulate button clicks and cross-tab navigation
btn = at.button(key="btn_navigate")
btn.click().run()
self.assertEqual(len(at.exception), 0)
self.assertEqual(at.session_state["main_nav_tab"], "📂 Expected Tab")
```

For complete recipes, see [Streamlit AppTest Patterns](./references/streamlit_apptest_patterns.md).

---

## 3. Mathematical Invariant Property Testing

Avoid brittle static counts:
```python
# ❌ FRAGILE: Breaks as soon as real operations run
self.assertEqual(res["matching_evicted"], 22)
self.assertEqual(res["already_archived"], 378)

# ✅ INVARIANT: Holds true across all database states
reconciled_sum = (
    res["matching_evicted"]
    + res["brand_new"]
    + res["missing_assets"]
    + res["newer_content"]
    + res["already_archived"]
)
self.assertEqual(res["total_discovered"], reconciled_sum, "100% of discovered records must be accounted for")
self.assertEqual(
    res["actionable_count"],
    res["matching_evicted"] + res["brand_new"] + res["missing_assets"] + res["newer_content"]
)
```

---

## 4. The Subagent Auditor Delegation Pattern

When performing complex or high-risk tasks, decouple the builder from the auditor using Antigravity subagents:

```python
invoke_subagent(
    Subagents=[{
        "TypeName": "quality-auditor",
        "Role": "Adversarial Code & UX Auditor",
        "Prompt": """
        Conduct an adversarial review of recent code changes:
        1. Scan for inline widget state modifications (Streamlit lifecycle violations).
        2. Verify that headless AppTest integration tests exist for all new UI controls.
        3. Confirm test assertions evaluate mathematical invariants rather than transient literals.
        4. Attempt to break the implementation with edge cases (empty states, widget deselections).
        Run the test suite and report all defects with file paths and line numbers.
        """
    }]
)
```

---

## 5. Pre-Delivery 6-Point Verification Checklist

Before reporting completion to the user, ensure all 6 gates are satisfied:
1. [x] **Layer Completeness**: Both backend data routines AND frontend/UI presentation scripts have automated tests.
2. [x] **Widget Lifecycle Contract**: All cross-tab navigation and keyed widget mutations use `on_click` callbacks (zero inline `st.session_state[widget_key]` assignments).
3. [x] **Reactivity & Null Safety**: Widgets handle deselection (`None`) and reactive state updates cleanly.
4. [x] **Conservation**: 100% of discovered/filtered data is accounted for; zero unannounced silent truncations.
5. [x] **Idempotence**: Tests pass cleanly against both fresh and pre-existing/mutated database states.
6. [x] **Automated Suite Run**: Terminal test command executed with **0 failures and 0 errors**.
