from typing import Optional

def classify_session(title: Optional[str], preview: Optional[str], step_count: int, is_pinned: bool) -> str:
    """
    Classifies a conversation into a functional category:
    - 'pinned': Manually pinned high-value threads
    - 'automated_queue': Background recurring queue or cron runs
    - 'interactive': High-turn human design, coding, or strategy sessions
    - 'general': Standard sessions
    """
    if is_pinned:
        return "pinned"

    t = (title or "").lower()
    p = (preview or "").lower()
    combined = t + " " + p

    queue_keywords = [
        "queue", "remediate", "abstract", "flash", "who's who", 
        "pending", "cron", "scheduled", "worker", "process next",
        "process journal", "remediate journal"
    ]
    if any(k in combined for k in queue_keywords):
        return "automated_queue"

    if step_count >= 15:
        return "interactive"

    return "general"

import re

def format_display_title(raw_title: Optional[str], raw_preview: Optional[str], max_len: int = 85) -> str:
    """
    Returns a clean, compact title suitable for card headers and dropdown lists.
    - If explicit title exists, returns it cleaned.
    - If title is empty, extracts a concise headline from preview (stripping file paths,
      backticks, and truncating cleanly at word boundaries).
    """
    if raw_title and raw_title.strip():
        return raw_title.strip()

    if not raw_preview or not raw_preview.strip():
        return "Untitled Conversation"

    text = raw_preview.strip()
    # Strip markdown symbols that distort titles
    text = text.replace("`", "").replace("*", "").replace("#", "")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    first_line = lines[0] if lines else text

    # Shorten full paths to their filename/basename
    def path_repl(m):
        p = m.group(0).replace('\\', '/').rstrip('/.')
        parts = [x for x in p.split('/') if x]
        return parts[-1] if parts else p

    cleaned = re.sub(r'[A-Za-z]:\\[^ \t\n\r"\'`]+', path_repl, first_line)
    cleaned = re.sub(r'file:///[^ \t\n\r"\'`]+', path_repl, cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    if len(cleaned) > max_len:
        truncated = cleaned[:max_len].rsplit(" ", 1)[0]
        return f"{truncated}..."
    return cleaned
