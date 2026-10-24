# -*- coding: utf-8 -*-
"""
gn_toolkit.git_integration — collaboration transport facade.

Git remains the authority for history, merge and credentials. This module
locates the repository behind the tracked JSONs, caches per-repo state for
the panel and drives the main-thread job pipeline; the engines live behind
two interchangeable backends:

  - ``git_backend_git``     — the system Git CLI (GitHub/release builds);
  - ``git_backend_dulwich`` — the embedded pure-Python engine (the
    extension-platform build ships only this one).

Selection order: ``set_backend_override`` / the ``GNT_GIT_BACKEND``
environment variable (tests, headless), then the add-on preference
``git_backend``, else ``auto`` — the Git CLI when installed, the embedded
engine otherwise.

Nothing here raises to the UI — operators translate the results into
reports. Every stage runs on the main thread: jobs are small generators
driven by the timer pump in ``sync_operators``, and long operations are
task objects polled across ticks (a task returns None while pending), so
the UI never blocks on a slow scan or remote. No worker thread exists
anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import os

from .file_utils import write_file_bytes, write_json_file
from .merge import merge_package_dicts, pair_renamed_files

_log = logging.getLogger("GNToolkit.git")

_BACKEND_ENV = "GNT_GIT_BACKEND"
_VALID_BACKENDS = ("auto", "dulwich", "git")

_MERGE_COMMIT_MESSAGE = "Merge remote changes into node JSON files (GNToolkit)"

_available_cache = None
_backend_cache = None
_backend_override = None
_state_cache = {}


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def set_backend_override(name):
    """Force a backend ('auto'|'dulwich'|'git') or None; resets caches."""
    global _backend_override
    _backend_override = name if name in _VALID_BACKENDS else None
    clear_backend_caches()


def clear_backend_caches():
    """Forget the resolved engine, availability and per-repo state.

    This only asks the loaded engine backends to drop their cached
    repository state; it never touches Python modules.
    """
    global _backend_cache, _available_cache, _state_cache
    _backend_cache = None
    _available_cache = None
    _state_cache = {}
    for module in (_load_git_backend(), _load_dulwich_backend()):
        if module is None:
            continue
        try:
            module.invalidate()
        except Exception:
            pass


def _addon_preference():
    import bpy
    try:
        preferences = bpy.context.preferences.addons[__package__].preferences
        name = getattr(preferences, "git_backend", "auto")
    except Exception:
        return "auto"
    return name if name in _VALID_BACKENDS else "auto"


def _requested_backend():
    if _backend_override:
        return _backend_override
    from_env = os.environ.get(_BACKEND_ENV, "").strip().lower()
    if from_env in _VALID_BACKENDS:
        return from_env
    return _addon_preference()


def _load_git_backend():
    try:
        from . import git_backend_git
        return git_backend_git
    except Exception:
        return None


def _load_dulwich_backend():
    try:
        from . import git_backend_dulwich
        return git_backend_dulwich
    except Exception:
        return None


def _backend():
    global _backend_cache
    if _backend_cache is not None:
        return _backend_cache
    name = _requested_backend()
    chosen = None
    if name == "git":
        module = _load_git_backend()
        chosen = module if module is not None and module.available() else None
    elif name == "dulwich":
        module = _load_dulwich_backend()
        chosen = module if module is not None and module.available() else None
    else:
        module = _load_git_backend()
        if module is not None and module.available():
            chosen = module
        else:
            module = _load_dulwich_backend()
            chosen = module if module is not None and module.available() else None
    _backend_cache = chosen
    return chosen


def active_backend_name():
    backend = _backend()
    if backend is None:
        return ""
    return "dulwich" if backend.__name__.endswith("git_backend_dulwich") \
        else "git"


def git_available() -> bool:
    global _available_cache
    if _available_cache is None:
        _available_cache = _backend() is not None
    return _available_cache


# ---------------------------------------------------------------------------
# Repository discovery (engine-independent)
# ---------------------------------------------------------------------------

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


def changed_tracked_paths(repo_root: str):
    """Tracked JSON paths changed in the working tree, as (rel, abs) pairs.

    Local only (no fetch/push).  Used by the Git Commit dialog to describe
    the JSON the commit is about to write, not the pending .blend edits.
    """
    try:
        paths = repos_for_tracked().get(repo_root) or []
        status = _backend().status(repo_root)
    except Exception:
        return []
    if not status.get("ok"):
        return []
    changed = set(status.get("changed") or [])
    out = []
    for path in paths:
        rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
        if rel in changed:
            out.append((rel, path))
    return out


def staged_foreign_paths(repo_root: str):
    """Repo-relative staged paths that are NOT tracked JSON.

    ``git commit`` commits the whole index, so a file staged by another
    tool would ride along.  The commit operator calls this guard and
    refuses instead of touching somebody else's staged work.
    """
    try:
        paths = repos_for_tracked().get(repo_root) or []
        tracked = {os.path.relpath(p, repo_root).replace(os.sep, "/")
                   for p in paths}
        staged = {p.replace(os.sep, "/")
                  for p in _backend().staged_names(repo_root)}
    except Exception:
        return []
    return sorted(staged - tracked)


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


# ---------------------------------------------------------------------------
# Synchronous operations (direct API + tests)
# ---------------------------------------------------------------------------

def repo_status(repo_root, paths=None):
    backend = _backend()
    if backend is None:
        return {"ok": False, "error": "No Git engine available"}
    if paths is None:
        try:
            paths = repos_for_tracked().get(repo_root)
        except Exception:
            paths = None
    return backend.status(repo_root, paths)


def remote_url(repo_root):
    """Configured URL of the repo's active remote ("" when none)."""
    backend = _backend()
    if backend is None:
        return ""
    fn = getattr(backend, "remote_url", None)
    if fn is None:
        return ""
    try:
        return fn(repo_root)
    except Exception:
        return ""


