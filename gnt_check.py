# -*- coding: utf-8 -*-
"""
gnt_check — headless JSON-side status check for GNToolkit (pure Python, NO bpy).

Given a folder of per-group JSONs (folder workflow) or a package file, and an
optional baseline, this reports which groups changed on the JSON side without
opening Blender.  Use it in git hooks, CI or a release gate.

Usage:
    python gnt_check.py <folder_or_package> [--baseline <file>] [--json] [--strict]

Audit modes (same pure-Python core, no Blender):
    python gnt_check.py --impact "<group>" --baseline project.blend.gntsync [--modifiers DIR]
    python gnt_check.py <folder> --duplicates
    python gnt_check.py <folder> [--baseline project.blend.gntsync] --health

Cross-project identity (same pure-Python core, no Blender):
    python gnt_check.py --cross projectA/NodeGroups projectB/NodeGroups [--strict]

Baseline sources:
    * a .gntsync sidecar next to a .blend (paths relative to the sidecar dir)
    * a flat JSON mapping group name -> canonical hash

    Sidecar baselines record the hash-algorithm version that produced their
    hashes.  Comparing a baseline with a checker from a different release
    would report every group as changed, so the checker refuses to compare
    (exit 3) and asks for matched versions instead.

Exit codes:
    0  all groups clean (or, without a baseline, everything parsed)
    1  changes or missing groups detected
    2  usage / IO / parse errors (with --strict, unparseable files also exit 2)
    3  baseline written by a different hash-algorithm version (update the
       checker to the add-on's release, or open the project once with the
       current add-on to re-stamp the baseline)
    In --cross mode, --strict exits 1 when divergent forks are found.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import types

_ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_pure(module_name: str, path: str):
    """Load a repo module by file path, resolving its relative imports
    against a synthetic package so bpy-free modules can run standalone."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_hash_utils():
    pkg = types.ModuleType("gnt_core")
    pkg.__path__ = []
    sys.modules["gnt_core"] = pkg
    _load_pure("gnt_core.constants", os.path.join(_ROOT, "constants.py"))
    return _load_pure("gnt_core.hash_utils", os.path.join(_ROOT, "hash_utils.py"))


def _load_audit():
    """Load audit.py (pure) after the synthetic package exists."""
    if "gnt_core.hash_utils" not in sys.modules:
        _load_hash_utils()
    return _load_pure("gnt_core.audit", os.path.join(_ROOT, "audit.py"))


def _is_gntsync(path: str) -> bool:
    return path.lower().endswith(".gntsync")


def _current_hash_version():
    """Hash-algorithm version of this checker (from its own constants.py)."""
    _load_hash_utils()
    return getattr(sys.modules.get("gnt_core.constants"), "HASH_VERSION", None)


