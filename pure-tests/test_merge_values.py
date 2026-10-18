# -*- coding: utf-8 -*-
"""Pure-Python tests for merge.py (3-way value merge, v1) — no bpy.

Covers the acceptance battery of the merge v1 spec (2026-10-16): decision
table, structural adoption (P4), interface/socket matching by occurrence,
canonical-noise equivalence, group-level rules, determinism and (when the
tests/582 fixture exists) a real-corpus round.

Usage: python tests\\test_merge_values.py
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
hu = gnt_check._load_hash_utils()


# ---------------------------------------------------------------------------
# Builders
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


def _math_node(name, value=1.0, operation="ADD", use_clamp=False, label=""):
    return _node(name, props={"operation": operation, "use_clamp": use_clamp},
                 inputs=[_sock("Value", value)], label=label)


def _entry(nodes=None, interface=None, links=None, tree_props=None):
    return {"name": "TestGroup", "inputs": [], "outputs": [],
            "nodes": nodes or [], "links": links or [],
            "interface_items": interface or [],
            "tree_properties": dict(tree_props or {})}


def _iface(name, default=1.0, inout="INPUT", stype="NodeSocketFloat",
           props=None, bl_socket=None):
    properties = {"default_value": default}
    if props:
        properties.update(props)
    return {"name": name, "item_type": "SOCKET", "in_out": inout,
            "socket_type": stype, "bl_socket_idname": bl_socket or stype,
            "properties": properties}


def _link(from_node, to_node, from_sock="Result", to_sock="Value"):
    return {"from_node": from_node, "from_socket_id": from_sock,
            "from_socket_name": from_sock, "to_node": to_node,
            "to_socket_id": to_sock, "to_socket_name": to_sock}


def _value_of(result, node_name, sock_index=0):
    for node in result["merged"]["nodes"]:
        if node["name"] == node_name:
            return node["inputs"][sock_index].get("default_value")
    return None


# ---------------------------------------------------------------------------
# 1. Decision table (values only)
# ---------------------------------------------------------------------------

BASE = _entry(nodes=[_math_node("Math")])

r = merge.merge_group_entries(BASE, deepcopy(BASE), deepcopy(BASE))
check("1: identical trio -> unchanged",
      r["status"] == "unchanged" and not r["conflicts"] and not r["applied"])

r = merge.merge_group_entries(BASE, _entry(nodes=[_math_node("Math", value=2.0)]),
                              deepcopy(BASE))
check("2: ours-only value kept, nothing applied",
      r["status"] == "merged" and _value_of(r, "Math") == 2.0
      and not r["applied"] and not r["conflicts"])

r = merge.merge_group_entries(BASE, deepcopy(BASE),
                              _entry(nodes=[_math_node("Math", value=3.0)]))
check("3: theirs-only value applied",
      r["status"] == "merged" and _value_of(r, "Math") == 3.0
      and len(r["applied"]) == 1 and r["applied"][0]["from"] == "theirs")

same = _entry(nodes=[_math_node("Math", value=4.0)])
r = merge.merge_group_entries(BASE, deepcopy(same), deepcopy(same))
check("4: both sides changed identically",
      r["status"] == "merged" and _value_of(r, "Math") == 4.0
      and not r["conflicts"])

r = merge.merge_group_entries(BASE, _entry(nodes=[_math_node("Math", value=2.0)]),
                              _entry(nodes=[_math_node("Math", value=3.0)]))
check("5: both changed differently -> conflict with base/ours/theirs",
      r["status"] == "conflicted" and r["merged"] is None
      and r["conflicts"] and r["conflicts"][0]["kind"] == "value"
      and r["conflicts"][0]["base"] == "1"
      and r["conflicts"][0]["ours"] == "2"
      and r["conflicts"][0]["theirs"] == "3")

r = merge.merge_group_entries(BASE, _entry(nodes=[_math_node("Math", operation="SUBTRACT")]),
                              deepcopy(BASE))
check("6: node property ours-only kept",
      r["status"] == "merged" and r["merged"]["nodes"][0]["properties"]["operation"]
      == "SUBTRACT")

r = merge.merge_group_entries(BASE, deepcopy(BASE),
                              _entry(nodes=[_math_node("Math", label="Hi")]))
check("7: label theirs applied",
      r["status"] == "merged" and r["merged"]["nodes"][0]["label"] == "Hi"
      and r["applied"] and r["applied"][0]["from"] == "theirs")


# ---------------------------------------------------------------------------
# 2. Structural adoption (P4 and friends)
# ---------------------------------------------------------------------------

ours = _entry(nodes=[_math_node("Math"), _math_node("Extra", value=9.0)])
theirs = _entry(nodes=[_math_node("Math", value=5.0)])
r = merge.merge_group_entries(BASE, ours, theirs)
check("8: P4 - ours restructures, theirs value survives",
      r["status"] == "merged" and r["adopted_structure"] == "ours"
      and {n["name"] for n in r["merged"]["nodes"]} == {"Math", "Extra"}
      and _value_of(r, "Math") == 5.0
      and any(a["from"] == "theirs" for a in r["applied"]))

extra = _math_node("Extra", value=9.0)
ours = _entry(nodes=[_math_node("Math", value=2.0), deepcopy(extra)])
theirs = _entry(nodes=[_math_node("Math", use_clamp=True), deepcopy(extra)])
r = merge.merge_group_entries(BASE, ours, theirs)
check("9: both restructured identically + disjoint values",
      r["status"] == "merged" and r["notices"]
      and _value_of(r, "Math") == 2.0
      and r["merged"]["nodes"][0]["properties"]["use_clamp"] is True)

r = merge.merge_group_entries(BASE,
                              _entry(nodes=[_math_node("Math"), _math_node("X")]),
                              _entry(nodes=[_math_node("Math"), _math_node("Y")]))
check("10: both restructured differently -> structural conflict",
      r["status"] == "conflicted" and r["merged"] is None
      and r["conflicts"] and all(c["kind"] == "structure" for c in r["conflicts"])
      and any("only in ours" in c["item"] for c in r["conflicts"])
      and any("only in theirs" in c["item"] for c in r["conflicts"]))

r = merge.merge_group_entries(_entry(nodes=[_math_node("A")]),
                              _entry(nodes=[_math_node("B")]),
                              _entry(nodes=[_math_node("A", value=2.0)]))
check("11: edit on item absent from adopted structure -> explicit conflict",
      r["status"] == "conflicted" and r["merged"] is None
      and any("not present in the adopted structure" in c["item"]
              for c in r["conflicts"]))


# ---------------------------------------------------------------------------
# 3. Interface and socket matching
# ---------------------------------------------------------------------------

base = _entry(interface=[_iface("Radius", 1.0), _iface("Height", 2.0)])
ours = _entry(interface=[_iface("Radius", 5.0), _iface("Height", 2.0)])
theirs = _entry(interface=[_iface("Radius", 1.0, props={"min_value": 0.5}),
                           _iface("Height", 2.0)])
r = merge.merge_group_entries(base, ours, theirs)
radius = next(i for i in r["merged"]["interface_items"]
              if i["name"] == "Radius")
check("12: disjoint interface properties both applied",
      r["status"] == "merged"
      and radius["properties"]["default_value"] == 5.0
      and radius["properties"]["min_value"] == 0.5)

r = merge.merge_group_entries(base,
                              _entry(interface=[_iface("Radius", 5.0),
                                                _iface("Height", 2.0)]),
                              _entry(interface=[_iface("Radius", 7.0),
                                                _iface("Height", 2.0)]))
check("13: same interface property both different -> conflict",
      r["status"] == "conflicted" and r["merged"] is None
      and any(c["kind"] == "value" for c in r["conflicts"]))

typ = _entry(interface=[_iface("Radius", 3, stype="NodeSocketInt",
                               bl_socket="NodeSocketInt"),
                        _iface("Height", 2.0)])
r = merge.merge_group_entries(base, typ,
                              _entry(interface=[_iface("Radius", 2.0),
                                                _iface("Height", 2.0)]))
check("14: interface type change ours + default edit theirs -> conflict",
      r["status"] == "conflicted"
      and any(c["kind"] == "value" for c in r["conflicts"]))

base = _entry(interface=[_iface("R", 1.0), _iface("R", 1.0)])
ours = _entry(interface=[_iface("R", 2.0), _iface("R", 1.0)])
theirs = _entry(interface=[_iface("R", 1.0), _iface("R", 3.0)])
r = merge.merge_group_entries(base, ours, theirs)
check("15: interface homonyms merge to the right slots",
      r["status"] == "merged"
      and [i["properties"]["default_value"]
           for i in r["merged"]["interface_items"]] == [2.0, 3.0])

base = _entry(nodes=[_node("Math", inputs=[_sock("Value", 1.0),
                                           _sock("Value", 1.0)])])
ours = _entry(nodes=[_node("Math", inputs=[_sock("Value", 2.0),
                                           _sock("Value", 1.0)])])
theirs = _entry(nodes=[_node("Math", inputs=[_sock("Value", 1.0),
                                             _sock("Value", 3.0)])])
r = merge.merge_group_entries(base, ours, theirs)
check("16: node socket homonyms merge to the right slots",
      r["status"] == "merged"
      and [s["default_value"] for s in r["merged"]["nodes"][0]["inputs"]]
      == [2.0, 3.0])


# ---------------------------------------------------------------------------
# 4. Canonical noise never conflicts
# ---------------------------------------------------------------------------

base = _entry(nodes=[_math_node("Math", value=0.0)])
r = merge.merge_group_entries(base, _entry(nodes=[_math_node("Math", value=-0.0)]),
                              deepcopy(base))
check("17: -0.0 vs 0.0 -> unchanged", r["status"] == "unchanged")

base = _entry(interface=[_iface("R", 1.0)])
r = merge.merge_group_entries(
    base, _entry(interface=[_iface("R", 1.0, props={"is_panel_toggle": False})]),
    deepcopy(base))
check("18: folded interface default (is_panel_toggle) -> unchanged",
      r["status"] == "unchanged")

r = merge.merge_group_entries(
    base, _entry(interface=[_iface("R", 1.0, props={"default_input": "VALUE"})]),
    _entry(interface=[_iface("R", 1.0, props={"default_input": ""})]))
check("19: folded default_input ('VALUE'/'' vs absent) -> unchanged",
      r["status"] == "unchanged")

variant = _iface("R", 1.0, stype="NodeSocketFloat",
                 bl_socket="NodeSocketFloatAngle")
r = merge.merge_group_entries(
    _entry(interface=[deepcopy(variant)]),
    _entry(interface=[deepcopy(variant)]),
    _entry(interface=[_iface("R", 1.0, props={"subtype": "ANGLE"})]))
check("20: variant class vs base+subtype (all sides) -> unchanged",
      r["status"] == "unchanged")

ours_variant = _entry(interface=[_iface("R", 1.0, stype="NodeSocketFloat",
                                        bl_socket="NodeSocketFloatAngle")])
snapshot = deepcopy(ours_variant)
r = merge.merge_group_entries(
    base, ours_variant,
    _entry(interface=[_iface("R", 1.0, props={"subtype": "ANGLE"})]))
check("20b: both sides agree on the variant, base differs -> merged clean",
      r["status"] == "merged" and not r["conflicts"])
check("20c: merging does not modify its inputs",
      ours_variant == snapshot)

ref_sock = {"name": "Image", "identifier": "Image", "type": "IMAGE",
            "bl_idname": "NodeSocketImage", "hide": False,
            "default_value": {"type": "Image", "name": "img.png"}}
plain_sock = {k: v for k, v in ref_sock.items() if k != "default_value"}
r = merge.merge_group_entries(
    _entry(nodes=[_node("Tex", inputs=[deepcopy(ref_sock)])]),
    _entry(nodes=[_node("Tex", inputs=[deepcopy(ref_sock)])]),
    _entry(nodes=[_node("Tex", inputs=[deepcopy(plain_sock)])]))
check("21: datablock-ref default vs absent -> unchanged",
      r["status"] == "unchanged")


# ---------------------------------------------------------------------------
# 5. Edge semantics
# ---------------------------------------------------------------------------

def _conn_entry(with_link, value=1.0):
    return _entry(nodes=[_math_node("A", value=value), _node("B")],
                  links=[_link("B", "A")] if with_link else [])


base = _conn_entry(False)
ours = _conn_entry(True)
theirs = _conn_entry(False, value=2.0)
r = merge.merge_group_entries(base, ours, theirs)
check("22: default edited on a socket the other side connected -> conflict",
      r["status"] == "conflicted"
      and any("not present in the adopted structure" in c["item"]
              for c in r["conflicts"]))

r = merge.merge_group_entries(base, _conn_entry(True), deepcopy(base))
check("23: connection adopted when the other side did not edit",
      r["status"] == "merged"
      and any(l["from_node"] == "B" for l in r["merged"]["links"]))

base = _entry(nodes=[_math_node("Math")])
ours = _entry(nodes=[_math_node("Math")])
ours["nodes"][0]["custom_properties"] = {"note": 1}
theirs = _entry(nodes=[_math_node("Math")])
theirs["nodes"][0]["custom_properties"] = {"tag": 2}
r = merge.merge_group_entries(base, ours, theirs)
check("24: disjoint node custom properties both applied",
      r["status"] == "merged"
      and r["merged"]["nodes"][0]["custom_properties"] == {"note": 1, "tag": 2})

ours = _entry(nodes=[_math_node("Math")])
ours["nodes"][0]["custom_properties"] = {"note": 1}
theirs = _entry(nodes=[_math_node("Math")])
theirs["nodes"][0]["custom_properties"] = {"note": 2}
r = merge.merge_group_entries(base, ours, theirs)
check("25: same node custom property both different -> conflict",
      r["status"] == "conflicted")

ours = _entry(nodes=[_math_node("Math")])
ours["nodes"][0]["custom_properties"] = {"gnt_bookkeeping": 1}
r = merge.merge_group_entries(base, ours, deepcopy(base))
check("26: gnt_* custom properties are invisible -> unchanged",
      r["status"] == "unchanged")

base = _entry(tree_props={"cfg_a": 0, "cfg_b": 0})
ours = _entry(tree_props={"cfg_a": 1, "cfg_b": 0})
theirs = _entry(tree_props={"cfg_a": 0, "cfg_b": 2})
r = merge.merge_group_entries(base, ours, theirs)
check("27: disjoint tree properties both applied",
      r["status"] == "merged"
      and r["merged"]["tree_properties"] == {"cfg_a": 1, "cfg_b": 2})

r = merge.merge_group_entries(
    _entry(tree_props={"cfg": 0}), _entry(tree_props={"cfg": 1}),
    _entry(tree_props={"cfg": 2}))
check("28: same tree property both different -> conflict",
      r["status"] == "conflicted")

r = merge.merge_group_entries(
    _entry(tree_props={"default_group_node_width": 140}),
    _entry(tree_props={"default_group_node_width": 140}),
    _entry(tree_props={"default_group_node_width": 160}))
check("29: folded default vs edited tree property",
      r["status"] == "merged"
      and r["merged"]["tree_properties"]["default_group_node_width"] == 160)

L1 = _link("A", "T")
L2 = _link("B", "T")


def _mi_entry(links, value=1.0):
    return _entry(nodes=[_math_node("Math", value=value), _node("A"),
                         _node("B"), _node("T")],
                  links=links)


base = _mi_entry([L1, L2])
ours = _mi_entry([L2, L1])
r = merge.merge_group_entries(base, ours, _mi_entry([L1, L2], value=5.0))
check("30: multi-input reorder adopted with the other side's value",
      r["status"] == "merged"
      and [l["from_node"] for l in r["merged"]["links"]] == ["B", "A"]
      and _value_of(r, "Math") == 5.0)

r = merge.merge_group_entries(base, _mi_entry([L2, L1]), _mi_entry([L1]))
check("31: both sides changed links differently -> structural conflict",
      r["status"] == "conflicted"
      and any(c["kind"] == "structure" and "links differ" in c["item"]
              for c in r["conflicts"]))

base = _entry(nodes=[_math_node("Math")])
ours = deepcopy(base)
ours["has_animation"] = True
ours["bakes"] = [{"mode": "X"}]
r = merge.merge_group_entries(base, ours, deepcopy(base))
check("32: non-canonical fields travel with the adopted side, no conflict",
      r["status"] == "unchanged" and r["merged"].get("has_animation") is True
      and "bakes" in r["merged"])


def _legacy(default=1.0):
    return {"name": "Legacy", "nodes": [], "links": [],
            "inputs": [{"name": "In", "identifier": "In", "type": "VALUE",
                        "bl_idname": "NodeSocketFloat",
                        "default_value": default}],
            "outputs": []}


r = merge.merge_group_entries(_legacy(1.0), _legacy(1.0), _legacy(3.0))
check("33: legacy inputs/outputs format merges and transplants",
      r["status"] == "merged"
      and r["merged"]["inputs"][0]["default_value"] == 3.0)


# ---------------------------------------------------------------------------
# 6. Group-map rules
# ---------------------------------------------------------------------------

E = _entry(nodes=[_math_node("Math")])

r = merge.merge_group_maps({}, {"G": deepcopy(E)}, {})
check("34: group added on ours only -> added",
      r["groups"]["G"]["status"] == "added"
      and r["groups"]["G"]["merged"] == E and r["groups"]["G"]["merged_hash"]
      and r["ok"])

r = merge.merge_group_maps({"G": deepcopy(E)}, {}, {})
check("35: group removed on both sides -> removed",
      r["groups"]["G"]["status"] == "removed"
      and r["groups"]["G"]["merged"] is None and r["ok"])

r = merge.merge_group_maps(
    {"G": deepcopy(E)},
    {"G": _entry(nodes=[_math_node("Math", value=2.0)])}, {})
check("36: deleted one side, modified the other -> delete/modify conflict",
      r["groups"]["G"]["status"] == "conflicted"
      and r["groups"]["G"]["conflicts"][0]["kind"] == "delete/modify"
      and not r["ok"])

r = merge.merge_group_maps({"G": deepcopy(E)}, {"G": deepcopy(E)}, {})
check("37: deleted one side, untouched the other -> removed",
      r["groups"]["G"]["status"] == "removed" and r["ok"])

r = merge.merge_group_maps({}, {"G": deepcopy(E)}, {"G": deepcopy(E)})
check("38: added on both sides with identical content -> added",
      r["groups"]["G"]["status"] == "added"
      and r["groups"]["G"]["notices"])

r = merge.merge_group_maps(
    {}, {"G": _entry(nodes=[_math_node("Math", value=2.0)])},
    {"G": _entry(nodes=[_math_node("Math", value=3.0)])})
check("39: added on both sides differently -> both-added conflict",
      r["groups"]["G"]["status"] == "conflicted"
      and r["groups"]["G"]["conflicts"][0]["kind"] == "both-added"
      and not r["ok"])


def _shader(value):
    return {"name": "S", "tree_type": "ShaderNodeTree",
            "nodes": [_math_node("Math", value=value)], "links": [],
            "interface_items": [], "tree_properties": {},
            "inputs": [], "outputs": []}


r = merge.merge_group_maps({"S": _shader(1)}, {"S": _shader(2)},
                           {"S": _shader(3)})
check("40: non-GN three-way -> unsupported notice, not a failure",
      r["groups"]["S"]["status"] == "unsupported"
      and r["groups"]["S"]["merged"] is None
      and r["groups"]["S"]["notices"] and r["ok"])

ours_shader = deepcopy(E)
ours_shader["tree_type"] = "ShaderNodeTree"
r = merge.merge_group_maps({"G": deepcopy(E)}, {"G": ours_shader},
                           {"G": deepcopy(E)})
check("41: tree type mismatch -> identity conflict, never fused",
      r["groups"]["G"]["status"] == "conflicted"
      and r["groups"]["G"]["conflicts"][0]["kind"] == "identity"
      and not r["ok"])

r = merge.merge_group_maps({"Old": deepcopy(E)}, {"New": deepcopy(E)},
                           {"New2": deepcopy(E)})
check("42: same group renamed to two names -> rename/rename conflict",
      r["groups"]["Old"]["status"] == "conflicted"
      and r["groups"]["Old"]["conflicts"][0]["kind"] == "rename/rename"
      and "New" not in r["groups"] and "New2" not in r["groups"]
      and not r["ok"])

maps = ({"G": deepcopy(E)},
        {"G": _entry(nodes=[_math_node("Math", value=2.0)]),
         "H": deepcopy(E)},
        {"G": _entry(nodes=[_math_node("Math", value=3.0)]),
         "H2": deepcopy(E)})
maps_snapshot = deepcopy(maps)
r1 = merge.merge_group_maps(*maps)
r2 = merge.merge_group_maps(*maps)
check("43: deterministic report across runs, inputs untouched",
      json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
      and maps == maps_snapshot)


# ---------------------------------------------------------------------------
# 7. Real corpus (tests/582) — conditional
# ---------------------------------------------------------------------------

corpus_dir = os.path.join(_THIS_DIR, "582", "NodeGroups")
if not os.path.isdir(corpus_dir):
    # Public checkout: the 582 fixture stays local — use the private tests/
    # copy when present, and skip this round otherwise.
    corpus_dir = os.path.join(_ROOT, "tests", "582", "NodeGroups")
if os.path.isdir(corpus_dir):
    sd = gnt_check._load_semantic_diff()
    groups = sd.load_groups(corpus_dir)
    picked = 0
    for name in sorted(groups):
        if picked >= 5:
            break
        base_g = deepcopy(groups[name])
        targets = []
        for ni, node in enumerate(base_g.get("nodes", [])):
            if not isinstance(node, dict):
                continue
            for si, sock in enumerate(node.get("inputs", [])):
                if (isinstance(sock, dict)
                        and isinstance(sock.get("default_value"), (int, float))
                        and not isinstance(sock.get("default_value"), bool)):
                    targets.append((ni, si))
        if len(targets) < 2:
            continue
        ours_g = deepcopy(base_g)
        theirs_g = deepcopy(base_g)
        ni, si = targets[0]
        ours_g["nodes"][ni]["inputs"][si]["default_value"] += 1.5
        ni, si = targets[1]
        theirs_g["nodes"][ni]["inputs"][si]["default_value"] += 2.5
        if len({hu.canonical_hash_from_json_data(e)
                for e in (base_g, ours_g, theirs_g)}) < 3:
            continue
        r = merge.merge_group_entries(base_g, ours_g, theirs_g, name=name)
        r2 = merge.merge_group_entries(deepcopy(base_g), deepcopy(ours_g),
                                       deepcopy(theirs_g), name=name)
        picked += 1
        check(f"44.{picked}: corpus '{name}' merges deterministically",
              r["status"] == "merged" and r["applied"]
              and r["merged_hash"] == r2["merged_hash"])
    check("44: corpus sample merged", picked >= 1, f"({picked} groups)")
else:
    print("[SKIP] 44: tests/582 corpus not present")


# ---------------------------------------------------------------------------
# 8. Exact-content renames (v2)
# ---------------------------------------------------------------------------

E1 = _entry(nodes=[_math_node("Math", value=1.0)])

# Pure rename on ours, theirs untouched.
r = merge.merge_group_maps({"A": deepcopy(E1)}, {"B": deepcopy(E1)},
                           {"A": deepcopy(E1)})
check("45: exact-content rename detected; old removed, new renamed",
      r["ok"] and r["groups"]["B"]["status"] == "renamed"
      and r["groups"]["A"]["status"] == "removed"
      and r["groups"]["B"]["notices"]
      and r["groups"]["B"]["merged"] is not None)

# Rename on ours + value edit on theirs: the star case.
r = merge.merge_group_maps(
    {"A": deepcopy(E1)}, {"B": deepcopy(E1)},
    {"A": _entry(nodes=[_math_node("Math", value=5.0)])})
check("46: rename + remote value edit merges cleanly under the new name",
      r["ok"] and r["groups"]["B"]["status"] == "renamed"
      and _value_of(r["groups"]["B"], "Math") == 5.0
      and any(item["from"] == "theirs"
              for item in r["groups"]["B"]["applied"]))

# Mirror: rename on theirs + value edit on ours.
r = merge.merge_group_maps(
    {"A": deepcopy(E1)},
    {"A": _entry(nodes=[_math_node("Math", value=7.0)])},
    {"B": deepcopy(E1)})
check("47: theirs-rename + local value edit keeps the local value",
      r["ok"] and r["groups"]["B"]["status"] == "renamed"
      and _value_of(r["groups"]["B"], "Math") == 7.0)

# Rename on ours while theirs removed the old name.
r = merge.merge_group_maps({"A": deepcopy(E1)}, {"B": deepcopy(E1)}, {})
check("48: rename + delete on the other side keeps the rename",
      r["ok"] and r["groups"]["B"]["status"] == "renamed"
      and r["groups"]["A"]["status"] == "removed")

# Ambiguous content (two identical base groups, one addition): no pair.
ambiguous = _entry(nodes=[_math_node("Math", value=2.0)])
r = merge.merge_group_maps(
    {"A1": deepcopy(ambiguous), "A2": deepcopy(ambiguous)},
    {"B": deepcopy(ambiguous)},
    {"A1": _entry(nodes=[_math_node("Math", value=9.0)]),
     "A2": deepcopy(ambiguous)})
check("49: ambiguous fingerprints are never guessed",
      not r["ok"] and r["groups"]["B"]["status"] == "added"
      and r["groups"]["A1"]["status"] == "conflicted"
      and r["groups"]["A1"]["conflicts"][0]["kind"] == "delete/modify")

# Same-side rename+edit: fingerprint differs, so no pairing (v1 result).
r = merge.merge_group_maps(
    {"A": deepcopy(E1)},
    {"B": _entry(nodes=[_math_node("Math", value=3.0)])},
    {"A": _entry(nodes=[_math_node("Math", value=8.0)])})
check("50: rename that also edited values on the same side is not paired",
      not r["ok"] and r["groups"]["A"]["status"] == "conflicted"
      and r["groups"]["A"]["conflicts"][0]["kind"] == "delete/modify"
      and r["groups"]["B"]["status"] == "added")

# Non-GN entries stay out of rename detection (GN-only scope).
def _shader_entry(value):
    shader = _entry(nodes=[_math_node("Math", value=value)])
    shader["tree_type"] = "ShaderNodeTree"
    return shader


r = merge.merge_group_maps(
    {"S": _shader_entry(1.0)}, {"S2": _shader_entry(1.0)},
    {"S": _shader_entry(5.0)})
check("51: non-GN renames are not detected (delete/modify stands)",
      not r["ok"] and r["groups"]["S"]["status"] == "conflicted"
      and all(g["status"] != "renamed" for g in r["groups"].values()))

# Residual notice: only unmatched adds/removes trigger it.
r = merge.merge_group_maps(
    {"A": deepcopy(E1), "X": _entry(nodes=[_math_node("Math", value=9.0)])},
    {"B": deepcopy(E1), "C": _entry(nodes=[_math_node("Math", value=4.0)])},
    {"A": deepcopy(E1)})
check("52: matched renames leave no generic notice; leftovers do",
      r["ok"] and r["groups"]["B"]["status"] == "renamed"
      and any("no exact-content match" in n for n in r["notices"]))

# Fully matched renames do not fire the generic notice.
r = merge.merge_group_maps({"A": deepcopy(E1)}, {"B": deepcopy(E1)},
                           {"A": deepcopy(E1)})
check("53: fully matched pairs produce no generic rename notice",
      not any("possible renames" in n for n in r["notices"]))

# Determinism and untouched inputs with rename pairs.
maps = ({"A": deepcopy(E1)}, {"B": deepcopy(E1)},
        {"A": _entry(nodes=[_math_node("Math", value=5.0)])})
snapshot = json.dumps(maps, sort_keys=True)
r1 = merge.merge_group_maps(*maps)
r2 = merge.merge_group_maps(*maps)
check("54: rename reports are deterministic and inputs untouched",
      json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
      and json.dumps(maps, sort_keys=True) == snapshot)

check("55: MERGE_VERSION is 2", merge.MERGE_VERSION == 2)

print()
print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
if FAILED:
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