def git_log(repo_root, rel_path=None, count=10):
    backend = _backend()
    if backend is None:
        return []
    return backend.log(repo_root, rel_path, count)


def revisions_for_path(repo_root, rel_path, limit=50):
    """Revisions of *rel_path* with metadata, newest first (bounded).

    Each entry is ``{"hash", "author", "date", "subject"}``.
    """
    backend = _backend()
    if backend is None:
        return []
    fn = getattr(backend, "revisions_for_path", None)
    if fn is None:
        return []
    try:
        return fn(repo_root, rel_path, limit)
    except Exception:
        return []


def read_blob_at(repo_root, rev, rel_path):
    """Content of *rel_path* at *rev*, or None when absent/unreadable."""
    backend = _backend()
    if backend is None:
        return None
    fn = getattr(backend, "read_blob_at", None)
    if fn is None:
        return None
    try:
        return fn(repo_root, rev, rel_path)
    except Exception:
        return None


def git_commit(repo_root, message, paths):
    backend = _backend()
    if backend is None:
        return False, ("No Git engine available — collaboration is optional, "
                       "everything else keeps working")
    return backend.commit(repo_root, message, paths)


def git_sync(repo_root, report_files=False):
    backend = _backend()
    if backend is None:
        result = ("error",
                  "No Git engine available — collaboration is optional, "
                  "everything else keeps working")
        return result + ([],) if report_files else result
    sync_fn = getattr(backend, "git_sync", None)
    if sync_fn is None:
        result = ("error", "The embedded engine brings remote changes in the "
                           "next phase — use the installed Git engine meanwhile")
        return result + ([],) if report_files else result
    return sync_fn(repo_root, report_files=report_files)


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
    backend = _backend()
    if fetch:
        fetch_fn = getattr(backend, "fetch", None)
        if fetch_fn is not None:
            for root in repos:
                try:
                    fetch_fn(root)
                except Exception:
                    pass
    tracked_set = set()
    for root, paths in repos.items():
        _state_cache["repos"][root] = _build_repo_entry(
            root, paths, backend.status(root, paths))
        tracked_set.update(paths)
    _state_cache["conflicts"] = detect_conflict_markers(tracked_set)
    return _state_cache


def get_git_state():
    return _state_cache


def invalidate_git_state():
    global _state_cache
    _state_cache = {}


# ---------------------------------------------------------------------------
# Job pipeline — every stage runs on the main thread
# ---------------------------------------------------------------------------
#
# Jobs are generators that yield small requests; ``advance_git_jobs``
# executes a bounded amount of work per timer tick
# (``sync_operators._git_pump_tick``):
#
#   ("task", task)  a backend task: poll() returns None while pending and
#                   the final payload once done — child processes for the
#                   Git CLI, inline calls for the embedded engine
#   ("chunk",)      yield control between batches of Python work (path
#                   resolution, conflict scans)

