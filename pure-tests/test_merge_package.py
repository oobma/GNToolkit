# -*- coding: utf-8 -*-
"""Pure-Python tests for merge_package_dicts (whole package files) — no bpy.

Covers the F4 acceptance battery of the merge v1 spec (2026-10-16):
per-group merge inside the package, the modifiers task list merged per
task with per-key input decisions (local overrides rebase onto upstream),
materials/worlds and unknown fields with the one-side adoption rule,
delete/modify and both-added rules, determinism, no input mutation and
legacy standalone wrapping.

Usage: python tests\\test_merge_package.py
"""

import json
import os
import sys
from copy import deepcopy

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


merge = gnt_check._load_merge()


# ---------------------------------------------------------------------------
# Builders (group entries reuse the shape of the value battery)
# ---------------------------------------------------------------------------

def _sock(name, value, stype="VALUE"):
    return {"name": name, "identifier": name, "type": stype,
            "bl_idname": "NodeSocketFloat", "hide": False,
            "default_value": value}


def _math_node(name, value=1.0, label="", operation="ADD"):
    return {"name": name, "type": "GeometryNodeMath", "label": label,
            "location": [0.0, 0.0], "parent": None,
            "inputs": [_sock("Value", value)], "outputs": [],
            "properties": {"operation": operation, "use_clamp": False},
            "zone_items": [], "zone_paired_node": None,
            "menu_items_data": []}


def _entry(value=1.0, label="", nodes=None):
    return {"name": "TestGroup", "inputs": [], "outputs": [],
            "nodes": nodes or [_math_node("Math", value, label=label)],
            "links": [], "interface_items": [],
            "tree_properties": {}}


def _task(obj="Cube", mod="GN", group="G", order=0, inputs=None,
          viewport=True, render=True, bake=None):
    task = {"object": obj, "modifier_name": mod, "node_group": group,
            "order": order, "show_viewport": viewport, "show_render": render,
            "inputs": dict(inputs if inputs is not None else {"S1": 1.0})}
    if bake is not None:
        task["bake"] = dict(bake)
    return task


def _pkg(groups=None, modifiers=None, materials=None, extra=None):
    pkg = {"version": "0.4.0", "type": "GN_UNIFIED_PACKAGE",
           "export_method": "PACKAGE_EXPORT"}
    if groups is not None:
        pkg["node_groups"] = groups
    if modifiers is not None:
        pkg["modifiers"] = modifiers
    if materials is not None:
        pkg["materials"] = materials
    if extra:
        pkg.update(extra)
    return pkg


base_entry = _entry(1.0)
ours_entry = _entry(2.0)
theirs_entry = _entry(1.0, label="remote")

# --- A. node_groups through the per-group core ------------------------------
try:
    base = _pkg(groups={"G1": deepcopy(base_entry)})
    ours = _pkg(groups={"G1": deepcopy(ours_entry), "Added": _entry(5.0)})
    theirs = _pkg(groups={"G1": deepcopy(theirs_entry)})
    result = merge.merge_package_dicts(base, ours, theirs)
    merged = result["merged"]
    math = merged["node_groups"]["G1"]["nodes"][0]
    check("A1: clean group merge keeps the local value + applies the label",
          result["ok"]
          and math["inputs"][0]["default_value"] == 2.0
          and math["label"] == "remote"
          and "Added" in merged["node_groups"],
          str(result["summary"]))

    ours2 = _pkg(groups={"G1": _entry(3.0)})
    theirs2 = _pkg(groups={"G1": _entry(4.0)})
    result2 = merge.merge_package_dicts(base, ours2, theirs2)
    check("A2: same value on both sides -> file conflict, no merged",
          not result2["ok"] and result2["merged"] is None
          and any("group 'G1'" in c["item"]
                  for c in result2["conflicts"]),
          str(result2["conflicts"]))
except Exception as exc:
    check("A: node_groups", False, f"-> {exc!r}")

