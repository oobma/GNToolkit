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

Render modes (Graphviz diagrams — repo/documentation tool, NOT canonical):
    python gnt_check.py --render deps <folder> [--focus GROUP] [--hops N]
                        [--new <other_folder>] [--out DIR] [--png]
    python gnt_check.py --render stats <folder>
    python gnt_check.py --render group <json_file> [--layout dot|real]
    python gnt_check.py --render diff A B [--repo R --subdir D] [--layout dot|real]

    SVG by default (PNG for detail views with --png).  Needs the Graphviz
    ``dot``/``neato`` binary on PATH (or a portable copy under
    %TEMP%\\opencode\\graphviz\\*\\bin), except with ``--dot-only``, which
    just writes the ``.dot`` source.  Placement is decided by Graphviz and
    may vary between its versions: a different-looking render does NOT mean
    the project changed — the hashes remain the source of truth.

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
import glob
import importlib.util
import json
import os
import re
import shutil
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
# packages), ignoring layout-only properties.  The comparison itself lives
# in semantic_diff.py (shared with the add-on's commit-message suggestion).
# ---------------------------------------------------------------------------

def _load_semantic_diff():
    """Load semantic_diff.py (pure) after the synthetic package exists."""
    if "gnt_core.hash_utils" not in sys.modules:
        _load_hash_utils()
    if "gnt_core.semantic_diff" not in sys.modules:
        _load_pure("gnt_core.semantic_diff",
                   os.path.join(_ROOT, "semantic_diff.py"))
    return sys.modules["gnt_core.semantic_diff"]


def _add_diff_data(groups: dict, data, fallback_name: str) -> None:
    _load_semantic_diff().add_group_data(groups, data, fallback_name)


def _load_diff_groups(path: str) -> dict:
    return _load_semantic_diff().load_groups(path)


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


def run_diff(path_a: str, path_b: str, repo: str | None = None,
             subdir: str = "") -> int:
    """Print a semantic summary of the changes from *path_a* to *path_b*.

    A and B are folders or JSON files; when they do not exist as paths and
    *repo* is given, they are read as git revisions (JSONs under *subdir*).
    """
    semantic_diff = _load_semantic_diff()
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
        result = semantic_diff.compare_groups(groups_a[name], groups_b[name])
        if result is None:
            continue
        lines += semantic_diff.format_group_report(name, result)
    if not lines:
        lines.append("No semantic changes.")
    for line in lines:
        print(line)
    return 0


# ---------------------------------------------------------------------------
# Render modes (Graphviz) — repo/documentation tool, not canonical
# ---------------------------------------------------------------------------

_RENDER_STYLES = {
    "added": ('fillcolor="#dcf5dc", color="#2e7d32"', "#2e7d32", "solid"),
    "removed": ('fillcolor="#f9dcdc", color="#b3261e"', "#b3261e", "dashed"),
    "changed": ('fillcolor="#fff1c9", color="#a9791c"', "#a9791c", "solid"),
    "same": ('fillcolor="white", color="#9e9e9e"', "#cfd4da", "solid"),
}

_RENDER_CHIP = {
    "added": ("#dcf5dc", "#2e7d32"),
    "removed": ("#f9dcdc", "#b3261e"),
    "changed": ("#fff1c9", "#a9791c"),
    "same": ("#ffffff", "#9e9e9e"),
}

_RENDER_NOTE = ("note: rendered graphs are not canonical — Graphviz decides "
                "the placement and it may differ between versions; a "
                "different-looking render does not mean the project changed "
                "(the hashes remain the source of truth).")


def _render_find_bin(name: str):
    exe = shutil.which(name)
    if exe:
        return exe
    # Standard Windows install locations (a fresh install may not be on the
    # PATH of already-running processes).
    for base in (os.environ.get("ProgramFiles") or r"C:\Program Files",
                 os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA") or "",
                              "Programs")):
        if not base:
            continue
        candidate = os.path.join(base, "Graphviz", "bin", name + ".exe")
        if os.path.isfile(candidate):
            return candidate
    pattern = os.path.join(os.environ.get("TEMP", ""), "opencode",
                           "graphviz", "*", "bin", name + ".exe")
    candidates = glob.glob(pattern)
    return candidates[0] if candidates else None


