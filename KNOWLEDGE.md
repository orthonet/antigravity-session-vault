# Technical Knowledge Base: Google Antigravity Storage Internals

**Document ID:** AGY-KB-001  
**Classification:** Technical Reference / Reverse-Engineered Internals  
**Date:** October 2, 2026  
**Scope:** File layout, SQLite schemas, binary protobuf payload format, eviction mechanics, and revival protocols for Google Antigravity.

---

## 1. Antigravity Filesystem Layout

On Windows, all Antigravity runtime, trajectory, and session data is housed under:
`%USERPROFILE%\.gemini\antigravity\` (typically `C:\Users\<user>\.gemini\antigravity\`).

```
~/.gemini/antigravity/
├── conversation_summaries.db         <-- Global SQLite catalog (powers the sidebar history)
├── conversation_summaries.db-shm     <-- SQLite Shared Memory file (WAL mode)
├── conversation_summaries.db-wal     <-- SQLite Write-Ahead Log
├── agyhub_summaries_proto.pb         <-- Serialized protobuf export of summaries
├── antigravity_state.pbtxt           <-- Installation UUID, onboarding flags, migrations
├── annotations/                      <-- Protobuf text files storing per-conversation UI flags
│   ├── <conversation_id>.pbtxt       <-- Pinned status, last viewed timestamp
│   └── ...
├── conversations/                    <-- Low-level trajectory SQLite databases
│   ├── <conversation_id>.db          <-- Full session trajectory (steps, tool payloads, code)
│   └── ...
└── brain/                            <-- Per-conversation directory structure
    ├── <conversation_id>/            <-- Subfolder named by Conversation UUID
    │   ├── .system_generated/
    │   │   ├── logs/
    │   │   │   ├── transcript.jsonl      <-- High-level JSONL chat stream
    │   │   │   └── transcript_full.jsonl <-- Untruncated message logs
    │   │   └── steps/<idx>/output.txt    <-- Stdout/stderr of executed CLI tools
    │   ├── scratch/                      <-- Temporary scripts created during session
    │   └── *.md, *.metadata.json         <-- Generated markdown artifacts
    └── tempmediaStorage/             <-- Ephemeral images, audio, browser recordings
```

> [!IMPORTANT]
> - `conversations/` contains **flat `.db` files**, one per conversation.
> - `brain/` contains **subdirectories**, one per conversation.
> - `annotations/` contains **`.pbtxt` text files** tracking UI metadata such as whether a session was pinned by the user.

---

## 2. Schema Specification: `conversation_summaries.db`

This SQLite database serves as the catalog displayed in Antigravity's "Past Conversations" sidebar.

### 2.1 Table Definition: `conversation_summaries`
```sql
CREATE TABLE `conversation_summaries` (
  `conversation_id` TEXT PRIMARY KEY,
  `title` TEXT NOT NULL DEFAULT "",
  `preview` TEXT NOT NULL DEFAULT "",
  `step_count` INTEGER NOT NULL DEFAULT 0,
  `last_modified_time` DATETIME NOT NULL,
  `workspace_uris` TEXT NOT NULL,
  `status` TEXT NOT NULL DEFAULT "",
  `source` TEXT NOT NULL DEFAULT "",
  `project_id` TEXT NOT NULL DEFAULT "",
  `agent_name` TEXT NOT NULL DEFAULT "",
  `parent_conversation_id` TEXT NOT NULL DEFAULT "",
  `nesting_depth` INTEGER NOT NULL DEFAULT 0,
  `battle_id` TEXT NOT NULL DEFAULT "",
  `winning_conversation_id` TEXT NOT NULL DEFAULT "",
  `not_fully_idle` NUMERIC NOT NULL DEFAULT false,
  `killed` NUMERIC NOT NULL DEFAULT false,
  `last_user_input_time` DATETIME NOT NULL,
  `last_user_input_step_index` INTEGER NOT NULL DEFAULT -1,
  `app_data_dir` TEXT NOT NULL DEFAULT "",
  `raw_summary` BLOB,
  `group_id` TEXT NOT NULL DEFAULT ""
);

CREATE INDEX `idx_conversation_summaries_last_user_input_time` 
  ON `conversation_summaries`(`last_user_input_time`);

CREATE INDEX `idx_conversation_summaries_last_modified_time` 
  ON `conversation_summaries`(`last_modified_time`);
