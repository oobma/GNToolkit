# -*- coding: utf-8 -*-
"""gnt_check --merge: 3-way semantic merge preview (pure, no bpy).

Usage:
    python tests\\test_gnt_check_merge.py
"""

import contextlib
import copy
import hashlib
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


def run_merge(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = gnt_check.main(["--merge"] + list(args))
    return code, out.getvalue()


def math_node(name, value=1.0):
    return {"name": name, "type": "GeometryNodeMath", "label": "",
            "location": [0.0, 0.0], "parent": None,
            "inputs": [{"name": "Value", "identifier": "Value",
                        "type": "VALUE", "bl_idname": "NodeSocketFloat",
                        "hide": False, "default_value": value}],
            "outputs": [], "properties": {"operation": "ADD",
                                          "use_clamp": False},
            "zone_items": [], "zone_paired_node": None,
            "menu_items_data": []}


def entry(name="G", value=1.0, tree_type=None, nodes=None):
    data = {"name": name, "inputs": [], "outputs": [],
            "nodes": nodes if nodes is not None else [math_node("Math", value)],
            "links": [], "interface_items": [], "tree_properties": {}}
    if tree_type:
        data["tree_type"] = tree_type
    return data


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def side(root, name, value, sub="", tree_type=None):
    path = os.path.join(root, name, *([sub] if sub else []), "G.json")
    write_json(path, entry(value=value, tree_type=tree_type))
    return os.path.dirname(path)


def snapshot(root):
    state = {}
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            state[os.path.relpath(path, root)] = digest
    return state


work = tempfile.mkdtemp(prefix="gnt_merge_cli_")

# ---------------------------------------------------------------------------
# 1. Human report and exit codes
# ---------------------------------------------------------------------------
base_dir = side(work, "base", 1.0)
ours_dir = side(work, "ours", 2.0)
theirs_dir = side(work, "theirs", 1.0)

code, out = run_merge(base_dir, ours_dir, theirs_dir)
check("1: clean merge exits 0 and reports adoption",
      code == 0 and "== G [merged]" in out and "adopted: ours" in out
      and "hash " in out, out.strip().splitlines()[-1])

code, out = run_merge(base_dir, base_dir, side(work, "theirs2", 5.0))
check("2: theirs-only change is applied and reported",
      code == 0 and "<- theirs" in out and "5" in out, out.strip())

conflict_dir = side(work, "theirs_conflict", 3.0)
code, out = run_merge(base_dir, ours_dir, conflict_dir)
check("3: value conflict exits 1 with base/ours/theirs",
      code == 1 and "== G [conflicted]" in out
      and "! value:" in out and "base 1, ours 2, theirs 3" in out,
      out.strip().splitlines()[-1])

# ---------------------------------------------------------------------------
# 2. --json schema
# ---------------------------------------------------------------------------
out = io.StringIO()
with contextlib.redirect_stdout(out):
    code_json = gnt_check.main(["--merge", base_dir, ours_dir, conflict_dir,
                                "--json"])
payload = json.loads(out.getvalue())
check("4: --json reports the documented schema",
      code_json == 1 and payload["mode"] == "merge"
      and payload["sides"]["base"] == base_dir
      and payload["hash_version"] == gnt_check._current_hash_version()
      and payload["merge_version"] == 3
      and payload["groups"]["G"]["status"] == "conflicted"
      and payload["groups"]["G"]["conflicts"][0]["kind"] == "value"
      and payload["summary"]["conflicted"] == 1 and payload["ok"] is False)

out = io.StringIO()
with contextlib.redirect_stdout(out):
    code_json = gnt_check.main(["--merge", base_dir, base_dir, ours_dir,
                                "--json"])
payload = json.loads(out.getvalue())
check("5: --json clean merge carries applied entries and ok",
      code_json == 0 and payload["ok"] is True
      and payload["groups"]["G"]["status"] == "merged"
      and payload["groups"]["G"]["applied"][0]["from"] == "theirs"
      and payload["summary"]["merged"] == 1)

# ---------------------------------------------------------------------------
# 3. Strict reads (D12) and error paths
# ---------------------------------------------------------------------------
bad_dir = os.path.join(work, "bad")
os.makedirs(bad_dir)
with open(os.path.join(bad_dir, "broken.json"), "w", encoding="utf-8") as fh:
    fh.write("{not json")
code, out = run_merge(base_dir, ours_dir, bad_dir)
check("6: unreadable JSON in any side is a hard error",
      code == 2 and "cannot read JSON for merge" in out, out.strip())

code, out = run_merge("NOPE_REV", "ALSO_NO", "STILL_NO")
check("7: missing path without --repo gives the actionable error",
      code == 2 and "--repo" in out, out.strip())

# ---------------------------------------------------------------------------
# 4. Package files and group-level rules
# ---------------------------------------------------------------------------
pkg_base = os.path.join(work, "pkg_base.json")
pkg_ours = os.path.join(work, "pkg_ours.json")
pkg_theirs = os.path.join(work, "pkg_theirs.json")
write_json(pkg_base, {"type": "GN_UNIFIED_PACKAGE", "version": "0",
                      "node_groups": {"G": entry(value=1.0)}})
write_json(pkg_ours, {"type": "GN_UNIFIED_PACKAGE", "version": "0",
                      "node_groups": {"G": entry(value=1.0),
                                      "H": entry(name="H", value=7.0)}})
write_json(pkg_theirs, {"type": "GN_UNIFIED_PACKAGE", "version": "0",
                        "node_groups": {"G": entry(value=1.0)}})
code, out = run_merge(pkg_base, pkg_ours, pkg_theirs)
check("8: unified packages work; added group reported",
      code == 0 and "== H [added]" in out and "[unchanged]" in out,
      out.strip().splitlines()[-1])

rename_base = side(work, "ren_base", 1.0)
rename_ours = side(work, "ren_ours", 1.0)
rename_theirs = side(work, "ren_theirs", 1.0)
os.rename(os.path.join(rename_ours, "G.json"),
          os.path.join(rename_ours, "New.json"))
os.rename(os.path.join(rename_theirs, "G.json"),
          os.path.join(rename_theirs, "New2.json"))
with open(os.path.join(rename_ours, "New.json"), encoding="utf-8") as fh:
    data = json.load(fh)
data["name"] = "New"
write_json(os.path.join(rename_ours, "New.json"), data)
with open(os.path.join(rename_theirs, "New2.json"), encoding="utf-8") as fh:
    data = json.load(fh)
data["name"] = "New2"
write_json(os.path.join(rename_theirs, "New2.json"), data)
code, out = run_merge(rename_base, rename_ours, rename_theirs)
check("9: same group renamed to two names -> rename/rename conflict",
      code == 1 and "== G [conflicted]" in out
      and "rename/rename" in out
      and "renamed to 'New' on ours" in out
      and "-> 'New2'" in out, out.strip()[-160:])

# ---------------------------------------------------------------------------
# 5. Shader merge and unsupported tree types
# ---------------------------------------------------------------------------
sh_base = side(work, "sh_base", 1.0, tree_type="ShaderNodeTree")
sh_ours = side(work, "sh_ours", 2.0, tree_type="ShaderNodeTree")
sh_theirs = side(work, "sh_theirs", 1.0, tree_type="ShaderNodeTree")
code, out = run_merge(sh_base, sh_ours, sh_theirs)
check("10: shader groups merge per item (one-side change)",
      code == 0 and "== G [merged]" in out and "adopted: ours" in out,
      out.strip().splitlines()[-1])

un_base = side(work, "un_base", 1.0, tree_type="CompositorNodeTree")
un_ours = side(work, "un_ours", 2.0, tree_type="CompositorNodeTree")
un_theirs = side(work, "un_theirs", 3.0, tree_type="CompositorNodeTree")
code, out = run_merge(un_base, un_ours, un_theirs)
check("11: a non-mergeable tree type is an unsupported notice",
      code == 0 and "[unsupported]" in out and "not covered" in out,
      out.strip().splitlines()[-1])
code, out = run_merge(un_base, un_ours, un_theirs, "--strict")
check("11b: --strict turns unsupported entries into exit 1",
      code == 1, f"exit={code}")


def side_embedded(root, name, section, value=1.0, metallic=0.1):
    path = os.path.join(root, name, "M.json")
    data = {"name": "Mat", "inputs": [], "outputs": [],
            "nodes": [math_node("Math", value)], "links": [],
            "interface_items": [], "tree_properties": {},
            "tree_type": "ShaderNodeTree",
            "owner_settings": {"properties": {"metallic": metallic}}}
    write_json(path, {"type": "GN_UNIFIED_PACKAGE", section: {"Mat": data}})
    return os.path.dirname(path)


em_base = side_embedded(work, "em_base", "materials", 1.0, 0.1)
em_ours = side_embedded(work, "em_ours", "materials", 2.0, 0.1)
em_theirs = side_embedded(work, "em_theirs", "materials", 1.0, 0.1)
code, out = run_merge(em_base, em_ours, em_theirs)
check("11c: material one-side merge is reported",
      code == 0 and "== material 'Mat' [merged]" in out,
      out.strip().splitlines()[-1])

em_conflict = side_embedded(work, "em_conflict", "materials", 1.0, 0.9)
em_conflict_ours = side_embedded(work, "em_conflict_ours", "materials",
                                 1.0, 0.5)
code, out = run_merge(em_base, em_conflict_ours, em_conflict)
check("11d: material owner-setting conflict exits 1",
      code == 1 and "== material 'Mat' [conflicted]" in out
      and "owner setting 'metallic'" in out,
      out.strip().splitlines()[-1])

out = io.StringIO()
with contextlib.redirect_stdout(out):
    code_json = gnt_check.main(["--merge", em_base, em_ours, em_theirs,
                                "--json"])
payload = json.loads(out.getvalue())
check("11e: --json reports embedded entries",
      code_json == 0
      and payload["embedded"]["materials"]["Mat"]["status"] == "merged"
      and payload["embedded_summary"]["merged"] == 1,
      str(payload.get("embedded_summary")))

w_base = side_embedded(work, "w_base", "worlds", 1.0, 0.1)
w_ours = side_embedded(work, "w_ours", "worlds", 2.0, 0.1)
w_theirs = side_embedded(work, "w_theirs", "worlds", 1.0, 0.1)
code, out = run_merge(w_base, w_ours, w_theirs)
check("11f: world one-side merge is reported",
      code == 0 and "== world 'Mat' [merged]" in out,
      out.strip().splitlines()[-1])

# ---------------------------------------------------------------------------
# 6. Git revisions, determinism and the no-write guarantee
# ---------------------------------------------------------------------------
if shutil.which("git") is None:
    print("[SKIP] 12: git not available; revision mode not tested")
else:
    repo_dir = os.path.join(work, "repo")
    sub = os.path.join(repo_dir, "NodeGroups")
    os.makedirs(sub)

    def git(*args):
        return subprocess.run(["git", "-C", repo_dir] + list(args),
                              capture_output=True, text=True)

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@localhost")
    write_json(os.path.join(sub, "G.json"), entry(value=1.0))
    git("add", ".")
    git("commit", "-m", "base")
    write_json(os.path.join(sub, "G.json"), entry(value=4.0))
    git("add", ".")
    git("commit", "-m", "ours")
    code, out = run_merge("HEAD~1", "HEAD", base_dir,
                          "--repo", repo_dir, "--subdir", "NodeGroups")
    check("12: git revisions as sides (BASE=HEAD~1, OURS=HEAD)",
          code == 0 and "HEAD~1" in out and "adopted: ours" in out,
          out.strip().splitlines()[-1])

out1 = io.StringIO()
with contextlib.redirect_stdout(out1):
    gnt_check.main(["--merge", base_dir, ours_dir, conflict_dir])
code, out2 = run_merge(base_dir, ours_dir, conflict_dir)
check("13: deterministic CLI output across runs",
      out2 == out1.getvalue() and code == 1)

state_before = snapshot(work)
run_merge(base_dir, ours_dir, conflict_dir)
check("14: the preview never writes to the sides",
      snapshot(work) == state_before)

# ---------------------------------------------------------------------------
# 7. Exact-content renames (v2)
# ---------------------------------------------------------------------------
star_base = side(work, "star_base", 1.0)
star_ours = side(work, "star_ours", 1.0)
star_theirs = side(work, "star_theirs", 5.0)
os.rename(os.path.join(star_ours, "G.json"),
          os.path.join(star_ours, "New.json"))
with open(os.path.join(star_ours, "New.json"), encoding="utf-8") as fh:
    data = json.load(fh)
data["name"] = "New"
write_json(os.path.join(star_ours, "New.json"), data)
code, out = run_merge(star_base, star_ours, star_theirs)
check("15: rename + remote value edit previews clean under the new name",
      code == 0 and "== New [renamed]" in out
      and "renamed from 'G'" in out and "<- theirs" in out
      and "no exact-content match" not in out,
      out.strip().splitlines()[-1])

print()
print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
if FAILED:
    for f in FAILED:
        print(f"  - {f}")
sys.exit(1 if FAILED else 0)
