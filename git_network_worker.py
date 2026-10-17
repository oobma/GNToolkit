# -*- coding: utf-8 -*-
"""
gn_toolkit.git_network_worker — out-of-process network operations.

Run by ``git_backend_dulwich`` with Blender's bundled Python interpreter
(never the system Git): fetch / pull --ff-only / push for one repository,
so the Blender main thread only polls the process between pump ticks and
can cancel it at any time.

Protocol:
  argv:   <script> --op pull_ff|push|fetch --repo <path>
  stdin:  one JSON line {"username": ..., "token": ...} — only when the
          remote needs authentication; end of stream means none
  stdout: exactly one JSON line
          {"status": "ok"|"diverged"|"error", "detail": ..., "head_after": ...}
  stderr: diagnostics only — credentials are never written anywhere

The script is self-contained (no relative imports): the parent process
passes the location of the installed dulwich through the standard
``PYTHONPATH`` variable, so it runs in the source checkout and in
extension installs alike. Remotes over SSH are rejected —
the embedded engine speaks HTTPS/files only (SSH stays on the Git CLI
engine).
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import sys

_SSH_REMOTE = re.compile(r"^(ssh|git\+ssh)://|^[^/\s@]+@[^/\s:]+:")


def _head(repo_path):
    try:
        from dulwich.repo import Repo
        with Repo(repo_path) as repo:
            return repo.head().decode()
    except Exception:
        return ""


def _remote_info(repo_path):
    from dulwich import porcelain
    from dulwich.repo import Repo
    with Repo(repo_path) as repo:
        try:
            branch = porcelain.active_branch(repo).decode("utf-8", "replace")
        except (KeyError, IndexError, ValueError):
            return None, "Detached HEAD — switch to a branch with an upstream"
        config = repo.get_config_stack()
        key = (b"branch", branch.encode("utf-8"))
        try:
            remote = config.get(key, b"remote").decode("utf-8", "replace")
            merge = config.get(key, b"merge").decode("utf-8", "replace")
        except KeyError:
            return None, ("No upstream branch — set it with your Git tooling "
                          "(push with -u, or set the branch upstream)")
        try:
            url = config.get((b"remote", remote.encode("utf-8")),
                             b"url").decode("utf-8", "replace")
        except KeyError:
            return None, (f"Remote '{remote}' has no URL configured — add it "
                          "with your Git tooling")
    if _SSH_REMOTE.match(url):
        return None, ("SSH remotes are not supported by the embedded engine "
                      "yet — use the Git engine for SSH")
    return {"branch": branch, "remote": remote, "url": url, "merge": merge}, None


def _creds(payload):
    token = str(payload.get("token") or "").strip()
    if not token:
        return {}
    username = str(payload.get("username") or "").strip() or "git"
    return {"username": username, "password": token}


def _detail_from(exc):
    text = str(exc).strip()
    return text or type(exc).__name__


_AUTH_DETAIL = ("authentication failed — check the token for this host in "
                "the Collaboration panel")


def _auth_error(repo_path):
    return {"status": "error", "detail": _AUTH_DETAIL,
            "head_after": _head(repo_path)}


def _update_tracking(repo_path, remote, refs):
    from dulwich.repo import Repo
    updated = 0
    remote_b = remote.encode("utf-8")
    with Repo(repo_path) as repo:
        for ref, sha in refs.items():
            if not ref.startswith(b"refs/heads/") or sha is None:
                continue
            tracking = b"refs/remotes/" + remote_b + b"/" \
                + ref[len(b"refs/heads/"):]
            try:
                current = repo.refs[tracking]
            except KeyError:
                current = None
            if current != sha:
                repo.refs[tracking] = sha
                updated += 1
    return updated


def _op_fetch(repo_path, info, creds):
    from dulwich import porcelain
    from dulwich.client import HTTPUnauthorized, HTTPProxyUnauthorized

    try:
        result = porcelain.fetch(repo_path, info["url"],
                                 outstream=io.StringIO(),
                                 errstream=io.BytesIO(),
                                 quiet=True, **creds)
    except (HTTPUnauthorized, HTTPProxyUnauthorized):
        return _auth_error(repo_path)
    updated = _update_tracking(repo_path, info["remote"], result.refs)
    return {"status": "ok",
            "detail": f"fetched {len(result.refs)} remote ref(s), "
                      f"{updated} tracking ref(s) updated",
            "head_after": _head(repo_path)}


def _check_file_matches_fixed(repo_object_store, full_path, entry_sha,
                              entry_mode, current_stat, honor_filemode,
                              blob_normalizer=None, tree_path=None):
    """Check a work-tree file against a blob with line-ending conversion.

    Mirrors dulwich 1.2.18's ``_check_file_matches`` (upstream issue #2466,
    commit 8a5738873c): the raw-size shortcut is skipped when a blob
    normalizer is active and the work-tree bytes are cleaned before the
    comparison, as git does.
    """
    from dulwich.objects import Blob

    if honor_filemode:
        current_mode = stat.S_IMODE(current_stat.st_mode)
        expected_mode = stat.S_IMODE(entry_mode)
        if stat.S_ISREG(current_stat.st_mode):
            current_mode_normalized = current_mode & 0o755
            expected_mode_normalized = expected_mode & 0o755
            if expected_mode_normalized not in (0o644, 0o755):
                expected_mode_normalized = 0o644
            if current_mode_normalized not in (0o644, 0o755):
                if current_mode & 0o100:
                    current_mode_normalized = 0o755
                else:
                    current_mode_normalized = 0o644
            if current_mode_normalized != expected_mode_normalized:
                return False
        else:
            if current_mode != expected_mode:
                return False

    blob_obj = repo_object_store[entry_sha]
    normalize = blob_normalizer is not None and tree_path is not None
    if not normalize and current_stat.st_size != blob_obj.raw_length():
        return False

    try:
        with open(full_path, "rb") as handle:
            current_content = handle.read()
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        return False

    if normalize:
        current_content = blob_normalizer.checkin_normalize(
            Blob.from_string(current_content), tree_path).as_raw_string()
    return current_content == blob_obj.as_raw_string()


def _patch_check_file_matches():
    """Install the fixed check on the bundled dulwich 1.2.17 wheel."""
    import dulwich
    from dulwich import index

    version = getattr(dulwich, "__version__", ())
    if isinstance(version, str):
        version = tuple(int(part) for part in version.split(".")
                        if part.isdigit())
    try:
        if tuple(version) >= (1, 2, 18):
            return
    except TypeError:
        pass
    current = getattr(index, "_check_file_matches", None)
    if current is None or getattr(current, "__name__", "") \
            == "_check_file_matches_fixed":
        return
    index._check_file_matches = _check_file_matches_fixed


def _op_pull_ff(repo_path, info, creds):
    from dulwich import porcelain
    from dulwich.client import HTTPUnauthorized, HTTPProxyUnauthorized
    from dulwich.diff_tree import tree_changes
    from dulwich.errors import WorkingTreeModifiedError
    from dulwich.graph import can_fast_forward
    from dulwich.repo import Repo

    _patch_check_file_matches()

    try:
        result = porcelain.fetch(repo_path, info["url"],
                                 outstream=io.StringIO(),
                                 errstream=io.BytesIO(),
                                 quiet=True, **creds)
    except (HTTPUnauthorized, HTTPProxyUnauthorized):
        return _auth_error(repo_path)
    _update_tracking(repo_path, info["remote"], result.refs)
    merge_ref = info["merge"].encode("utf-8")
    remote_sha = result.refs.get(merge_ref)
    if remote_sha is None:
        return {"status": "error",
                "detail": "remote branch not found — push the branch first "
                          "or check the upstream",
                "head_after": _head(repo_path)}
    branch_ref = b"refs/heads/" + info["branch"].encode("utf-8")
    try:
        with Repo(repo_path) as repo:
            try:
                local_sha = repo.refs[branch_ref]
            except KeyError:
                local_sha = None
            if local_sha == remote_sha:
                return {"status": "ok", "detail": "already up to date",
                        "head_after": (local_sha or b"").decode()}
            if local_sha is not None \
                    and can_fast_forward(repo, remote_sha, local_sha):
                return {"status": "ok",
                        "detail": "already up to date (local commits ahead)",
                        "head_after": local_sha.decode()}
            if local_sha is not None \
                    and not can_fast_forward(repo, local_sha, remote_sha):
                return {"status": "diverged",
                        "detail": "not possible to fast-forward — resolve "
                                  "with your Git tooling",
                        "head_after": local_sha.decode()}
            old_tree = repo[local_sha].tree if local_sha else None
            new_tree = repo[remote_sha].tree
            config = repo.get_config_stack()
            try:
                porcelain.update_working_tree(
                    repo, old_tree, new_tree,
                    change_iterator=tree_changes(repo.object_store, old_tree,
                                                 new_tree),
                    blob_normalizer=repo.get_blob_normalizer(config=config),
                    allow_overwrite_modified=False,
                    config=config)
            except WorkingTreeModifiedError:
                return {"status": "error",
                        "detail": "local modifications would be overwritten "
                                  "— commit or discard them first",
                        "head_after": _head(repo_path)}
            repo.refs[branch_ref] = remote_sha
            return {"status": "ok", "detail": "fast-forwarded",
                    "head_after": remote_sha.decode()}
    except KeyError as exc:
        return {"status": "error",
                "detail": f"cannot fast-forward ({exc})",
                "head_after": _head(repo_path)}


def _op_push(repo_path, info, creds):
    from dulwich import porcelain
    from dulwich.client import HTTPUnauthorized, HTTPProxyUnauthorized
    from dulwich.errors import SendPackError
    from dulwich.repo import Repo

    refspec = f"refs/heads/{info['branch']}:{info['merge']}"
    try:
        porcelain.push(repo_path, info["url"], refspecs=refspec,
                       outstream=io.BytesIO(), errstream=io.BytesIO(),
                       **creds)
    except (HTTPUnauthorized, HTTPProxyUnauthorized):
        return _auth_error(repo_path)
    except SendPackError as exc:
        detail = _detail_from(exc)
        low = detail.lower()
        if "non-fast-forward" in low or "fetch first" in low \
                or "rejected" in low:
            detail = "push rejected (non-fast-forward) — bring the remote " \
                     "changes first"
        return {"status": "error", "detail": detail,
                "head_after": _head(repo_path)}
    head = _head(repo_path)
    if head:
        short = info["merge"][len("refs/heads/"):] if info["merge"].startswith(
            "refs/heads/") else info["merge"]
        tracking = b"refs/remotes/" + info["remote"].encode("utf-8") + b"/" \
            + short.encode("utf-8")
        with Repo(repo_path) as repo:
            repo.refs[tracking] = head.encode("utf-8")
    return {"status": "ok", "detail": "pushed", "head_after": head}


_OPS = {"fetch": _op_fetch, "pull_ff": _op_pull_ff, "push": _op_push}


def _parse_argv(argv):
    values = {}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in ("--op", "--repo") and index + 1 < len(argv):
            values[item[2:]] = argv[index + 1]
            index += 2
            continue
        index += 1
    return values


def _read_credentials():
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main(argv):
    import traceback

    options = _parse_argv(argv)
    op = options.get("op", "")
    repo_path = options.get("repo", "")
    result = {"status": "error", "detail": "", "head_after": ""}
    try:
        if op not in _OPS:
            result["detail"] = f"unknown operation: {op!r}"
        elif not repo_path or not os.path.isdir(repo_path):
            result["detail"] = f"repository not found: {repo_path!r}"
        else:
            info, error = _remote_info(repo_path)
            if error:
                result["detail"] = error
            else:
                result = _OPS[op](repo_path, info, _creds(_read_credentials()))
    except Exception as exc:
        result = {"status": "error", "detail": _detail_from(exc),
                  "head_after": _head(repo_path)}
        traceback.print_exc(file=sys.stderr)
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
