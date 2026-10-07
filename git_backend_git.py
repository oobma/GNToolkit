# -*- coding: utf-8 -*-
"""
gn_toolkit.git_backend_git — Git CLI backend for the collaboration layer.

The system Git program stays the authority for history, merge and
credentials. This module only:

  - reports porcelain state (branch, upstream, ahead/behind, changed
    files), including the default identity for commits,
  - adds + commits ONLY the JSON files the addon tracks (never ``-A``),
  - syncs with ``pull --ff-only`` + ``push`` (never an automatic merge),
  - shows per-file / repository history,
  - starts fetch/pull/push as polled child processes.

Every call runs on the main thread through the task protocol of
``git_integration``: short local commands run inline, long ones
(status scans, network) are child processes polled across pump ticks, so
the UI never blocks on a slow remote. Child output goes to temporary
files, never pipes: a verbose command cannot deadlock on a full buffer.

This module is EXCLUDED from the extension-platform build (the platform
zip ships only ``git_backend_dulwich``); the GitHub/release build keeps
it and can use either engine.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time

_TIMEOUT = 20.0
_FETCH_ON_LOAD_TIMEOUT = 10.0
_NOCOLOR_FLAGS = ["-c", "color.ui=false", "-c", "core.quotepath=false",
                  "--no-pager"]
_STATUS_ARGS = ["status", "--porcelain=v1", "-b", "-uall", "-z"]

_available_cache = None

_UNSET = object()


def available() -> bool:
    global _available_cache
    if _available_cache is None:
        try:
            _git(["--version"], None)
            _available_cache = True
        except (OSError, subprocess.SubprocessError):
            _available_cache = False
    return _available_cache


def _git_argv(args):
    return ["git"] + _NOCOLOR_FLAGS + list(args)


def _git(args, cwd, timeout=_TIMEOUT):
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        _git_argv(args), cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=creationflags,
    )
    return proc.returncode, proc.stdout, proc.stderr


def invalidate(repo_root=None):
    pass


# ---------------------------------------------------------------------------
# Synchronous operations (direct API + tests)
# ---------------------------------------------------------------------------

def status(repo_root, tracked_paths=None):
    """Porcelain state: branch, upstream, ahead/behind, changed files."""
    if not available():
        return {"ok": False, "error": "Git not found"}
    rc, out, err = _git(_STATUS_ARGS, repo_root)
    if rc != 0:
        return {"ok": False, "error": (err or out).strip() or "git status failed"}
    return _parse_status(out)


def _parse_status(out):
    lines = [e for e in out.split("\x00") if e]
    branch = ""
    upstream = None
    ahead = 0
    behind = 0
    if lines and lines[0].startswith("## "):
        head = lines[0][3:]
        if head.startswith("No commits yet on "):
            branch = head[len("No commits yet on "):].strip()
        else:
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
    changed = [line[3:] for line in lines[1:] if len(line) >= 4]
    return {
        "ok": True,
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "changed": changed,
    }


def log(repo_root, rel_path=None, count=10):
    """Last commits as {hash, author, date, subject}; whole repo or one file."""
    if not available():
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


def commit(repo_root, message, paths):
    """Stage ONLY *paths* (tracked JSONs) and commit. Returns (ok, detail)."""
    if not available():
        return False, ("Git not found — install Git only for Collaboration, "
                       "then restart Blender")
    rel = [os.path.relpath(p, repo_root).replace(os.sep, "/") for p in paths]
    rc, out, err = _git(["add", "--"] + rel, repo_root)
    if rc != 0:
        return False, (err or out).strip() or "git add failed"
    rc, out, err = _git(["commit", "-m", message], repo_root)
    if rc != 0:
        return False, (err or out).strip() or "git commit failed"
    return True, (out or err).strip()


def head_sha(repo_root):
    rc, out, _ = _git(["rev-parse", "HEAD"], repo_root)
    return out.strip() if rc == 0 else ""


def diff_names(repo_root, sha_a, sha_b):
    rc, out, _ = _git(["diff", "--name-only", sha_a, sha_b], repo_root)
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def classify_pull_failure(text):
    """(status, detail) for a failed ``pull --ff-only`` from its output."""
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


def classify_push_failure(text):
    """Human detail for a failed ``push`` from its output."""
    if "no upstream" in text.lower():
        return "No upstream branch — set it with your git client"
    return text.splitlines()[0] if text else "push failed"


def git_sync(repo_root, report_files=False):
    """``pull --ff-only`` then ``push``. Returns (status, detail) with
    status in {'ok', 'diverged', 'error'}. With report_files=True the
    result is (status, detail, files) where files are the repo-relative
    paths (forward slashes) that the pull changed (empty when already
    up to date)."""
    if not available():
        result = ("error",
                  "Git not found — install Git only for Collaboration, "
                  "then restart Blender")
        return result + ([],) if report_files else result
    head_before = ""
    if report_files:
        head_before = head_sha(repo_root)
    rc, out, err = _git(["pull", "--ff-only"], repo_root)
    if rc != 0:
        status, detail = classify_pull_failure((err or out).strip())
        return (status, detail, []) if report_files else (status, detail)
    rc, out, err = _git(["push"], repo_root)
    if rc != 0:
        status = "error"
        detail = classify_push_failure((err or out).strip())
        return (status, detail, []) if report_files else (status, detail)
    files = []
    if report_files and head_before:
        head_after = head_sha(repo_root)
        if head_after and head_after != head_before:
            files = diff_names(repo_root, head_before, head_after)
    result = ("ok", (out or err).strip())
    return result + (files,) if report_files else result


def fetch(repo_root):
    """Silent ``git fetch`` (nothing raises; offline is just a no-op)."""
    try:
        _git(["fetch", "--quiet"], repo_root, timeout=_FETCH_ON_LOAD_TIMEOUT)
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        pass


# ---------------------------------------------------------------------------
# Task protocol — polled by the main-thread pump in git_integration
# ---------------------------------------------------------------------------

class _Task:
    """poll() -> None while pending, final payload once done."""

    def poll(self):
        raise NotImplementedError

    def kill(self):
        pass


class _CallTask(_Task):
    def __init__(self, fn):
        self._fn = fn
        self._value = _UNSET

    def poll(self):
        if self._value is _UNSET:
            self._value = self._fn()
        return self._value


class _MapTask(_Task):
    def __init__(self, inner, transform):
        self._inner = inner
        self._transform = transform

    def poll(self):
        value = self._inner.poll()
        if value is None:
            return None
        return self._transform(value)

    def kill(self):
        self._inner.kill()


class _SpawnTask(_Task):
    """Start a git child with output to temp files; polled across ticks."""

    def __init__(self, args, cwd, timeout):
        self._args = args
        self._cwd = cwd
        self._timeout = timeout
        self._proc = None
        self._out = None
        self._err = None
        self._deadline = None
        self._value = _UNSET

    def poll(self):
        if self._value is not _UNSET:
            return self._value
        if self._proc is None:
            self._out = tempfile.TemporaryFile()
            self._err = tempfile.TemporaryFile()
            try:
                self._proc = subprocess.Popen(
                    _git_argv(self._args), cwd=self._cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=self._out, stderr=self._err,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as exc:
                self._close()
                self._value = (None, "", str(exc))
                return self._value
            self._deadline = time.monotonic() + self._timeout
        rc = self._proc.poll()
        if rc is None:
            if time.monotonic() < self._deadline:
                return None
            try:
                self._proc.kill()
            except OSError:
                pass
            self._value = (None, self._read(self._out),
                           (self._read(self._err).strip()
                            + "\ncommand timed out").strip())
        else:
            self._value = (rc, self._read(self._out), self._read(self._err))
        self._close()
        return self._value

    def kill(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
        self._close()

    def _close(self):
        for handle in (self._out, self._err):
            if handle is not None:
                try:
                    handle.close()
                except (OSError, ValueError):
                    pass
        self._out = None
        self._err = None

    @staticmethod
    def _read(handle):
        if handle is None:
            return ""
        try:
            handle.seek(0)
            return handle.read().decode("utf-8", "replace")
        except (OSError, ValueError):
            return ""


def _status_result(result):
    rc, out, err = result
    if rc == 0:
        return _parse_status(out)
    return {"ok": False, "error": (err or out).strip() or "git status failed"}


def status_task(repo_root, tracked_paths=None):
    return _MapTask(_SpawnTask(_STATUS_ARGS, repo_root, _TIMEOUT), _status_result)


def fetch_task(repo_root):
    task = _SpawnTask(["fetch", "--quiet"], repo_root, _FETCH_ON_LOAD_TIMEOUT)

    def _result(result):
        rc, out, err = result
        return {"ok": rc == 0, "detail": (err or out).strip()}

    return _MapTask(task, _result)


def head_task(repo_root):
    return _MapTask(
        _CallTask(lambda: _git(["rev-parse", "HEAD"], repo_root)),
        lambda result: result[1].strip() if result[0] == 0 else "")


def diff_names_task(repo_root, sha_a, sha_b):
    return _MapTask(
        _CallTask(lambda: _git(["diff", "--name-only", sha_a, sha_b], repo_root)),
        lambda result: [line.strip() for line in result[1].splitlines()
                        if line.strip()] if result[0] == 0 else [])


def pull_ff_task(repo_root):
    task = _SpawnTask(["pull", "--ff-only"], repo_root, _TIMEOUT)

    def _result(result):
        rc, out, err = result
        if rc == 0:
            return {"status": "ok", "detail": (out or err).strip()}
        status, detail = classify_pull_failure((err or out).strip())
        return {"status": status, "detail": detail}

    return _MapTask(task, _result)


def push_task(repo_root):
    task = _SpawnTask(["push"], repo_root, _TIMEOUT)

    def _result(result):
        rc, out, err = result
        if rc == 0:
            return {"status": "ok", "detail": (out or err).strip()}
        return {"status": "error",
                "detail": classify_push_failure((err or out).strip())}

    return _MapTask(task, _result)
