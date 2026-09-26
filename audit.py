# -*- coding: utf-8 -*-
"""
audit.py — project-level audits for GNToolkit (pure Python, no bpy).

Three questions answered from data the addon already stores:

* impact     — "what breaks if I change this group?" — reverse dependency
               graph + transitive closure over the metadata's depends_on
               uuids (cycles are safe).
* duplicates — "do I have the same logic copied under two names?" —
               content hash that ignores group identity (the group name
               and the name-derived ``node_tool_idname``).
* health     — "is the JSON side of the project sound?" — missing files,
               unreadable JSONs, git conflict markers, external
               references and duplicate buckets.

Used by gnt_check (headless CLI) and by the addon's Audit operator.
"""

from __future__ import annotations

import hashlib
import json
import os

from .hash_utils import canonicalize_node_tree_data

_IDENTITY_TREE_PROPS = ("node_tool_idname",)
_CONFLICT_MARKER = "<<<<<<<"


# ---------------------------------------------------------------------------
# Dependency impact
# ---------------------------------------------------------------------------

def _tracked(metadata):
    if not isinstance(metadata, dict):
        return {}
    tracked = metadata.get("tracked_groups", {})
    return tracked if isinstance(tracked, dict) else {}


def resolve_uid(metadata, name_or_uid: str):
    """Accept a uid or a group name; exact match.  Returns None if unknown."""
    tracked = _tracked(metadata)
    if name_or_uid in tracked:
        return name_or_uid
    for uid, info in tracked.items():
        if isinstance(info, dict) and info.get("blend_name") == name_or_uid:
            return uid
    return None


def group_name(metadata, uid: str) -> str:
    info = _tracked(metadata).get(uid)
    if isinstance(info, dict) and info.get("blend_name"):
        return info["blend_name"]
    return uid


def reverse_deps(metadata) -> dict:
    """Return {dependency_uid: [dependent_uids...]} (direct edges only)."""
    rev: dict = {}
    for uid, info in _tracked(metadata).items():
        for dep in (info.get("depends_on") or []):
            rev.setdefault(dep, [])
            if uid not in rev[dep]:
                rev[dep].append(uid)
    for users in rev.values():
        users.sort()
    return rev


def impact(metadata, name_or_uid: str):
    """Dependents of a group: direct + transitive closure (cycle-safe).

    Returns {"uid", "name", "direct": [...], "transitive": [...]} where
    the lists hold {"uid", "name"} dicts.  ``transitive`` is the full
    closure INCLUDING the direct dependents, excluding the target itself.
    Returns None when the group is not tracked.
    """
    uid = resolve_uid(metadata, name_or_uid)
    if uid is None:
        return None
    rev = reverse_deps(metadata)
    direct = list(rev.get(uid, []))
    seen = {uid}
    queue = list(direct)
    order = []
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        order.append(current)
        queue.extend(rev.get(current, []))

    def entry(u):
        return {"uid": u, "name": group_name(metadata, u)}

    return {
        "uid": uid,
        "name": group_name(metadata, uid),
        "direct": [entry(u) for u in direct],
        "transitive": [entry(u) for u in order],
    }


# ---------------------------------------------------------------------------
# Content identity / duplicates
# ---------------------------------------------------------------------------

def content_hash_from_json_data(data: dict) -> str:
    """Canonical hash of the group's *logic*, ignoring group identity.

    Excluded: the group name and the name-derived ``node_tool_idname``
    tree property.  Everything else (nodes, links, interface, socket
    properties, remaining tree properties) is part of the identity.
    """
    d = dict(data) if isinstance(data, dict) else {}
    d.pop("name", None)
    props = d.get("tree_properties")
    if isinstance(props, dict):
        props = {k: v for k, v in props.items()
                 if k not in _IDENTITY_TREE_PROPS}
        if props:
            d["tree_properties"] = props
        else:
            d.pop("tree_properties", None)
    canonical = canonicalize_node_tree_data(d)
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def duplicates_from_records(records) -> list:
    """records: iterable of {"name", "hash"}.  Returns buckets > 1 member,
    biggest first."""
    by_hash: dict = {}
    for rec in records:
        by_hash.setdefault(rec["hash"], []).append(rec["name"])
    buckets = [{"hash": h, "names": sorted(names)}
               for h, names in by_hash.items() if len(names) > 1]
    buckets.sort(key=lambda b: (-len(b["names"]), b["names"][0]))
    return buckets


