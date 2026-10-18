# -*- coding: utf-8 -*-
"""Pure-Python tests for audit.py — no bpy.

Usage: python tests\\test_audit.py
"""

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

# ---------------------------------------------------------------------------
# 1. Dependency impact
# ---------------------------------------------------------------------------
META = {"hash_version": 3, "tracked_groups": {
    "uA": {"blend_name": "Base", "depends_on": []},
    "uB": {"blend_name": "Mid", "depends_on": ["uA"]},
    "uC": {"blend_name": "Leaf", "depends_on": ["uB"]},
    "uD": {"blend_name": "Other", "depends_on": ["uB"]},
    "uE": {"blend_name": "Cyc1", "depends_on": ["uF"]},
    "uF": {"blend_name": "Cyc2", "depends_on": ["uE"]},
}}

rev = audit.reverse_deps(META)
check("1: reverse_deps Base <- Mid", rev.get("uA") == ["uB"], str(rev.get("uA")))
check("1: reverse_deps Mid <- Leaf, Other", rev.get("uB") == ["uC", "uD"])

imp = audit.impact(META, "Base")
check("1: impact accepts group name", imp is not None and imp["uid"] == "uA")
check("1: impact direct = [Mid]",
      len(imp["direct"]) == 1 and imp["direct"][0]["name"] == "Mid")
check("1: impact transitive = Mid, Leaf, Other",
      sorted(e["name"] for e in imp["transitive"]) == ["Leaf", "Mid", "Other"])

imp_uid = audit.impact(META, "uA")
check("1: impact accepts uid", imp_uid is not None
      and [e["uid"] for e in imp_uid["direct"]] == ["uB"])
check("1: impact unknown -> None", audit.impact(META, "not-there") is None)

imp_cyc = audit.impact(META, "Cyc1")
check("1: impact cycle-safe",
      len(imp_cyc["transitive"]) == 1
      and imp_cyc["transitive"][0]["name"] == "Cyc2")

# ---------------------------------------------------------------------------
# 2. Content identity / duplicates
# ---------------------------------------------------------------------------
base = {"name": "A", "nodes": [], "links": [],
        "tree_properties": {"node_tool_idname": "geometry.a",
                            "show_modifier_manage_panel": False}}
same_logic = {"name": "B", "nodes": [], "links": [],
              "tree_properties": {"node_tool_idname": "geometry.b",
                                  "show_modifier_manage_panel": False}}
different = {"name": "B", "nodes": [], "links": [],
             "tree_properties": {"node_tool_idname": "geometry.b",
                                 "show_modifier_manage_panel": True}}

h_a = audit.content_hash_from_json_data(base)
h_b = audit.content_hash_from_json_data(same_logic)
h_c = audit.content_hash_from_json_data(different)
check("2: content hash ignores name + node_tool_idname", h_a == h_b)
check("2: content hash keeps other tree properties", h_b != h_c)
check("2: content hash deterministic",
      h_a == audit.content_hash_from_json_data(base))

buckets = audit.duplicates_from_records([
    {"name": "A", "hash": "h1"}, {"name": "B", "hash": "h1"},
    {"name": "C", "hash": "h2"},
    {"name": "D", "hash": "h3"}, {"name": "E", "hash": "h3"},
    {"name": "F", "hash": "h3"},
])
check("2: duplicates -> 2 buckets", len(buckets) == 2)
check("2: duplicates biggest first",
      buckets[0]["names"] == ["D", "E", "F"]
      and buckets[1]["names"] == ["A", "B"])

# ---------------------------------------------------------------------------
# 3. Folder scan / health
# ---------------------------------------------------------------------------
META2 = {"hash_version": 3, "tracked_groups": {
    "t1": {"blend_name": "Good", "json_path": "good.json", "depends_on": []},
    "t2": {"blend_name": "Missing", "json_path": "missing.json",
           "depends_on": []},
    "t3": {"blend_name": "ExtUser", "json_path": "good.json",
           "depends_on": ["ghost"]},
}}

