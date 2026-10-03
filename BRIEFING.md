# Executive Briefing: Antigravity Session Vault & Storage Architecture

**Document Version:** 1.0.0  
**Date:** October 2, 2026  
**Target Audience:** Enterprise Engineering Leads, Autonomous Agent Operators, Antigravity Developers  
**Project Location:** `d:\Developer\agy-dashboard\`  
**Archive Location:** `d:\Antigravity-Backup\`

---

## 1. Executive Summary

During operational analysis of Google Antigravity across production projects (such as `orthopaedicsone`), high-value strategy and architectural conversations were found to be disappearing from the user's sidebar history. 

A forensic reverse-engineering investigation into Antigravity's local storage engine uncovered a critical architectural design limitation:

1. **Hardcoded ~500-Session Eviction Cap**: Antigravity's internal language server daemon (`language_server.exe`) silently enforces a rolling disk retention cap of approximately 500 active sessions.
2. **Silent Hard Deletions**: When total conversation volume exceeds this cap, Antigravity executes unrecoverable, permanent file deletions of both the conversation trajectory database (`conversations/<id>.db`) and the message transcript directory (`brain/<id>/`). No warning is shown, and files bypass the Windows Recycle Bin.
3. **The "Pinned" Session Bug**: Even though the Antigravity UI permits users to "Pin" critical conversations (recording `pinned: true` in `annotations/<id>.pbtxt`), the eviction engine ignores this flag and purges pinned conversations purely on a `last_modified_time` basis.
4. **Devastating for Autonomous / Scheduled Agents**: In environments running scheduled agent jobs (e.g. cron runs every 10–15 minutes), 100 to 140+ conversations are generated daily. Consequently, **the entire 500-session retention window is completely recycled every 3 to 4 days**, permanently wiping out all earlier human design and strategy sessions.
5. **Phantom Catalog**: Antigravity leaves the session title and metadata inside `conversation_summaries.db` (which accumulated 2,729 entries on this system), giving the false impression that conversation history is intact when the underlying data is already gone.

To solve this, the **Antigravity Session Vault** was planned, built, verified, and deployed.

---

## 2. Key Findings & Forensic Evidence

### 2.1 The Disappearing Conversation Case Study
* **Target Conversation**: *"OrthopaedicsOne Content Generator Strategy"* (`d071f820-62cd-4b9e-a3cb-5bba1f451bb0`).
* **Attributes**: 303 steps, created September 26, 2026, marked `pinned: true`.
* **Finding**: Metadata existed in `conversation_summaries.db`, but the physical files (`conversations/d071...db` and `brain/d071.../`) were permanently deleted.
* **Root Cause**: Between September 26 and October 2, **1,738 automated queue conversations** were generated. The session was pushed to workspace rank **#1,725**. By September 28 (~500 sessions later), the background daemon evicted and deleted all underlying trajectory data.

### 2.2 System Retention Inventory (At Time of Discovery)
* **Total Catalogued Records in live DB**: 2,729 sessions
* **Surviving Sessions with Physical Data on Disk**: Exactly 508 sessions
* **Permanently Purged Historical Sessions**: 2,221 sessions (evicted prior to backup)
* **Pinned Sessions Safeguarded During Initial Snapshot**: 11 sessions

---

## 3. The Solution: Antigravity Session Vault

The Antigravity Session Vault is a local Python system and Streamlit web dashboard located at `d:\Developer\agy-dashboard\` that operates on protected storage at `d:\Antigravity-Backup\`.

### Core Capabilities:
1. **Urgent Initial Snapshot**:
   - Safeguarded all **508 surviving active sessions** (~828 MB) to protected storage on `D:` drive (993 GB available).
   - Ingested all **2,221 legacy metadata records** into a unified catalog database (`backup_catalog.db`), preserving titles, dates, workspace associations, and step counts.
2. **Self-Contained SQLite Data Extraction**:
   - Discovered and proved that Antigravity's `.db` files retain 100% of the raw, uncompressed text of all `write_to_file` steps (`task.md`, `implementation_plan.md`, `walkthrough.md`, source code files) inside `steps.step_payload`.
   - The dashboard viewer can extract and visualize full conversation chat histories and plan documents directly from the `.db` files even if the `brain/` directory is missing.
3. **Offsite Nightly Backup Importer**:
   - Provides a dedicated tool allowing users to point to external hard drives, network shares, or nightly computer backup folders.
   - Automatically scans for recovered `.db` files, matches them against the 2,221 evicted sessions, and upgrades them to **"Complete"** with full transcript viewing and restoration.
4. **1-Click Sidebar Revival Engine**:
   - Allows users to select any archived session and click **"⚡ Restore"**.
   - Copies `.db`, `brain/`, and annotations back to live Antigravity, sets `last_modified_time = NOW()`, and instantly positions the conversation at **slot #1 under "Today"** in the Antigravity IDE sidebar.
5. **Continuous Ingestion Daemon**:
   - A background sync script (`core/daemon.py`) that polls every 30 seconds.
   - Mirrors new and modified conversations in real time, ensuring no future conversation will ever be evicted or lost.

---

## 4. Architecture Diagram

```
+-------------------------------------------------------------------------+
|                       LIVE ANTIGRAVITY ENVIRONMENT                      |
|                  (C:\Users\<User>\.gemini\antigravity\)                 |
|                                                                         |
|  conversation_summaries.db   conversations/<id>.db      brain/<id>/     |
|    (Rolling Catalog)           (Active ~500 DBs)      (Logs/Artifacts)  |
+---------------------+--------------------+--------------------+---------+
                      |                    |                    |
                      | 30s Polling Sync   | Real-time Mirror   | Real-time Mirror
                      v                    v                    v