_CHUNK_SIZE = 50
_TASK_BUDGET = 2
_CHUNK_BUDGET = 4
_FETCH_ON_LOAD_TIMEOUT = 10.0

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
    st["name"] = os.path.basename(os.path.normpath(root)) or root
    try:
        st["remote_url"] = remote_url(root)
    except Exception:
        st["remote_url"] = ""
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
    st["upstream"] = st.get("upstream") or ""
    st.setdefault("head", "")
    st.setdefault("head_subject", "")
    st.setdefault("staged", [])
    st.setdefault("staged_tracked", [])
    st.setdefault("staged_foreign", [])
    tracked_rel = {os.path.relpath(p, root).replace(os.sep, "/")
                   for p in paths}
    if st.get("ok"):
        try:
            entries = _backend().log(root, None, 1)
        except Exception:
            entries = []
        if entries:
            st["head"] = entries[0].get("hash", "")
            st["head_subject"] = entries[0].get("subject", "")
        try:
            staged = {p.replace(os.sep, "/")
                      for p in _backend().staged_names(root)}
        except Exception:
            staged = set()
        st["staged"] = sorted(staged)
        st["staged_tracked"] = sorted(staged & tracked_rel)
        st["staged_foreign"] = sorted(staged - tracked_rel)
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
    backend = _backend()
    if payload.get("fetch", False) and backend is not None:
        fetch_task = getattr(backend, "fetch_task", None)
        if fetch_task is not None:
            for root in repos:
                yield ("task", fetch_task(root, timeout=_FETCH_ON_LOAD_TIMEOUT))
            invalidate = getattr(backend, "invalidate", None)
            if invalidate is not None:
                try:
                    invalidate()
                except Exception:
                    pass
    tracked = set()
    for root, paths in repos.items():
        st = yield ("task", backend.status_task(root, paths))
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
    backend = _backend()
    if backend is None:
        return {"ok": False, "detail": "No Git engine available",
                "committed": [], "nothing": False}
    paths = repos_for_tracked(payload.get("metadata"),
                              payload.get("blend_dir")).get(repo, [])
    st = backend.status(repo, paths)
    if not st.get("ok"):
        return {"ok": False, "detail": st.get("error", "status failed"),
                "committed": [], "nothing": False}
    changed = []
    for p in paths:
        rel = os.path.relpath(p, repo).replace(os.sep, "/")
        if rel in st["changed"]:
            changed.append(p)
    if not changed:
        return {"ok": False, "detail": "nothing to commit",
                "committed": [], "nothing": True}
    ok, detail = backend.commit(repo, payload["message"], changed)
    return {"ok": ok, "detail": detail, "committed": changed,
            "nothing": False}


def _job_sync(payload):
    """pull --ff-only then push; on a real divergence, merge the tracked
    JSON files semantically (values per item) into a two-parent merge
    commit and push it (F4, spec 2026-10-16).  Nothing is written when
    anything conflicts."""
    repo = payload["repo"]
    network_timeout = payload.get("network_timeout")
    backend = _backend()
    if backend is None:
        return {"status": "error", "detail": "No Git engine available",
                "files": []}

    def _drop_cache():
        invalidate = getattr(backend, "invalidate", None)
        if invalidate is not None:
            try:
                invalidate(repo)
            except Exception:
                pass

    head_before = yield ("task", backend.head_task(repo))
    pull = yield ("task", backend.pull_ff_task(repo, timeout=network_timeout))
    if pull.get("status") != "ok":
        if pull.get("status") == "diverged":
            result = yield from _merge_remote_changes(repo, payload, backend)
            _drop_cache()
            return result
        _drop_cache()
        return {"status": pull.get("status", "error"),
                "detail": pull.get("detail", ""), "files": []}
    push = yield ("task", backend.push_task(repo, timeout=network_timeout))
    if push.get("status") != "ok":
        _drop_cache()
        return {"status": "error", "detail": push.get("detail", ""),
                "files": []}
    files = []
    if head_before:
        head_after = yield ("task", backend.head_task(repo))
        if head_after and head_after != head_before:
            files = yield ("task", backend.diff_names_task(repo,
                                                           head_before,
                                                           head_after))
    _drop_cache()
    return {"status": "ok",
            "detail": push.get("detail") or pull.get("detail") or "",
            "files": files}


