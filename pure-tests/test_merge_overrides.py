# -*- coding: utf-8 -*-
"""Pure-Python tests for merge.py manual overrides (editor E1) — no bpy.

Covers the acceptance battery of the editor spec (2026-10-17, D-E1..D-E6):
the byte-identical no-overrides regression, stable value keys, per-item
decisions (ours/theirs/base/custom) across every value family, structural
resolution as one whole side, partial decisions, unplaceable winners and
determinism.

Usage: python tests\\test_merge_overrides.py
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
# Builders (same shapes as test_merge_values.py)
# ---------------------------------------------------------------------------

def _sock(name, value, stype="VALUE"):
    return {"name": name, "identifier": name, "type": stype,
            "bl_idname": "NodeSocketFloat", "hide": False,
            "default_value": value}


def _node(name, props=None, inputs=None, label="", bl_type="GeometryNodeMath"):
    return {"name": name, "type": bl_type, "label": label,
            "location": [0.0, 0.0], "parent": None,
            "inputs": inputs or [], "outputs": [],
            "properties": dict(props or {}),
            "zone_items": [], "zone_paired_node": None,
            "menu_items_data": []}


def _math_node(name, value=1.0, operation="ADD", label=""):
    return _node(name, props={"operation": operation},
                 inputs=[_sock("Value", value)], label=label)


def _entry(nodes=None, interface=None, tree_props=None):
    return {"name": "TestGroup", "inputs": [], "outputs": [],
            "nodes": nodes or [], "links": [],
            "interface_items": interface or [],
            "tree_properties": dict(tree_props or {})}


def _iface(name, default=1.0, inout="INPUT", stype="NodeSocketFloat"):
    return {"name": name, "item_type": "SOCKET", "in_out": inout,
            "socket_type": stype, "bl_socket_idname": stype,
            "properties": {"default_value": default}}


def _value_of(result, node_name, sock_index=0):
    for node in result["merged"]["nodes"]:
        if node["name"] == node_name:
            return node["inputs"][sock_index].get("default_value")
    return None


def _conflict_key(result):
    return result["conflicts"][0]["key"]


# ---------------------------------------------------------------------------
# 1. No-overrides regression (byte-identical)
# ---------------------------------------------------------------------------

CLEAN_BASE = _entry(nodes=[_math_node("Math")])
CLEAN_R = merge.merge_group_entries(
    CLEAN_BASE, deepcopy(CLEAN_BASE),
    _entry(nodes=[_math_node("Math", value=3.0)]))
CLEAN_R2 = merge.merge_group_entries(
    CLEAN_BASE, deepcopy(CLEAN_BASE),
    _entry(nodes=[_math_node("Math", value=3.0)]), overrides=None)
CLEAN_R3 = merge.merge_group_entries(
    CLEAN_BASE, deepcopy(CLEAN_BASE),
    _entry(nodes=[_math_node("Math", value=3.0)]), overrides={})
check("1: a clean merge is unchanged by empty overrides",
      CLEAN_R == CLEAN_R2 == CLEAN_R3
      and CLEAN_R["status"] == "merged")

CONF_BASE = _entry(nodes=[_math_node("Math")])
CONF_OURS = _entry(nodes=[_math_node("Math", value=2.0)])
CONF_THEIRS = _entry(nodes=[_math_node("Math", value=3.0)])
CONF_R = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS)
CONF_R2 = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS,
                                    overrides=None)
CONF_R3 = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS,
                                    overrides={})
check("2: a conflicted merge is unchanged by empty overrides",
      CONF_R == CONF_R2 == CONF_R3
      and CONF_R["status"] == "conflicted" and CONF_R["merged"] is None)

# ---------------------------------------------------------------------------
# 2. Stable keys
# ---------------------------------------------------------------------------

KEY = _conflict_key(CONF_R)
check("3: value conflicts carry a deterministic key",
      isinstance(KEY, str) and KEY == _conflict_key(CONF_R2)
      and KEY == merge.value_key_id(("sockdef", "Math", "inputs", "Value",
                                     "VALUE", 0)),
      str(KEY))

STRUCT_R = merge.merge_group_entries(
    CONF_BASE,
    _entry(nodes=[_math_node("Math"), _math_node("X")]),
    _entry(nodes=[_math_node("Math"), _math_node("Y")]))
check("4: structure conflicts keep their old shape (no key field)",
      STRUCT_R["status"] == "conflicted"
      and all(c["kind"] == "structure" and "key" not in c
              for c in STRUCT_R["conflicts"]))

# ---------------------------------------------------------------------------
# 3. Value decisions per family
# ---------------------------------------------------------------------------

r = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS,
                              overrides={"values": {KEY: "ours"}})
check("5: sockdef -> ours",
      r["status"] == "merged" and _value_of(r, "Math") == 2.0
      and not r["conflicts"] and r["applied"][0]["from"] == "ours",
      str(r["applied"]))

r = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS,
                              overrides={"values": {KEY: "theirs"}})
check("6: sockdef -> theirs",
      r["status"] == "merged" and _value_of(r, "Math") == 3.0
      and r["applied"][0]["from"] == "theirs")

r = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS,
                              overrides={"values": {KEY: "base"}})
check("7: sockdef -> base",
      r["status"] == "merged" and _value_of(r, "Math") == 1.0
      and r["applied"][0]["from"] == "base")

r = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS,
                              overrides={"values": {KEY: {"custom": 8.0}}})
check("8: sockdef -> custom scalar",
      r["status"] == "merged" and _value_of(r, "Math") == 8.0
      and r["applied"][0]["from"] == "custom")

BASE_L = _entry(nodes=[_math_node("Math", label="A")])
OURS_L = _entry(nodes=[_math_node("Math", label="B")])
THEIRS_L = _entry(nodes=[_math_node("Math", label="C")])
R_L = merge.merge_group_entries(BASE_L, OURS_L, THEIRS_L)
r = merge.merge_group_entries(BASE_L, OURS_L, THEIRS_L,
                              overrides={"values": {
                                  _conflict_key(R_L): {"custom": "D"}}})
check("9: label -> custom string",
      r["status"] == "merged" and r["merged"]["nodes"][0]["label"] == "D")

BASE_P = _entry(nodes=[_math_node("Math", operation="ADD")])
OURS_P = _entry(nodes=[_math_node("Math", operation="SUBTRACT")])
THEIRS_P = _entry(nodes=[_math_node("Math", operation="MULTIPLY")])
R_P = merge.merge_group_entries(BASE_P, OURS_P, THEIRS_P)
r = merge.merge_group_entries(BASE_P, OURS_P, THEIRS_P,
                              overrides={"values": {
                                  _conflict_key(R_P): "theirs"}})
check("10: node property -> theirs",
      r["status"] == "merged"
      and r["merged"]["nodes"][0]["properties"]["operation"] == "MULTIPLY")

BASE_C = _entry(nodes=[_math_node("Math")])
OURS_C = _entry(nodes=[_math_node("Math")])
THEIRS_C = _entry(nodes=[_math_node("Math")])
BASE_C["nodes"][0]["custom_properties"] = {"tag": 1}
OURS_C["nodes"][0]["custom_properties"] = {"tag": 2}
THEIRS_C["nodes"][0]["custom_properties"] = {"tag": 3}
R_C = merge.merge_group_entries(BASE_C, OURS_C, THEIRS_C)
r = merge.merge_group_entries(BASE_C, OURS_C, THEIRS_C,
                              overrides={"values": {
                                  _conflict_key(R_C): {"custom": 9}}})
check("11: node custom property -> custom",
      r["status"] == "merged"
      and r["merged"]["nodes"][0]["custom_properties"]["tag"] == 9)

BASE_I = _entry(interface=[_iface("Size")])
OURS_I = _entry(interface=[_iface("Size", default=2.0)])
THEIRS_I = _entry(interface=[_iface("Size", default=3.0)])
R_I = merge.merge_group_entries(BASE_I, OURS_I, THEIRS_I)
r = merge.merge_group_entries(BASE_I, OURS_I, THEIRS_I,
                              overrides={"values": {
                                  _conflict_key(R_I): "ours"}})
check("12: interface property -> ours",
      r["status"] == "merged"
      and r["merged"]["interface_items"][0]["properties"]["default_value"]
      == 2.0)

BASE_T = _entry(nodes=[_math_node("Math")], tree_props={"cfg": 1})
OURS_T = _entry(nodes=[_math_node("Math")], tree_props={"cfg": 2})
THEIRS_T = _entry(nodes=[_math_node("Math")], tree_props={"cfg": 3})
R_T = merge.merge_group_entries(BASE_T, OURS_T, THEIRS_T)
r = merge.merge_group_entries(BASE_T, OURS_T, THEIRS_T,
                              overrides={"values": {
                                  _conflict_key(R_T): "theirs"}})
check("13: tree property -> theirs",
      r["status"] == "merged" and r["merged"]["tree_properties"]["cfg"] == 3)

# Base absent, both sides added a different default -> base means "remove".
BASE_A = _entry(nodes=[_math_node("Math")])
OURS_A = _entry(nodes=[_math_node("Math")])
THEIRS_A = _entry(nodes=[_math_node("Math")])
OURS_A["nodes"][0]["inputs"].append(_sock("Add", 2.0))
THEIRS_A["nodes"][0]["inputs"].append(_sock("Add", 3.0))
R_A = merge.merge_group_entries(BASE_A, OURS_A, THEIRS_A)
r = merge.merge_group_entries(BASE_A, OURS_A, THEIRS_A,
                              overrides={"values": {
                                  _conflict_key(R_A): "base"}})
added = [s for s in r["merged"]["nodes"][0]["inputs"]
         if s["name"] == "Add"]
check("14: base decision removes a value absent in base",
      r["status"] == "merged" and added
      and "default_value" not in added[0]
      and r["applied"][0]["value"] == "(removed)")

# ---------------------------------------------------------------------------
# 4. Partial decisions
# ---------------------------------------------------------------------------

BASE_M = _entry(nodes=[_math_node("Math", value=1.0, operation="ADD")])
OURS_M = _entry(nodes=[_math_node("Math", value=2.0, operation="SUBTRACT")])
THEIRS_M = _entry(nodes=[_math_node("Math", value=3.0,
                                    operation="MULTIPLY")])
R_M = merge.merge_group_entries(BASE_M, OURS_M, THEIRS_M)
keys = {c["key"]: c["item"] for c in R_M["conflicts"]}
value_key = next(k for k, v in keys.items() if "default" in v)
op_key = next(k for k, v in keys.items() if "property" in v)
r = merge.merge_group_entries(BASE_M, OURS_M, THEIRS_M,
                              overrides={"values": {value_key: "theirs"}})
check("15: partial decisions report only the pending conflicts",
      r["status"] == "conflicted" and [c["key"] for c in r["conflicts"]]
      == [op_key] and r["applied"] and r["applied"][0]["from"] == "theirs")

# Unknown key: ignored, same report as without overrides.
r = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS,
                              overrides={"values": {"nope": "ours"}})
check("16: unknown override keys are ignored",
      r["status"] == "conflicted" and r["conflicts"] == CONF_R["conflicts"])

# Unknown decision: the conflict stays explicit.
r = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS,
                              overrides={"values": {KEY: "bogus"}})
check("17: unknown decisions leave the conflict pending",
      r["status"] == "conflicted" and r["conflicts"][0]["key"] == KEY)

# A winner that cannot be placed on the adopted structure stays a conflict.
BASE_N = _entry(nodes=[_math_node("Math", label="A")])
OURS_N = _entry(nodes=[])
THEIRS_N = _entry(nodes=[_math_node("Math", label="Z")])
R_N = merge.merge_group_entries(BASE_N, OURS_N, THEIRS_N)
r = merge.merge_group_entries(BASE_N, OURS_N, THEIRS_N,
                              overrides={"values": {
                                  _conflict_key(R_N): "theirs"}})
check("18: an unplaceable winner stays an explicit conflict",
      R_N["status"] == "conflicted" and r["status"] == "conflicted"
      and r["merged"] is None
      and "not present in the adopted structure" in r["conflicts"][0]["item"],
      str(r["conflicts"]))

# ---------------------------------------------------------------------------
# 5. Structural decisions
# ---------------------------------------------------------------------------

BASE_S = _entry(nodes=[_math_node("Math")])
OURS_S = _entry(nodes=[_math_node("Math"), _math_node("X")])
THEIRS_S = _entry(nodes=[_math_node("Math"), _math_node("Y")])
R_S = merge.merge_group_entries(BASE_S, OURS_S, THEIRS_S)
r = merge.merge_group_entries(BASE_S, OURS_S, THEIRS_S,
                              overrides={"structure": "theirs"})
check("19: a conflicting structure resolves as one whole side",
      R_S["status"] == "conflicted" and r["status"] == "merged"
      and {n["name"] for n in r["merged"]["nodes"]} == {"Math", "Y"}
      and any("resolved manually" in n for n in r["notices"]))

r = merge.merge_group_entries(BASE_S, OURS_S, THEIRS_S,
                              overrides={"structure": "ours"})
check("20: the other side works too",
      r["status"] == "merged"
      and {n["name"] for n in r["merged"]["nodes"]} == {"Math", "X"})

# Structure override without a structural conflict: ignored.
r = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS,
                              overrides={"structure": "theirs"})
check("21: a structure override without a structure conflict is ignored",
      r == CONF_R)

# ---------------------------------------------------------------------------
# 6. Determinism
# ---------------------------------------------------------------------------

over_a = {"values": {KEY: {"custom": 7.0}}, "structure": "ours"}
over_b = {"structure": "ours", "values": {KEY: {"custom": 7.0}}}
r1 = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS,
                               overrides=deepcopy(over_a))
r2 = merge.merge_group_entries(CONF_BASE, CONF_OURS, CONF_THEIRS,
                               overrides=deepcopy(over_b))
check("22: override order does not change the result",
      r1 == r2 and r1["merged_hash"] == r2["merged_hash"]
      and _value_of(r1, "Math") == 7.0)

print()
print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
if FAILED:
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
