# -*- coding: utf-8 -*-
"""
gn_toolkit.git_integration — thin Git transport for the DNA/RNA sync.

Git remains the authority for history, merge and credentials. This module
only:

  - locates the repository a JSON file belongs to (walking up to ``.git``),
  - reports porcelain state (branch, upstream, ahead/behind, changed files),
  - adds + commits ONLY the JSON files the addon tracks (never ``-A``),
  - syncs with ``pull --ff-only`` + ``push`` (never an automatic merge),
  - shows per-file / repository history,
  - flags merge-conflict markers in tracked JSON files.

Nothing here raises to the UI — operators translate the results into
reports. All subprocesses run locale-safe, without pager/color, with a
hidden console window and a timeout.
"""

from __future__ import annotations

import os
import subprocess

_TIMEOUT = 20.0
_NOCOLOR_FLAGS = ["-c", "color.ui=false", "-c", "core.quotepath=false", "--no-pager"]

_available_cache = None
_state_cache = {}


def git_available() -> bool:
    global _available_cache
    if _available_cache is None:
        try:
            _git(["--version"], None)
            _available_cache = True
        except (OSError, subprocess.SubprocessError):
            _available_cache = False
    return _available_cache


def _git(args, cwd):
    cmd = ["git"] + _NOCOLOR_FLAGS + args
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=_TIMEOUT, creationflags=creationflags,
    )
    return proc.returncode, proc.stdout, proc.stderr


def find_git_repo(path: str):
    """Absolute repository root containing *path*, or None."""
    if not path:
        return None
    d = os.path.abspath(os.path.dirname(path) if os.path.isfile(path) else path)
    while True:
        if os.path.isdir(os.path.join(d, ".git")) or os.path.isfile(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def repos_for_tracked(metadata=None):
    """Map repo root -> sorted list of tracked JSON paths living inside it."""
    from .sync_manager import sync_manager
    from .sync_metadata import resolve_json_path

    if metadata is None:
        metadata = sync_manager.metadata
    blend_dir = sync_manager._blend_dir()
    repos = {}
    for entry in metadata.get("tracked_groups", {}).values():
        abs_path = resolve_json_path(entry.get("json_path", ""), blend_dir)
        if not abs_path or not os.path.exists(abs_path):
            continue
        root = find_git_repo(abs_path)
        if not root:
            continue
        repos.setdefault(root, set()).add(abs_path)
    return {root: sorted(paths) for root, paths in repos.items()}


def repo_status(repo_root):
    """Porcelain state: branch, upstream, ahead/behind, changed files."""
    if not git_available():
        return {"ok": False, "error": "Git not found"}
    rc, out, err = _git(["status", "--porcelain=v1", "-b"], repo_root)
    if rc != 0:
        return {"ok": False, "error": (err or out).strip() or "git status failed"}
    lines = out.splitlines()
    branch = ""
    upstream = None
    ahead = 0
    behind = 0
    if lines and lines[0].startswith("## "):
        head = lines[0][3:]
        branch = head.split("...")[0].strip()
        if "..." in head:
            up_part = head.split("...", 1)[1]
            upstream = up_part.split(" ")[0].strip()
            if "[ahead" in up_part:
                try:
                    ahead = int(up_part.split("[ahead ")[1].split("]")[0].split(",")[0])
                except (ValueError, IndexError):
                    ahead = 0
            if "[behind" in up_part:
                try:
                    behind = int(up_part.split("[behind ")[1].split("]")[0].split(",")[0])
                except (ValueError, IndexError):
                    behind = 0
    changed = [line[3:].strip() for line in lines[1:] if len(line) >= 4 and line[3:].strip()]
    return {
        "ok": True,
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "changed": changed,
    }


def git_log(repo_root, rel_path=None, count=10):
    """Last commits as {hash, author, date, subject}; whole repo or one file."""
    if not git_available():
        return []
    args = ["log", "--format=%h|%an|%ad|%s", "--date=short", f"-n{count}"]
    if rel_path:
        args += ["--", rel_path]
    rc, out, err = _git(args, repo_root)
    if rc != 0:
        return []
    entries = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            entries.append({
                "hash": parts[0],
                "author": parts[1],
                "date": parts[2],
                "subject": parts[3],
            })
    return entries


def git_commit(repo_root, message, paths):
    """Stage ONLY *paths* (tracked JSONs) and commit. Returns (ok, detail)."""
    if not git_available():
        return False, "Git not found — install Git and restart Blender"
    rel = [os.path.relpath(p, repo_root).replace(os.sep, "/") for p in paths]
    rc, out, err = _git(["add", "--"] + rel, repo_root)
    if rc != 0:
        return False, (err or out).strip() or "git add failed"
    rc, out, err = _git(["commit", "-m", message], repo_root)
    if rc != 0:
        return False, (err or out).strip() or "git commit failed"
    return True, (out or err).strip()


def git_sync(repo_root):
    """``pull --ff-only`` then ``push``. Returns (status, detail) with
    status in {'ok', 'diverged', 'error'}."""
    if not git_available():
        return "error", "Git not found — install Git and restart Blender"
    rc, out, err = _git(["pull", "--ff-only"], repo_root)
    if rc != 0:
        text = (err or out).strip()
        low = text.lower()
        if "not a git repository" in low:
            return "error", "Not a git repository"
        if "no upstream" in low or "no tracking information" in low:
            return "error", "No upstream branch — set it with your git client"
        if "no such remote" in low or "does not appear to be a git repository" in low:
            return "error", "No remote configured — add one with your git client"
        if "not possible to fast-forward" in low or "diverged" in low:
            return "diverged", text.splitlines()[0] if text else "versions diverged"
        return "diverged", text.splitlines()[0] if text else "pull failed"
    rc, out, err = _git(["push"], repo_root)
    if rc != 0:
        text = (err or out).strip()
        low = text.lower()
        if "no upstream" in low:
            return "error", "No upstream branch — set it with your git client"
        return "error", text.splitlines()[0] if text else "push failed"
    return "ok", (out or err).strip()


def detect_conflict_markers(paths):
    """Absolute paths whose content carries git merge-conflict markers."""
    hits = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(64 * 1024)
        except OSError:
            continue
        if "<<<<<<<" in head:
            hits.append(p)
    return hits


def refresh_git_state():
    """Rebuild the panel cache: per-repo status + conflict flags."""
    global _state_cache
    _state_cache = {"available": git_available(), "repos": {}, "conflicts": []}
    if not _state_cache["available"]:
        return _state_cache
    try:
        repos = repos_for_tracked()
    except Exception:
        return _state_cache
    tracked_set = set()
    for root, paths in repos.items():
        st = repo_status(root)
        st["root"] = root
        st["name"] = os.path.basename(root.rstrip("\\/")) or root
        st["tracked_changed"] = []
        for p in paths:
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if rel in st["changed"]:
                st["tracked_changed"].append(rel)
        _state_cache["repos"][root] = st
        tracked_set.update(paths)
    _state_cache["conflicts"] = detect_conflict_markers(tracked_set)
    return _state_cache


def get_git_state():
    return _state_cache


def invalidate_git_state():
    global _state_cache
    _state_cache = {}
