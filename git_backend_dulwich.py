# -*- coding: utf-8 -*-
"""
gn_toolkit.git_backend_dulwich — embedded Git engine (dulwich).

Pure-Python implementation of the local collaboration operations: repo
state, staging + commit of the tracked JSONs, history, identity and
remote-tracking ahead/behind. No external program and no subprocess is
ever launched by this module — that is what lets the add-on collaborate
without Git installed (the extension-platform build ships only this
backend).

If dulwich is not importable (legacy installs and the source checkout),
the wheels bundled in ``<addon>/wheels/*.whl`` are appended to
``sys.path`` and imported from there.

A repo status is cached behind a cheap stat signature (index, refs,
config and the tracked JSONs), so refreshing the panel on a clean repo
skips the full scan. Commits invalidate the repo's entry.

Commits from the embedded engine do not run repository hooks (no shell is
assumed to exist) — hook-driven workflows stay on the Git CLI engine.

Network operations (fetch/pull/push) arrive with the worker phase; until
then the task factories return an honest error through the same task
protocol as the Git CLI backend.
"""

from __future__ import annotations

import glob
import logging
import os
import sys
import time

_log = logging.getLogger("GNToolkit.git")

_NETWORK_PENDING = ("The embedded engine brings remote changes in the "
                    "next phase — use the installed Git engine meanwhile")

_dulwich = None
_dulwich_error = None
_porcelain_module = None
_repo_class = None

_status_cache = {}


def _ensure_dulwich():
    global _dulwich, _dulwich_error
    if _dulwich is not None:
        return _dulwich
    if _dulwich_error is not None:
        return None
    try:
        import dulwich
        _dulwich = dulwich
        return _dulwich
    except ImportError as exc:
        first_error = exc
    wheels_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "wheels")
    for whl in sorted(glob.glob(os.path.join(wheels_dir, "*.whl"))):
        path = os.path.abspath(whl)
        if path not in sys.path:
            sys.path.append(path)
    try:
        import dulwich
        _dulwich = dulwich
        _log.info("embedded Git engine loaded from bundled wheels")
        return _dulwich
    except ImportError:
        _dulwich_error = str(first_error)
        return None


def available() -> bool:
    return _ensure_dulwich() is not None


def invalidate(repo_root=None):
    global _status_cache
    if repo_root is None:
        _status_cache = {}
    else:
        _status_cache.pop(repo_root, None)


def _porcelain():
    global _porcelain_module
    if _porcelain_module is None:
        _ensure_dulwich()
        from dulwich import porcelain
        _porcelain_module = porcelain
    return _porcelain_module


def _repo(path):
    global _repo_class
    if _repo_class is None:
        _ensure_dulwich()
        from dulwich.repo import Repo
        _repo_class = Repo
    return _repo_class(path)


def _friendly_error(exc):
    name = type(exc).__name__
    if name == "NotGitRepository":
        return "Not a git repository"
    text = str(exc).strip()
    return text or name


