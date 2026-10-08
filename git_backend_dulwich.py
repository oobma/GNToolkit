# -*- coding: utf-8 -*-
"""
gn_toolkit.git_backend_dulwich — embedded Git engine (dulwich).

Pure-Python implementation of the local collaboration operations: repo
state, staging + commit of the tracked JSONs, history, identity and
remote-tracking ahead/behind. No external program and no subprocess is
ever launched by this module — that is what lets the add-on collaborate
without Git installed (the extension-platform build ships only this
backend).

The extension-platform package relies on Blender installing the bundled
wheels when the add-on is enabled. The legacy (GitHub) build appends its
own ``wheels/*.whl`` to the module search path before importing dulwich
(the marked block below; stripped from the platform package).

A repo status is cached behind a cheap stat signature (index, refs,
config and the tracked JSONs), so refreshing the panel on a clean repo
skips the full scan. Commits invalidate the repo's entry.

Commits from the embedded engine do not run repository hooks (no shell is
assumed to exist) — hook-driven workflows stay on the Git CLI engine.

Network operations (fetch/pull/push) run in ``git_network_worker.py``,
launched with Blender's bundled Python interpreter and polled across pump
ticks, so the UI never blocks and the operation can be cancelled; the
credentials travel over the worker's standard input, never in argv or
logs. The direct ``git_sync``/``fetch`` API runs the same worker
synchronously. Until the vault phase lands, credentials come from the
session API or ``GNT_GIT_USERNAME``/``GNT_GIT_TOKEN``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

# platform-strip:begin
import glob
import sys


def _append_legacy_wheels():
    """Legacy add-on installs don't install bundled wheels automatically."""
    wheels_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "wheels")
    for whl in sorted(glob.glob(os.path.join(wheels_dir, "*.whl"))):
        path = os.path.abspath(whl)
        if path not in sys.path:
            sys.path.append(path)


_append_legacy_wheels()
# platform-strip:end

import dulwich

_NETWORK_TIMEOUT = 60.0

_porcelain_module = None
_repo_class = None

_status_cache = {}


def available() -> bool:
    """The module only imports when dulwich is importable."""
    return True


def invalidate(repo_root=None):
    global _status_cache
    if repo_root is None:
        _status_cache = {}
    else:
        _status_cache.pop(repo_root, None)


def _porcelain():
    global _porcelain_module
    if _porcelain_module is None:
        from dulwich import porcelain
        _porcelain_module = porcelain
    return _porcelain_module


def _repo(path):
    global _repo_class
    if _repo_class is None:
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


def _refs_sig(gitdir):
    parts = []
    for sub in ("refs/heads", "refs/remotes"):
        base = os.path.join(gitdir, *sub.split("/"))
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, gitdir).replace(os.sep, "/")
                parts.append((rel,) + _stat_sig(path)[1:])
    parts.sort()
    return tuple(parts)


def _signature(repo_root, tracked_paths):
    gitdir = os.path.join(repo_root, ".git")
    parts = [_stat_sig(os.path.join(gitdir, name))
             for name in ("index", "HEAD", "packed-refs", "FETCH_HEAD",
                          "config")]
    parts.append(_refs_sig(gitdir))
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


def remote_url(repo_root):
    """Configured URL of the active branch's remote ("" when none)."""
    if not available():
        return ""
    try:
        with _repo(repo_root) as repo:
            branch = ""
            try:
                branch = _porcelain().active_branch(repo).decode("utf-8",
                                                                 "replace")
            except (KeyError, IndexError, ValueError):
                branch = ""
            config = repo.get_config_stack()
            remote = ""
            if branch:
                try:
                    remote = config.get(
                        (b"branch", branch.encode("utf-8")),
                        b"remote").decode("utf-8", "replace")
                except KeyError:
                    remote = ""
            if not remote:
                remote = "origin"
            try:
                return config.get((b"remote", remote.encode("utf-8")),
                                  b"url").decode("utf-8", "replace")
            except KeyError:
                return ""
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


_bundled_python_cache = None


def set_network_credentials(username, token):
    """Session credentials for any host (scripts, tests, headless runs)."""
    try:
        from . import credentials
        credentials.set_session("", username, token)
    except Exception:
        pass


def _credentials_for(repo_root):
    try:
        from . import credentials
        username, token, _source = credentials.lookup(remote_url(repo_root))
        if token:
            return (username or "git", token)
        return (None, None)
    except Exception:
        token = os.environ.get("GNT_GIT_TOKEN", "").strip()
        if token:
            username = os.environ.get("GNT_GIT_USERNAME", "").strip()
            return (username or "git", token)
        return (None, None)


def _bundled_python():
    global _bundled_python_cache
    if _bundled_python_cache is not None:
        return _bundled_python_cache or None
    import bpy
    base = os.path.dirname(os.path.abspath(bpy.app.binary_path))
    version = f"{bpy.app.version[0]}.{bpy.app.version[1]}"
    names = ("python.exe", "python3.exe", "python3.13", "python3.12",
             "python3.11", "python3.10", "python", "python3")
    for directory in (os.path.join(base, version, "python", "bin"),
                      os.path.join(os.path.dirname(base), "Resources",
                                   version, "python", "bin")):
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                _bundled_python_cache = path
                return path
    _bundled_python_cache = ""
    return None