+-------------------------------------------------------------------------+
|                       PROTECTED ARCHIVE STORAGE                         |
|                         (d:\Antigravity-Backup\)                        |
|                                                                         |
|   backup_catalog.db            conversations/            brain/         |
|  (Unified 2,729 Records)     (Lossless .db Store)   (Protected Folders) |
+---------------------+--------------------+--------------------+---------+
                      |                    |                    |
                      +--------------------+--------------------+
                                           |
                                           v
+-------------------------------------------------------------------------+
|                  STREAMLIT WEB DASHBOARD & ENGINE                       |
|                     (d:\Developer\agy-dashboard\)                       |
|                                                                         |
|  [Tab 1] Conversation Explorer    --> Filter by Active / Evicted / Pin  |
|  [Tab 2] Transcript & Plan Viewer --> Renders task.md, plans, full chat |
|  [Tab 3] Global Full-Text Search  --> Search payloads across all DBs    |
|  [Tab 4] Offsite Backup Importer  --> Recover from external backups     |
|  [Tab 5] Health & Settings        --> Live daemon status & disk monitor |
|                                                                         |
|  [ACTION] ⚡ Restore to Sidebar   --> Bumps session to Slot #1 in AGY   |
+-------------------------------------------------------------------------+
```

---

## 5. Operations & Quick Start Guide

### Starting & Stopping the Web Dashboard
* **START**: Double-click [run_dashboard.bat](file:///d:/Developer/agy-dashboard/run_dashboard.bat) or run:
  ```powershell
  python -m streamlit run d:\Developer\agy-dashboard\app.py
  ```
  Open **http://localhost:8501** in your browser.
* **STOP**: Double-click [stop_dashboard.bat](file:///d:/Developer/agy-dashboard/stop_dashboard.bat) or press `Ctrl + C` in the terminal.

### Starting & Stopping the Continuous Sync Daemon
To ensure 24/7 protection against eviction while Antigravity is running:
* **START**: Double-click [run_daemon.bat](file:///d:/Developer/agy-dashboard/run_daemon.bat) or run:
  ```powershell
  python d:\Developer\agy-dashboard\core\daemon.py 30
  ```
* **STOP**: Double-click [stop_daemon.bat](file:///d:/Developer/agy-dashboard/stop_daemon.bat) or press `Ctrl + C` in the terminal.

### Restoring an Evicted Conversation
1. Open the dashboard at `http://localhost:8501`.
2. Locate the conversation in the **Conversation Explorer** (or search via the search bar).
3. Click **"⚡ Restore"**.
4. Switch to your Antigravity IDE—the conversation will appear at the top of your sidebar under "Today".

### Recovering Sessions from Nightly Backups
1. Connect your external drive or locate your nightly backup folder.
2. Go to the **Offsite Backup Importer** tab.
3. Enter the folder path and click **"🔍 Scan Source Folder"**.
4. Review the matching sessions and click **"🚀 Import All Discovered Sessions"**.
5. The sessions will immediately be upgraded from *Metadata Only* to *Complete*, restoring full transcript viewing and sidebar revival capabilities.
