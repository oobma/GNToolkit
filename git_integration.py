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

Every git call runs on the main thread. Jobs are small generators driven
by the timer pump in ``sync_operators``: short local commands run inline
(milliseconds), while fetch/pull/push are started as child processes and
polled across ticks, so the UI never blocks on a slow remote. No worker
thread exists anywhere in this module (and therefore no Blender data is
ever touched from one).
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time

_log = logging.getLogger("GNToolkit.git")

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


def repos_for_tracked(metadata=None, blend_dir=None):
    """Map repo root -> sorted list of tracked JSON paths living inside it.

    ``metadata`` and ``blend_dir`` are resolved by the caller on the main
    thread and travel in the job payload, so the job bodies never reach
    into Blender data (the defaults exist for direct/scripted callers)."""
    from .sync_metadata import resolve_json_path

    if metadata is None or blend_dir is None:
        from .sync_manager import sync_manager
        if metadata is None:
            metadata = sync_manager.metadata
        if blend_dir is None:
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


_STATUS_ARGS = ["status", "--porcelain=v1", "-b", "-uall", "-z"]


def repo_status(repo_root):
    """Porcelain state: branch, upstream, ahead/behind, changed files."""
    if not git_available():
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
    if not git_available():
        result = ("error", "Git not found — install Git and restart Blender")
        return result + ([],) if report_files else result
    head_before = ""
    if report_files:
        rc, out, _ = _git(["rev-parse", "HEAD"], repo_root)
        head_before = out.strip() if rc == 0 else ""
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
        rc, out_head, _ = _git(["rev-parse", "HEAD"], repo_root)
        head_after = out_head.strip() if rc == 0 else ""
        if head_after and head_after != head_before:
            rc, out_diff, _ = _git(["diff", "--name-only", head_before, head_after],
                                   repo_root)
            files = [line.strip() for line in out_diff.splitlines() if line.strip()]
    result = ("ok", (out or err).strip())
    return result + (files,) if report_files else result


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


_FETCH_ON_LOAD_TIMEOUT = 10.0


def _fetch_repo(repo_root):
    """Silent ``git fetch`` (nothing raises; offline is just a no-op)."""
    try:
        _git(["fetch", "--quiet"], repo_root, timeout=_FETCH_ON_LOAD_TIMEOUT)
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        pass


def refresh_git_state(fetch=False):
    """Rebuild the panel cache: per-repo status + conflict flags.

    With fetch=True every repo's remote is fetched first (silently), so
    ``behind`` reflects the shared repository as-is when opening a file."""
    global _state_cache
    _state_cache = {"available": git_available(), "repos": {}, "conflicts": []}
    if not _state_cache["available"]:
        return _state_cache
    try:
        repos = repos_for_tracked()
    except Exception:
        return _state_cache
    if fetch:
        for root in repos:
            _fetch_repo(root)
    tracked_set = set()
    for root, paths in repos.items():
        _state_cache["repos"][root] = _build_repo_entry(
            root, paths, repo_status(root))
        tracked_set.update(paths)
    _state_cache["conflicts"] = detect_conflict_markers(tracked_set)
    return _state_cache


def get_git_state():
    return _state_cache


def invalidate_git_state():
    global _state_cache
    _state_cache = {}


# ---------------------------------------------------------------------------
# Job pipeline — every git call runs on the main thread
# ---------------------------------------------------------------------------
#
# Jobs are generators that yield small requests; ``advance_git_jobs``
# executes a bounded amount of work per timer tick
# (``sync_operators._git_pump_tick``):
#
#   ("run",   args, cwd, timeout)  short local command, inline (milliseconds)
#   ("spawn", args, cwd, timeout)  child process polled across ticks — used
#                                  for fetch/pull/push, the slow networked ones
#   ("chunk",)                     yield control between batches of Python
#                                  work (path resolution, conflict scans)
#
# Child output is redirected to temporary files, never pipes: a verbose
# command cannot deadlock on a full pipe buffer while nobody reads it.

_CHUNK_SIZE = 50
_RUN_BUDGET = 2
_CHUNK_BUDGET = 4
_SPAWN_BUDGET = 1

_job_queue = []
_active_job = None
_results = []
_status_job_pending = False
_NO_SEND = object()


def _job_failure(exc):
    return {"ok": False, "detail": str(exc)}


def _repo_inputs():
    """(blend_dir, metadata) resolved on the main thread for repo discovery."""
    try:
        from .sync_manager import sync_manager
        return sync_manager._blend_dir(), sync_manager.metadata
    except Exception:
        return "", None


