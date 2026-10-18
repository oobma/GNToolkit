# -*- coding: utf-8 -*-
"""gnt_check refuses to compare against a baseline from another release.

A .gntsync sidecar records the hash-algorithm version that produced its
hashes.  Comparing it with a checker from a different release would report
every group as changed, so the checker exits 3 with an actionable message.

Usage:
    python tests\\test_gnt_check_version_guard.py
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _ROOT)

import gnt_check

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"[PASS] {name} {detail}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name} {detail}")


def run_main(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = gnt_check.main(argv)
    return code, out.getvalue()


print("=== gnt_check version guard (pure Python) ===")

hu = gnt_check._load_hash_utils()
group = {"name": "GG", "nodes": [], "links": []}
good_hash = hu.canonical_hash_from_json_data(group)
current = gnt_check._current_hash_version()

tmp = tempfile.mkdtemp(prefix="gnt_guard_")
json_path = os.path.join(tmp, "GG.json")
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(group, f)


def write_sidecar(path, hash_version, with_version=True, h=good_hash):
    side = {
        "version": "0.3.1",
        "tracked_groups": {
            "uid-1": {"blend_name": "GG", "json_path": "GG.json",
                      "last_json_hash": h, "last_blend_hash": h},
        },
    }
    if with_version:
        side["hash_version"] = hash_version
    with open(path, "w", encoding="utf-8") as f:
        json.dump(side, f)


side_ok = os.path.join(tmp, "ok.gntsync")
write_sidecar(side_ok, current)
code, out = run_main([tmp, "--baseline", side_ok])
check("1: matching version compares normally", code == 0,
      f"exit={code} out={out.strip()!r}")

side_bad = os.path.join(tmp, "old.gntsync")
write_sidecar(side_bad, 1)
code, out = run_main([tmp, "--baseline", side_bad])
check("2: mismatched version exits 3", code == 3, f"exit={code}")
check("3: the message is actionable",
      "same release" in out and "re-stamp" in out, out.strip())

side_ancient = os.path.join(tmp, "ancient.gntsync")
write_sidecar(side_ancient, 0, with_version=False)
code, out = run_main([tmp, "--baseline", side_ancient])
check("4: version-less sidecar exits 3", code == 3, f"exit={code}")

flat_dir = tempfile.mkdtemp(prefix="gnt_guard_base_")
flat = os.path.join(flat_dir, "flat.json")
with open(flat, "w", encoding="utf-8") as f:
    json.dump({"GG": good_hash}, f)
code, out = run_main([tmp, "--baseline", flat])
check("5: flat baselines are not version-guarded", code == 0,
      f"exit={code} out={out.strip()!r}")

proc = subprocess.run(
    [sys.executable, os.path.join(_ROOT, "gnt_check.py"), tmp,
     "--baseline", side_bad],
    capture_output=True, text=True)
check("6: the real CLI exits 3 too", proc.returncode == 3,
      f"exit={proc.returncode} err={proc.stderr.strip()!r}")

print()
print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
if FAILED:
    for f in FAILED:
        print(f"  - {f}")
sys.exit(1 if FAILED else 0)
