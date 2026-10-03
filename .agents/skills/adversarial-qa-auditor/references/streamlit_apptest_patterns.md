# Streamlit Headless Testing with AppTest: Patterns & Recipes

## 1. Setting Up AppTest

```python
from pathlib import Path
from streamlit.testing.v1 import AppTest

# Initialize AppTest pointing to your entrypoint
at = AppTest.from_file("../app.py", default_timeout=20)

# Pre-populate session state before the first script run
at.session_state["main_nav_tab"] = "📥 Target Tab"
at.session_state["custom_data"] = {"key": "value"}

# Execute initial script run
at.run()

# Always assert zero unhandled exceptions
assert len(at.exception) == 0, f"Exceptions raised on initial run: {at.exception}"
```

---

## 2. Interacting with Widgets

### Segmented Controls & Pills
```python
# Set value and trigger rerun
at.segmented_control(key="importer_filter_view").set_value("all").run()
assert len(at.exception) == 0
assert len(at.dataframe[0].value) == expected_all_count

# Test null-state deselection (simulating user toggling off active pill)
at.segmented_control(key="importer_filter_view").set_value(None).run()
assert len(at.exception) == 0
```

### Text Inputs & Search
```python
# Simulate typing into search box and pressing enter
at.text_input(key="table_search_input").set_value("SearchQuery").run()
assert len(at.exception) == 0
df_val = at.dataframe[0].value
assert len(df_val) <= total_rows
```

### Buttons & Cross-Tab Navigation
```python
# Find button by key
btn = at.button(key="btn_view_explorer")
assert btn is not None

# Click and run
btn.click().run()

# Verify callback executed and tab shifted
assert len(at.exception) == 0
assert at.session_state["main_nav_tab"] == "📂 Conversation Explorer"
```

---

## 3. Inspecting UI Outputs

### Dataframes
```python
# Access pandas DataFrame value
rendered_df = at.dataframe[0].value
assert "Title" in rendered_df.columns
assert len(rendered_df) == expected_count
```

### Metric Cards
```python
# Extract all metric values
metric_vals = [m.value for m in at.metric]
assert "401" in metric_vals
```

### Info, Warning, Error Banners
```python
# Check empty state messages
assert len(at.info) > 0
assert "No sessions match" in at.info[0].value
```
