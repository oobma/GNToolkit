# -*- coding: utf-8 -*-
"""Pure-Python tests for cross-project identity (audit.cross_project and
gnt_check --cross) — no bpy.

Usage: python tests\\test_cross_project.py
"""

import contextlib
import io
import json
import os
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


audit = gnt_check._load_audit()


def group(name, flag, tag=None):
    props = {"show_modifier_manage_panel": flag}
    if tag is not None:
        props["test_tag"] = tag
    return {"name": name, "nodes": [], "links": [], "tree_properties": props}


def write(folder, fname, payload):
    path = os.path.join(folder, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


with tempfile.TemporaryDirectory(prefix="gnt_cross_") as td:
    pa = os.path.join(td, "projA")
    pb = os.path.join(td, "projB")
    pc = os.path.join(td, "projC")
    for d in (pa, pb, pc):
        os.makedirs(d)

    # projA: Base · Fork · OnlyA
    write(pa, "base.json", group("Base", False))
    write(pa, "fork.json", group("Fork", False, "f"))
    write(pa, "only_a.json", group("OnlyA", False, "a"))
    # projB: same names/logic + one unreadable file
    write(pb, "base.json", group("Base", False))
    write(pb, "fork.json", group("Fork", False, "f"))
    write(pb, "only_b.json", group("OnlyB", False, "b"))
    with open(os.path.join(pb, "broken.json"), "w", encoding="utf-8") as f:
        f.write("{oops")
    # projC: same logic under a different name + a diverged Fork
    write(pc, "common.json", group("Common", False))
    write(pc, "fork.json", group("Fork", True, "f"))
    # projD: a single unified package (two groups)
    pack = write(td, "pack.json", {
        "type": "GN_UNIFIED_PACKAGE",
        "node_groups": {"Base": group("Base", False),
                        "Unique": group("Unique", False, "d")}})

    targets = [pa, pb, pc, pack]
    rep = audit.cross_project(targets)

    labels = [p["label"] for p in rep["projects"]]
    check("A: project labels (folders + package file)",
          labels == ["projA", "projB", "projC", "pack.json"], str(labels))

    counts = {p["label"]: p["groups"] for p in rep["projects"]}
    check("B: per-project group counts",
          counts == {"projA": 3, "projB": 3, "projC": 2, "pack.json": 2},
          str(counts))
    unreadable = {p["label"]: p["unreadable"] for p in rep["projects"]}
    check("B: unreadable file counted for projB",
          unreadable["projB"] == 1 and sum(unreadable.values()) == 1,
          str(unreadable))
    check("B: total groups = 10", sum(counts.values()) == 10)

    check("C: shared buckets = 2", len(rep["shared"]) == 2,
          str(len(rep["shared"])))
    biggest = rep["shared"][0]["groups"]
    check("C: biggest bucket spans 4 projects (Base logic, one renamed)",
          len(biggest) == 4
          and {(g["project"], g["name"]) for g in biggest} == {
              ("projA", "Base"), ("projB", "Base"),
              ("projC", "Common"), ("pack.json", "Base")})
    smaller = rep["shared"][1]["groups"]
    check("C: second bucket = the two identical Fork copies",
          {(g["project"], g["name"]) for g in smaller}
          == {("projA", "Fork"), ("projB", "Fork")})

    check("D: one divergent fork", len(rep["forks"]) == 1
          and rep["forks"][0]["name"] == "Fork")
    versions = rep["forks"][0]["versions"]
    check("D: fork version spread 2x1 + 1x1",
          [(v["count"], len(v["groups"])) for v in versions] == [(2, 2), (1, 1)])

    h_false = audit.content_hash_from_json_data(group("Fork", False, "f"))
    h_true = audit.content_hash_from_json_data(group("Fork", True, "f"))
    check("D: fork hashes are the canonical content hashes",
          versions[0]["hash"] == h_false and versions[1]["hash"] == h_true)
    check("D: 'Base' is not a fork (all versions identical)",
          all(f["name"] != "Base" for f in rep["forks"]))

    check("E: deterministic report", rep == audit.cross_project(targets))

    def run_cli(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = gnt_check.main(argv)
        return code, buf.getvalue()

    code, out = run_cli(["--cross", pa, pb, pc, pack])
    check("F: CLI informational -> exit 0, fork listed",
          code == 0 and "divergent forks: 1" in out, f"exit={code}")

    code, _out = run_cli(["--cross", pa, pb, pc, pack, "--strict"])
    check("F: CLI --strict -> exit 1 on divergent fork", code == 1,
          f"exit={code}")

    code, _out = run_cli(["--cross", pa, pack, "--strict"])
    check("F: CLI --strict without forks -> exit 0", code == 0,
          f"exit={code}")

    code, _out = run_cli(["--cross", pa, os.path.join(td, "ghost")])
    check("F: CLI missing target -> exit 2", code == 2, f"exit={code}")

    empty = os.path.join(td, "empty")
    empty2 = os.path.join(td, "empty2")
    os.makedirs(empty)
    os.makedirs(empty2)
    code, _out = run_cli(["--cross", empty, empty2])
    check("F: CLI nothing readable -> exit 2", code == 2, f"exit={code}")

    code, out = run_cli(["--cross", pa, pb, pc, pack, "--json"])
    data = json.loads(out)
    check("G: --json shape",
          code == 0 and set(data) == {"projects", "shared", "forks"}
          and len(data["shared"]) == 2 and len(data["forks"]) == 1)

    check("H: bpy never imported", "bpy" not in sys.modules)

with tempfile.TemporaryDirectory(prefix="gnt_cross_labels_") as td:
    a = os.path.join(td, "one", "NodeGroups")
    b = os.path.join(td, "two", "NodeGroups")
    os.makedirs(a)
    os.makedirs(b)
    write(a, "g.json", group("G", False))
    write(b, "g.json", group("G", False))
    rep = audit.cross_project([a, b])
    labels = [p["label"] for p in rep["projects"]]
    check("I: equal basenames -> full given paths disambiguate",
          labels == [a, b], str(labels))

print()
print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
if FAILED:
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