def _build_repo_entry(root, paths, st):
    """Decorate a status dict with root/name/tracked_changed and defaults."""
    st["root"] = root
    st["name"] = os.path.basename(root.rstrip("\\/")) or root
    st["tracked_changed"] = []
    if st.get("ok"):
        for p in paths:
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if rel in st.get("changed", []):
                st["tracked_changed"].append(rel)
    st.setdefault("changed", [])
    st.setdefault("ahead", 0)
    st.setdefault("behind", 0)
    st.setdefault("branch", "")
    return st


def _job_status(payload):
    """Rebuild the panel cache: optional fetch, per-repo status, conflicts."""
    state = {"available": git_available(), "repos": {}, "conflicts": []}
    if not state["available"]:
        return state
    try:
        repos = repos_for_tracked(payload.get("metadata"),
                                  payload.get("blend_dir"))
    except Exception:
        return state
    if payload.get("fetch", False):
        for root in repos:
            yield ("spawn", ["fetch", "--quiet"], root, _FETCH_ON_LOAD_TIMEOUT)
    tracked = set()
    for root, paths in repos.items():
        rc, out, err = yield ("spawn", _STATUS_ARGS, root, _TIMEOUT)
        if rc == 0:
            st = _parse_status(out)
        else:
            st = {"ok": False,
                  "error": (err or out).strip() or "git status failed"}
        state["repos"][root] = _build_repo_entry(root, paths, st)
        tracked.update(paths)
    tracked_sorted = sorted(tracked)
    hits = []
    for i in range(0, len(tracked_sorted), _CHUNK_SIZE):
        yield ("chunk",)
        hits.extend(detect_conflict_markers(tracked_sorted[i:i + _CHUNK_SIZE]))
    state["conflicts"] = hits
    return state


def _job_commit(payload):
    """Stage ONLY the tracked JSONs that changed and commit them."""
    repo = payload["repo"]
    st = repo_status(repo)
    if not st.get("ok"):
        return {"ok": False, "detail": st.get("error", "git status failed"),
                "committed": [], "nothing": False}
    paths = repos_for_tracked(payload.get("metadata"),
                              payload.get("blend_dir")).get(repo, [])
    changed = []
    for p in paths:
        rel = os.path.relpath(p, repo).replace(os.sep, "/")
        if rel in st["changed"]:
            changed.append(p)
    if not changed:
        return {"ok": False, "detail": "nothing to commit",
                "committed": [], "nothing": True}
    ok, detail = git_commit(repo, payload["message"], changed)
    return {"ok": ok, "detail": detail, "committed": changed,
            "nothing": False}


def _job_sync(payload):
    """pull --ff-only then push, both polled as child processes."""
    repo = payload["repo"]
    head_before = ""
    rc, out, _ = yield ("run", ["rev-parse", "HEAD"], repo, _TIMEOUT)
    if rc == 0:
        head_before = out.strip()
    rc, out, err = yield ("spawn", ["pull", "--ff-only"], repo, _TIMEOUT)
    if rc != 0:
        status, detail = classify_pull_failure((err or out).strip())
        return {"status": status, "detail": detail, "files": []}
    rc, out, err = yield ("spawn", ["push"], repo, _TIMEOUT)
    if rc != 0:
        return {"status": "error",
                "detail": classify_push_failure((err or out).strip()),
                "files": []}
    files = []
    if head_before:
        rc, out_head, _ = yield ("run", ["rev-parse", "HEAD"], repo, _TIMEOUT)
        head_after = out_head.strip() if rc == 0 else ""
        if head_after and head_after != head_before:
            rc, out_diff, _ = yield ("run",
                                     ["diff", "--name-only", head_before,
                                      head_after], repo, _TIMEOUT)
            files = [line.strip() for line in out_diff.splitlines()
                     if line.strip()]
    return {"status": "ok", "detail": (out or err).strip(), "files": files}


def _run_job(kind, payload):
    if kind == "status":
        return _job_status(payload)
    if kind == "commit":
        return _job_commit(payload)
    if kind == "sync":
        return _job_sync(payload)
    return {"ok": False, "detail": f"unknown job kind: {kind}",
            "committed": [], "nothing": False}


