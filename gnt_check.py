# -*- coding: utf-8 -*-
"""
gnt_check — headless JSON-side status check for GNToolkit (pure Python, NO bpy).

Given a folder of per-group JSONs (folder workflow) or a package file, and an
optional baseline, this reports which groups changed on the JSON side without
opening Blender.  Use it in git hooks, CI or a release gate.

Usage:
    python gnt_check.py <folder_or_package> [--baseline <file>] [--json] [--strict]

Audit modes (same pure-Python core, no Blender):
    python gnt_check.py --impact "<group>" --baseline project.blend.gntsync
    python gnt_check.py <folder> --duplicates
    python gnt_check.py <folder> [--baseline project.blend.gntsync] --health

Baseline sources:
    * a .gntsync sidecar next to a .blend (paths relative to the sidecar dir)
    * a flat JSON mapping group name -> canonical hash

Exit codes:
    0  all groups clean (or, without a baseline, everything parsed)
    1  changes or missing groups detected
    2  usage / IO / parse errors (with --strict, unparseable files also exit 2)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
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
    print("selftest: " + ("OK — canonical hashing works without bpy"
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


def run_impact(baseline_path: str, target_name: str, as_json: bool) -> int:
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
        print(f'  unreadable: {len(report["unreadable"])} · '
              f'conflicts: {len(report["conflicts"])} · '
              f'duplicate buckets: {len(report["duplicates"])} · '
              f'missing files: {len(report["missing_files"])} · '
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="gnt_check",
        description="Headless JSON-side check and audits for GNToolkit "
                    "(no Blender needed).")
    ap.add_argument("target", nargs="?",
                    help="folder of per-group JSONs or a package file")
    ap.add_argument("--baseline", help=".gntsync sidecar or flat {name: hash} JSON")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 if any file cannot be parsed")
    ap.add_argument("--selftest", action="store_true",
                    help="verify the pure-Python hasher and exit")
    ap.add_argument("--impact", metavar="GROUP",
                    help="report direct/transitive dependents of GROUP "
                         "(requires --baseline)")
    ap.add_argument("--duplicates", action="store_true",
                    help="report groups with identical logic (content hash)")
    ap.add_argument("--health", action="store_true",
                    help="consolidated JSON-side health report")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.impact:
        return run_impact(args.baseline, args.impact, args.json)
    if args.duplicates:
        if not args.target:
            ap.error("--duplicates requires a target folder")
        return run_duplicates(args.target, args.json)
    if args.health:
        return run_health(args.target, args.baseline, args.json, args.strict)

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