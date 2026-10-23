# -*- coding: utf-8 -*-
"""Pure tests for stable node identity (S1, no bpy).

Covers: identity map is hash-neutral; semantic diff ignores it; the merge
carries it independently of the adopted structure; legacy sides without a
map do not erase the other side's identities.

Usage: python pure-tests\\test_node_identity.py
"""

import json
import os
import sys
from copy import deepcopy

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _ROOT)

import gnt_check

merge = gnt_check._load_merge()
hu = gnt_check._load_hash_utils()
sd = gnt_check._load_semantic_diff()

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("[PASS] %s %s" % (name, detail))
    else:
        FAILED.append(name)
        print("[FAIL] %s %s" % (name, detail))


def node(name, value=1.0):
    return {
        "name": name,
        "type": "ShaderNodeMath",
        "label": "",
        "location": [0.0, 0.0],
        "parent": None,
        "inputs": [
            {"name": "Value", "identifier": "Value", "type": "VALUE",
             "bl_idname": "NodeSocketFloat", "hide": False,
             "default_value": value},
        ],
        "outputs": [
            {"name": "Value", "identifier": "Value", "type": "VALUE",
             "bl_idname": "NodeSocketFloat", "hide": False},
        ],
        "properties": {"operation": "ADD"},
        "zone_items": [],
        "zone_paired_node": None,
        "menu_items_data": [],
    }


def base_entry():
    return {
        "name": "Identity Group",
        "nodes": [node("A"), node("B")],
        "links": [{"from_node": "A", "from_socket_name": "Value",
                   "to_node": "B", "to_socket_name": "Value"}],
        "interface_items": [],
        "tree_properties": {},
    }


def with_map(entry, uids=None):
    out = deepcopy(entry)
    out["node_uids"] = dict(uids or {
        n["name"]: "uid-" + n["name"].lower()
        for n in out["nodes"]})
    return out


def main():
    base = base_entry()
    mapped = with_map(base)

    check("map does not move the hash",
          hu.canonical_hash_from_json_data(base)
          == hu.canonical_hash_from_json_data(mapped))
    check("semantic diff ignores the map",
          sd.compare_groups(base, mapped) is None)

    res = merge.merge_group_entries(mapped, mapped, mapped)
    check("unchanged keeps the map",
          res["status"] == "unchanged"
          and res["merged"].get("node_uids") == mapped["node_uids"])

    ours = with_map(base)
    ours["nodes"][0]["inputs"][0]["default_value"] = 2.5
    res = merge.merge_group_entries(mapped, ours, mapped)
    check("value merge keeps the map",
          res["status"] == "merged"
          and res["merged"].get("node_uids") == mapped["node_uids"])

    theirs = with_map(base)
    theirs["nodes"].append(node("C"))
    theirs["node_uids"]["C"] = "uid-c"
    res = merge.merge_group_entries(mapped, mapped, theirs)
    check("structure adoption keeps both maps",
          res["status"] == "merged"
          and res["merged"].get("node_uids", {}).get("A") == "uid-a"
          and res["merged"].get("node_uids", {}).get("C") == "uid-c")

    legacy = deepcopy(base)
    legacy["nodes"].append(node("C"))
    res = merge.merge_group_entries(mapped, mapped, legacy)
    uids = (res.get("merged") or {}).get("node_uids") or {}
    check("legacy side does not erase ours",
          res["status"] == "merged"
          and uids.get("A") == "uid-a"
          and uids.get("B") == "uid-b"
          and "C" not in uids)

    only_theirs = with_map(base)
    only_theirs["nodes"].append(node("C"))
    only_theirs["node_uids"]["C"] = "uid-c"
    no_map_ours = deepcopy(base)
    no_map_ours["nodes"][0]["inputs"][0]["default_value"] = 4.5
    res = merge.merge_group_entries(base, no_map_ours, only_theirs)
    uids = (res.get("merged") or {}).get("node_uids") or {}
    check("theirs map fills a legacy ours",
          res["status"] == "merged"
          and uids.get("A") == "uid-a"
          and uids.get("B") == "uid-b")

    res = merge.merge_group_entries(mapped, mapped, mapped, overrides={})
    check("overrides path keeps the map",
          res["status"] == "unchanged"
          and res["merged"].get("node_uids") == mapped["node_uids"])

    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  - %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