# --- B. shader groups: per-item values --------------------------------------
try:
    def _shader(value, other=1.0):
        entry = _entry(nodes=[_math_node("Math", value),
                              _math_node("Math2", other)])
        entry["tree_type"] = "ShaderNodeTree"
        return entry

    base = _pkg(groups={"S": _shader(1.0, 1.0)})
    ours = _pkg(groups={"S": _shader(2.0, 1.0)})
    theirs = _pkg(groups={"S": _shader(1.0, 3.0)})
    result = merge.merge_package_dicts(base, ours, theirs)
    check("B1: shader disjoint values merge per item",
          result["ok"]
          and result["merged"]["node_groups"]["S"]["nodes"][0]["inputs"][0]
          ["default_value"] == 2.0
          and result["merged"]["node_groups"]["S"]["nodes"][1]["inputs"][0]
          ["default_value"] == 3.0)

    theirs_b = _pkg(groups={"S": _shader(3.0, 1.0)})
    result_b = merge.merge_package_dicts(base, ours, theirs_b)
    check("B2: shader same value changed on both sides conflicts",
          not result_b["ok"] and result_b["merged"] is None
          and any(c["kind"] == "value" and "group 'S'" in c["item"]
                  for c in result_b["conflicts"]),
          str(result_b["conflicts"]))

    def _compositor(value):
        entry = _entry(value)
        entry["tree_type"] = "CompositorNodeTree"
        return entry

    base_c = _pkg(groups={"C": _compositor(1.0)})
    ours_c = _pkg(groups={"C": _compositor(2.0)})
    result_c1 = merge.merge_package_dicts(base_c, ours_c, base_c)
    check("B3: a non-mergeable type changed on one side is adopted",
          result_c1["ok"]
          and result_c1["merged"]["node_groups"]["C"]["nodes"][0]
          ["inputs"][0]["default_value"] == 2.0)

    theirs_c = _pkg(groups={"C": _compositor(3.0)})
    result_c2 = merge.merge_package_dicts(base_c, ours_c, theirs_c)
    check("B4: a non-mergeable type changed on both sides conflicts",
          not result_c2["ok"] and result_c2["merged"] is None
          and any("not a Geometry Nodes or Shader tree" in n
                  for n in result_c2["notices"]),
          str(result_c2["conflicts"]))
except Exception as exc:
    check("B: shader groups", False, f"-> {exc!r}")

# --- C. modifiers: per-task, per-input --------------------------------------
try:
    base = _pkg(modifiers=[_task(inputs={"S1": 1.0, "S2": 2.0})])
    ours = _pkg(modifiers=[_task(inputs={"S1": 9.0, "S2": 2.0})])
    theirs = _pkg(modifiers=[_task(inputs={"S1": 1.0, "S2": 7.0})])
    result = merge.merge_package_dicts(base, ours, theirs)
    inputs = result["merged"]["modifiers"][0]["inputs"]
    check("C1: disjoint input edits on the same modifier both survive",
          result["ok"] and inputs["S1"] == 9.0 and inputs["S2"] == 7.0,
          str(inputs))

    ours_c = _pkg(modifiers=[_task(inputs={"S1": 9.0})])
    theirs_c = _pkg(modifiers=[_task(inputs={"S1": 3.0})])
    result_c = merge.merge_package_dicts(base, ours_c, theirs_c)
    check("C2: same input changed differently on both sides conflicts",
          not result_c["ok"] and result_c["merged"] is None
          and any(c["kind"] == "value" and "'S1'" in c["item"]
                  for c in result_c["conflicts"]),
          str(result_c["conflicts"]))

    base_st = _pkg(modifiers=[_task(group="G", order=0)])
    ours_st = _pkg(modifiers=[_task(group="G", order=1)])
    theirs_st = _pkg(modifiers=[_task(group="G", order=0)])
    result_st = merge.merge_package_dicts(base_st, ours_st, theirs_st)
    check("C3: stack settings changed on one side are adopted",
          result_st["ok"]
          and result_st["merged"]["modifiers"][0]["order"] == 1)

    theirs_st2 = _pkg(modifiers=[_task(group="H", order=0)])
    result_st2 = merge.merge_package_dicts(base_st, ours_st, theirs_st2)
    check("C4: stack settings changed on both sides conflict",
          not result_st2["ok"] and result_st2["merged"] is None
          and any(c["kind"] == "structure"
                  for c in result_st2["conflicts"]))
except Exception as exc:
    check("C: modifier inputs", False, f"-> {exc!r}")