def _json_load_bytes(blob):
    if blob is None:
        return None
    return json.loads(blob.decode("utf-8-sig"))


def _file_merge_report(rel, report):
    """Report dict for the panel (same shape the in-addon merge stores)."""
    return {
        "name": rel,
        "status": "conflicted",
        "summary": "%d conflict(s)" % len(report.get("conflicts") or []),
        "applied": list(report.get("applied") or []),
        "conflicts": list(report.get("conflicts") or []),
        "notices": list(report.get("notices") or []),
    }


def _merge_remote_changes(repo, payload, backend):
    """Generator: semantic continuation of a diverged sync (F4).

    ours = HEAD, theirs = upstream, base = merge-base.  Tracked JSON
    files changed on both sides merge per item; a tracked file deleted on
    one side and re-added under a new name on that same side with
    identical content is a file-level rename and merges onto the new
    name; any other file changed on both sides aborts.  A file deleted on
    both sides stays deleted.  A clean result is written, staged and
    committed as a real two-parent merge commit, then pushed.
    """
    tracked_paths = repos_for_tracked(payload.get("metadata"),
                                      payload.get("blend_dir")).get(repo, [])
    tracked_rels = {os.path.relpath(p, repo).replace(os.sep, "/")
                    for p in tracked_paths}
    tracked_norm = {os.path.normcase(rel): rel for rel in tracked_rels}
    if not tracked_rels:
        return {"status": "diverged",
                "detail": "no tracked JSON files in this repository - "
                          "resolve with your Git client",
                "files": []}

    st = yield ("task", backend.status_task(repo, tracked_paths))
    if not st.get("ok"):
        return {"status": "error",
                "detail": st.get("error", "status failed"), "files": []}
    if not (st.get("ahead") and st.get("behind")):
        return {"status": "diverged",
                "detail": "the pull did not fast-forward and the branch is "
                          "not diverged - check the repository state",
                "files": []}
    changed_norm = {os.path.normcase(p) for p in st.get("changed") or []}
    if set(tracked_norm) & changed_norm:
        return {"status": "dirty",
                "detail": "commit your JSON changes first (Git Commit), "
                          "then sync again",
                "files": []}
    staged = yield ("task", backend.staged_names_task(repo))
    if staged:
        return {"status": "dirty",
                "detail": "the repository index has staged changes - "
                          "commit or unstage them first",
                "files": []}

    head = yield ("task", backend.head_task(repo))
    upstream = yield ("task", backend.upstream_sha_task(repo))
    base = yield ("task", backend.merge_base_task(repo))
    if not head or not upstream:
        return {"status": "error",
                "detail": "no upstream branch - set it with your Git client",
                "files": []}
    if not base:
        return {"status": "error",
                "detail": "no common ancestor with the upstream branch",
                "files": []}

    ours_changed = yield ("task", backend.diff_names_task(repo, base, head))
    theirs_changed = yield ("task", backend.diff_names_task(repo, base,
                                                            upstream))
    ours_only = sorted(set(ours_changed) - set(theirs_changed))
    both = sorted(set(ours_changed) & set(theirs_changed))
    theirs_only = sorted(set(theirs_changed) - set(ours_changed))

    # File-level renames (v2): a tracked JSON deleted on one side and
    # re-added under a new name on that same side with identical content
    # pairs up and the other side's edits land on the new file.  Exact
    # 1-to-1 content matches only; anything else falls back to v1 rules.
    def _soft_json(blob):
        try:
            return _json_load_bytes(blob)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    both_blobs = {}
    for rel in both:
        ours_blob = yield ("task", backend.read_blob_task(repo, head, rel))
        theirs_blob = yield ("task", backend.read_blob_task(repo, upstream,
                                                           rel))
        base_blob = yield ("task", backend.read_blob_task(repo, base, rel))
        both_blobs[rel] = (ours_blob, theirs_blob, base_blob)
    deletions = {"ours": {}, "theirs": {}}
    for rel, (ours_blob, theirs_blob, base_blob) in both_blobs.items():
        if ours_blob is None and theirs_blob is not None:
            deletions["ours"][rel] = _soft_json(base_blob)
        elif theirs_blob is None and ours_blob is not None:
            deletions["theirs"][rel] = _soft_json(base_blob)
    additions = {"ours": {}, "theirs": {}}
    for side, names, sha in (("ours", ours_only, head),
                             ("theirs", theirs_only, upstream)):
        for rel in names:
            base_blob = yield ("task",
                               backend.read_blob_task(repo, base, rel))
            if base_blob is not None:
                continue
            added_blob = yield ("task",
                                backend.read_blob_task(repo, sha, rel))
            additions[side][rel] = _soft_json(added_blob)
    renamed_ours = pair_renamed_files(deletions["ours"], additions["ours"])
    renamed_theirs = pair_renamed_files(deletions["theirs"],
                                        additions["theirs"])
    clashes = sorted(set(renamed_ours) & set(renamed_theirs))
    if clashes:
        old_rel = clashes[0]
        report = {
            "conflicts": [{
                "kind": "rename/rename",
                "item": "file renamed to '%s' on ours and to '%s' on theirs"
                        % (renamed_ours[old_rel], renamed_theirs[old_rel]),
                "base": "(same content)",
                "ours": "-> '%s'" % renamed_ours[old_rel],
                "theirs": "-> '%s'" % renamed_theirs[old_rel]}],
            "applied": [], "notices": []}
        return {"status": "merge_conflict",
                "detail": "file '%s' was renamed on both sides - resolve "
                          "it with your Git client" % old_rel,
                "files": [], "report": _file_merge_report(old_rel, report)}

    merged_files = {}
    plan_writes = {}
    plan_deletes = set()
    paired_new = set(renamed_theirs.values())
    for rel in theirs_only:
        if rel in paired_new:
            continue  # the pair merge below writes the new file
        blob = yield ("task", backend.read_blob_task(repo, upstream, rel))
        if blob is None:
            plan_deletes.add(rel)
        else:
            plan_writes[rel] = blob

    for old_rel in sorted(renamed_ours):
        new_rel = renamed_ours[old_rel]
        _ours_blob, theirs_blob, base_blob = both_blobs[old_rel]
        new_blob = yield ("task", backend.read_blob_task(repo, head,
                                                         new_rel))
        try:
            ours_data = _json_load_bytes(new_blob)
            theirs_data = _json_load_bytes(theirs_blob)
            base_data = _json_load_bytes(base_blob)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"status": "merge_blocked",
                    "detail": "cannot read '%s': %s" % (old_rel, exc),
                    "files": []}
        report = merge_package_dicts(base_data, ours_data, theirs_data)
        if not report["ok"]:
            return {"status": "merge_conflict",
                    "detail": "merge conflicts in '%s' (renamed to '%s') - "
                              "nothing was written" % (old_rel, new_rel),
                    "files": [],
                    "report": _file_merge_report(old_rel, report)}
        merged_files[new_rel] = report["merged"]
        plan_deletes.add(old_rel)

    for old_rel in sorted(renamed_theirs):
        new_rel = renamed_theirs[old_rel]
        ours_blob, _theirs_blob, base_blob = both_blobs[old_rel]
        new_blob = yield ("task", backend.read_blob_task(repo, upstream,
                                                         new_rel))
        try:
            ours_data = _json_load_bytes(ours_blob)
            theirs_data = _json_load_bytes(new_blob)
            base_data = _json_load_bytes(base_blob)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"status": "merge_blocked",
                    "detail": "cannot read '%s': %s" % (old_rel, exc),
                    "files": []}
        report = merge_package_dicts(base_data, ours_data, theirs_data)
        if not report["ok"]:
            return {"status": "merge_conflict",
                    "detail": "merge conflicts in '%s' (renamed to '%s') - "
                              "nothing was written" % (old_rel, new_rel),
                    "files": [],
                    "report": _file_merge_report(old_rel, report)}
        merged_files[new_rel] = report["merged"]
        plan_deletes.add(old_rel)

    for rel in both:
        ours_blob, theirs_blob, base_blob = both_blobs[rel]
        if rel in renamed_ours or rel in renamed_theirs:
            continue  # handled as a rename pair above
        if ours_blob is None and theirs_blob is None:
            # deleted on both sides: it stays deleted (it used to be
            # rewritten as an empty package)
            plan_deletes.add(rel)
            continue
        if os.path.normcase(rel) not in tracked_norm:
            return {"status": "merge_blocked",
                    "detail": "file '%s' changed on both sides; only tracked "
                              "JSON files can merge - resolve it with your "
                              "Git client" % rel,
                    "files": []}
        try:
            ours_data = _json_load_bytes(ours_blob)
            theirs_data = _json_load_bytes(theirs_blob)
            base_data = _json_load_bytes(base_blob)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"status": "merge_blocked",
                    "detail": "cannot read '%s': %s" % (rel, exc),
                    "files": []}
        report = merge_package_dicts(base_data, ours_data, theirs_data)
        if not report["ok"]:
            return {"status": "merge_conflict",
                    "detail": "merge conflicts in '%s' - nothing was written"
                              % rel,
                    "files": [], "report": _file_merge_report(rel, report)}
        merged_files[rel] = report["merged"]

    touched = sorted(set(merged_files) | set(plan_writes) | plan_deletes)
    originals = {}
    for rel in touched:
        abs_path = os.path.join(repo, rel.replace("/", os.sep))
        try:
            with open(abs_path, "rb") as handle:
                originals[rel] = handle.read()
        except OSError:
            originals[rel] = None
    try:
        for rel, merged in merged_files.items():
            write_json_file(os.path.join(repo, rel.replace("/", os.sep)),
                            merged)
        for rel, blob in plan_writes.items():
            write_file_bytes(os.path.join(repo, rel.replace("/", os.sep)),
                             blob)
        for rel in plan_deletes:
            try:
                os.remove(os.path.join(repo, rel.replace("/", os.sep)))
            except OSError:
                pass
    except OSError as exc:
        yield ("task", backend.restore_paths_task(repo, originals))
        return {"status": "error",
                "detail": "the merge could not write its files: %s" % exc,
                "files": []}

    ok, detail = yield ("task", backend.commit_merge_task(
        repo, _MERGE_COMMIT_MESSAGE, touched, upstream))
    if not ok:
        yield ("task", backend.restore_paths_task(repo, originals))
        return {"status": "error",
                "detail": "the merge commit failed: %s" % detail,
                "files": []}
    push = yield ("task", backend.push_task(repo,
                                            timeout=payload.get(
                                                "network_timeout")))
    if push.get("status") != "ok":
        return {"status": "error",
                "detail": "the merge was committed locally, but the push "
                          "failed: %s" % push.get("detail", ""),
                "files": touched}
    return {"status": "merged",
            "detail": "merged %d file(s) with the remote changes"
                      % len(touched),
            "files": touched}