```

### 2.2 Column Semantics
* `conversation_id`: Canonical UUID string (e.g. `be8c334a-1c51-47bf-8055-11a28b10a8cf`).
* `title`: User-defined or auto-generated session title. If empty, the IDE UI falls back to `preview`.
* `preview`: Short summary of the primary task or prompt intent.
* `step_count`: Total trajectory steps executed.
* `last_modified_time`: ISO-8601 UTC timestamp string with timezone (e.g. `2026-10-02 14:48:22.4815336+00:00`). **Crucial: The IDE sidebar sorts primarily by this field in descending order.**
* `workspace_uris`: JSON string encoding an array of workspace URIs (e.g. `'["file:///d%3A/Developer/orthopaedicsone"]'`).
* `project_id`: UUID string mapping the session to an Antigravity project environment.
* `status`: Execution status enum (e.g. `CASCADE_RUN_STATUS_IDLE`, `CASCADE_RUN_STATUS_RUNNING`).
* `app_data_dir`: Constant string identifier, standard value is `"antigravity"`.
* `raw_summary`: Protobuf binary blob (~200 to 2,000 bytes, average 691 bytes) storing binary summary structures. It contains title, preview, timestamp, and workspace URI, but **does not contain the chat messages or file contents**.

---

## 3. Schema Specification: `<conversation_id>.db`

Each conversation has a dedicated SQLite database located at `~/.gemini/antigravity/conversations/<conversation_id>.db`. This is the execution trajectory database.

### 3.1 Tables
```sql
CREATE TABLE `trajectory_meta` (
  `trajectory_id` TEXT PRIMARY KEY,
  `cascade_id` TEXT,
  `trajectory_type` INTEGER,
  `source` INTEGER
);