# ---------------------------------------------------------------------------
# Folder scan / health
# ---------------------------------------------------------------------------

def _read_text(path: str):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except (UnicodeDecodeError, OSError):
        return None


def scan_folder(folder: str) -> list:
    """Read every *.json under *folder*.

    Returns records: {"path", "name" or None, "data" or None,
    "error": None | "unreadable" | "conflict"}.
    """
    files = []
    for root, _dirs, names in os.walk(folder):
        for n in sorted(names):
            if n.endswith(".json"):
                files.append(os.path.join(root, n))

    records = []
    for path in sorted(files):
        text = _read_text(path)
        if text is None:
            records.append({"path": path, "name": None, "data": None,
                            "error": "unreadable"})
            continue
        if _CONFLICT_MARKER in text:
            records.append({"path": path, "name": None, "data": None,
                            "error": "conflict"})
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            records.append({"path": path, "name": None, "data": None,
                            "error": "unreadable"})
            continue
        if not isinstance(data, dict):
            records.append({"path": path, "name": None, "data": None,
                            "error": "unreadable"})
            continue
        if data.get("type") == "GN_UNIFIED_PACKAGE":
            for gname, gdata in (data.get("node_groups") or {}).items():
                records.append({"path": path, "name": gname, "data": gdata,
                                "error": None})
        else:
            records.append({"path": path, "name": data.get("name"),
                            "data": data, "error": None})
    return records


def external_refs(metadata) -> list:
    """Tracked groups whose depends_on points outside the tracked set."""
    tracked = _tracked(metadata)
    out = []
    for uid, info in tracked.items():
        missing = [d for d in (info.get("depends_on") or [])
                   if d not in tracked]
        if missing:
            out.append({"uid": uid,
                        "name": (info.get("blend_name") or uid),
                        "missing_uuids": sorted(set(missing))})
    out.sort(key=lambda e: e["name"])
    return out


def missing_json_files(metadata, metadata_dir: str) -> list:
    """Tracked entries whose recorded json_path does not exist on disk."""
    out = []
    for uid, info in _tracked(metadata).items():
        rel = info.get("json_path") or ""
        name = info.get("blend_name") or uid
        if not rel:
            out.append({"uid": uid, "name": name, "path": None})
            continue
        path = os.path.normpath(os.path.join(metadata_dir, rel))
        if not os.path.isfile(path):
            out.append({"uid": uid, "name": name, "path": path})
    out.sort(key=lambda e: e["name"])
    return out


def health(folder: str | None = None, metadata: dict | None = None,
           metadata_dir: str | None = None) -> dict:
    """Consolidated JSON-side health report.

    Folder scan (optional) covers unreadable/conflicted files and
    duplicates; metadata (optional) covers missing files and external
    references.
    """
    report = {
        "files": 0, "groups": 0, "unreadable": [], "conflicts": [],
        "duplicates": [], "missing_files": [], "external_refs": [],
    }
    if folder:
        records = scan_folder(folder)
        report["files"] = len({r["path"] for r in records})
        good = [r for r in records if r["error"] is None and r["name"]]
        report["groups"] = len(good)
        report["unreadable"] = [r["path"] for r in records
                                if r["error"] == "unreadable"]
        report["conflicts"] = [r["path"] for r in records
                               if r["error"] == "conflict"]
        hashed = [{"name": r["name"],
                   "hash": content_hash_from_json_data(r["data"])}
                  for r in good]
        report["duplicates"] = duplicates_from_records(hashed)
    if metadata is not None:
        report["missing_files"] = missing_json_files(metadata,
                                                     metadata_dir or "")
        report["external_refs"] = external_refs(metadata)
    return report