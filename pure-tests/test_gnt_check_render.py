# -*- coding: utf-8 -*-
"""Pure-Python verification of the gnt_check --render modes (no Graphviz).

Uses --dot-only: the .dot emission is ours and deterministic, so the
project/neighborhood graph, diff coloring, single-group view and
node-level diff can be verified without the Graphviz binary.  A tiny
synthetic project keeps the checks exact.

Usage:
    python tests\\test_gnt_check_render.py
"""

import json
import os
import subprocess
import sys
import tempfile

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
GNT = os.path.join(_ROOT, "gnt_check.py")

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"[PASS] {name} {detail}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name} {detail}")


def run(*args):
    return subprocess.run([sys.executable, GNT] + list(args),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def group_data(name, refs=(), default=1.0):
    nodes = [{"name": "In", "type": "NodeGroupInput"},
             {"name": "Out", "type": "NodeGroupOutput"}]
    for ref in refs:
        nodes.append({"name": "Use " + ref, "type": "GeometryNodeGroup",
                      "node_tree_reference": ref})
    nodes.append({"name": "Math", "type": "ShaderNodeMath",
                  "inputs": [{"identifier": "Value_001",
                              "default_value": default}]})
    return {"name": name, "nodes": nodes, "links": []}


def write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


work = tempfile.mkdtemp(prefix="gnt_render_test_")
proj1 = os.path.join(work, "p1")
proj2 = os.path.join(work, "p2")
out = os.path.join(work, "out")
for folder in (proj1, proj2):
    os.makedirs(folder)

write(os.path.join(proj1, "A.json"), group_data("A", refs=("B", "Ghost")))
write(os.path.join(proj1, "B.json"), group_data("B", refs=("C",)))
write(os.path.join(proj1, "C.json"), group_data("C", default=1.0))

write(os.path.join(proj2, "A.json"), group_data("A", refs=("B", "Ghost")))
write(os.path.join(proj2, "B.json"), group_data("B", refs=("C",)))
write(os.path.join(proj2, "C.json"), group_data("C", default=2.0))
write(os.path.join(proj2, "D.json"), group_data("D"))


def dot_of(base):
    with open(os.path.join(out, base + ".dot"), encoding="utf-8") as f:
        return f.read()


# --- project graph ---------------------------------------------------------
r = run("--render", "deps", proj1, "--out", out, "--dot-only")
dots = dot_of("deps_full")
check("deps: project graph emitted", r.returncode == 0,
      r.stdout[-200:] + r.stderr[-200:])
check("deps: all groups as nodes",
      all(f'"{n}"' in dots for n in ("A", "B", "C")))
check("deps: dependency edges present",
      '"A" -> "B"' in dots and '"B" -> "C"' in dots)
check("deps: missing reference shown dashed/external",
      '"Ghost"' in dots and 'style="rounded,dashed"' in dots)

# --- determinism -----------------------------------------------------------
first = dots
run("--render", "deps", proj1, "--out", out, "--dot-only")
check("deps: identical bytes on re-run (deterministic emission)",
      dot_of("deps_full") == first)

# --- neighborhood ----------------------------------------------------------
r = run("--render", "deps", proj1, "--focus", "A", "--hops", "1",
        "--out", out, "--dot-only")
one_hop = dot_of("deps_A")
check("neighborhood hops=1 keeps A, B and the external ref (not C)",
      '"A"' in one_hop and '"B"' in one_hop and '"Ghost"' in one_hop
      and '"C"' not in one_hop
      and "3 nodes kept" in r.stdout, r.stdout.splitlines()[1])

r = run("--render", "deps", proj1, "--focus", "A", "--hops", "2",
        "--out", out, "--dot-only")
two_hop = dot_of("deps_A")
check("neighborhood hops=2 reaches C", '"C"' in two_hop
      and "4 nodes kept" in r.stdout)

# --- project diff coloring -------------------------------------------------
r = run("--render", "deps", proj1, "--new", proj2, "--out", out,
        "--dot-only")
diffdots = dot_of("depsdiff_full")
check("project diff counts (added 1, changed 1)",
      "added 1" in r.stdout and "changed 1" in r.stdout, r.stdout[-160:])
check("project diff colors the changed and added nodes",
      "#a9791c" in diffdots and "#2e7d32" in diffdots)

# --- single-group view -----------------------------------------------------
r = run("--render", "group", os.path.join(proj1, "A.json"),
        "--out", out, "--dot-only")
gdots = dot_of("group_A_dot_dot")
check("group: single-group graph emitted", r.returncode == 0)
check("group: reference shown in the label", "Use B\\n(B)" in gdots)
check("group: node types annotated", "[ShaderNodeMath]" in gdots)

# --- node-level diff -------------------------------------------------------
r = run("--render", "diff", os.path.join(proj1, "C.json"),
        "--new", os.path.join(proj2, "C.json"),
        "--out", out, "--dot-only")
ddots = dot_of("diff_C_dot_dot")
check("node diff: changed count reported", "~1" in r.stdout, r.stdout[-160:])
check("node diff: changed style applied", "#a9791c" in ddots)

# --- stats and error paths -------------------------------------------------
r = run("--render", "stats", proj1)
check("stats: folder summary", r.returncode == 0 and "3 groups in" in r.stdout)

r = run("--render", "group", os.path.join(proj1, "missing.json"),
        "--out", out, "--dot-only")
check("missing file: clean error, exit 2",
      r.returncode == 2 and "error:" in r.stdout, r.stdout[-120:])

print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
for name in FAILED:
    print(f"  - {name}")
sys.exit(1 if FAILED else 0)