CREATE TABLE `steps` (
  `idx` INTEGER PRIMARY KEY,
  `step_type` INTEGER NOT NULL DEFAULT 0,
  `status` INTEGER NOT NULL DEFAULT 0,
  `has_subtrajectory` NUMERIC NOT NULL DEFAULT false,
  `metadata` BLOB,
  `error_details` BLOB,
  `permissions` BLOB,
  `task_details` BLOB,
  `render_info` BLOB,
  `step_payload` BLOB,
  `step_format` INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE `trajectory_metadata_blob` (
  `id` TEXT PRIMARY KEY DEFAULT "main",
  `data` BLOB
);

CREATE TABLE `gen_metadata` (
  `idx` INTEGER PRIMARY KEY,
  `data` BLOB,
  `size` INTEGER NOT NULL DEFAULT 0
);
```

### 3.2 Step Types & Payloads
* `step_type = 14`: Represents a `USER_INPUT` step. The `step_payload` contains the user request text (`<USER_REQUEST>...`).
* `step_type = 15`: Represents a `PLANNER_RESPONSE` step. Contains model reasoning and token metrics.
* `step_type = 132`: Represents an agent tool execution (e.g. `view_file`, `run_command`, `write_to_file`, `replace_file_content`).

---

## 4. Binary Payload Format & Artifact Extraction

A critical discovery made during the vault development is that **`conversations/<id>.db` is completely self-contained**. Even if the `brain/<id>/` folder is missing or deleted, all chat transcripts and generated text files can be recovered directly from SQLite.

### 4.1 How `write_to_file` is Encoded in `steps.step_payload`
When an agent calls `write_to_file` to create or update files (such as `task.md`, `implementation_plan.md`, or source code), the arguments are serialized inside `step_payload` as a standard UTF-8 JSON object embedded within a protobuf envelope.

Structure of the embedded JSON:
```json
{
  "TargetFile": "C:\\Users\\...\\implementation_plan.md",
  "CodeContent": "# Implementation Plan\n\nFull raw uncompressed markdown text...",
  "Overwrite": true,
  "Description": "Detailed user-facing explanation",
  "ArtifactMetadata": {
    "Summary": "Detailed summary...",
    "UserFacing": true,
    "RequestFeedback": true
  },
  "toolAction": "Writing implementation plan artifact",
  "toolSummary": "Create implementation plan artifact"
}
```

### 4.2 Extraction Algorithm (Python)
Because the JSON payload is embedded within protobuf bytes, finding the start boundary `{"` and matching braces allows lossless extraction of 100% of the raw code or document text:

```python
def extract_file_from_payload(payload: bytes):
    start = payload.find(b'{"')
    if start == -1:
        return None
    
    raw = payload[start:]
    depth = 0
    in_string = False
    escape = False
    json_end = -1
    
    for i, b in enumerate(raw):
        c = chr(b)
        if in_string:
            if escape:
                escape = False
            elif c == '\\':
                escape = True
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    json_end = i + 1
                    break
                    
    if json_end != -1:
        data = json.loads(raw[:json_end].decode('utf-8'))
        return {
            "target_file": data.get("TargetFile"),
            "content": data.get("CodeContent"),
            "summary": data.get("ArtifactMetadata", {}).get("Summary", "")
        }
    return None
```

---

## 5. Eviction Mechanics & The Pinned Bug

### 5.1 The Eviction Engine in `language_server.exe`
The Antigravity backend language server (`resources\bin\language_server.exe`, compiled Go binary) runs an internal session manager (`MeshDaemon` / trajectory manager) with an eviction cleaner:
```
[MeshDaemon] Session cap %d exceeded; evicting least-recently-active session %s
Failed to prune trajectories: %v
```

### 5.2 The Retention Cap
- The trajectory storage manager enforces a hardcoded limit of approximately **500 active sessions** on disk.
- When new conversations are created, any conversation ranking beyond #500 based on `last_modified_time` is physically deleted:
  - `~/.gemini/antigravity/conversations/<id>.db` is deleted.
  - `~/.gemini/antigravity/brain/<id>/` is deleted.
  - Deletions are hard deletes (`os.RemoveAll`), bypassing the Windows Recycle Bin.

### 5.3 The Pinned Session Bug
Antigravity stores pinned state in `annotations/<id>.pbtxt`:
```text
title: "OrthopaedicsOne Content Generator Strategy"
last_user_view_time: { seconds: 1790955741 nanos: 224000000 }
pinned: true
```
**Defect**: The backend eviction routine only evaluates `last_modified_time` and **does not check the `pinned` field**. In high-throughput environments running cron jobs every 10 minutes (144 conversations/day), pinned strategy conversations are permanently destroyed after 3.5 days.

---

## 6. Programmatic Revival / Restoration Protocol

To restore any archived or evicted conversation so that it becomes fully loadable and appears at **slot #1 under "Today"** in the Antigravity sidebar, execute the following protocol:

```python
import sqlite3
import shutil
from pathlib import Path
from datetime import datetime, timezone

def revive_conversation(
    archive_dir: Path,
    conversation_id: str,
    target_workspace_uri: str = None,
    live_antigravity_dir: Path = Path.home() / ".gemini" / "antigravity"
):
    # 1. Restore the trajectory SQLite database
    conv_db_src = archive_dir / "conversations" / f"{conversation_id}.db"
    conv_db_dst = live_antigravity_dir / "conversations" / f"{conversation_id}.db"
    conv_db_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(conv_db_src, conv_db_dst)

    # 2. Restore the brain folder
    brain_src = archive_dir / "brain" / conversation_id
    brain_dst = live_antigravity_dir / "brain" / conversation_id
    if brain_src.exists():
        brain_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(brain_src, brain_dst, dirs_exist_ok=True)

    # 3. Restore annotations if present
    ann_src = archive_dir / "annotations" / f"{conversation_id}.pbtxt"
    ann_dst = live_antigravity_dir / "annotations" / f"{conversation_id}.pbtxt"
    if ann_src.exists():
        ann_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ann_src, ann_dst)

    # 4. Read metadata from backup catalog
    cat_db = archive_dir / "backup_catalog.db"
    with sqlite3.connect(cat_db) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM backed_up_conversations WHERE conversation_id = ?", (conversation_id,))
        meta = dict(cur.fetchone())

    # 5. Insert/Update conversation_summaries.db with NOW() timestamp
    live_db = live_antigravity_dir / "conversation_summaries.db"
    now_iso = datetime.now(timezone.utc).isoformat()
    ws = f'["{target_workspace_uri}"]' if target_workspace_uri else meta.get("workspace_uris", '[""]')

    with sqlite3.connect(live_db) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(conversation_summaries)")
        columns = [c[1] for c in cur.fetchall()]

        data = {
            "conversation_id": conversation_id,
            "title": meta.get("title", ""),
            "preview": meta.get("preview", ""),
            "step_count": meta.get("step_count", 0),
            "last_modified_time": now_iso,        # Bumps to slot #1 in sidebar
            "workspace_uris": ws,
            "status": "CASCADE_RUN_STATUS_IDLE",  # Marks session as idle/ready
            "project_id": meta.get("project_id", ""),
            "last_user_input_time": now_iso,
            "last_user_input_step_index": meta.get("step_count", 0) - 1,
            "app_data_dir": "antigravity",
            "raw_summary": meta.get("raw_summary", None)
        }

        # Filter only existing columns
        filtered = {k: v for k, v in data.items() if k in columns}
        cols_str = ", ".join(f"`{k}`" for k in filtered.keys())
        qmarks = ", ".join("?" for _ in filtered)
        sql = f"INSERT OR REPLACE INTO `conversation_summaries` ({cols_str}) VALUES ({qmarks})"

        cur.execute(sql, list(filtered.values()))
        conn.commit()

    print(f"Conversation {conversation_id} revived to slot #1 in Antigravity sidebar!")
```

---

## 7. Service Lifecycle & Operational Controls

The Antigravity Session Vault includes dedicated 1-click batch scripts and terminal commands to control the Web Dashboard and the Background Sync Daemon.

### 7.1 Control Scripts Reference Table

| Service | Action | 1-Click Batch Script | PowerShell Command |
| :--- | :--- | :--- | :--- |
| **Web Dashboard** | **START** | [`run_dashboard.bat`](file:///d:/Developer/agy-dashboard/run_dashboard.bat) | `python -m streamlit run d:\Developer\agy-dashboard\app.py` |
| **Web Dashboard** | **STOP** | [`stop_dashboard.bat`](file:///d:/Developer/agy-dashboard/stop_dashboard.bat) | `Stop-Process -Id (Get-NetTCPConnection -LocalPort 8501 -State Listen).OwningProcess -Force` |
| **Sync Daemon** | **START** | [`run_daemon.bat`](file:///d:/Developer/agy-dashboard/run_daemon.bat) | `python d:\Developer\agy-dashboard\core\daemon.py 30` |
| **Sync Daemon** | **STOP** | [`stop_daemon.bat`](file:///d:/Developer/agy-dashboard/stop_daemon.bat) | `Get-CimInstance Win32_Process \| Where-Object { $_.CommandLine -match 'core[\\/]daemon\.py' } \| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }` |

### 7.2 Daemon Heartbeat & Health Monitoring
* **Heartbeat Path**: `d:\Antigravity-Backup\state\daemon_heartbeat.json`
* **Payload Structure**:
  ```json
  {
    "last_heartbeat": "2026-10-02T18:25:01.343789+00:00",
    "status": "active",
    "last_synced_dbs": 1,
    "last_synced_brains": 1
  }
  ```
* **Liveness Detection**: The Streamlit dashboard computes `age_seconds = NOW() - last_heartbeat`. If `age_seconds < 120` and `status == "active"`, the UI displays `🟢 Active (Synced Xs ago)`. Otherwise, it displays `🟡 Idle / Inactive`.

---

## 8. High-Speed Global Search Architecture

### 8.1 Dual-Target Scanning
Global search indexes and queries two complementary data structures:
1. **Chat Transcripts**: Scans `~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl` (line-by-line JSON stream of User requests, Agent planner thoughts, and tool inputs) with a fallback to SQLite `<id>.db` `steps` table if `transcript.jsonl` is absent.
2. **Generated Plans & Artifacts**: Scans `~/.gemini/antigravity/brain/<id>/*.md` (`implementation_plan.md`, `walkthrough.md`, `task.md`, specialized review docs).

### 8.2 Performance Safeguards & Circuit Breaker
* **Recency-Sorted Traversal**: Candidates in `backed_up_conversations` are ordered by `last_modified_time DESC`. Common search terms match recent conversations almost immediately (34ms vs 17s cold brute force).
* **Early Circuit-Breaker Exit**: As soon as the number of matching conversations reaches `max_results` (25, 50, or 100), traversal terminates immediately.
* **Workspace Pre-Filtering**: Filtering by workspace restricts candidate queries at the SQL level (`WHERE (has_db_file = 1 OR has_brain_folder = 1) AND workspace_uris LIKE ?`), narrowing candidate pools from 886 down to ~20–40 sessions (<0.1s scan time).
* **Context Highlighting**: Matching snippets extract 70 characters before and after the matched keyword, escape HTML characters, and wrap the term in a styled `<mark>` tag.


