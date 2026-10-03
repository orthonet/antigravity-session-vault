import html
import json
import re
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

from config import BACKUP_CONVERSATIONS_DIR, BACKUP_BRAIN_DIR
from core.catalog import CatalogManager
from core.classifier import format_display_title


def highlight_snippet(text: str, query: str, window: int = 70) -> str:
    """
    Extracts a text snippet around the query and returns an HTML-safe string
    with the matched term highlighted in a styled <mark> tag.
    """
    if not text:
        return ""

    lower_text = text.lower()
    lower_query = query.lower()
    idx = lower_text.find(lower_query)

    if idx == -1:
        snippet_raw = text[: window * 2].replace("\n", " ").strip()
        return html.escape(snippet_raw)

    start = max(0, idx - window)
    end = min(len(text), idx + len(query) + window)

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""

    before = text[start:idx].replace("\n", " ")
    match_str = text[idx : idx + len(query)].replace("\n", " ")
    after = text[idx + len(query) : end].replace("\n", " ")

    esc_before = html.escape(before)
    esc_match = html.escape(match_str)
    esc_after = html.escape(after)

    mark_tag = (
        f'<mark style="background-color: #fef08a; color: #854d0e; font-weight: bold; '
        f'padding: 1px 4px; border-radius: 3px;">{esc_match}</mark>'
    )

    return f"{prefix}{esc_before}{mark_tag}{esc_after}{suffix}".strip()