with tempfile.TemporaryDirectory(prefix="gnt_audit_") as td:
    def w(fname, payload):
        with open(os.path.join(td, fname), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    w("good.json", {"name": "Good", "nodes": [], "links": []})
    w("good2.json", {"name": "Good2", "nodes": [], "links": []})
    with open(os.path.join(td, "bad.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    with open(os.path.join(td, "conflict.json"), "w", encoding="utf-8") as f:
        f.write("<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> other\n")

    rep = audit.health(td, META2, td)

    check("3: health counts files", rep["files"] == 4, str(rep["files"]))
    check("3: health counts groups", rep["groups"] == 2, str(rep["groups"]))
    check("3: health unreadable", len(rep["unreadable"]) == 1
          and rep["unreadable"][0].endswith("bad.json"))
    check("3: health conflicts", len(rep["conflicts"]) == 1
          and rep["conflicts"][0].endswith("conflict.json"))
    check("3: health duplicates bucket = Good + Good2",
          len(rep["duplicates"]) == 1
          and rep["duplicates"][0]["names"] == ["Good", "Good2"])
    check("3: health missing files",
          [m["name"] for m in rep["missing_files"]] == ["Missing"])
    check("3: health external refs",
          len(rep["external_refs"]) == 1
          and rep["external_refs"][0]["name"] == "ExtUser"
          and rep["external_refs"][0]["missing_uuids"] == ["ghost"])
    check("3: bpy never imported", "bpy" not in sys.modules)

# ---------------------------------------------------------------------------
# 4. group_data_from_file (single-entry reads used by the addon UI)
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory(prefix="gnt_audit_file_") as td:
    def w(fname, payload=None, raw=None):
        p = os.path.join(td, fname)
        with open(p, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(payload))
        return p

    p_uni = w("uni.json", {"type": "GN_UNIFIED_PACKAGE",
                           "node_groups": {"Packed": {"name": "Packed",
                                                      "nodes": [], "links": []}}})
    p_std = w("std.json", {"name": "Std", "nodes": [], "links": []})
    p_bad = w("bad.json", raw="{oops")
    p_con = w("conf.json", raw="<<<<<<< HEAD\n{}\n")

    d, e = audit.group_data_from_file(p_std, "Std")
    check("4: standalone file read",
          d is not None and e is None and d["name"] == "Std")
    d, e = audit.group_data_from_file(p_uni, "Packed")
    check("4: unified package group read", d is not None and e is None)
    d, e = audit.group_data_from_file(p_uni, "Nope")
    check("4: unified missing group", d is None and e == "missing-group")
    d, e = audit.group_data_from_file(p_bad, "X")
    check("4: unreadable file", d is None and e == "unreadable")
    d, e = audit.group_data_from_file(p_con, "X")
    check("4: conflict file", d is None and e == "conflict")

# ---- 5. Modifier consumers (objects using a group) -----------------------
with tempfile.TemporaryDirectory(prefix="gnt_mods_") as td:
    mods_dir = os.path.join(td, "Modifiers")
    os.makedirs(mods_dir)

    def w_mod(name, payload=None, raw=None):
        path = os.path.join(mods_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(payload))
        return path

    w_mod("cube_a.json", {"object": "Cube", "modifier_name": "GN A",
                          "node_group": "Group A"})
    w_mod("cube_b.json", {"object": "Cube", "modifier_name": "GN B",
                          "node_group": "Group A"})
    w_mod("sphere.json", {"object": "Sphere", "modifier_name": "GN",
                          "node_group": "Group B"})
    w_mod("no_group.json", {"object": "Empty"})
    w_mod("broken.json", raw="{oops")

    recs = audit.scan_modifier_records(mods_dir)
    check("5: modifier scan tolerant", len(recs) == 3, f"{len(recs)} records")
    check("5: consumers filtered and sorted",
          audit.modifier_consumers(recs, "Group A") == [
              {"object": "Cube", "modifier": "GN A"},
              {"object": "Cube", "modifier": "GN B"}])
    check("5: no consumers -> empty",
          audit.modifier_consumers(recs, "Group C") == [])
    check("5: missing folder -> empty",
          audit.scan_modifier_records(os.path.join(td, "nope")) == [])

    ng_dir = os.path.join(td, "NodeGroups")
    os.makedirs(ng_dir)
    meta = {"tracked_groups": {"u1": {"blend_name": "Group A",
                                      "json_path": "//NodeGroups/g.json"}}}
    check("5: modifiers folder discovery (// paths)",
          audit.modifiers_dir_for(meta, td) == mods_dir,
          audit.modifiers_dir_for(meta, td))
    check("5: Blender-relative path resolution",
          audit._metadata_path("//NodeGroups/g.json", td)
          == os.path.join(ng_dir, "g.json"))

print()
print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
if FAILED:
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)