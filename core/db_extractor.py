import sqlite3
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional

def extract_file_payloads(db_path: Optional[Path] = None, brain_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Extracts all files written or modified during the session by inspecting
    'write_to_file' and 'replace_file_content' tool calls in the steps table,
    and supplemented with direct Markdown artifacts and scratch scripts stored in the brain/ directory.
    Operates 100% within the Archive Path without any external filesystem dependency.
    """
    seen_files = {}

    if db_path and db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT idx, step_type, status, step_payload FROM steps ORDER BY idx")
                rows = cur.fetchall()

                for idx, stype, status, payload in rows:
                    if not payload:
                        continue
                    if b"write_to_file" not in payload and b"replace_file_content" not in payload:
                        continue

                    # Locate JSON argument block
                    start_json = payload.find(b'{"')
                    if start_json == -1:
                        continue

                    raw_sub = payload[start_json:]
                    depth = 0
                    in_string = False
                    escape = False
                    json_end = -1
                    for i, b in enumerate(raw_sub):
                        char = chr(b)
                        if in_string:
                            if escape:
                                escape = False
                            elif char == '\\':
                                escape = True
                            elif char == '"':
                                in_string = False
                        else:
                            if char == '"':
                                in_string = True
                            elif char == '{':
                                depth += 1
                            elif char == '}':
                                depth -= 1
                                if depth == 0:
                                    json_end = i + 1
                                    break

                    if json_end != -1:
                        try:
                            args = json.loads(raw_sub[:json_end].decode('utf-8'))
                            target_file = args.get("TargetFile", "")
                            if not target_file:
                                continue

                            fname = Path(target_file).name
                            code_content = args.get("CodeContent", "")
                            target_content = args.get("TargetContent", "")
                            replacement_content = args.get("ReplacementContent", "")
                            summary = args.get("Description", "") or args.get("toolSummary", "")
                            meta = args.get("ArtifactMetadata", {})
                            if isinstance(meta, dict) and meta.get("Summary"):
                                summary = meta.get("Summary")

                            # Disambiguation key based on target_file path
                            file_key = target_file.replace("\\", "/").lower()

                            if file_key in seen_files and seen_files[file_key].get("content"):
                                # Incrementally patch existing content in memory
                                if target_content and replacement_content and target_content in seen_files[file_key]["content"]:
                                    seen_files[file_key]["content"] = seen_files[file_key]["content"].replace(target_content, replacement_content, 1)
                                    seen_files[file_key]["size_bytes"] = len(seen_files[file_key]["content"].encode("utf-8"))
                                seen_files[file_key]["step_index"] = idx
                                if summary:
                                    seen_files[file_key]["summary"] = summary
                            else:
                                # New file entry
                                content = code_content or replacement_content or ""
                                seen_files[file_key] = {
                                    "step_index": idx,
                                    "filename": fname,
                                    "target_path": target_file,
                                    "content": content,
                                    "summary": summary,
                                    "tool_action": args.get("toolAction", ""),
                                    "is_artifact": bool(args.get("ArtifactMetadata")),
                                    "size_bytes": len(content.encode("utf-8"))
                                }
                        except Exception:
                            pass
        except Exception as e:
            print(f"Error extracting file payloads from {db_path}: {e}")

    # Also scan brain_dir for markdown artifacts and scratch scripts directly in the archive
    if brain_dir and brain_dir.exists():
        try:
            # 1. Markdown deliverables
            for fpath in brain_dir.glob("*.md"):
                fname = fpath.name
                file_key = str(fpath).replace("\\", "/").lower()
                fname_matched = next((k for k in seen_files if Path(k).name == fname and seen_files[k].get("content")), None)
                if not fname_matched:
                    try:
                        content = fpath.read_text(encoding="utf-8", errors="ignore")
                        summary = ""
                        meta_path = brain_dir / f"{fname}.metadata.json"
                        if meta_path.exists():
                            try:
                                mdata = json.loads(meta_path.read_text(encoding="utf-8", errors="ignore"))
                                summary = mdata.get("Summary", "")
                            except Exception:
                                pass
                        seen_files[file_key] = {
                            "step_index": 0,
                            "filename": fname,
                            "target_path": str(fpath),
                            "content": content,
                            "summary": summary or "Saved Artifact",
                            "tool_action": "Saved Artifact",
                            "is_artifact": True,
                            "size_bytes": len(content.encode("utf-8"))
                        }
                    except Exception:
                        pass

            # 2. Scratch scripts in brain/<cid>/scratch/
            scratch_dir = brain_dir / "scratch"
            if scratch_dir.exists() and scratch_dir.is_dir():
                for fpath in scratch_dir.glob("*"):
                    if fpath.is_file() and not fpath.name.endswith(".metadata.json"):
                        fname = fpath.name
                        file_key = str(fpath).replace("\\", "/").lower()
                        fname_matched = next((k for k in seen_files if Path(k).name == fname and seen_files[k].get("content")), None)
                        if not fname_matched:
                            try:
                                content = fpath.read_text(encoding="utf-8", errors="ignore")
                                seen_files[file_key] = {
                                    "step_index": 0,
                                    "filename": fname,
                                    "target_path": str(fpath),
                                    "content": content,
                                    "summary": "Scratch Script / Utility",
                                    "tool_action": "Scratch Script",
                                    "is_artifact": False,
                                    "size_bytes": len(content.encode("utf-8"))
                                }
                            except Exception:
                                pass
        except Exception as e:
            print(f"Error reading artifacts from {brain_dir}: {e}")

    return sorted(list(seen_files.values()), key=lambda x: x["filename"].lower())

def extract_chat_timeline(db_path: Optional[Path] = None, brain_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Extracts the chronological conversation timeline.
    Prefers brain/<id>/.system_generated/logs/transcript_full.jsonl if present (to avoid truncation),
    falling back to transcript.jsonl or directly reconstructing from SQLite <id>.db steps.
    """
    # 1. Try transcript_full.jsonl or transcript.jsonl from archive
    if brain_dir and brain_dir.exists():
        logs_dir = brain_dir / ".system_generated" / "logs"
        full_t = logs_dir / "transcript_full.jsonl"
        compact_t = logs_dir / "transcript.jsonl"
        transcript_file = full_t if full_t.exists() else compact_t
        if transcript_file.exists():
            timeline = _parse_transcript_jsonl(transcript_file)
            if timeline:
                return timeline

    # 2. Fall back to SQLite db extraction
    if db_path and db_path.exists():
        return _extract_timeline_from_db(db_path)

    return []

def _parse_transcript_jsonl(transcript_path: Path) -> List[Dict[str, Any]]:
    timeline = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    idx = data.get("step_index", 0)
                    stype = data.get("type", "")
                    source = data.get("source", "")
                    ts = data.get("created_at", "")
                    content = data.get("content", "")
                    thinking = data.get("thinking", "")
                    tool_calls = data.get("tool_calls", [])

                    sender = "Assistant"
                    if stype == "USER_INPUT" or source == "USER_EXPLICIT":
                        sender = "User"
                    elif stype == "GENERIC" or source == "SYSTEM":
                        sender = "System / Tool Result"

                    timeline.append({
                        "step_index": idx,
                        "sender": sender,
                        "type": stype,
                        "timestamp": ts,
                        "content": content,
                        "thinking": thinking,
                        "tool_calls": tool_calls,
                    })
                except Exception:
                    pass
        return timeline
    except Exception as e:
        print(f"Error parsing transcript {transcript_path}: {e}")
        return []

def _extract_timeline_from_db(db_path: Path) -> List[Dict[str, Any]]:
    timeline = []
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT idx, step_type, status, step_payload FROM steps ORDER BY idx")
            rows = cur.fetchall()

            for idx, stype, status, payload in rows:
                if not payload:
                    continue

                # Check if this is a user input
                if b"<USER_REQUEST>" in payload or stype == 14:
                    matches = re.findall(rb"(?:<USER_REQUEST>|Process)[^\x00\x08\x10]{5,1000}", payload)
                    user_text = ""
                    if matches:
                        user_text = matches[0].decode('utf-8', errors='ignore')
                    if user_text:
                        timeline.append({
                            "step_index": idx,
                            "sender": "User",
                            "type": "USER_INPUT",
                            "timestamp": "",
                            "content": user_text,
                            "thinking": "",
                            "tool_calls": []
                        })
                        continue

                # Check for tool call
                start_json = payload.find(b'{"')
                if start_json != -1:
                    raw_sub = payload[start_json:]
                    depth = 0
                    in_string = False
                    escape = False
                    json_end = -1
                    for i, b in enumerate(raw_sub):
                        char = chr(b)
                        if in_string:
                            if escape:
                                escape = False
                            elif char == '\\':
                                escape = True
                            elif char == '"':
                                in_string = False
                        else:
                            if char == '"':
                                in_string = True
                            elif char == '{':
                                depth += 1
                            elif char == '}':
                                depth -= 1
                                if depth == 0:
                                    json_end = i + 1
                                    break
                    if json_end != -1:
                        try:
                            args = json.loads(raw_sub[:json_end].decode('utf-8'))
                            t_action = args.get("toolAction", "")
                            t_summary = args.get("toolSummary", "")
                            tool_name = "tool_call"
                            for tn in ["view_file", "run_command", "write_to_file", "replace_file_content", "ask_question", "search_web"]:
                                if tn.encode() in payload[:start_json]:
                                    tool_name = tn
                                    break
                            timeline.append({
                                "step_index": idx,
                                "sender": "Tool Execution",
                                "type": "TOOL_CALL",
                                "timestamp": "",
                                "content": f"**{t_action or tool_name}**: {t_summary}",
                                "thinking": "",
                                "tool_calls": [{"name": tool_name, "args": args}]
                            })
                            continue
                        except Exception:
                            pass

                # Assistant text / thoughts
                strings = re.findall(rb"[\x20-\x7e]{15,}", payload)
                clean_strings = [
                    s.decode('ascii', errors='ignore')
                    for s in strings
                    if not s.startswith(b"bot-") and not s.startswith(b"sessionID")
                ]
                if clean_strings:
                    timeline.append({
                        "step_index": idx,
                        "sender": "Assistant",
                        "type": "PLANNER_RESPONSE",
                        "timestamp": "",
                        "content": clean_strings[-1] if len(clean_strings) == 1 else " \n\n".join(clean_strings[:3]),
                        "thinking": "",
                        "tool_calls": []
                    })

        return timeline
    except Exception as e:
        print(f"Error extracting timeline from db {db_path}: {e}")
        return []