# --- D. modifiers: add / delete-modify / bakes ------------------------------
try:
    base = _pkg(modifiers=[_task()])
    ours_d = _pkg(modifiers=[_task(), _task(obj="Sphere", mod="New")])
    theirs_d = _pkg(modifiers=[_task()])
    result_d = merge.merge_package_dicts(base, ours_d, theirs_d)
    keys = [(t["object"], t["modifier_name"])
            for t in result_d["merged"]["modifiers"]]
    check("D1: a task added on ours only is kept",
          result_d["ok"] and ("Sphere", "New") in keys, str(keys))

    ours_d2 = _pkg(modifiers=[])
    theirs_d2 = _pkg(modifiers=[_task(inputs={"S1": 5.0})])
    result_d2 = merge.merge_package_dicts(base, ours_d2, theirs_d2)
    check("D2: deleted on ours + modified on theirs conflicts",
          not result_d2["ok"]
          and any(c["kind"] == "delete/modify"
                  for c in result_d2["conflicts"]),
          str(result_d2["conflicts"]))

    base_d3 = _pkg(modifiers=[_task(bake={"bake_mode": "A"})])
    ours_d3 = _pkg(modifiers=[_task(bake={"bake_mode": "B"})])
    theirs_d3 = _pkg(modifiers=[_task(bake={"bake_mode": "A"})])
    result_d3 = merge.merge_package_dicts(base_d3, ours_d3, theirs_d3)
    check("D3: bake metadata rides the winning task",
          result_d3["ok"]
          and result_d3["merged"]["modifiers"][0]["bake"]["bake_mode"] == "B")
except Exception as exc:
    check("D: add/delete/bakes", False, f"-> {exc!r}")

# --- E. sections (materials) per entry --------------------------------------
try:
    base = _pkg(materials={"M": _entry(1.0)})
    ours = _pkg(materials={"M": _entry(2.0)})
    theirs = _pkg(materials={"M": _entry(1.0)})
    result = merge.merge_package_dicts(base, ours, theirs)
    check("E1: material changed on ours only is adopted",
          result["ok"]
          and result["merged"]["materials"]["M"]["nodes"][0]["inputs"][0]
          ["default_value"] == 2.0)

    theirs_e = _pkg(materials={"M": _entry(3.0)})
    result_e = merge.merge_package_dicts(base, ours, theirs_e)
    check("E2: material changed on both sides differently conflicts",
          not result_e["ok"] and result_e["merged"] is None
          and any("materials 'M'" in c["item"]
                  for c in result_e["conflicts"]),
          str(result_e["conflicts"]))

    ours_e3 = _pkg(materials={"M": _entry(1.0), "N": _entry(9.0)})
    theirs_e3 = _pkg(materials={"M": _entry(1.0), "N": _entry(9.0)})
    result_e3 = merge.merge_package_dicts(base, ours_e3, theirs_e3)
    check("E3: an entry added identically on both sides is kept once",
          result_e3["ok"] and "N" in result_e3["merged"]["materials"])
except Exception as exc:
    check("E: sections", False, f"-> {exc!r}")

# --- F. unknown fields, header and standalone wrapping ----------------------
try:
    base = _pkg(extra={"custom_meta": "a"}, modifiers=[])
    ours = _pkg(extra={"custom_meta": "b"}, modifiers=[])
    theirs = _pkg(extra={"custom_meta": "a"}, modifiers=[])
    result = merge.merge_package_dicts(base, ours, theirs)
    check("F1: unknown field changed on ours only is adopted",
          result["ok"] and result["merged"]["custom_meta"] == "b")

    theirs_f = _pkg(extra={"custom_meta": "c"}, modifiers=[])
    result_f = merge.merge_package_dicts(base, ours, theirs_f)
    check("F2: unknown field changed on both sides conflicts",
          not result_f["ok"] and result_f["merged"] is None)

    ours_f3 = _pkg(modifiers=[])
    theirs_f3 = _pkg(modifiers=[])
    ours_f3.pop("custom_meta", None)
    theirs_f3.pop("custom_meta", None)
    result_f3 = merge.merge_package_dicts(base, ours_f3, theirs_f3)
    check("F3: field deleted on both sides disappears from the result",
          result_f3["ok"] and "custom_meta" not in result_f3["merged"])

    standalone = _entry(1.0)
    standalone["name"] = "Solo"
    result_f4 = merge.merge_package_dicts(
        {"name": "Solo", "nodes": standalone["nodes"], "links": [],
         "inputs": [], "outputs": [], "tree_properties": {},
         "interface_items": []},
        _pkg(groups={"Solo": _entry(2.0)}),
        _pkg(groups={"Solo": _entry(1.0, label="remote")}))
    solo = result_f4["merged"]["node_groups"]["Solo"]["nodes"][0]
    check("F4: legacy standalone files are wrapped as a package",
          result_f4["ok"] and solo["inputs"][0]["default_value"] == 2.0
          and solo["label"] == "remote")

    base_t = _pkg(modifiers=[])
    ours_t = {"version": "0.4.0", "type": "OTHER", "modifiers": []}
    result_t = merge.merge_package_dicts(base_t, ours_t, base_t)
    check("F5: file type mismatch conflicts",
          not result_t["ok"]
          and any(c["kind"] == "identity" for c in result_t["conflicts"]))