def search_backup_conversations(
    query: str,
    catalog: Optional[CatalogManager] = None,
    scope: str = "All Content",
    workspace_filter: Optional[str] = None,
    max_results: int = 25,
    max_scan_conversations: Optional[int] = None,
    brain_dir: Path = BACKUP_BRAIN_DIR,
    db_dir: Path = BACKUP_CONVERSATIONS_DIR,
) -> Dict[str, Any]:
    """
    Searches across backed-up conversations, supporting transcripts and generated artifacts.
    Groups results by conversation, sorts by recency, and terminates early once max_results is reached.
    """
    if catalog is None:
        catalog = CatalogManager()

    clean_query = query.strip()
    if not clean_query or len(clean_query) < 2:
        return {
            "results": [],
            "scanned_count": 0,
            "stopped_early": False,
            "elapsed_time": 0.0,
        }

    lower_query = clean_query.lower()
    t0 = time.time()

    # Determine what scopes to search
    search_artifacts = scope in ("All Content", "Generated Plans & Files Only")
    search_transcripts = scope in ("All Content", "Transcripts & Prompts Only")

    # Fetch candidate conversations ordered by recency
    sql = """
        SELECT conversation_id, title, preview, workspace_uris, last_modified_time, category, retention_status
        FROM backed_up_conversations
        WHERE (has_db_file = 1 OR has_brain_folder = 1)
    """
    params = []
    if workspace_filter and workspace_filter != "All" and workspace_filter != "All Workspaces":
        sql += " AND workspace_uris LIKE ?"
        params.append(f"%{workspace_filter}%")

    sql += " ORDER BY last_modified_time DESC"

    with catalog._get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    matching_conversations = []
    scanned_count = 0
    stopped_early = False

    for r in rows:
        scanned_count += 1
        if max_scan_conversations and scanned_count > max_scan_conversations:
            stopped_early = True
            break

        cid = r["conversation_id"]
        c_brain_dir = brain_dir / cid
        conv_hits = []

        # ---------------------------------------------------------
        # 1. Search Generated Artifacts (*.md in brain/<cid>)
        # ---------------------------------------------------------
        if search_artifacts and c_brain_dir.exists():
            for md_file in c_brain_dir.glob("*.md"):
                try:
                    text = md_file.read_text(encoding="utf-8", errors="ignore")
                    if lower_query in text.lower():
                        snip = highlight_snippet(text, clean_query)
                        conv_hits.append({
                            "type": "artifact",
                            "label": f"📜 Generated Artifact: {md_file.name}",
                            "snippet": snip,
                            "filename": md_file.name,
                        })
                except Exception:
                    pass

        # ---------------------------------------------------------
        # 2. Search Transcripts (transcript.jsonl with SQLite fallback)
        # ---------------------------------------------------------
        if search_transcripts:
            t_file = c_brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
            transcript_found = False

            if t_file.exists():
                try:
                    with open(t_file, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if lower_query in line.lower():
                                try:
                                    data = json.loads(line)
                                    s_idx = data.get("step_index", 0)
                                    stype = data.get("type", "")
                                    source = data.get("source", "")
                                    content = data.get("content", "") or data.get("thinking", "")
                                    if not content and "tool_calls" in data:
                                        content = str(data["tool_calls"])

                                    if lower_query in content.lower():
                                        sender = (
                                            "👤 User"
                                            if (source == "USER_EXPLICIT" or stype == "USER_INPUT")
                                            else ("🤖 Agent" if source == "MODEL" else "⚙️ Tool")
                                        )
                                        snip = highlight_snippet(content, clean_query)
                                        conv_hits.append({
                                            "type": "transcript",
                                            "label": f"💬 Step {s_idx} ({sender})",
                                            "snippet": snip,
                                            "step_index": s_idx,
                                        })
                                        if len(conv_hits) >= 10:
                                            break
                                except Exception:
                                    pass
                    transcript_found = True
                except Exception:
                    pass

            # Fallback to SQLite DB if transcript.jsonl was not found or yielded nothing
            if not transcript_found:
                db_file = db_dir / f"{cid}.db"
                if db_file.exists():
                    try:
                        import sqlite3
                        with sqlite3.connect(db_file) as db_conn:
                            cur = db_conn.cursor()
                            cur.execute(
                                "SELECT idx, step_type, step_payload FROM steps WHERE step_payload IS NOT NULL"
                            )
                            for idx, stype, payload in cur.fetchall():
                                if clean_query.encode() in payload.lower():
                                    # Extract ASCII strings
                                    strings = re.findall(rb"[\x20-\x7e]{15,}", payload)
                                    matched_strs = [
                                        s.decode("ascii", errors="ignore")
                                        for s in strings
                                        if clean_query.encode() in s.lower()
                                    ]
                                    sample_text = matched_strs[0] if matched_strs else f"Step {idx} payload"
                                    snip = highlight_snippet(sample_text, clean_query)
                                    conv_hits.append({
                                        "type": "transcript",
                                        "label": f"💬 Step {idx} (Recorded Step)",
                                        "snippet": snip,
                                        "step_index": idx,
                                    })
                                    if len(conv_hits) >= 10:
                                        break
                    except Exception:
                        pass

        # If any hits were found in this conversation, bundle into conversation card
        if conv_hits:
            raw_ws = r["workspace_uris"]
            ws_display = raw_ws or "Unknown Workspace"
            try:
                parsed_ws = json.loads(raw_ws)
                if isinstance(parsed_ws, list) and parsed_ws:
                    ws_display = parsed_ws[0]
            except Exception:
                pass
            if ws_display.startswith("file:///"):
                ws_display = ws_display.replace("file:///", "").replace("%3A", ":")

            date_str = str(r["last_modified_time"])[:16] if r["last_modified_time"] else "Unknown Date"
            disp_title = format_display_title(r["title"], r["preview"])

            matching_conversations.append({
                "conversation_id": cid,
                "title": disp_title or cid,
                "workspace": ws_display,
                "date": date_str,
                "category": r["category"] or "interactive",
                "retention_status": r["retention_status"] or "unknown",
                "hits": conv_hits,
            })

            if len(matching_conversations) >= max_results:
                stopped_early = True
                break

    elapsed = time.time() - t0

    return {
        "results": matching_conversations,
        "scanned_count": scanned_count,
        "stopped_early": stopped_early,
        "elapsed_time": elapsed,
    }
