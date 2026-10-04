import os
import stat
import shutil
import sqlite3
import sys
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Union

from config import LIVE_ANNOTATIONS_DIR, BACKUP_ANNOTATIONS_DIR

def make_writable(path: Union[str, Path]) -> None:
    """
    Ensures a file or directory has write permissions. On Windows NTFS,
    this removes the FILE_ATTRIBUTE_READONLY flag so the file can be
    overwritten, truncated, or unlinked.
    """
    try:
        p = Path(path)
        if p.exists():
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass

def are_files_identical(src: Path, dst: Path) -> bool:
    """
    Determines if two files are identical:
    - First compares file sizes.
    - For small files (<= 64 KB, including git objects & logs), performs a fast byte comparison.
    - For large files (> 64 KB), checks modification times (within 1s).
    """
    try:
        s_stat = src.stat()
        d_stat = dst.stat()
        if s_stat.st_size != d_stat.st_size:
            return False
        if s_stat.st_size <= 65536:
            return src.read_bytes() == dst.read_bytes()
        return abs(s_stat.st_mtime - d_stat.st_mtime) < 1.0
    except Exception:
        return False

def copy_file_robust(src: Path, dst: Path, check_identical: bool = False) -> bool:
    """
    Safely copies a single file from src to dst.
    - If check_identical=True and dst is identical to src, skips copying.
    - If dst exists and is read-only, clears the read-only attribute before writing.
    - Preserves metadata (timestamps and flags).
    Returns True if the file was written, False if skipped.
    """
    src = Path(src)
    dst = Path(dst)

    if not src.exists():
        return False

    if dst.exists():
        if check_identical and are_files_identical(src, dst):
            return False
        make_writable(dst)

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True

def backup_sqlite_db_safe(src_db: Path, dst_db: Path) -> bool:
    """
    Safely snapshots a SQLite database from src_db to dst_db.
    Correctly handles active databases in WAL mode (such as live Antigravity conversations)
    by using SQLite's native online backup API. This:
    1. Flushes and integrates uncheckpointed WAL pages into the target file.
    2. Guarantees transactional consistency with zero risk of torn page copies.
    3. Produces a self-contained, standalone .db file at the destination.
    Falls back to copy_file_robust if the file is not a standard SQLite database.
    """
    src_db = Path(src_db)
    dst_db = Path(dst_db)

    if not src_db.exists():
        return False

    dst_db.parent.mkdir(parents=True, exist_ok=True)
    if dst_db.exists():
        make_writable(dst_db)

    try:
        # Use read-only URI connection to prevent acquiring an exclusive write lock on live DB
        src_uri = f"file:{src_db.resolve().as_posix()}?mode=ro"
        src_conn = sqlite3.connect(src_uri, uri=True, timeout=10.0)
        try:
            dst_conn = sqlite3.connect(dst_db, timeout=10.0)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

        # Mirror modification timestamp from source
        try:
            src_stat = src_db.stat()
            os.utime(dst_db, (src_stat.st_atime, src_stat.st_mtime))
        except Exception:
            pass

        return True
    except Exception:
        # Fallback to file-level copy
        return copy_file_robust(src_db, dst_db, check_identical=False)

def sync_directory_tree(src_dir: Path, dst_dir: Path) -> Dict[str, Any]:
    """
    Safely and incrementally synchronizes src_dir to dst_dir.
    - Recursively traverses src_dir.
    - Skips identical files to eliminate redundant I/O on immutable Git objects.
    - Clears read-only attributes on pre-existing destination files before updating.
    - Handles permission errors gracefully without aborting the entire directory sync.
    Returns: {"success": bool, "copied": int, "skipped": int, "errors": List[str]}
    """
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)

    if not src_dir.exists() or not src_dir.is_dir():
        return {
            "success": False,
            "copied": 0,
            "skipped": 0,
            "errors": [f"Source directory '{src_dir}' does not exist or is not a directory."]
        }

    dst_dir.mkdir(parents=True, exist_ok=True)
    copied_count = 0
    skipped_count = 0
    errors: List[str] = []

    for root, dirs, files in os.walk(src_dir):
        rel_path = Path(root).relative_to(src_dir)
        target_root = dst_dir / rel_path

        # Ensure subdirectories exist
        for d in dirs:
            (target_root / d).mkdir(parents=True, exist_ok=True)

        for f in files:
            src_file = Path(root) / f
            dst_file = target_root / f

            try:
                if copy_file_robust(src_file, dst_file, check_identical=True):
                    copied_count += 1
                else:
                    skipped_count += 1
            except Exception as e:
                errors.append(f"Failed to copy '{src_file}' -> '{dst_file}': {e}")

    return {
        "success": len(errors) == 0,
        "copied": copied_count,
        "skipped": skipped_count,
        "errors": errors
    }

def rmtree_robust(target_dir: Union[str, Path]) -> None:
    """
    Safely removes a directory tree on Windows, automatically stripping
    read-only attributes on any file that triggers PermissionError.
    """
    target = Path(target_dir)
    if not target.exists():
        return

    def _handle_remove_readonly(func, path, exc_info):
        try:
            make_writable(path)
            func(path)
        except Exception:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=lambda func, path, exc: _handle_remove_readonly(func, path, None))
    else:
        shutil.rmtree(target, onerror=_handle_remove_readonly)

def update_annotation_pin_state(
    cid: str,
    is_pinned: bool,
    title: Optional[str] = None,
    live_ann_dir: Optional[Path] = None,
    backup_ann_dir: Optional[Path] = None
) -> bool:
    """
    Safely writes or updates the pinned status in the protobuf annotation file (.pbtxt)
    across both live Antigravity annotations and the Vault backup annotations directory.
    Ensures that pinning operations within Session Vault or Antigravity stay in lockstep.
    """
    target_live = live_ann_dir if live_ann_dir is not None else LIVE_ANNOTATIONS_DIR
    target_backup = backup_ann_dir if backup_ann_dir is not None else BACKUP_ANNOTATIONS_DIR

    pin_str = "pinned:true" if is_pinned else "pinned:false"
    success = False

    for ann_dir in [target_live, target_backup]:
        if ann_dir is None:
            continue
        try:
            ann_dir.mkdir(parents=True, exist_ok=True)
            ann_file = ann_dir / f"{cid}.pbtxt"
            if ann_file.exists():
                make_writable(ann_file)
                txt = ann_file.read_text(encoding="utf-8", errors="ignore")
                if re.search(r'\bpinned\s*:\s*(true|false)\b', txt, re.IGNORECASE):
                    new_txt = re.sub(r'\bpinned\s*:\s*(true|false)\b', pin_str, txt, flags=re.IGNORECASE)
                else:
                    new_txt = txt.rstrip() + f"  {pin_str}\n"
            else:
                if title:
                    new_txt = f'title:"{title}"  {pin_str}\n'
                else:
                    new_txt = f'{pin_str}\n'
            ann_file.write_text(new_txt, encoding="utf-8")
            success = True
        except Exception:
            pass

    return success