except Exception as exc:
    check("F: fields and wrapping", False, f"-> {exc!r}")

# --- G. noise, determinism and input mutation -------------------------------
try:
    base = _pkg(modifiers=[_task(inputs={"S1": 0.0})])
    ours = _pkg(modifiers=[_task(inputs={"S1": -0.0})])
    theirs = _pkg(modifiers=[_task(inputs={"S1": 1e-9})])
    result = merge.merge_package_dicts(base, ours, theirs)
    check("G1: -0.0 / 0.0 / 1e-9 input noise produces no conflict",
          result["ok"], str(result["conflicts"]))

    base = _pkg(groups={"G1": _entry(1.0)}, modifiers=[_task()])
    ours = _pkg(groups={"G1": _entry(2.0)}, modifiers=[_task()])
    theirs = _pkg(groups={"G1": _entry(1.0, label="remote")},
                  modifiers=[_task()])
    snap = json.dumps([base, ours, theirs], sort_keys=True)
    first = merge.merge_package_dicts(base, ours, theirs)
    second = merge.merge_package_dicts(base, ours, theirs)
    check("G2: deterministic output and untouched inputs",
          json.dumps(first["merged"], sort_keys=True)
          == json.dumps(second["merged"], sort_keys=True)
          and json.dumps([base, ours, theirs], sort_keys=True) == snap)
except Exception as exc:
    check("G: noise/determinism", False, f"-> {exc!r}")

# --- H. exact-content renames (v2) ------------------------------------------
try:
    base = _pkg(groups={"G1": _entry(1.0)})
    ours = _pkg(groups={"G2": _entry(1.0)})
    theirs = _pkg(groups={"G1": _entry(1.0, label="remote")})
    result = merge.merge_package_dicts(base, ours, theirs)
    merged_groups = result["merged"]["node_groups"]
    check("H1: renamed group merges under the new name (star case)",
          result["ok"] and "G2" in merged_groups
          and "G1" not in merged_groups
          and merged_groups["G2"]["nodes"][0]["label"] == "remote"
          and merged_groups["G2"]["nodes"][0]["inputs"][0]["default_value"]
          == 1.0
          and result["summary"]["renamed"] == 1
          and result["summary"]["removed"] == 1,
          str(result["summary"]))

    ours_h2 = _pkg(groups={"G2": _entry(1.0)})
    theirs_h2 = _pkg(groups={"G3": _entry(1.0)})
    result_h2 = merge.merge_package_dicts(base, ours_h2, theirs_h2)
    check("H2: same group renamed to two names is a file-level conflict",
          not result_h2["ok"] and result_h2["merged"] is None
          and any(c["kind"] == "rename/rename"
                  for c in result_h2["conflicts"]),
          str(result_h2["conflicts"]))
except Exception as exc:
    check("H: renames", False, f"-> {exc!r}")

# --- I. file-level rename pairing (v2, F4b) ---------------------------------
try:
    old_file = _entry(1.0)
    new_file = _entry(1.0)
    new_file["name"] = "Renamed"
    pairs = merge.pair_renamed_files(
        {"NodeGroups/Old.json": old_file}, {"NodeGroups/New.json": new_file})
    check("I1: exact-content file rename pairs up",
          pairs == {"NodeGroups/Old.json": "NodeGroups/New.json"},
          str(pairs))

    pairs = merge.pair_renamed_files(
        {"NodeGroups/A.json": deepcopy(old_file),
         "NodeGroups/B.json": deepcopy(old_file)},
        {"NodeGroups/N.json": deepcopy(new_file)})
    check("I2: ambiguous file fingerprints never guess", pairs == {},
          str(pairs))

    shader = _entry(1.0)
    shader["tree_type"] = "ShaderNodeTree"
    pairs = merge.pair_renamed_files(
        {"NodeGroups/S.json": shader},
        {"NodeGroups/T.json": deepcopy(shader)})
    check("I3: shader file renames are paired",
          pairs == {"NodeGroups/S.json": "NodeGroups/T.json"}, str(pairs))

    unsupported = _entry(1.0)
    unsupported["tree_type"] = "CompositorNodeTree"
    pairs = merge.pair_renamed_files(
        {"NodeGroups/C.json": unsupported},
        {"NodeGroups/D.json": deepcopy(unsupported)})
    check("I4: a non-mergeable file type is not paired", pairs == {},
          str(pairs))
except Exception as exc:
    check("I: file renames", False, f"-> {exc!r}")

print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
for name in FAILED:
    print(f"  - {name}")
sys.exit(1 if FAILED else 0)