def _worker_env():
    """Environment for the worker: expose where dulwich was imported from.

    Blender installs the bundled wheels outside the interpreter's default
    search path, so the subprocess receives the location through the
    standard ``PYTHONPATH`` variable (no ``sys.path`` changes in code).
    """
    env = os.environ.copy()
    try:
        location = os.path.dirname(os.path.dirname(
            os.path.abspath(dulwich.__file__)))
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = location + (os.pathsep + existing
                                        if existing else "")
    except Exception:
        pass
    return env


def _worker_command():
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "git_network_worker.py")
    python = _bundled_python()
    if python:
        return [python, "-s", "-u", worker]
    import bpy
    return [bpy.app.binary_path, "--background", "--factory-startup",
            "--python-use-system-env", "--python", worker, "--"]


class _WorkerTask(_Task):
    """Run git_network_worker.py and poll it across pump ticks."""

    def __init__(self, op, repo_root, username, token):
        self._op = op
        self._repo = repo_root
        self._username = username
        self._token = token
        self._proc = None
        self._out = None
        self._err = None
        self._deadline = None
        self._done = False
        self._value = None

    def poll(self):
        if self._done:
            return self._value
        if self._proc is None:
            self._start()
            if self._done:
                return self._value
        returncode = self._proc.poll()
        if returncode is None:
            if time.monotonic() < self._deadline:
                return None
            self._kill_process()
            self._value = {"status": "error",
                           "detail": "network operation timed out",
                           "head_after": ""}
            self._close_files()
            self._done = True
            return self._value
        self._value = self._collect(returncode)
        self._close_files()
        self._done = True
        return self._value

    def kill(self):
        self._kill_process()
        self._close_files()
        self._token = ""
        self._done = True

    def _start(self):
        command = _worker_command() + ["--op", self._op, "--repo", self._repo]
        self._out = tempfile.TemporaryFile()
        self._err = tempfile.TemporaryFile()
        try:
            self._proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=self._out,
                stderr=self._err, env=_worker_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            self._value = {
                "status": "error",
                "detail": f"could not start the network worker: {exc}",
                "head_after": ""}
            self._close_files()
            self._done = True
            return
        payload = json.dumps({"username": self._username or "",
                              "token": self._token or ""}) + "\n"
        try:
            self._proc.stdin.write(payload.encode("utf-8"))
            self._proc.stdin.close()
        except OSError:
            pass
        self._token = ""
        self._deadline = time.monotonic() + _NETWORK_TIMEOUT

    def _collect(self, returncode):
        text = _read_temp(self._out)
        line = ""
        for candidate in text.splitlines():
            if candidate.strip():
                line = candidate.strip()
        result = None
        if line:
            try:
                result = json.loads(line)
            except ValueError:
                result = None
        if not isinstance(result, dict) or "status" not in result:
            tail = _read_temp(self._err).strip().splitlines()
            detail = tail[-1] if tail else ""
            result = {"status": "error",
                      "detail": detail or
                      f"network worker failed (exit {returncode})",
                      "head_after": ""}
        result.setdefault("detail", "")
        result.setdefault("head_after", "")
        return result

    def _kill_process(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass

    def _close_files(self):
        for handle in (self._out, self._err):
            if handle is not None:
                try:
                    handle.close()
                except (OSError, ValueError):
                    pass
        self._out = None
        self._err = None


def _read_temp(handle):
    if handle is None:
        return ""
    try:
        handle.seek(0)
        return handle.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


def _run_task_sync(task):
    deadline = time.monotonic() + _NETWORK_TIMEOUT + 10.0
    while True:
        value = task.poll()
        if value is not None:
            return value
        if time.monotonic() > deadline:
            task.kill()
            return {"status": "error",
                    "detail": "network operation timed out", "head_after": ""}
        time.sleep(0.02)


def status_task(repo_root, tracked_paths=None):
    return _CallTask(lambda: status(repo_root, tracked_paths))


def head_task(repo_root):
    return _CallTask(lambda: head_sha(repo_root))


def diff_names_task(repo_root, sha_a, sha_b):
    return _CallTask(lambda: diff_names(repo_root, sha_a, sha_b))


def fetch_task(repo_root):
    username, token = _credentials_for(repo_root)
    return _WorkerTask("fetch", repo_root, username, token)


def pull_ff_task(repo_root):
    username, token = _credentials_for(repo_root)
    return _WorkerTask("pull_ff", repo_root, username, token)


def push_task(repo_root):
    username, token = _credentials_for(repo_root)
    return _WorkerTask("push", repo_root, username, token)


def fetch(repo_root):
    """Silent fetch polled to completion (fetch-on-load path)."""
    result = _run_task_sync(fetch_task(repo_root))
    invalidate(repo_root)
    return result


def git_sync(repo_root, report_files=False):
    """``pull --ff-only`` then ``push`` through the worker, synchronously."""
    head_before = head_sha(repo_root) if report_files else ""
    pull = _run_task_sync(pull_ff_task(repo_root))
    if pull.get("status") != "ok":
        invalidate(repo_root)
        result = (pull.get("status", "error"), pull.get("detail", ""))
        return result + ([],) if report_files else result
    push = _run_task_sync(push_task(repo_root))
    invalidate(repo_root)
    if push.get("status") != "ok":
        result = ("error", push.get("detail", ""))
        return result + ([],) if report_files else result
    files = []
    if report_files and head_before:
        head_after = head_sha(repo_root)
        if head_after and head_after != head_before:
            files = diff_names(repo_root, head_before, head_after)
    detail = push.get("detail") or pull.get("detail") or ""
    result = ("ok", detail)
    return result + (files,) if report_files else result