def _run_job(kind, payload):
    if kind == "status":
        return _job_status(payload)
    if kind == "commit":
        return _job_commit(payload)
    if kind == "sync":
        return _job_sync(payload)
    return {"ok": False, "detail": f"unknown job kind: {kind}",
            "committed": [], "nothing": False}


def _close_task(task):
    if task is None:
        return
    try:
        task.kill()
    except Exception:
        pass


def _finish_job(job, result):
    global _active_job
    _log.info("[GitJob] done: %s", job["kind"])
    _close_task(job.get("task"))
    job["task"] = None
    _results.append((job["kind"], job["payload"], result))
    _active_job = None


def advance_git_jobs():
    """Advance the pipeline one tick. Returns True while work remains."""
    global _active_job
    task_budget, chunk_budget = _TASK_BUDGET, _CHUNK_BUDGET
    while True:
        if _active_job is None:
            if not _job_queue:
                return False
            kind, payload = _job_queue.pop(0)
            _log.info("[GitJob] start: %s", kind)
            _active_job = {"kind": kind, "payload": payload, "gen": None,
                           "task": None, "send": _NO_SEND, "request": None}
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

        if job["task"] is not None:
            try:
                result = job["task"].poll()
            except Exception as exc:
                _finish_job(job, _job_failure(exc))
                continue
            if result is None and not job["task"].done():
                return True
            _close_task(job["task"])
            job["task"] = None
            job["send"] = result
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
        if stage == "task":
            if task_budget <= 0:
                job["request"] = request
                return True
            task_budget -= 1
            job["task"] = request[1]
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
    if kind in ("status", "commit", "sync") and "blend_dir" not in payload:
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
        _close_task(job.get("task"))
        job["task"] = None
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