def _render_missing_bin(engine: str) -> int:
    print(f"error: Graphviz '{engine}' not found. Install Graphviz "
          "(https://graphviz.org/download/) and make sure it is on PATH, or "
          "pass --dot-only to write just the .dot source. (Rendering is a "
          "repo/documentation tool; the JSON check does not need it.)")
    return 2


def _render_q(text) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _render_lbl(*parts) -> str:
    return "\\n".join(_render_q(p) for p in parts)


def _render_html(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _render_chip_label(title_lines, chips) -> str:
    span = max(1, len(chips))
    rows = "".join(
        f'<TR><TD COLSPAN="{span}" BORDER="0">{_render_html(line)}</TD></TR>'
        for line in title_lines)
    cells = "".join(
        f'<TD BGCOLOR="{fill}" COLOR="{border}" BORDER="1" CELLPADDING="6">'
        f'<FONT POINT-SIZE="11">{_render_html(text)}</FONT></TD>'
        for text, fill, border in chips)
    return (f'<<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="8">{rows}'
            f"<TR>{cells}</TR></TABLE>>")


def _render_slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:80] or "graph"


def _render_default_out(target: str) -> str:
    return os.path.join(os.path.abspath(os.getcwd()), "gnt_render")


def _render_write_and_run(dot_text: str, out_base: str, engine: str,
                          extra=None, png: bool = False,
                          dot_only: bool = False) -> list:
    dot_path = out_base + ".dot"
    os.makedirs(os.path.dirname(dot_path), exist_ok=True)
    with open(dot_path, "w", encoding="utf-8") as f:
        f.write(dot_text)
    if dot_only:
        return [dot_path]
    args = [engine] + (extra or [])
    svg = out_base + ".svg"
    subprocess.run(args + ["-Tsvg", dot_path, "-o", svg], check=True)
    outs = [dot_path, svg]
    if png:
        png_path = out_base + ".png"
        subprocess.run(args + ["-Tpng", dot_path, "-o", png_path], check=True)
        outs.append(png_path)
    return outs


def _render_engine(layout: str, dot_only: bool):
    """(engine path or None, error code) — None engine only with --dot-only."""
    if dot_only:
        return "dot", 0
    engine = _render_find_bin("dot" if layout == "dot" else "neato")
    if not engine:
        return None, _render_missing_bin("dot" if layout == "dot" else "neato")
    return engine, 0


def _is_group_node(node_type) -> bool:
    """True for group nodes of any tree kind (Geometry/Shader/...)."""
    return bool(node_type) and str(node_type).endswith("NodeGroup")


def _render_deps_of(data: dict) -> list:
    refs = []
    for node in data.get("nodes", []):
        if _is_group_node(node.get("type")):
            ref = node.get("node_tree_reference")
            if ref:
                refs.append(ref)
    return refs