def _stat_sig(path):
    try:
        st = os.stat(path)
        return (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return (path, None, None)


def _dir_sig(path):
    try:
        st = os.stat(path)
        return (path, st.st_mtime_ns)
    except OSError:
        return (path, None)


def _signature(repo_root, tracked_paths):
    gitdir = os.path.join(repo_root, ".git")
    parts = [_stat_sig(os.path.join(gitdir, name))
             for name in ("index", "HEAD", "packed-refs", "FETCH_HEAD",
                          "config")]
    parts.append(_dir_sig(os.path.join(gitdir, "refs", "heads")))
    parts.append(_dir_sig(os.path.join(gitdir, "refs", "remotes")))
    for path in sorted(tracked_paths or ()):
        parts.append(_stat_sig(path))
    return tuple(parts)


def _fs_rel(path):
    rel = os.fsdecode(path) if isinstance(path, bytes) else str(path)
    return rel.replace(os.sep, "/")


def _branch_info(repo):
    porcelain = _porcelain()
    try:
        branch = porcelain.active_branch(repo).decode("utf-8", "replace")
    except (KeyError, IndexError, ValueError):
        return "HEAD (no branch)", None, 0, 0
    upstream = None
    ahead = 0
    behind = 0
    try:
        config = repo.get_config_stack()
        key = (b"branch", branch.encode("utf-8"))
        remote = config.get(key, b"remote").decode("utf-8", "replace")
        merge = config.get(key, b"merge").decode("utf-8", "replace")
    except (KeyError, UnicodeDecodeError, ValueError):
        remote = None
        merge = None
    if remote and merge:
        short = merge[len("refs/heads/"):] if merge.startswith("refs/heads/") \
            else merge
        upstream = f"{remote}/{short}"
        try:
            local = repo.refs[repo.refs.follow(b"HEAD")[0][1]]
        except KeyError:
            local = None
        try:
            remote_sha = repo.refs[f"refs/remotes/{remote}/{short}"
                                   .encode("utf-8")]
        except KeyError:
            remote_sha = None
        if local and remote_sha and local != remote_sha:
            try:
                ahead = sum(1 for _ in repo.get_walker(
                    include=[local], exclude=[remote_sha]))
                behind = sum(1 for _ in repo.get_walker(
                    include=[remote_sha], exclude=[local]))
            except Exception:
                ahead = 0
                behind = 0
    return branch, upstream, ahead, behind


def status(repo_root, tracked_paths=None):
    if not available():
        error = "Embedded Git engine unavailable"
        if _dulwich_error:
            error += f" ({_dulwich_error})"
        return {"ok": False, "error": error}
    signature = _signature(repo_root, tracked_paths)
    cached = _status_cache.get(repo_root)
    if cached is not None and cached[0] == signature:
        return cached[1]
    porcelain = _porcelain()
    try:
        with _repo(repo_root) as repo:
            result = porcelain.status(repo, untracked_files="all",
                                      optional_locks=False)
            branch, upstream, ahead, behind = _branch_info(repo)
        changed = set()
        for paths in result.staged.values():
            for path in paths:
                changed.add(_fs_rel(path))
        for path in result.unstaged:
            changed.add(_fs_rel(path))
        for path in result.untracked:
            changed.add(_fs_rel(path))
        entry = {"ok": True, "branch": branch, "upstream": upstream,
                 "ahead": ahead, "behind": behind,
                 "changed": sorted(changed)}
    except Exception as exc:
        entry = {"ok": False, "error": _friendly_error(exc)}
    _status_cache[repo_root] = (signature, entry)
    return entry


def _identity(repo):
    name = ""
    email = ""
    try:
        config = repo.get_config_stack()
        name = config.get((b"user",), b"name").decode("utf-8",
                                                      "replace").strip()
    except (KeyError, UnicodeDecodeError, ValueError):
        pass
    try:
        config = repo.get_config_stack()
        email = config.get((b"user",), b"email").decode("utf-8",
                                                        "replace").strip()
    except (KeyError, UnicodeDecodeError, ValueError):
        pass
    return f"{name or 'GNToolkit'} <{email or 'noreply@localhost'}>".encode(
        "utf-8")


def commit(repo_root, message, paths):
    if not available():
        return False, "Embedded Git engine unavailable"
    porcelain = _porcelain()
    rel = [os.path.relpath(p, repo_root).replace(os.sep, "/") for p in paths]
    try:
        with _repo(repo_root) as repo:
            try:
                old_head = repo.head().decode()
            except KeyError:
                old_head = ""
            porcelain.add(repo, rel)
            identity = _identity(repo)
            try:
                sha = porcelain.commit(repo, message=message.encode("utf-8"),
                                       author=identity, committer=identity,
                                       no_verify=True)
            except Exception as exc:
                try:
                    new_head = repo.head().decode()
                except KeyError:
                    new_head = ""
                if not new_head or new_head == old_head:
                    return False, _friendly_error(exc)
                sha = new_head.encode()
            try:
                branch = porcelain.active_branch(repo).decode("utf-8",
                                                              "replace")
            except Exception:
                branch = "detached"
    except Exception as exc:
        return False, _friendly_error(exc)
    invalidate(repo_root)
    subject = message.strip().splitlines()[0] if message.strip() else ""
    return True, f"[{branch} {sha.decode()[:7]}] {subject}"


def log(repo_root, rel_path=None, count=10):
    if not available():
        return []
    try:
        with _repo(repo_root) as repo:
            try:
                include = [repo.head()]
            except KeyError:
                return []
            paths = None
            if rel_path:
                paths = [rel_path.replace(os.sep, "/").encode("utf-8")]
            entries = []
            for entry in repo.get_walker(include=include, max_entries=count,
                                         paths=paths):
                commit_obj = entry.commit
                author = commit_obj.author.decode("utf-8", "replace")
                author = author.split("<")[0].strip()
                stamp = commit_obj.author_time + getattr(
                    commit_obj, "author_timezone", 0) * 60
                try:
                    date = time.strftime("%Y-%m-%d", time.gmtime(stamp))
                except (OverflowError, OSError, ValueError):
                    date = ""
                text = commit_obj.message.decode("utf-8", "replace") \
                    if commit_obj.message else ""
                entries.append({
                    "hash": commit_obj.id.decode()[:7],
                    "author": author,
                    "date": date,
                    "subject": text.splitlines()[0] if text else "",
                })
            return entries
    except Exception:
        return []


def head_sha(repo_root):
    if not available():
        return ""
    try:
        with _repo(repo_root) as repo:
            return repo.head().decode()
    except Exception:
        return ""


def diff_names(repo_root, sha_a, sha_b):
    if not available():
        return []
    try:
        from dulwich.diff_tree import tree_changes
        with _repo(repo_root) as repo:
            old = repo[sha_a.encode()].tree
            new = repo[sha_b.encode()].tree
            names = set()
            for change in tree_changes(repo.object_store, old, new):
                path = change.new.path or change.old.path
                if path:
                    names.add(_fs_rel(path))
            return sorted(names)
    except Exception:
        return []


class _Task:
    """poll() -> None while pending, final payload once done."""

    def poll(self):
        raise NotImplementedError

    def kill(self):
        pass


class _CallTask(_Task):
    def __init__(self, fn):
        self._fn = fn
        self._done = False
        self._value = None

    def poll(self):
        if not self._done:
            self._value = self._fn()
            self._done = True
        return self._value


class _ReadyTask(_Task):
    def __init__(self, value):
        self._value = value

    def poll(self):
        return self._value


def status_task(repo_root, tracked_paths=None):
    return _CallTask(lambda: status(repo_root, tracked_paths))


def head_task(repo_root):
    return _CallTask(lambda: head_sha(repo_root))


def pull_ff_task(repo_root):
    return _ReadyTask({"status": "error", "detail": _NETWORK_PENDING})


def push_task(repo_root):
    return _ReadyTask({"status": "error", "detail": _NETWORK_PENDING})
