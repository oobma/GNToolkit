# -*- coding: utf-8 -*-
"""
gn_toolkit.git_backend_git — Git CLI backend for the collaboration layer.

The system Git program stays the authority for history, merge and
credentials. This module only:

  - reports porcelain state (branch, upstream, ahead/behind, changed
    files), including the default identity for commits,
  - adds + commits ONLY the JSON files the addon tracks (never ``-A``),
  - syncs with ``pull --ff-only`` + ``push``; when the branch diverged
    the facade merges the tracked JSON files itself (see git_integration)
    and commits the result as a two-parent merge through ``commit_merge``,
  - shows per-file / repository history,
  - starts fetch/pull/push as polled child processes.

Every call runs on the main thread through the task protocol of
``git_integration``: short local commands run inline, long ones
(status scans, network) are child processes polled across pump ticks, so
the UI never blocks on a slow remote. Child output goes to temporary
files, never pipes: a verbose command cannot deadlock on a full buffer.

This module is EXCLUDED from the extension-platform box (which ships
only ``git_backend_dulwich``); the GitHub box keeps it and can use
either engine.
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


def _git_bytes(args, cwd, timeout=_TIMEOUT):
    """Like ``_git`` but returns raw bytes (file content safe)."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        _git_argv(args), cwd=cwd, capture_output=True,
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
                        ahead = int(up_part.split("[ahead ", 1)[1]
                                    .split("]")[0].split(",")[0])
                    except (ValueError, IndexError):
                        ahead = 0
                if "behind" in up_part:
                    try:
                        behind = int(up_part.split("behind ", 1)[1]
                                     .split("]")[0].split(",")[0])
                    except (ValueError, IndexError):
                        behind = 0
    changed = []
    index = 1 if (lines and lines[0].startswith("## ")) else 0
    while index < len(lines):
        entry = lines[index]
        if len(entry) >= 4:
            changed.append(entry[3:])
            if entry[0] in ("R", "C"):
                # Rename/copy: the next NUL field holds the source path.
                index += 1
        index += 1
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


def revisions_for_path(repo_root, rel_path, limit=50):
    """Revisions of *rel_path* with metadata, newest first (bounded).

    Each entry is ``{"hash", "author", "date", "subject"}`` (full hash;
    author date as ``YYYY-MM-DD``; subject is the first message line) —
    same contract as the dulwich backend.
    """
    if not available():
        return []
    rc, out, err = _git(["log", "--format=%H|%an|%ad|%s", "--date=short",
                         f"-n{int(limit)}", "--", rel_path], repo_root)
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


def read_blob_at(repo_root, rev, rel_path):
    """Content of *rel_path* at *rev* (bytes), or None when absent/failed."""
    if not available():
        return None
    rc, out, err = _git_bytes(["show", f"{rev}:{rel_path}"], repo_root)
    if rc != 0:
        return None
    return out


def upstream_sha(repo_root):
    """Commit id of the branch upstream ("" when none configured)."""
    if not available():
        return ""
    rc, out, err = _git(["rev-parse", "@{u}"], repo_root)
    return out.strip() if rc == 0 else ""


def merge_base(repo_root):
    """Best common ancestor of HEAD and its upstream ("" when none)."""
    if not available():
        return ""
    rc, out, err = _git(["merge-base", "HEAD", "@{u}"], repo_root)
    return out.strip() if rc == 0 else ""


def staged_names(repo_root):
    """Repo-relative paths with staged (index) changes."""
    if not available():
        return []
    rc, out, err = _git(["diff", "--cached", "--name-only", "-z"],
                        repo_root)
    if rc != 0:
        return []
    return [name for name in out.split("\x00") if name]


def commit_merge(repo_root, message, rel_paths, second_parent):
    """Commit the staged merge result with TWO parents (no MERGE_HEAD).

    Uses plumbing (add -A, write-tree, commit-tree, update-ref) so no
    hook or merge state is involved; the branch ref only moves when the
    old head still matches (no races).  Returns (ok, detail).
    """
    if not available():
        return False, ("Git not found — install Git only for Collaboration, "
                       "then restart Blender")
    rel = list(rel_paths)
    if rel:
        stageable = []
        for path in rel:
            abs_path = os.path.join(repo_root, path.replace("/", os.sep))
            if os.path.isfile(abs_path):
                stageable.append(path)
                continue
            rc, _out, _err = _git(["ls-files", "--error-unmatch", "--",
                                   path], repo_root)
            if rc == 0:
                stageable.append(path)
        if stageable:
            rc, out, err = _git(["add", "-A", "--"] + stageable, repo_root)
            if rc != 0:
                return False, (err or out).strip() or "git add failed"
    old_head = head_sha(repo_root)
    if not old_head:
        return False, "no HEAD in this repository"
    rc, tree, err = _git(["write-tree"], repo_root)
    if rc != 0:
        return False, (err or "").strip() or "git write-tree failed"
    rc, out, err = _git(["commit-tree", tree.strip(), "-p", old_head,
                         "-p", second_parent, "-m", message], repo_root)
    if rc != 0:
        return False, (err or out).strip() or "git commit-tree failed"
    new_commit = out.strip()
    rc, out, err = _git(["update-ref", "-m", message, "HEAD", new_commit,
                         old_head], repo_root)
    if rc != 0:
        return False, (err or out).strip() or "git update-ref failed"
    return True, new_commit


