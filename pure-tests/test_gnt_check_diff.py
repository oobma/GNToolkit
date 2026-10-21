# -*- coding: utf-8 -*-
"""gnt_check --diff: readable semantic summary between two JSON versions.

Usage:
    python tests\\test_gnt_check_diff.py
"""

import contextlib
import copy
import io
import json
import os
import shutil
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


def run_diff(a_path, b_path):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = gnt_check.main(["--diff", a_path, b_path])
    return code, out.getvalue()


def math_node(name, value=1.0, op="ADD"):
    return {
        "name": name, "type": "ShaderNodeMath", "label": "", "parent": None,
        "inputs": [{"name": "Value", "identifier": "Value", "type": "VALUE",
                    "default_value": value}],
        "outputs": [{"name": "Value", "identifier": "Value", "type": "VALUE"}],
        "properties": {"operation": op, "color": [0.6, 0.6, 0.6]},
        "zone_items": [], "zone_paired_node": None, "menu_items_data": [],
    }


def base_group():
    return {
        "name": "G",
        "nodes": [math_node("Math", 1.0), math_node("Math.001", 2.0,
                                                    "MULTIPLY")],
        "links": [{"from_node": "Math", "from_socket_id": "Value",
                   "from_socket_name": "Value", "to_node": "Math.001",
                   "to_socket_id": "Value", "to_socket_name": "Value"}],
        "interface_items": [
            {"identifier": "Socket_0", "item_type": "SOCKET",
             "in_out": "OUTPUT", "name": "Geometry",
             "socket_type": "NodeSocketGeometry", "properties": {}},
        ],
    }


work = tempfile.mkdtemp(prefix="gnt_diff_")
a = base_group()
b = copy.deepcopy(a)
b["interface_items"].append(
    {"identifier": "Socket_1", "item_type": "SOCKET", "in_out": "INPUT",
     "name": "Demo Id", "socket_type": "NodeSocketInt",
     "properties": {"default_value": 0}})
for i in (1, 2, 3):
    b["nodes"].append(math_node("Demo_0%d" % i, 0.5 * i))
b["nodes"][0]["inputs"][0]["default_value"] = 1.5
b["nodes"].append({
    "name": "Reroute", "type": "NodeReroute", "label": "", "parent": None,
    "inputs": [{"name": "Input", "identifier": "Input", "type": "CUSTOM"}],
    "outputs": [{"name": "Output", "identifier": "Output",
                 "type": "CUSTOM"}],
    "properties": {}, "zone_items": [], "zone_paired_node": None,
    "menu_items_data": [],
})
b["links"] = [
    {"from_node": "Math", "from_socket_id": "Value",
     "from_socket_name": "Value", "to_node": "Reroute",
     "to_socket_id": "Input", "to_socket_name": "Input"},
    {"from_node": "Reroute", "from_socket_id": "Output",
     "from_socket_name": "Output", "to_node": "Math.001",
     "to_socket_id": "Value", "to_socket_name": "Value"},
]

path_a = os.path.join(work, "a.json")
path_b = os.path.join(work, "b.json")
with open(path_a, "w", encoding="utf-8") as f:
    json.dump(a, f)
with open(path_b, "w", encoding="utf-8") as f:
    json.dump(b, f)

code, out = run_diff(path_a, path_b)
check("1: interface addition reported", "+ input 'Demo Id'" in out,
      out.splitlines()[1] if len(out.splitlines()) > 1 else out)
check("2: added nodes grouped by prefix", "Demo_* (3)" in out)
check("3: input value change reported", "Value = 1 -> 1.5" in out, out)
check("4: reroute insertion is not a re-link", "Links re-linked" not in out,
      out)

d = copy.deepcopy(a)
d["links"] = [{"from_node": "Math.001", "from_socket_id": "Value",
               "from_socket_name": "Value", "to_node": "Math.001",
               "to_socket_id": "Value", "to_socket_name": "Value"}]
path_d = os.path.join(work, "d.json")
with open(path_d, "w", encoding="utf-8") as f:
    json.dump(d, f)
code_relink, out_relink = run_diff(path_a, path_d)
check("4b: true re-link detected", "Links re-linked (1)" in out_relink,
      out_relink)

code_same, out_same = run_diff(path_a, path_a)
check("5: identical versions report no changes",
      code_same == 0 and "No semantic changes." in out_same, out_same.strip())

pkg_a = os.path.join(work, "pkg_a.json")
pkg_b = os.path.join(work, "pkg_b.json")
for path, group in ((pkg_a, a), (pkg_b, b)):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "GN_UNIFIED_PACKAGE", "version": "0",
                   "node_groups": {"G": group}}, f)
code_pkg, out_pkg = run_diff(pkg_a, pkg_b)
check("6: unified packages supported",
      code_pkg == 0 and "Demo Id" in out_pkg and "== G " in out_pkg, out_pkg)

c = copy.deepcopy(a)
c["nodes"][0]["location"] = [999.0, 999.0]
path_c = os.path.join(work, "c.json")
with open(path_c, "w", encoding="utf-8") as f:
    json.dump(c, f)
code_mv, out_mv = run_diff(path_a, path_c)
check("7: pure moves counted, not reported as changes",
      "only moved" in out_mv and "Nodes changed" not in out_mv, out_mv.strip())

if shutil.which("git") is None:
    print("[SKIP] git not available; revision mode not tested")
else:
    repo_dir = os.path.join(work, "repo")
    sub = os.path.join(repo_dir, "NodeGroups")
    os.makedirs(sub)

    def git(*args):
        return subprocess.run(["git", "-C", repo_dir] + list(args),
                              capture_output=True, text=True)

    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@localhost")
    with open(os.path.join(sub, "G.json"), "w", encoding="utf-8") as f:
        json.dump(a, f)
    git("add", ".")
    git("commit", "-m", "A")
    with open(os.path.join(sub, "G.json"), "w", encoding="utf-8") as f:
        json.dump(b, f)
    git("add", ".")
    git("commit", "-m", "B")

    out_git = io.StringIO()
    with contextlib.redirect_stdout(out_git):
        code_git = gnt_check.main(["--diff", "HEAD~1", "HEAD",
                                   "--repo", repo_dir,
                                   "--subdir", "NodeGroups"])
    text_git = out_git.getvalue()
    check("8: git revisions supported (--repo/--subdir)",
          code_git == 0 and "Demo Id" in text_git
          and "Nodes added" in text_git
          and "re-linked" not in text_git,
          text_git.strip().splitlines()[0] if text_git else "")

    out_err = io.StringIO()
    with contextlib.redirect_stdout(out_err):
        code_err = gnt_check.main(["--diff", "HEAD~1", "HEAD"])
    check("9: missing --repo gives an actionable error",
          code_err == 2 and "--repo" in out_err.getvalue(),
          out_err.getvalue().strip())

print()
print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
if FAILED:
    for f in FAILED:
        print(f"  - {f}")
sys.exit(1 if FAILED else 0)
