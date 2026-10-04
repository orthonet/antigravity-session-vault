# Antigravity Session Vault

[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/streamlit-1.35%2B-FF4B4B.svg)](https://streamlit.io/)
[![SQLite](https://img.shields.io/badge/sqlite-WAL%20Mode-003B57.svg)](https://www.sqlite.org/)
[![Tests](https://img.shields.io/badge/tests-39%2F39%20passing-brightgreen.svg)](tests/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Autonomous Continuous Backup, Lossless Archival Storage, Full-Text Retrieval, Vault Pin Sentry Anti-Re-Eviction Defense, and 1-Click IDE Revival for Google Antigravity.**

---

> ### 🩺 A Note from the Author
> 
> *I am an orthopaedic surgeon, not a professional software engineer.*
> 
> Like many researchers and developers, I rely heavily on [Google Antigravity](https://antigravity.google/) for day-to-day coding, clinical protocol synthesis, data pipelines, and autonomous agent orchestration. One morning after an update, months of irreplaceable architectural decisions, prompt chains, and custom code disappeared from my IDE sidebar. Clicking them returned a fatal message:
> 
> **`"The conversation could not be loaded because its data was not found."`**
> 
> I searched Reddit, developer forums, and bug trackers. Scores of users had posted about the exact same issue with zero resolution. I was on the verge of abandoning the Antigravity ecosystem entirely.
> 
> Instead, I spent a day reverse-engineering Antigravity's local storage engine and used Antigravity itself as my pair programmer to engineer this vault. Today, every session on my workstation is protected, searchable, and restorable with a single click. I am open-sourcing this vault so no one else has to lose weeks of work to silent eviction.

---

## 🚨 The Root Cause: Why Antigravity Conversations Disappear

Forensic reverse-engineering and disassembly of Antigravity's underlying language server process (`language_server.exe`, a 64-bit PE compiled Go binary embedded within the IDE) reveals the exact architectural mechanism causing session loss:

```
LIVE ANTIGRAVITY STORAGE (~/.gemini/antigravity/)
├── conversation_summaries.db   <-- Retains ALL titles & timestamps (accumulates thousands)
├── conversations/<id>.db        <-- Hard-capped at ~500 most recent files!
└── brain/<id>/                 <-- Permanently deleted once session exceeds ~500!
```

1. **Hardcoded ~500-Session Rolling Eviction Cap**: `language_server.exe` runs a periodic garbage-collection sweep enforcing a rolling retention ceiling of approximately 500 active sessions.
2. **Permanent Unrecoverable Deletions**: Once the count exceeds ~500, the system purges older sessions directly via Win32 `DeleteFileW` and `RemoveDirectoryW` (POSIX `unlink`/`rmdir`), unlinking both `conversations/<id>.db` and the corresponding `brain/<id>/` directories. **These deletions completely bypass the OS Recycle Bin / Trash.**
3. **The "Pinned" Session Vulnerability**: Even if you explicitly "Pin" an important conversation in the UI (writing `pinned: true` into `annotations/<id>.pbtxt`), the eviction engine sorts candidate files **strictly by file modification time (`last_modified_time`) and completely ignores the pinned annotation**. Once 500 subsequent sessions occur, pinned conversations drift to the tail end of the eviction queue and are permanently wiped from disk.
4. **Devastating for Autonomous / Scheduled Agents**: If you run scheduled cron agents or queue-based tasks (e.g., every 10–15 minutes), your agent generates 100–140+ conversations per day. **Your entire 500-session history is completely recycled and deleted every 3 to 4 days.**
5. **The "Phantom Catalog" Illusion**: Antigravity retains session titles in `conversation_summaries.db`. Your sidebar still lists old conversations, but clicking any of them fails with:
   > *"The conversation could not be loaded because its data was not found."*

---

## ✨ Features & Capabilities

Antigravity Session Vault provides complete protection against silent eviction:

### 🛡️ 1. Vault Pin Sentry (Anti-Re-Eviction Defense) (`core/pin_sentry.py`)
- **Automated Keep-Alive Heartbeat**: Prevents pinned sessions from aging out by periodically refreshing `last_modified_time` timestamps within Antigravity's active evaluation window.
- **Self-Healing Auto-Resurrection**: If an eviction occurs during offline periods or rapid language server sweeps, Pin Sentry detects the missing live `.db` or `brain/` folder and automatically resurrects them from the Vault back into live Antigravity without manual intervention.
- **Unified Sync Integration**: Automatically runs during every continuous sync cycle or via 1-click manual trigger.

### 🔄 2. Continuous Background Sync Daemon (`core/daemon.py`)
- Automatically monitors live Antigravity files every 30 seconds.
- Mirrors conversation databases (`.db`), brain directories (`brain/`), and annotations (`.pbtxt`) to an independent, safe archive location.
- Uses online SQLite snapshots (`sqlite3.backup()`) with read-only connection handles to safely flush and integrate active WAL pages (`.db-wal`) without database locking collisions.
- Features a single-instance PID lock (`daemon.pid`), live JSON heartbeat telemetry (`daemon_heartbeat.json`), and isolated stdout/stderr logging (`daemon.log`).
- Immune to Windows NTFS read-only file traps (`[Errno 13] Permission denied` on Git loose objects).

### ⚡ 3. 1-Click IDE Session Revival (`core/restorer.py`)
- Revive any archived or evicted conversation directly back into your live Antigravity IDE sidebar.
- Bumps the session to **Slot #1 under "Today"** in your workspace.
- **Active Session Protection**: If a conversation is currently active in Antigravity, restoration preserves your newer live steps and will never clobber active progress unless explicitly requested (`overwrite_live=True`).
- **Target Workspace Remapping**: Restore a conversation back to its original workspace URI or redirect it into a new project workspace.
- **Atomic Rollback**: If an error occurs during restore, newly created files are cleanly unlinked, preventing orphaned state.

### 📦 4. Offsite Backup Importer (`core/importer.py`)
- Recovers historical conversations from external USB drives, Acronis, Time Machine, or NAS backups.
- Automatically discovers uncatalogued trajectory `.db` files and nested `conversation_summaries.db` sources.
- **4-Tier Mathematical Reconciliation**:
  - 🎯 **Actionable Evicted Upgrades**: Upgrades metadata-only evicted records back to full file fidelity.
  - ➕ **Brand New Sessions**: Ingests previously unrecorded historical sessions.
  - ✅ **Already in Vault**: Safely skips identical, up-to-date archives.
  - 📋 **Total Discovered**: 100% accounted for with zero silent dropping.

### 🔍 5. Global Full-Text Deep Search (`core/search_engine.py`)
- Blazing-fast sub-second search across all archived conversation SQLite databases.
- Searches user queries, agent thought processes, tool invocations, and code payloads.
- Displays contextual snippets with search keyword highlighting and direct links into the conversation viewer.

### 📂 6. Interactive Streamlit Web Dashboard (`app.py`)
- **Balanced 3x2 Metrics Grid**: Real-time stats displaying `🟢 Active in AGY`, `🛡️ Safeguarded`, `📋 Metadata Only`, `📥 Offsite Restored`, `⭐ Pinned`, and `💼 Workspaces`.
- **Integrated Daemon & Sentry Controller**: Live process indicator (`🟢 Active`, PID, last sync elapsed time, protected pinned count) with 1-click Start/Stop and non-blocking 15-second `@st.fragment` background refreshes.
- **Full-Width Responsive UI**: Seamless intermediate progress spinners and action feedback banners spanning 100% of sidebar width.
- **Conversation Explorer**: Filter by granular retention status (`🟢 Complete Active`, `📦 Imported from Offsite`, `⚠️ Metadata Only (Evicted)`, `📭 Metadata Only (No Backup)`), category (`Pinned`, `Interactive`, `Automated Queue`), or workspace.
- **Self-Contained File Payload Extraction**: Antigravity saves complete, uncompressed files generated via `write_to_file` inside `steps.step_payload`. The viewer extracts and displays all code files, `task.md`, `implementation_plan.md`, and walkthroughs—**even if the brain folder was deleted**.
- **Complete Timeline & Transcript Viewer**: Review chronological turns, tool executions, and diffs with full copy/download controls.
- **Dual-Theme Adaptive UI**: Seamless native Light and Dark mode auto-detection with high-contrast token borders.

---

## 🏗️ Architecture

```
+-------------------------------------------------------------------------+
|                       LIVE ANTIGRAVITY ENVIRONMENT                      |
|                  (~/.gemini/antigravity/ on Win/Mac/Linux)              |
|                                                                         |
|  conversation_summaries.db   conversations/<id>.db      brain/<id>/     |
|    (Rolling Catalog)           (Active ~500 DBs)      (Logs/Artifacts)  |
+---------------------+--------------------+--------------------+---------+
                      |                    |                    |
                      | 30s Polling Sync   | Real-time Mirror   | Real-time Mirror
                      v                    v                    v
+-------------------------------------------------------------------------+
|                       PROTECTED ARCHIVE STORAGE                         |
|                 (e.g., D:/Antigravity-Backup or ~/Backup)               |
|                                                                         |
|   backup_catalog.db            conversations/            brain/         |
|  (Unified Full Catalog)      (Lossless .db Store)   (Protected Folders) |
+---------------------+--------------------+--------------------+---------+
                      |                    |                    |
                      +--------------------+--------------------+
                                           |
                                           v
+-------------------------------------------------------------------------+
|                  STREAMLIT WEB DASHBOARD & RESTORER                     |
|                                                                         |
|  [Tab 1] Conversation Explorer    --> Filter by Active / Evicted / Pin  |
|  [Tab 2] Transcript & Plan Viewer --> Extract task.md, plans, code files|
|  [Tab 3] Global Deep Search       --> Sub-second payload search         |
|  [Tab 4] Offsite Backup Importer  --> Ingest Acronis / Time Machine     |
|  [Tab 5] Health & Settings        --> Live daemon status & disk monitor |
|                                                                         |
|  [ACTION] ⚡ Restore to Sidebar   --> Bumps session to Slot #1 in AGY   |
+-------------------------------------------------------------------------+
```

---

## 🚀 Quickstart & Setup

### Prerequisites
- **Python 3.10+** (Tested up to Python 3.14)
- **Git**
- Google Antigravity installed on your machine

### 1. Clone the Repository
```bash
git clone https://github.com/orthonet/antigravity-session-vault.git
cd antigravity-session-vault
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```
*(Dependencies are lightweight: Streamlit >= 1.35.0. All storage engines utilize Python standard libraries `sqlite3`, `pathlib`, `shutil`, `json`.)*

### 3. Configure Backup Archive Location
By default, the vault archives sessions to `d:/Antigravity-Backup` (Windows). You can customize this to any local path, secondary SSD, external drive, or cloud mount via the `AGY_BACKUP_DIR` environment variable or by editing [`config.py`](file:///d:/Developer/agy-dashboard/config.py):

**Windows (PowerShell):**
```powershell
$env:AGY_BACKUP_DIR = "D:/Antigravity-Backup"   # or "C:/Antigravity-Backup"
```

**macOS / Linux:**
```bash
export AGY_BACKUP_DIR="$HOME/Antigravity-Backup"
```

### 4. Create Your Initial Baseline Snapshot
Safeguard all surviving active sessions and ingest your legacy metadata catalog:
```bash
python snapshot_initial.py
```
*This scans your live Antigravity directories, takes safe online snapshots of all active trajectory databases, copies your brain folders, and populates `backup_catalog.db`.*

### 5. Start the Continuous Sync Daemon
Keep your vault continuously up-to-date in the background:

**Windows:**
Double-click `run_daemon.bat` or run:
```powershell
python core/daemon.py 30
```

**macOS / Linux:**
```bash
python core/daemon.py 30
```
*(Runs every 30 seconds, maintaining a live heartbeat in `state/daemon_heartbeat.json`.)*

### 6. Launch the Web Dashboard
Access the visual explorer, transcript viewer, deep search, and 1-click restore engine:

**Windows:**
Double-click `run_dashboard.bat` or run:
```powershell
python -m streamlit run app.py --server.port 8501
```

**macOS / Linux:**
```bash
streamlit run app.py --server.port 8501
```

Open your browser to: **`http://localhost:8501`**

---

## ⚙️ Configuration Reference ([`config.py`](file:///d:/Developer/agy-dashboard/config.py))

| Setting | Default Value | Description |
| :--- | :--- | :--- |
| `LIVE_BASE_DIR` | `~/.gemini/antigravity` | Path to Antigravity's live data folder |
| `BACKUP_BASE_DIR` | `d:/Antigravity-Backup` or `$AGY_BACKUP_DIR` | Protected archive storage directory |
| `POLL_INTERVAL_SECONDS` | `30` | Sync daemon frequency in seconds |
| `PAGE_SIZE_DEFAULT` | `25` | Default page size in Conversation Explorer |

---

## 🧪 Rigorous Testing & Verification

Antigravity Session Vault enforces strict autonomous quality assurance standards through a dual-layer verification mandate:
- **Backend Invariant Tests (`tests/test_vault.py` - 26 tests)**: Mathematical conservation laws ($\sum \text{buckets} = \text{total}$), monotonicity laws (sessions can upgrade but never downgrade), online SQLite WAL backups, atomic rollback, path-traversal security verification, and detached subprocess lifecycle.
- **Headless UI Tests (`tests/test_ui_apptest.py` - 10 tests)**: Streamlit's official `st.testing.v1.AppTest` framework simulating clicks, tab switches, segmented control toggling, sidebar button interactions, and responsive full-width layout verification in-process.

Run the unified test suite:

**Windows:**
```powershell
run_tests.bat
```

**Cross-Platform:**
```bash
python -m unittest discover tests -v
```

```text
Ran 39 tests in ~25s
OK (0 failures, 0 errors)
```

---

## ❓ Frequently Asked Questions (FAQ)

#### Q: Can I recover conversations that were already deleted before I installed this vault?
**A:** If Antigravity already evicted the session from disk *and* you have no external backups (Acronis, Time Machine, File History, USB clones), the trajectory database itself is gone. However:
1. `snapshot_initial.py` and the catalog will preserve the session's title, creation date, step count, and workspace URI from `conversation_summaries.db`.
2. If you have an external backup drive or system image taken before the eviction occurred, connect it and use **Tab 4 (Offsite Backup Importer)**. The vault will scan the backup, recover the `.db` files, and upgrade those evicted sessions to full interactive fidelity!

#### Q: Will restoring an old session overwrite or mess up my active conversations?
**A:** No. `core/restorer.py` is equipped with active session protection (`overwrite_live=False` by default). If a session is already present in your live environment, restoration skips file copying to preserve your newest steps while safely bumping it to slot #1. Furthermore, if any step fails during restoration, an atomic rollback unlinks created files.

#### Q: Does running the sync daemon slow down Antigravity?
**A:** No. The daemon operates on a 30-second sleep interval and utilizes SQLite's online backup API (`sqlite3.backup()`) with read-only connection handles. It does not lock the database against Antigravity writes. For file mirroring, it checks file size and mtime before performing I/O, skipping identical files entirely.

#### Q: What operating systems are supported?
**A:** Windows 10/11, macOS, and Linux. The codebase includes specific protections for Windows (NTFS read-only attribute stripping on Git loose objects) and standard POSIX paths for macOS and Linux.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE) - see the LICENSE file for details.

---

*Google and Antigravity are trademarks of Google LLC. This project is an independent community open-source tool and is not officially affiliated with or endorsed by Google.*