def _spawn_git(args, cwd, timeout):
    """Start a git child with output to temp files; polled by the pump."""
    out_f = tempfile.TemporaryFile()
    err_f = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(
            _git_argv(args), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=out_f, stderr=err_f,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        out_f.close()
        err_f.close()
        raise
    return {"proc": proc, "out": out_f, "err": err_f,
            "deadline": time.monotonic() + timeout}


def _read_temp(handle):
    try:
        handle.seek(0)
        return handle.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


def _close_spawn(spawn):
    for key in ("out", "err"):
        try:
            spawn[key].close()
        except (OSError, ValueError):
            pass


def _finish_job(job, result):
    global _active_job
    _log.info("[GitJob] done: %s", job["kind"])
    _results.append((job["kind"], job["payload"], result))
    spawn = job.get("spawn")
    if spawn is not None:
        try:
            spawn["proc"].kill()
        except OSError:
            pass
        _close_spawn(spawn)
    _active_job = None


def advance_git_jobs():
    """Advance the pipeline one tick. Returns True while work remains."""
    global _active_job
    run_budget, chunk_budget, spawn_budget = (_RUN_BUDGET, _CHUNK_BUDGET,
                                              _SPAWN_BUDGET)
    while True:
        if _active_job is None:
            if not _job_queue:
                return False
            kind, payload = _job_queue.pop(0)
            _log.info("[GitJob] start: %s", kind)
            _active_job = {"kind": kind, "payload": payload, "gen": None,
                           "spawn": None, "send": _NO_SEND, "request": None}
            try:
                value = _run_job(kind, payload)
            except Exception as exc:
                _finish_job(_active_job, _job_failure(exc))
                continue
            if hasattr(value, "__next__"):
                _active_job["gen"] = value
            else:
                _finish_job(_active_job, value)
            continue

        job = _active_job

        if job["spawn"] is not None:
            spawn = job["spawn"]
            rc = spawn["proc"].poll()
            if rc is None:
                if time.monotonic() < spawn["deadline"]:
                    return True
                try:
                    spawn["proc"].kill()
                except OSError:
                    pass
                send = (None, _read_temp(spawn["out"]),
                        (_read_temp(spawn["err"]).strip()
                         + "\ncommand timed out").strip())
            else:
                send = (rc, _read_temp(spawn["out"]), _read_temp(spawn["err"]))
            _close_spawn(spawn)
            job["spawn"] = None
            job["send"] = send
            continue

        request = job["request"]
        job["request"] = None
        if request is None:
            try:
                if job["send"] is _NO_SEND:
                    request = next(job["gen"])
                else:
                    request = job["gen"].send(job["send"])
                    job["send"] = _NO_SEND
            except StopIteration as stop:
                _finish_job(job, stop.value if stop.value is not None else {})
                continue
            except Exception as exc:
                _finish_job(job, _job_failure(exc))
                continue
        stage = request[0]
        if stage == "run":
            if run_budget <= 0:
                job["request"] = request
                return True
            run_budget -= 1
            try:
                job["send"] = _git(request[1], request[2], request[3])
            except Exception as exc:
                _finish_job(job, _job_failure(exc))
            continue
        if stage == "spawn":
            if spawn_budget <= 0:
                job["request"] = request
                return True
            spawn_budget -= 1
            try:
                job["spawn"] = _spawn_git(request[1], request[2], request[3])
            except Exception as exc:
                job["send"] = (None, "", str(exc))
            continue
        if stage == "chunk":
            if chunk_budget <= 0:
                job["request"] = request
                return True
            chunk_budget -= 1
            job["send"] = None
            continue
        job["send"] = None


def submit_git_job(kind, **payload):
    """Queue a job; ``advance_git_jobs`` runs it on the main thread."""
    if kind in ("status", "commit") and "blend_dir" not in payload:
        blend_dir, metadata = _repo_inputs()
        payload["blend_dir"] = blend_dir
        payload["metadata"] = metadata
    _job_queue.append((kind, payload))
    return True


def git_busy():
    return _active_job is not None or bool(_job_queue)


def drain_git_results():
    global _results
    out = _results
    _results = []
    return out


def shutdown_git_jobs():
    global _active_job, _status_job_pending
    _job_queue.clear()
    job = _active_job
    if job is not None:
        spawn = job.get("spawn")
        if spawn is not None:
            try:
                spawn["proc"].kill()
            except OSError:
                pass
            _close_spawn(spawn)
    _active_job = None
    _status_job_pending = False


def queue_status_refresh(fetch=False):
    global _status_job_pending
    if _status_job_pending:
        return False
    _status_job_pending = True
    submit_git_job("status", fetch=fetch)
    return True


def ensure_status_job(fetch=False):
    if _status_job_pending:
        return False
    if get_git_state():
        return False
    return queue_status_refresh(fetch=fetch)


def clear_status_job_flag():
    global _status_job_pending
    _status_job_pending = False


def set_git_state(state):
    global _state_cache
    _state_cache = state