def restore_paths(repo_root, blobs):
    """Best-effort restore after a failed merge: unstage and rewrite.

    ``blobs`` maps repo-relative paths to their pre-merge bytes (None =
    the path must not exist).  Never raises.
    """
    if not available():
        return
    rel = sorted(blobs)
    try:
        if rel:
            _git(["reset", "-q", "--"] + rel, repo_root)
    except Exception:
        pass
    for rel_path, blob in blobs.items():
        abs_path = os.path.join(repo_root, rel_path.replace("/", os.sep))
        try:
            if blob is None:
                os.remove(abs_path)
            else:
                with open(abs_path, "wb") as handle:
                    handle.write(blob)
        except OSError:
            pass


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


def remote_url(repo_root):
    """Configured URL of the active branch's remote ("" when none)."""
    if not available():
        return ""
    rc, out, _ = _git(["config", "--get-regexp",
                       r"^(remote\..*\.url|branch\..*\.(remote|merge))$"],
                      repo_root)
    if rc != 0:
        return ""
    remote_urls = {}
    branch_remotes = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or " " not in line:
            continue
        key, value = line.split(" ", 1)
        if key.startswith("remote.") and key.endswith(".url"):
            remote_urls[key[len("remote."):-len(".url")]] = value
        elif key.startswith("branch.") and key.endswith(".remote"):
            branch_remotes[key[len("branch."):-len(".remote")]] = value
    rc, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    branch = out.strip() if rc == 0 else ""
    remote = branch_remotes.get(branch, "origin")
    return remote_urls.get(remote, "")


def diff_names(repo_root, sha_a, sha_b):
    # --no-renames: rename detection would collapse a delete + add into a
    # single new path and hide the deletion from the merge planning.
    rc, out, _ = _git(["diff", "--no-renames", "--name-only", sha_a, sha_b],
                      repo_root)
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
    """poll() -> None while pending, final payload once done.

    ``done()`` disambiguates a finished task whose payload is ``None``
    (reading a missing blob, a restore pass) from a task still pending.
    """

    def poll(self):
        raise NotImplementedError

    def done(self):
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

    def done(self):
        return self._value is not _UNSET


class _MapTask(_Task):
    def __init__(self, inner, transform):
        self._inner = inner
        self._transform = transform

    def poll(self):
        value = self._inner.poll()
        if value is None:
            return None
        return self._transform(value)

    def done(self):
        return self._inner.done()

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

    def done(self):
        return self._value is not _UNSET

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


def fetch_task(repo_root, timeout=None):
    task = _SpawnTask(["fetch", "--quiet"], repo_root,
                      timeout or _FETCH_ON_LOAD_TIMEOUT)

    def _result(result):
        rc, out, err = result
        return {"ok": rc == 0, "detail": (err or out).strip()}

    return _MapTask(task, _result)


def head_task(repo_root):
    return _MapTask(
        _CallTask(lambda: _git(["rev-parse", "HEAD"], repo_root)),
        lambda result: result[1].strip() if result[0] == 0 else "")


def upstream_sha_task(repo_root):
    return _CallTask(lambda: upstream_sha(repo_root))


def merge_base_task(repo_root):
    return _CallTask(lambda: merge_base(repo_root))


def staged_names_task(repo_root):
    return _CallTask(lambda: staged_names(repo_root))


def read_blob_task(repo_root, rev, rel_path):
    return _CallTask(lambda: read_blob_at(repo_root, rev, rel_path))


def commit_merge_task(repo_root, message, rel_paths, second_parent):
    return _CallTask(lambda: commit_merge(repo_root, message, rel_paths,
                                          second_parent))


def restore_paths_task(repo_root, blobs):
    return _CallTask(lambda: restore_paths(repo_root, blobs))


def diff_names_task(repo_root, sha_a, sha_b):
    return _MapTask(
        _CallTask(lambda: _git(["diff", "--no-renames", "--name-only",
                                sha_a, sha_b], repo_root)),
        lambda result: [line.strip() for line in result[1].splitlines()
                        if line.strip()] if result[0] == 0 else [])


def pull_ff_task(repo_root, timeout=None):
    task = _SpawnTask(["pull", "--ff-only"], repo_root, timeout or _TIMEOUT)

    def _result(result):
        rc, out, err = result
        if rc == 0:
            return {"status": "ok", "detail": (out or err).strip()}
        status, detail = classify_pull_failure((err or out).strip())
        return {"status": status, "detail": detail}

    return _MapTask(task, _result)


def push_task(repo_root, timeout=None):
    task = _SpawnTask(["push"], repo_root, timeout or _TIMEOUT)

    def _result(result):
        rc, out, err = result
        if rc == 0:
            return {"status": "ok", "detail": (out or err).strip()}
        return {"status": "error",
                "detail": classify_push_failure((err or out).strip())}

    return _MapTask(task, _result)