def _baseline_version_problem(path: str) -> str | None:
    """Return a message when a sidecar baseline cannot be compared here.

    Sidecar baselines record the hash-algorithm version that produced their
    hashes (``hash_version``); comparing across versions would report every
    group as changed.  Flat ``{name: hash}`` baselines carry no version and
    are left to the caller.
    """
    if not _is_gntsync(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or "tracked_groups" not in data:
        return None
    stored = data.get("hash_version")
    current = _current_hash_version()
    if stored == current:
        return None
    name = os.path.basename(path)
    if not isinstance(stored, int):
        return (f"baseline '{name}' has no hash algorithm version (old "
                f"sidecar): open the project once with the current add-on to "
                f"re-stamp it")
    return (f"baseline '{name}' was written with hash algorithm v{stored} but "
            f"this checker uses v{current}: use gnt_check.py from the same "
            f"release as the add-on that maintains the baseline, or open the "
            f"project once with the current add-on to re-stamp it")


def _load_baseline(path: str, base_dir: str):
    """Return {group_name: (expected_hash, file_path)} from a baseline file."""
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    out: dict = {}
    if isinstance(data, dict) and "tracked_groups" in data:
        for _uid, info in data["tracked_groups"].items():
            name = info.get("blend_name")
            if not name:
                continue
            rel = info.get("json_path", "")
            fp = os.path.normpath(os.path.join(base_dir, rel)) if rel else None
            out[name] = (info.get("last_json_hash"), fp)
    elif isinstance(data, dict):
        for name, h in data.items():
            if isinstance(h, str):
                out[name] = (h, None)
    return out


def _scan_folder(folder: str) -> list[str]:
    files = []
    for root, _dirs, names in os.walk(folder):
        for n in sorted(names):
            if n.endswith(".json"):
                files.append(os.path.join(root, n))
    return sorted(files)


def _read_json(fp: str):
    try:
        with open(fp, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def check(folder_or_file: str, baseline_path: str | None, strict: bool):
    hu = _load_hash_utils()
    if os.path.isdir(folder_or_file):
        files = _scan_folder(folder_or_file)
    else:
        files = [folder_or_file]

    base_dir = os.path.dirname(os.path.abspath(baseline_path)) if baseline_path else None
    baseline = _load_baseline(baseline_path, base_dir) if baseline_path else {}

    t0 = time.time()
    clean, changed, missing, unparseable = [], [], [], []
    seen = set()
    for fp in files:
        data = _read_json(fp)
        if not isinstance(data, dict):
            unparseable.append(fp)
            continue
        if data.get("type") == "GN_UNIFIED_PACKAGE":
            groups = data.get("node_groups", {})
            for name, gdata in groups.items():
                seen.add(name)
                h = hu.canonical_hash_from_json_data(gdata)
                if name in baseline:
                    expected, _exp_fp = baseline[name]
                    (clean if expected == h else changed).append(
                        name if expected == h else {"name": name, "file": fp,
                                                    "before": expected, "after": h})
                else:
                    clean.append(name)
        else:
            name = data.get("name")
            if not name:
                unparseable.append(fp)
                continue
            seen.add(name)
            h = hu.canonical_hash_from_json_data(data)
            if name in baseline:
                expected, _exp_fp = baseline[name]
                if expected == h:
                    clean.append(name)
                else:
                    changed.append({"name": name, "file": fp,
                                    "before": expected, "after": h})
            else:
                clean.append(name)
    for name, (_h, fp) in baseline.items():
        if name not in seen:
            missing.append({"name": name, "file": fp})

    dt = round(time.time() - t0, 3)
    report = {
        "files": len(files), "groups": len(seen), "clean": clean,
        "changed": changed, "missing": missing,
        "unparseable": unparseable, "dt_seconds": dt,
    }
    return report, bool(changed) or bool(missing), bool(unparseable and strict)


def _selftest() -> int:
    """Prove the canonical hasher runs without bpy on minimal group JSONs."""
    hu = _load_hash_utils()
    with tempfile.TemporaryDirectory(prefix="gnt_check_") as td:
        a = {"name": "Selftest A", "nodes": [], "links": []}
        b = {"name": "Selftest B", "nodes": [], "links": []}
        fa = os.path.join(td, "A.json")
        fb = os.path.join(td, "B.json")
        with open(fa, "w", encoding="utf-8") as f:
            json.dump(a, f)
        with open(fb, "w", encoding="utf-8") as f:
            json.dump(b, f)
        h1 = hu.canonical_hash_from_json_path(fa)
        h2 = hu.canonical_hash_from_json_path(fa)
        h3 = hu.canonical_hash_from_json_path(fb)
        ok = (h1 is not None and h1 == h2 and h1 != h3 and "bpy" not in sys.modules)
    print("selftest: " + ("OK - canonical hashing works without bpy"
                          if ok else "FAILED"))
    return 0 if ok else 1


def _load_metadata(path: str):
    """Load a .gntsync sidecar (requires tracked_groups); None otherwise."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if isinstance(data, dict) and isinstance(data.get("tracked_groups"), dict):
        return data
    return None


def run_impact(baseline_path: str, target_name: str, as_json: bool,
               modifiers_path: str = "") -> int:
    if not baseline_path:
        print("error: --impact requires --baseline (a .gntsync sidecar)")
        return 2
    metadata = _load_metadata(baseline_path)
    if metadata is None:
        print("error: baseline is not a .gntsync sidecar with tracked_groups")
        return 2
    audit = _load_audit()
    result = audit.impact(metadata, target_name)
    if result is None:
        print(f"error: group not tracked: {target_name}")
        return 2
    if modifiers_path:
        if not os.path.isdir(modifiers_path):
            print(f"error: --modifiers folder not found: {modifiers_path}")
            return 2
        mods_dir = modifiers_path
    else:
        mods_dir = audit.modifiers_dir_for(metadata,
                                           os.path.dirname(baseline_path))
    consumers = audit.modifier_consumers(
        audit.scan_modifier_records(mods_dir) if mods_dir else [],
        result["name"])
    result["modifiers"] = consumers
    if as_json:
        print(json.dumps(result))
        return 0
    direct_uids = {e["uid"] for e in result["direct"]}
    print(f'Impact of "{result["name"]}" ({result["uid"]}):')
    print(f'  {len(result["direct"])} direct, '
          f'{len(result["transitive"])} total dependents')
    for e in result["transitive"]:
        tag = "direct" if e["uid"] in direct_uids else "transitive"
        print(f'  [{tag}] {e["name"]}')
    if consumers:
        print(f'Used by {len(consumers)} object modifier(s):')
        for c in consumers:
            print(f'  [object] {c["object"]} - "{c["modifier"]}"')
    return 0


def run_duplicates(folder: str, as_json: bool) -> int:
    audit = _load_audit()
    records = [r for r in audit.scan_folder(folder)
               if r["error"] is None and r["name"]]
    hashed = [{"name": r["name"],
               "hash": audit.content_hash_from_json_data(r["data"])}
              for r in records]
    buckets = audit.duplicates_from_records(hashed)
    if as_json:
        print(json.dumps(buckets))
        return 0
    if not buckets:
        print(f"duplicates: none ({len(records)} groups checked)")
        return 0
    print(f"duplicates: {len(buckets)} bucket(s) with identical logic")
    for b in buckets:
        print(f'  [{len(b["names"])} groups] {b["hash"][:12]}...')
        for n in b["names"]:
            print(f'      {n}')
    return 0


def run_health(folder: str | None, baseline_path: str | None,
               as_json: bool, strict: bool) -> int:
    audit = _load_audit()
    metadata = None
    metadata_dir = None
    if baseline_path:
        metadata = _load_metadata(baseline_path)
        if metadata is None:
            print("error: baseline is not a .gntsync sidecar with tracked_groups")
            return 2
        metadata_dir = os.path.dirname(os.path.abspath(baseline_path))
    if not folder and metadata is None:
        print("error: --health needs a target folder and/or a .gntsync baseline")
        return 2
    report = audit.health(folder, metadata, metadata_dir)
    if as_json:
        print(json.dumps(report))
    else:
        print(f'health: {report["files"]} files, {report["groups"]} groups')
        print(f'  unreadable: {len(report["unreadable"])} - '
              f'conflicts: {len(report["conflicts"])} - '
              f'duplicate buckets: {len(report["duplicates"])} - '
              f'missing files: {len(report["missing_files"])} - '
              f'external refs: {len(report["external_refs"])}')
        for u in report["unreadable"]:
            print(f"  [UNREADABLE] {u}")
        for c in report["conflicts"]:
            print(f"  [CONFLICT]   {c}")
        for b in report["duplicates"]:
            print(f'  [DUP] {len(b["names"])} groups: {", ".join(b["names"])}')
        for m in report["missing_files"]:
            print(f'  [MISSING] {m["name"]}  ({m["path"]})')
        for e in report["external_refs"]:
            print(f'  [EXTERNAL] {e["name"]} -> '
                  f'{len(e["missing_uuids"])} untracked ref(s)')
    hard = bool(report["unreadable"] or report["conflicts"])
    return 2 if (strict and hard) else 0


def run_cross(targets, as_json: bool, strict: bool) -> int:
    audit = _load_audit()
    for target in targets:
        if not os.path.exists(target):
            print(f"error: target not found: {target}")
            return 2
    report = audit.cross_project(targets)
    total_groups = sum(p["groups"] for p in report["projects"])
    if as_json:
        print(json.dumps(report))
    else:
        print(f'cross-project: {len(report["projects"])} project(s), '
              f'{total_groups} group(s)')
        for p in report["projects"]:
            extra = (f', {p["unreadable"]} unreadable'
                     if p["unreadable"] else "")
            print(f'  [{p["label"]}] {p["groups"]} groups{extra}')
        print(f'shared logic: {len(report["shared"])} bucket(s)')
        for b in report["shared"]:
            print(f'  [{b["hash"][:12]}...]')
            for g in b["groups"]:
                print(f'      {g["project"]}: {g["name"]}')
        print(f'divergent forks: {len(report["forks"])} group(s)')
        for f in report["forks"]:
            print(f'  {f["name"]}')
            for v in f["versions"]:
                who = ", ".join(f'{g["project"]}:{g["name"]}'
                                for g in v["groups"])
                print(f'      [{v["hash"][:12]}... x{v["count"]}] {who}')
    if total_groups == 0:
        print("error: no readable group JSONs in the given targets")
        return 2
    if strict and report["forks"]:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Semantic diff (--diff A B): a readable summary of what changed between two
# versions of the JSON (folder or file; standalone files or unified
# packages), ignoring layout-only properties.  Designed for reviewing a git
# commit without reading hundreds of raw JSON lines.
# ---------------------------------------------------------------------------

_DIFF_IO = ("NodeGroupInput", "NodeGroupOutput")
_DIFF_COSMETIC = frozenset({
    "location", "location_absolute", "width", "height", "select",
    "socket_idname", "vector_dimensions",
})


def _diff_cosmetic_set():
    """Layout-only node properties, taken from the add-on's own exclusion
    list when available (falls back to the built-in set)."""
    _load_hash_utils()
    constants = sys.modules.get("gnt_core.constants")
    if constants is None:
        return _DIFF_COSMETIC
    excluded = getattr(constants, "HASH_EXCLUDE_NODE_PROPS", _DIFF_COSMETIC)
    return frozenset(excluded) | {"vector_dimensions"}


def _add_diff_data(groups: dict, data, fallback_name: str) -> None:
    if not isinstance(data, dict):
        return
    node_groups = data.get("node_groups")
    if isinstance(node_groups, dict):
        for name, group in node_groups.items():
            if isinstance(group, dict):
                groups[name] = group
    elif "nodes" in data:
        groups[data.get("name", fallback_name)] = data


def _load_diff_groups(path: str) -> dict:
    """{group name: data} from a folder (walks it) or a single JSON file."""
    files = []
    if os.path.isdir(path):
        for root, _dirs, names in os.walk(path):
            for name in sorted(names):
                if name.endswith(".json"):
                    files.append(os.path.join(root, name))
    else:
        files = [path]
    groups = {}
    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        _add_diff_data(groups, data, os.path.basename(file_path)[:-5])
    return groups


def _git_diff_groups(repo: str, rev: str, subdir: str) -> dict:
    """{group name: data} from a git revision (JSON files under *subdir*)."""
    def git(*args) -> bytes:
        proc = subprocess.run(["git", "-C", repo] + list(args),
                              capture_output=True)
        if proc.returncode:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip()
                               or "git failed")
        return proc.stdout

    prefix = subdir.strip("/\\").replace("\\", "/")
    listing = git("ls-tree", "-r", "--name-only", rev,
                  (prefix + "/") if prefix else ".")
    groups = {}
    for name in listing.decode("utf-8", "replace").splitlines():
        if not name.endswith(".json"):
            continue
        try:
            data = json.loads(git("show", "%s:%s" % (rev, name))
                              .decode("utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        _add_diff_data(groups, data, os.path.basename(name)[:-5])
    return groups


def _resolve_diff_side(side: str, repo: str | None, subdir: str) -> dict:
    if os.path.exists(side):
        return _load_diff_groups(side)
    if repo:
        return _git_diff_groups(repo, side, subdir)
    raise RuntimeError("'%s' is not an existing path; pass --repo to read "
                       "git revisions" % side)


def _diff_node_type(node: dict) -> str:
    bl_type = node.get("type", "?")
    for prefix in ("GeometryNode", "FunctionNode", "ShaderNode", "Node"):
        if bl_type.startswith(prefix) and len(bl_type) > len(prefix):
            return bl_type[len(prefix):]
    return bl_type


def _diff_short(value) -> str:
    if isinstance(value, float):
        return "%.6g" % value
    if (isinstance(value, list) and value
            and all(isinstance(x, (int, float)) for x in value)):
        return "(" + ", ".join("%.4g" % x for x in value) + ")"
    if isinstance(value, dict) and "name" in value:
        return "'%s'" % value["name"]
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else text[:57] + "..."


def _diff_interface(a: dict, b: dict) -> list:
    def items(data):
        return {i["identifier"]: i for i in data.get("interface_items", [])
                if i.get("item_type") == "SOCKET" and i.get("identifier")}

    ia, ib = items(a), items(b)
    lines = []
    for key in sorted(set(ib) - set(ia), key=lambda k: ib[k].get("name", "")):
        item = ib[key]
        props = item.get("properties", {})
        extra = ""
        if "min_value" in props and "max_value" in props:
            extra = "  %s..%s" % (_diff_short(props["min_value"]),
                                  _diff_short(props["max_value"]))
        lines.append("+ %s '%s' %s, default %s%s" % (
            item.get("in_out", "").lower(), item.get("name", ""),
            item.get("socket_type", "").replace("NodeSocket", ""),
            _diff_short(props.get("default_value")), extra))
    for key in sorted(set(ia) - set(ib), key=lambda k: ia[k].get("name", "")):
        lines.append("- %s '%s'" % (ia[key].get("in_out", "").lower(),
                                    ia[key].get("name", "")))
    for key in sorted(set(ia) & set(ib), key=lambda k: ib[k].get("name", "")):
        x, y = ia[key], ib[key]
        changes = []
        if x.get("name") != y.get("name"):
            changes.append("name '%s' -> '%s'" % (x.get("name"),
                                                  y.get("name")))
        if x.get("socket_type") != y.get("socket_type"):
            changes.append("type %s -> %s" % (x.get("socket_type"),
                                              y.get("socket_type")))
        px, py = x.get("properties", {}), y.get("properties", {})
        for field in ("default_value", "min_value", "max_value",
                      "hide_in_modifier"):
            if px.get(field) != py.get(field):
                changes.append("%s %s -> %s" % (
                    field, _diff_short(px.get(field)),
                    _diff_short(py.get(field))))
        if changes:
            lines.append("~ '%s': %s" % (y.get("name", ""),
                                         "; ".join(changes)))
    return lines


def _diff_node_lines(nodes: list) -> list:
    by_prefix = {}
    for node in nodes:
        name = node.get("name", "")
        prefix = None
        for sep in ("_", "."):
            if sep in name:
                prefix = name.split(sep)[0] + sep
                break
        by_prefix.setdefault(prefix, []).append(node)
    lines = []
    for prefix, group in sorted(by_prefix.items(),
                                key=lambda kv: (kv[0] is None, kv[0] or "")):
        if (prefix and len(group) >= 3
                and not prefix.rstrip("_.").isdigit()):
            kinds = {}
            for node in group:
                kind = _diff_node_type(node)
                kinds[kind] = kinds.get(kind, 0) + 1
            summary = ", ".join(
                "%d %s" % (count, kind) if count > 1 else kind
                for kind, count in sorted(kinds.items()))
            lines.append("%s* (%d): %s" % (prefix, len(group), summary))
            lines.append("    " + ", ".join(
                sorted(n.get("name", "")[len(prefix):] for n in group)))
        else:
            for node in sorted(group, key=lambda n: n.get("name", "")):
                lines.append("%s [%s]" % (node.get("name", ""),
                                          _diff_node_type(node)))
    return lines


def _diff_group_nodes(a: dict, b: dict, cosmetic) -> tuple:
    na = {n.get("name"): n for n in a.get("nodes", [])}
    nb = {n.get("name"): n for n in b.get("nodes", [])}
    added = [nb[k] for k in nb if k not in na]
    removed = [na[k] for k in na if k not in nb]
    changed = []
    moved = 0
    for key in sorted(set(na) & set(nb)):
        x, y = na[key], nb[key]
        changes = []
        if x.get("type") != y.get("type"):
            changes.append("type %s -> %s" % (_diff_node_type(x),
                                              _diff_node_type(y)))
        if (x.get("label") or "") != (y.get("label") or ""):
            changes.append("label '%s' -> '%s'" % (x.get("label"),
                                                   y.get("label")))
        if (x.get("parent") or None) != (y.get("parent") or None):
            changes.append("frame %s -> %s" % (x.get("parent"),
                                               y.get("parent")))
        px, py = x.get("properties", {}), y.get("properties", {})
        for prop in sorted(set(px) | set(py)):
            if prop in cosmetic or prop.startswith("bl_"):
                continue
            if px.get(prop) != py.get(prop):
                changes.append("%s %s -> %s" % (
                    prop, _diff_short(px.get(prop)),
                    _diff_short(py.get(prop))))
        if y.get("type") not in _DIFF_IO:
            ex = {s.get("identifier") or s.get("name"): s
                  for s in x.get("inputs", [])}
            ey = {s.get("identifier") or s.get("name"): s
                  for s in y.get("inputs", [])}
            for sock in sorted(set(ex) & set(ey)):
                va = ex[sock].get("default_value")
                vb = ey[sock].get("default_value")
                if va is None or vb is None:
                    continue
                if va != vb:
                    changes.append("%s = %s -> %s" % (
                        ey[sock].get("name", sock),
                        _diff_short(va), _diff_short(vb)))
            for sock in sorted(set(ey) - set(ex)):
                changes.append("input added %s"
                               % ey[sock].get("name", sock))
            for sock in sorted(set(ex) - set(ey)):
                changes.append("input removed %s"
                               % ex[sock].get("name", sock))
        for field in ("zone_items", "menu_items_data", "zone_paired_node"):
            if x.get(field) != y.get(field):
                changes.append("%s changed" % field)
        if changes:
            changed.append((y, changes))
        elif x.get("location") != y.get("location"):
            moved += 1
    return added, removed, changed, moved


def _diff_group_links(a: dict, b: dict) -> tuple:
    labels = {n.get("name"): (n.get("label") or n.get("name"))
              for n in list(a.get("nodes", [])) + list(b.get("nodes", []))}
    old_names = {n.get("name") for n in a.get("nodes", [])}

    def key(link):
        return (link.get("from_node"), link.get("from_socket_id"),
                link.get("to_node"), link.get("to_socket_id"))

    socket_names = {}
    for link in list(a.get("links", [])) + list(b.get("links", [])):
        socket_names[(link.get("from_node"), link.get("from_socket_id"))] = \
            link.get("from_socket_name") or link.get("from_socket_id")
        socket_names[(link.get("to_node"), link.get("to_socket_id"))] = \
            link.get("to_socket_name") or link.get("to_socket_id")
    la = {key(link) for link in a.get("links", [])}
    lb = {key(link) for link in b.get("links", [])}
    added, removed = lb - la, la - lb
    by_dest = {}
    for k in removed:
        by_dest.setdefault((k[2], k[3]), []).append(k)
    relinked, used_added, used_removed = [], set(), set()
    for k in sorted(added):
        candidates = by_dest.get((k[2], k[3]))
        if candidates:
            old = candidates.pop(0)
            relinked.append("%s.%s: %s.%s -> %s.%s" % (
                labels.get(k[2], k[2]), socket_names.get((k[2], k[3]), k[3]),
                labels.get(old[0], old[0]),
                socket_names.get((old[0], old[1]), old[1]),
                labels.get(k[0], k[0]),
                socket_names.get((k[0], k[1]), k[1])))
            used_added.add(k)
            used_removed.add(old)

    def text(k):
        return "%s.%s -> %s.%s" % (
            labels.get(k[0], k[0]), socket_names.get((k[0], k[1]), k[1]),
            labels.get(k[2], k[2]), socket_names.get((k[2], k[3]), k[3]))

    rest = added - used_added
    internal = {k for k in rest
                if k[0] not in old_names and k[2] not in old_names}
    return (sorted(text(k) for k in rest - internal),
            sorted(text(k) for k in removed - used_removed),
            relinked, len(internal))


def run_diff(path_a: str, path_b: str, repo: str | None = None,
             subdir: str = "") -> int:
    """Print a semantic summary of the changes from *path_a* to *path_b*.

    A and B are folders or JSON files; when they do not exist as paths and
    *repo* is given, they are read as git revisions (JSONs under *subdir*).
    """
    cosmetic = _diff_cosmetic_set()
    try:
        groups_a = _resolve_diff_side(path_a, repo, subdir)
        groups_b = _resolve_diff_side(path_b, repo, subdir)
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 2
    if not groups_a and not groups_b:
        print("error: no readable group JSONs in the given paths")
        return 2
    label_a = path_a if os.path.exists(path_a) else "%s:%s" % (repo, path_a)
    label_b = path_b if os.path.exists(path_b) else "%s:%s" % (repo, path_b)
    print("Semantic diff: %s -> %s" % (label_a, label_b))
    lines = []
    for name in sorted(set(groups_a) | set(groups_b)):
        if name not in groups_a:
            lines.append("== %s: NEW GROUP (%d nodes)"
                         % (name, len(groups_b[name].get("nodes", []))))
            continue
        if name not in groups_b:
            lines.append("== %s: REMOVED GROUP" % name)
            continue
        a, b = groups_a[name], groups_b[name]
        interface = _diff_interface(a, b)
        added, removed, changed, moved = _diff_group_nodes(a, b, cosmetic)
        new_links, gone_links, relinked, internal = _diff_group_links(a, b)
        if not (interface or added or removed or changed or new_links
                or gone_links or relinked or internal or moved):
            continue
        lines.append("== %s  (%d -> %d nodes, %d -> %d links)" % (
            name, len(a.get("nodes", [])), len(b.get("nodes", [])),
            len(a.get("links", [])), len(b.get("links", []))))
        if interface:
            lines.append("  Interface:")
            lines += ["    " + x for x in interface]
        if added:
            lines.append("  Nodes added (%d):" % len(added))
            lines += ["    " + x for x in _diff_node_lines(added)]
        if removed:
            lines.append("  Nodes removed (%d):" % len(removed))
            lines += ["    " + x for x in _diff_node_lines(removed)]
        if changed:
            lines.append("  Nodes changed (%d):" % len(changed))
            for node, changes in changed:
                lines.append("    %s: %s" % (node.get("name", ""),
                                             "; ".join(changes)))
        if relinked:
            lines.append("  Links re-linked (%d):" % len(relinked))
            lines += ["    " + x for x in relinked]
        if new_links:
            lines.append("  Links added (%d):" % len(new_links))
            lines += ["    " + x for x in new_links]
        if internal:
            lines.append("  (+%d internal links between new nodes)"
                         % internal)
        if gone_links:
            lines.append("  Links removed (%d):" % len(gone_links))
            lines += ["    " + x for x in gone_links]
        if moved:
            lines.append("  (%d nodes only moved)" % moved)
    if not lines:
        lines.append("No semantic changes.")
    for line in lines:
        print(line)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="gnt_check",
        description="Headless JSON-side check and audits for GNToolkit "
                    "(no Blender needed).")
    ap.add_argument("target", nargs="?",
                    help="folder of per-group JSONs or a package file")
    ap.add_argument("--baseline",
                    help=".gntsync sidecar or flat {name: hash} JSON (a "
                         "sidecar written by another hash-algorithm version "
                         "exits 3)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 if any file cannot be parsed")
    ap.add_argument("--selftest", action="store_true",
                    help="verify the pure-Python hasher and exit")
    ap.add_argument("--impact", metavar="GROUP",
                    help="report direct/transitive dependents of GROUP "
                         "(requires --baseline)")
    ap.add_argument("--modifiers", metavar="DIR",
                    help="folder of Modifiers JSON exports; --impact also "
                         "lists the objects/modifiers using the group "
                         "(default: auto-discovered next to the sidecar)")
    ap.add_argument("--duplicates", action="store_true",
                    help="report groups with identical logic (content hash)")
    ap.add_argument("--health", action="store_true",
                    help="consolidated JSON-side health report")
    ap.add_argument("--cross", nargs="+", metavar="PATH",
                    help="compare two or more projects (folders of JSONs "
                         "or JSON files) by content hash: shared logic and "
                         "divergent forks; with --strict, divergent forks "
                         "exit 1")
    ap.add_argument("--diff", nargs=2, metavar=("A", "B"),
                    help="readable summary of what changed between two JSON "
                         "versions (folders, files, or git revisions with "
                         "--repo; ignores layout-only properties)")
    ap.add_argument("--repo",
                    help="git repository for --diff when A/B are revisions")
    ap.add_argument("--subdir",
                    help="folder inside the repo with the JSONs (--diff "
                         "with git revisions)")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.baseline:
        problem = _baseline_version_problem(args.baseline)
        if problem:
            print(f"error: {problem}")
            return 3

    if args.diff:
        return run_diff(args.diff[0], args.diff[1], args.repo,
                        args.subdir or "")

    if args.impact:
        return run_impact(args.baseline, args.impact, args.json,
                          args.modifiers or "")
    if args.duplicates:
        if not args.target:
            ap.error("--duplicates requires a target folder")
        return run_duplicates(args.target, args.json)
    if args.health:
        return run_health(args.target, args.baseline, args.json, args.strict)
    if args.cross:
        if len(args.cross) < 2:
            ap.error("--cross needs at least two targets")
        return run_cross(args.cross, args.json, args.strict)

    if not args.target:
        ap.error("a target folder/file is required (or use --selftest)")

    report, has_changes, hard_fail = check(args.target, args.baseline, args.strict)

    if args.json:
        print(json.dumps(report))
    else:
        for n in report["clean"]:
            print(f"[OK]       {n}")
        for c in report["changed"]:
            print(f"[CHANGED]  {c['name']}  ({c['file']})")
        for m in report["missing"]:
            print(f"[MISSING]  {m['name']}  ({m['file']})")
        for u in report["unparseable"]:
            print(f"[UNREADABLE] {u}")
        print(f"--- {report['groups']} groups, {len(report['clean'])} clean, "
              f"{len(report['changed'])} changed, {len(report['missing'])} missing, "
              f"{len(report['unparseable'])} unreadable in {report['dt_seconds']}s")

    if hard_fail:
        return 2
    if has_changes:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())