def _render_stats(folder: str) -> int:
    groups = _load_diff_groups(folder)
    rows = []
    for name, data in groups.items():
        rows.append((len(data.get("nodes", [])), len(data.get("links", [])),
                     len(_render_deps_of(data)), name))
    rows.sort(reverse=True)
    print(f"{len(groups)} groups in {folder}")
    for nodes, links, refs, name in rows[:15]:
        print(f"  {nodes:4d} nodes  {links:4d} links  {refs:2d} group-refs  "
              f"{name}")
    if rows:
        med = rows[len(rows) // 2]
        print(f"median: {med[0]} nodes — {med[3]}")
    counts = {}
    for _name, data in groups.items():
        for ref in set(_render_deps_of(data)):
            counts[ref] = counts.get(ref, 0) + 1
    for ref, n in sorted(counts.items(), key=lambda kv: -kv[1])[:5]:
        print(f"  most referenced: {n}x  {ref}")
    return 0


def _render_side_groups(side: str, repo, subdir: str) -> dict:
    return _resolve_diff_side(side, repo, subdir)


def _render_deps(folder: str, focus, hops: int, out_dir: str, png: bool,
                 new_side, dot_only: bool, repo, subdir: str) -> int:
    t0 = time.time()
    groups = _render_side_groups(folder, repo, subdir)
    new_groups = (_render_side_groups(new_side, repo, subdir)
                  if new_side else {})
    names = set(groups) | set(new_groups)

    status = None
    if new_side:
        status = {}
        for name in names:
            if name not in groups:
                status[name] = "added"
            elif name not in new_groups:
                status[name] = "removed"
            elif _render_tree_same(groups[name], new_groups[name]):
                status[name] = "same"
            else:
                status[name] = "changed"

    edges = set()
    for data in list(groups.values()) + list(new_groups.values()):
        for ref in set(_render_deps_of(data)):
            edges.add((data.get("name"), ref))

    nodes = set(names)
    if focus:
        fwd, rev = {}, {}
        for a, b in edges:
            fwd.setdefault(a, set()).add(b)
            rev.setdefault(b, set()).add(a)
        keep = {focus} if focus in names else set()
        frontier = set(keep)
        for _ in range(hops):
            nxt = set()
            for n in frontier:
                nxt |= fwd.get(n, set()) | rev.get(n, set())
            nxt -= keep
            keep |= nxt
            frontier = nxt
        nodes = keep
        edges = {(a, b) for a, b in edges if a in nodes and b in nodes}
        print(f"focus '{focus}' hops {hops}: {len(nodes)} nodes kept")

    ext = sorted({b for _a, b in edges if b not in names})
    lines = [
        "digraph gnt_deps {",
        "  rankdir=LR;",
    ]
    if status is not None:
        counts = {k: sum(1 for v in status.values() if v == k)
                  for k in _RENDER_STYLES}
        label = _render_chip_label(
            [f"project diff: {os.path.basename(os.path.normpath(folder))} -> "
             f"{os.path.basename(os.path.normpath(new_side))}"],
            [(f"added {counts['added']}", *_RENDER_CHIP["added"]),
             (f"removed {counts['removed']}", *_RENDER_CHIP["removed"]),
             (f"changed {counts['changed']}", *_RENDER_CHIP["changed"]),
             (f"same {counts['same']}", *_RENDER_CHIP["same"])])
        lines.append(f'  graph [label={label}, labelloc=t, '
                     'fontname="Segoe UI", fontsize=12];')
    lines += [
        '  graph [overlap=false, splines=true, bgcolor="white", pad=0.3];',
        '  node [shape=box, style="rounded,filled", fillcolor="#eef3fb", '
        'color="#6b86b1", fontname="Segoe UI", fontsize=10];',
        '  edge [color="#9aa7b8", arrowsize=0.7];',
    ]
    for n in sorted(nodes):
        st = status.get(n) if status is not None else None
        if st is None:
            lines.append(f'  "{_render_q(n)}" [label="{_render_q(n)}"];')
        else:
            attrs, _ec, _es = _RENDER_STYLES[st]
            lines.append(f'  "{_render_q(n)}" [label="{_render_q(n)}", '
                         f"{attrs}];")
    for b in ext:
        lines.append(f'  "{_render_q(b)}" [label="{_render_q(b)}", '
                     'style="rounded,dashed", fillcolor="#f6f6f6", '
                     'color="#b9b9b9", fontcolor="#777777"];')
    for a, b in sorted(edges):
        attrs = ('style=dashed, color="#c9c9c9"'
                 if b not in names else "")
        lines.append(f'  "{_render_q(a)}" -> "{_render_q(b)}" [{attrs}];')
    lines.append("}")
    lines.append("")

    engine, code = _render_engine("dot", dot_only)
    if engine is None:
        return code
    prefix = "depsdiff_" if status is not None else "deps_"
    base = os.path.join(out_dir, prefix + (_render_slug(focus) if focus
                                           else "full"))
    outs = _render_write_and_run("\n".join(lines), base, engine, png=png,
                                 dot_only=dot_only)
    dt = round(time.time() - t0, 2)
    if status is not None:
        counts = {k: sum(1 for v in status.values() if v == k)
                  for k in _RENDER_STYLES}
        print(f"status: same {counts['same']} | changed {counts['changed']} | "
              f"added {counts['added']} | removed {counts['removed']}")
    print(f"{len(nodes)} groups, {len(edges)} edges in {dt}s")
    for path in outs:
        print(f"  {path}  ({os.path.getsize(path)} bytes)")
    return 0


def _render_node_attrs(node: dict) -> str:
    t = node.get("type", "")
    if _is_group_node(t):
        return 'fillcolor="#dbe8ff", color="#4a6fb5"'
    if t == "NodeGroupInput":
        return 'fillcolor="#ddf3dd", color="#4f9b4f"'
    if t == "NodeGroupOutput":
        return 'fillcolor="#ededed", color="#888888"'
    if t.startswith(("GeometryNodeRepeat", "GeometryNodeSimulation",
                     "GeometryNodeForeach")):
        return 'fillcolor="#ffe9cc", color="#d78a2a"'
    return 'fillcolor="white", color="#666666"'


def _render_bypass_reroutes(nodes: list, links: list):
    reroute_names = {n.get("name") for n in nodes
                     if n.get("type") == "NodeReroute"}
    if not reroute_names:
        return nodes, links
    kept = [n for n in nodes if n.get("name") not in reroute_names]
    incoming = {}
    for link in links:
        incoming.setdefault(link.get("to_node"), link)
    out = []
    for link in links:
        if (link.get("from_node") in reroute_names
                or link.get("to_node") in reroute_names):
            continue
        out.append(link)
    for link in links:
        if (link.get("from_node") not in reroute_names
                or link.get("to_node") in reroute_names):
            continue
        source = link.get("from_node")
        upstream = None
        guard = set()
        while source in reroute_names and source not in guard:
            guard.add(source)
            upstream = incoming.get(source)
            if upstream is None:
                source = None
                break
            source = upstream.get("from_node")
        if source is None or source in reroute_names or upstream is None:
            continue
        out.append({**link, "from_node": source,
                    "from_socket_name": upstream.get("from_socket_name")})
    return kept, out


def _render_group_side(data: dict, name: str, layout: str, reroutes: str,
                       lines: list, ids: dict) -> None:
    nodes = data.get("nodes", [])
    links = data.get("links", [])
    if reroutes == "hide":
        nodes, links = _render_bypass_reroutes(nodes, links)
    for i, node in enumerate(nodes):
        t = node.get("type", "")
        if reroutes == "dot" and t == "NodeReroute":
            attrs = 'shape=point, width=0.06, label=""'
            if layout == "real":
                x, y = node.get("location", [0, 0])
                attrs += f', pos="{x},{-y}!"'
            lines.append(f'  n{i} [{attrs}];')
            ids[node.get("name")] = f"n{i}"
            continue
        parts = [node.get("name", "")]
        if _is_group_node(t) and node.get("node_tree_reference"):
            parts.append("(" + node["node_tree_reference"] + ")")
        elif t and not _is_group_node(t):
            parts.append("[" + t.replace("GeometryNode", "")
                         .replace("NodeGroup", "") + "]")
        attrs = _render_node_attrs(node)
        if layout == "real":
            x, y = node.get("location", [0, 0])
            attrs += f', pos="{x},{-y}!"'
        lines.append(f'  n{i} [label="{_render_lbl(*parts)}", {attrs}];')
        ids[node.get("name")] = f"n{i}"
    for link in links:
        a = ids.get(link.get("from_node"))
        b = ids.get(link.get("to_node"))
        if a and b:
            tip = _render_q(f'{link.get("from_socket_name", "")} -> '
                            f'{link.get("to_socket_name", "")}')
            lines.append(f'  {a} -> {b} [tooltip="{tip}"];')


def _render_group(json_path: str, layout: str, out_dir: str, png: bool,
                  reroutes: str, dot_only: bool, repo, subdir: str) -> int:
    t0 = time.time()
    groups = _render_side_groups(json_path, repo, subdir)
    if len(groups) == 1:
        data = next(iter(groups.values()))
    else:
        data = None
        for gname, gdata in groups.items():
            if gdata.get("name") == os.path.basename(json_path)[:-5]:
                data = gdata
                break
        if data is None:
            print(f"error: '{json_path}' does not contain a single group; "
                  "use --render deps for folders/packages")
            return 2
    name = data.get("name") or os.path.basename(json_path)
    reroute_count = sum(1 for n in data.get("nodes", [])
                        if n.get("type") == "NodeReroute")

    ids = {}
    lines = [
        f'digraph "{_render_q(name)}" {{',
        "  rankdir=LR;",
        '  graph [bgcolor="white", pad=0.3];',
        '  node [shape=box, style="rounded,filled", fontname="Segoe UI", '
        'fontsize=10];',
        '  edge [color="#7a8aa0", arrowsize=0.7];',
    ]
    _render_group_side(data, name, layout, reroutes, lines, ids)
    lines.append("}")

    engine, code = _render_engine(layout, dot_only)
    if engine is None:
        return code
    extra = ["-n2"] if layout == "real" else None
    suffix = "" if reroutes == "box" else "_" + reroutes
    base = os.path.join(out_dir,
                        "group_" + _render_slug(name) + "_" + layout + suffix)
    outs = _render_write_and_run("\n".join(lines), base, engine, extra=extra,
                                 png=png, dot_only=dot_only)
    dt = round(time.time() - t0, 2)
    print(f'"{name}": {len(data.get("nodes", []))} nodes, '
          f'{len(data.get("links", []))} links, layout={layout}, '
          f'reroutes={reroutes} ({reroute_count} found) in {dt}s')
    for path in outs:
        print(f"  {path}  ({os.path.getsize(path)} bytes)")
    return 0


def _render_node_map(data: dict) -> dict:
    return {n.get("name"): n for n in data.get("nodes", []) if n.get("name")}


def _render_semantic(node: dict, connected: set) -> str:
    hu = _load_hash_utils()
    copy = dict(node)
    copy["inputs"] = [
        (dict(inp, default_value=None) if inp.get("identifier") in connected
         else inp)
        for inp in node.get("inputs", [])
    ]
    core = hu.canonicalize_node_tree_data({"nodes": [copy], "links": []})
    return json.dumps(core, sort_keys=True, separators=(",", ":"))


def _render_link_key(link: dict):
    return (link.get("from_node"), link.get("from_socket_name"),
            link.get("to_node"), link.get("to_socket_name"))


def _render_tree_same(a: dict, b: dict) -> bool:
    def prep(data):
        conn = {}
        for link in data.get("links", []):
            conn.setdefault(link.get("to_node"), set()).add(
                link.get("to_socket_id"))
        sigs = {}
        for node in data.get("nodes", []):
            if node.get("name"):
                sigs[node["name"]] = _render_semantic(
                    node, conn.get(node["name"], set()))
        links = sorted(tuple(link.get(k) for k in (
            "from_node", "from_socket_name", "to_node", "to_socket_name"))
            for link in data.get("links", []))
        return sigs, links
    return prep(a) == prep(b)


def _render_diff(old_path: str, new_path: str, layout: str, out_dir: str,
                 png: bool, reroutes: str, dot_only: bool, repo,
                 subdir: str) -> int:
    t0 = time.time()
    old_groups = _render_side_groups(old_path, repo, subdir)
    new_groups = _render_side_groups(new_path, repo, subdir)

    def single(groups, path):
        if len(groups) == 1:
            return next(iter(groups.values()))
        base = os.path.basename(path)[:-5]
        return groups.get(base)

    old = single(old_groups, old_path)
    new = single(new_groups, new_path)
    if old is None or new is None:
        print("error: --render diff needs paths that resolve to a single "
              "group each (a JSON file or a one-group folder)")
        return 2

    name = new.get("name") or old.get("name") or os.path.basename(new_path)
    old_nodes, old_links = old.get("nodes", []), old.get("links", [])
    new_nodes, new_links = new.get("nodes", []), new.get("links", [])
    reroute_count = sum(1 for n in new_nodes if n.get("type") == "NodeReroute")
    if reroutes == "hide":
        old_nodes, old_links = _render_bypass_reroutes(old_nodes, old_links)
        new_nodes, new_links = _render_bypass_reroutes(new_nodes, new_links)
    old = {"nodes": old_nodes, "links": old_links}
    new = {"nodes": new_nodes, "links": new_links}

    om, nm = _render_node_map(old), _render_node_map(new)

    def conn_map(data):
        m = {}
        for link in data.get("links", []):
            m.setdefault(link.get("to_node"), set()).add(
                link.get("to_socket_id"))
        return m

    conn_old, conn_new = conn_map(old), conn_map(new)
    status = {}
    for gname in set(om) | set(nm):
        if gname not in om:
            status[gname] = "added"
        elif gname not in nm:
            status[gname] = "removed"
        elif (_render_semantic(om[gname], conn_old.get(gname, set()))
              != _render_semantic(nm[gname], conn_new.get(gname, set()))):
            status[gname] = "changed"
        else:
            status[gname] = "same"
    counts = {k: sum(1 for v in status.values() if v == k)
              for k in _RENDER_STYLES}

    links_old = {_render_link_key(l) for l in old_links}
    links_new = {_render_link_key(l) for l in new_links}
    l_added = links_new - links_old
    l_removed = links_old - links_new

    label = _render_chip_label(
        [f"diff: {name}"],
        [(f"added {counts['added']}", *_RENDER_CHIP["added"]),
         (f"removed {counts['removed']}", *_RENDER_CHIP["removed"]),
         (f"changed {counts['changed']}", *_RENDER_CHIP["changed"]),
         (f"same {counts['same']}", *_RENDER_CHIP["same"]),
         (f"links added {len(l_added)}", *_RENDER_CHIP["added"]),
         (f"links removed {len(l_removed)}", *_RENDER_CHIP["removed"])])

    lines = [
        f'digraph "{_render_q(name)}" {{',
        "  rankdir=LR;",
        f'  graph [label={label}, labelloc=t, fontname="Segoe UI", '
        'fontsize=12];',
        '  graph [bgcolor="white", pad=0.3];',
        '  node [shape=box, style="rounded,filled", fontname="Segoe UI", '
        'fontsize=10];',
    ]
    ids = {}
    for i, node_name in enumerate(sorted(status)):
        node = nm.get(node_name) or om[node_name]
        t = node.get("type", "")
        if reroutes == "dot" and t == "NodeReroute":
            attrs = 'shape=point, width=0.06, label=""'
            if layout == "real":
                loc = node.get("location") or [0, 0]
                attrs += f', pos="{loc[0]},{-loc[1]}!"'
            lines.append(f'  n{i} [{attrs}];')
            ids[node_name] = f"n{i}"
            continue
        parts = [node_name]
        if _is_group_node(t) and node.get("node_tree_reference"):
            parts.append("(" + node["node_tree_reference"] + ")")
        elif t and not _is_group_node(t):
            parts.append("[" + t.replace("GeometryNode", "")
                         .replace("NodeGroup", "") + "]")
        attrs, _ec, _es = _RENDER_STYLES[status[node_name]]
        if layout == "real":
            loc = node.get("location") or [0, 0]
            attrs += f', pos="{loc[0]},{-loc[1]}!"'
        lines.append(f'  n{i} [label="{_render_lbl(*parts)}", {attrs}];')
        ids[node_name] = f"n{i}"
    for key in sorted(links_old | links_new):
        a, b = ids.get(key[0]), ids.get(key[2])
        if not a or not b:
            continue
        if key in l_added:
            st = "added"
        elif key in l_removed:
            st = "removed"
        else:
            st = "same"
        _fill, ecolor, estyle = _RENDER_STYLES[st]
        tip = _render_q(f"{key[1]} -> {key[3]}")
        lines.append(f'  {a} -> {b} [color="{ecolor}", style={estyle}, '
                     f'arrowsize=0.7, tooltip="{tip}"];')
    lines.append("}")

    engine, code = _render_engine(layout, dot_only)
    if engine is None:
        return code
    extra = ["-n2"] if layout == "real" else None
    suffix = "" if reroutes == "box" else "_" + reroutes
    base = os.path.join(out_dir,
                        "diff_" + _render_slug(name) + "_" + layout + suffix)
    outs = _render_write_and_run("\n".join(lines), base, engine, extra=extra,
                                 png=png, dot_only=dot_only)
    dt = round(time.time() - t0, 2)
    print(f'diff "{name}": nodes +{counts["added"]} -{counts["removed"]} '
          f'~{counts["changed"]} ={counts["same"]} · links +{len(l_added)} '
          f'-{len(l_removed)}, reroutes={reroutes} ({reroute_count} found) '
          f'in {dt}s')
    for path in outs:
        print(f"  {path}  ({os.path.getsize(path)} bytes)")
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
    ap.add_argument("--render", choices=["stats", "deps", "group", "diff"],
                    help="render a Graphviz diagram: whole project and/or "
                         "neighborhood (deps), single group, or semantic "
                         "diff (repo/documentation tool; not canonical; "
                         "needs Graphviz unless --dot-only)")
    ap.add_argument("--focus", metavar="GROUP",
                    help="--render deps: keep the neighborhood of GROUP")
    ap.add_argument("--hops", type=int, default=2,
                    help="--render deps: neighborhood depth (default 2)")
    ap.add_argument("--new", metavar="PATH",
                    help="--render deps/diff: the other side to compare")
    ap.add_argument("--out", metavar="DIR",
                    help="--render: output folder (default ./gnt_render)")
    ap.add_argument("--png", action="store_true",
                    help="--render: also write a PNG next to the SVG")
    ap.add_argument("--layout", choices=["dot", "real"], default="dot",
                    help="--render group/diff: Graphviz layout (real = use "
                         "the stored node positions via neato)")
    ap.add_argument("--reroutes", choices=["box", "dot", "hide"],
                    default="dot",
                    help="--render group/diff: how to treat reroute nodes")
    ap.add_argument("--dot-only", action="store_true",
                    help="--render: write the .dot source and skip Graphviz")
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

    if args.render:
        try:
            if args.render == "stats":
                if not args.target:
                    ap.error("--render stats requires a target folder")
                return _render_stats(args.target)
            out_dir = args.out or _render_default_out(
                args.target or os.getcwd())
            print(_RENDER_NOTE)
            if args.render == "deps":
                if not args.target:
                    ap.error("--render deps requires a target folder")
                return _render_deps(args.target, args.focus, args.hops,
                                    out_dir, args.png, args.new,
                                    args.dot_only, args.repo or "",
                                    args.subdir or "")
            if args.render == "group":
                if not args.target:
                    ap.error("--render group requires a target JSON file")
                return _render_group(args.target, args.layout, out_dir,
                                     args.png, args.reroutes, args.dot_only,
                                     args.repo or "", args.subdir or "")
            if not args.target or not args.new:
                ap.error("--render diff requires A (target) and --new B")
            return _render_diff(args.target, args.new, args.layout, out_dir,
                                args.png, args.reroutes, args.dot_only,
                                args.repo or "", args.subdir or "")
        except RuntimeError as exc:
            print(f"error: {exc}")
            return 2
        except subprocess.CalledProcessError as exc:
            print(f"error: Graphviz failed ({exc})")
            return 2

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