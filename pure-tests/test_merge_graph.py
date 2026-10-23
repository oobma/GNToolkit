# -*- coding: utf-8 -*-
"""Pure tests for the graph matcher and per-entity diff (S2, no bpy).

Usage: python pure-tests\\test_merge_graph.py
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

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("[PASS] %s %s" % (name, detail))
    else:
        FAILED.append(name)
        print("[FAIL] %s %s" % (name, detail))


def node(name, node_type, inputs, outputs, props=None):
    return {
        "name": name,
        "type": node_type,
        "label": "",
        "location": [0.0, 0.0],
        "parent": None,
        "inputs": [
            {"name": sock, "identifier": sock, "type": stype,
             "bl_idname": bl, "hide": False, "default_value": value}
            for sock, stype, bl, value in inputs
        ],
        "outputs": [
            {"name": sock, "identifier": sock, "type": stype,
             "bl_idname": bl, "hide": False}
            for sock, stype, bl in outputs
        ],
        "properties": dict(props or {}),
        "zone_items": [],
        "zone_paired_node": None,
        "menu_items_data": [],
    }


def base_entry():
    return {
        "name": "Graph Group",
        "nodes": [
            node("A", "ShaderNodeMath",
                 [("Value", "VALUE", "NodeSocketFloat", 1.0)],
                 [("Value", "VALUE", "NodeSocketFloat")],
                 {"operation": "ADD"}),
            node("B", "ShaderNodeVectorMath",
                 [("Vector", "VECTOR", "NodeSocketVector", (0.0, 0.0, 0.0))],
                 [("Vector", "VECTOR", "NodeSocketVector")],
                 {"operation": "ADD"}),
            node("C", "ShaderNodeMapRange",
                 [("Value", "VALUE", "NodeSocketFloat", 0.5)],
                 [("Result", "VALUE", "NodeSocketFloat")],
                 {"interpolation_type": "LINEAR"}),
        ],
        "links": [
            {"from_node": "A", "from_socket_name": "Value",
             "to_node": "B", "to_socket_name": "Vector"},
            {"from_node": "B", "from_socket_name": "Vector",
             "to_node": "C", "to_socket_name": "Value"},
        ],
        "interface_items": [],
        "tree_properties": {},
        "node_uids": {"A": "uid-a", "B": "uid-b", "C": "uid-c"},
    }


def rename(entry, old, new):
    node_data = next(n for n in entry["nodes"] if n["name"] == old)
    node_data["name"] = new
    for link in entry["links"]:
        if link["from_node"] == old:
            link["from_node"] = new
        if link["to_node"] == old:
            link["to_node"] = new
    uid = entry.get("node_uids", {}).pop(old, None) if "node_uids" in entry else None
    if uid is not None:
        entry["node_uids"][new] = uid
    return entry


def add_node(entry, name, uid):
    entry["nodes"].append(node(
        name, "ShaderNodeMix",
        [("Factor", "VALUE", "NodeSocketFloat", 0.0)],
        [("Result", "VALUE", "NodeSocketFloat")]))
    entry["node_uids"][name] = uid
    return entry


def add_link(entry, from_node, to_node, from_sock="Result", to_sock="Value"):
    entry["links"].append({"from_node": from_node,
                           "from_socket_name": from_sock,
                           "to_node": to_node,
                           "to_socket_name": to_sock})
    return entry


def graph_merge(base, ours, theirs, overrides=None):
    return merge.merge_group_entries(base, ours, theirs,
                                     overrides=overrides)


def test_graph_merge():
    base = base_entry()

    ours = add_node(deepcopy(base), "D", "uid-d")
    add_link(ours, "C", "D", "Result", "Factor")
    theirs = rename(deepcopy(base), "B", "B2")
    theirs["nodes"][0]["inputs"][0]["default_value"] = 3.5
    res = graph_merge(base, ours, theirs)
    check("S3 disjoint add + rename/value is merged",
          res["status"] == "merged" and not res["conflicts"])
    merged = res.get("merged") or {}
    names = [n["name"] for n in merged.get("nodes", [])]
    check("S3 merged node set", names == ["A", "B2", "C", "D"], str(names))
    check("S3 links follow the rename",
          [(l["from_node"], l["to_node"]) for l in merged.get("links", [])]
          == [("A", "B2"), ("B2", "C"), ("C", "D")])
    check("S3 rename keeps the UID", merged.get("node_uids", {}).get("B2") == "uid-b")
    check("S3 disjoint value survives",
          merged["nodes"][0]["inputs"][0]["default_value"] == 3.5)

    ours = rename(deepcopy(base), "B", "B2")
    theirs = deepcopy(base)
    theirs["nodes"][1]["custom_properties"] = {"note": "x"}
    res = graph_merge(base, ours, theirs)
    merged = res.get("merged") or {}
    b2 = next(n for n in merged.get("nodes", []) if n["name"] == "B2")
    check("S3 rename + same-node value merges",
          res["status"] == "merged" and not res["conflicts"]
          and b2.get("custom_properties") == {"note": "x"})

    ours = deepcopy(base)
    ours["nodes"] = [n for n in ours["nodes"] if n["name"] != "B"]
    ours["links"] = [lk for lk in ours["links"]
                     if lk["from_node"] != "B" and lk["to_node"] != "B"]
    ours["node_uids"].pop("B")
    theirs = deepcopy(base)
    theirs["nodes"][1]["custom_properties"] = {"note": "x"}
    res = graph_merge(base, ours, theirs)
    check("S3 delete vs edit conflicts",
          res["status"] == "conflicted"
          and any(c["kind"] == "node/delete-edit" for c in res["conflicts"]))

    ours = add_node(deepcopy(base), "D", "uid-d")
    theirs = add_node(deepcopy(base), "E", "uid-e")
    add_link(ours, "C", "D", "Result", "Factor")
    add_link(theirs, "A", "E", "Value", "Factor")
    res = graph_merge(base, ours, theirs)
    merged = res.get("merged") or {}
    check("S3 both-add with disjoint links",
          res["status"] == "merged"
          and [n["name"] for n in merged.get("nodes", [])]
          == ["A", "B", "C", "D", "E"]
          and ("C", "D") in [(l["from_node"], l["to_node"])
                             for l in merged.get("links", [])]
          and ("A", "E") in [(l["from_node"], l["to_node"])
                             for l in merged.get("links", [])])

    ours = deepcopy(base)
    ours["links"] = [lk for lk in ours["links"]
                     if not (lk["from_node"] == "A" and lk["to_node"] == "B")]
    theirs = rename(deepcopy(base), "B", "B2")
    res = graph_merge(base, ours, theirs)
    merged = res.get("merged") or {}
    check("S3 link removal + rename merge",
          res["status"] == "merged"
          and [(l["from_node"], l["to_node"])
               for l in merged.get("links", [])] == [("B2", "C")])

    ours = deepcopy(base)
    ours["nodes"] = [n for n in ours["nodes"] if n["name"] != "B"]
    ours["links"] = [lk for lk in ours["links"]
                     if lk["from_node"] != "B" and lk["to_node"] != "B"]
    ours["node_uids"].pop("B")
    theirs = deepcopy(ours)
    res = graph_merge(base, ours, theirs)
    check("S3 both delete the same node",
          res["status"] == "merged"
          and [n["name"] for n in (res.get("merged") or {}).get("nodes", [])]
          == ["A", "C"])

    ours = deepcopy(base)
    ours["interface_items"] = [{
        "name": "Our Out", "in_out": "OUTPUT", "item_type": "SOCKET",
        "description": "", "properties": {"socket_type": "NodeSocketFloat"}}]
    theirs = deepcopy(base)
    theirs["nodes"][0]["inputs"][0]["default_value"] = 3.5
    res = graph_merge(base, ours, theirs)
    merged = res.get("merged") or {}
    check("S3 one-side interface + value merge",
          res["status"] == "merged"
          and merged.get("interface_items", [{}])[0].get("name") == "Our Out"
          and merged["nodes"][0]["inputs"][0]["default_value"] == 3.5)

    ours = rename(deepcopy(base), "B", "B2")
    ours["nodes"][1]["custom_properties"] = {"note": "ours"}
    theirs = rename(deepcopy(base), "B", "B2")
    theirs["nodes"][1]["custom_properties"] = {"note": "theirs"}
    res = graph_merge(base, ours, theirs)
    check("S3 same-node value conflict",
          res["status"] == "conflicted"
          and any(c["kind"] == "value" for c in res["conflicts"]))
    key = res["conflicts"][0]["key"]
    resolved = graph_merge(base, ours, theirs,
                           overrides={"values": {key: "ours"}})
    check("S3 value override resolves the graph conflict",
          resolved["status"] == "merged"
          and next(n for n in resolved["merged"]["nodes"]
                   if n["name"] == "B2").get("custom_properties")
          == {"note": "ours"})

    ours = deepcopy(base)
    ours["nodes"][0]["inputs"][0]["default_value"] = 2.0
    theirs = deepcopy(base)
    theirs["nodes"][0]["inputs"][0]["default_value"] = 3.0
    res = graph_merge(base, ours, theirs)
    check("S3 same value still conflicts",
          res["status"] == "conflicted"
          and any(c["kind"] == "value" for c in res["conflicts"]))

    ours = rename(deepcopy(base), "B", "B2")
    theirs = rename(deepcopy(base), "B", "B3")
    res = graph_merge(base, ours, theirs)
    check("S3 rename/rename conflicts",
          res["status"] == "conflicted"
          and any(c["kind"] == "node/rename-rename" for c in res["conflicts"]))

    ours = add_link(deepcopy(base), "B", "A", "Vector", "Value")
    theirs = add_link(deepcopy(base), "C", "A", "Result", "Value")
    res = graph_merge(base, ours, theirs)
    check("S3 link contention conflicts",
          res["status"] == "conflicted"
          and any(c["kind"] == "link/contention" for c in res["conflicts"]))

    ours = deepcopy(base)
    ours["interface_items"] = [{
        "name": "Our Out", "in_out": "OUTPUT", "item_type": "SOCKET",
        "description": "", "properties": {"socket_type": "NodeSocketFloat"}}]
    theirs = deepcopy(base)
    theirs["interface_items"] = [{
        "name": "Their Out", "in_out": "OUTPUT", "item_type": "SOCKET",
        "description": "", "properties": {"socket_type": "NodeSocketFloat"}}]
    res = graph_merge(base, ours, theirs)
    check("S3 interface divergence conflicts",
          res["status"] == "conflicted"
          and any(c["kind"] == "structure" for c in res["conflicts"]))

    ours = add_node(deepcopy(base), "D", "uid-d")
    res = graph_merge(base, ours, base)
    check("S3 fast-forward matches ours",
          res["status"] == "merged"
          and res["merged_hash"] == hu.canonical_hash_from_json_data(ours))

    ours = add_node(deepcopy(base), "D", "uid-d")
    theirs = rename(deepcopy(base), "B", "B2")
    forward = graph_merge(base, ours, theirs)
    backward = graph_merge(base, theirs, ours)
    check("S3 symmetry of disjoint merges",
          forward["status"] == backward["status"] == "merged"
          and forward["merged_hash"] == backward["merged_hash"])

    embedded = deepcopy(base)
    embedded["owner_settings"] = {"properties": {"metallic": 0.1}}
    ours = add_node(deepcopy(embedded), "D", "uid-d")
    theirs = deepcopy(embedded)
    theirs["owner_settings"]["properties"]["metallic"] = 0.9
    res = graph_merge(embedded, ours, theirs)
    check("S3 embedded owner settings merge with structure",
          res["status"] == "merged"
          and res["merged"]["owner_settings"]["properties"]["metallic"] == 0.9
          and "D" in res["merged"]["node_uids"])

    ours = deepcopy(base)
    ours["nodes"] = [n for n in ours["nodes"] if n["name"] != "B"]
    ours["links"] = [lk for lk in ours["links"]
                     if lk["from_node"] != "B" and lk["to_node"] != "B"]
    ours["node_uids"].pop("B")
    theirs = deepcopy(base)
    theirs["nodes"][1]["custom_properties"] = {"note": "edited"}
    res = graph_merge(base, ours, theirs)
    key = next(c["key"] for c in res["conflicts"]
               if c["kind"] == "node/delete-edit")
    kept = graph_merge(base, ours, theirs,
                       overrides={"entities": {key: "theirs"}})
    removed = graph_merge(base, ours, theirs,
                          overrides={"entities": {key: "ours"}})
    check("S5 delete/edit override keeps the chosen side",
          kept["status"] == "merged"
          and "B" in kept["merged"]["node_uids"]
          and next(n for n in kept["merged"]["nodes"]
                   if n["name"] == "B").get("custom_properties") == {"note": "edited"})
    check("S5 delete/edit override removes on the delete side",
          removed["status"] == "merged"
          and "B" not in removed["merged"]["node_uids"])

    ours = rename(deepcopy(base), "B", "B2")
    theirs = rename(deepcopy(base), "B", "B3")
    res = graph_merge(base, ours, theirs)
    key = next(c["key"] for c in res["conflicts"]
               if c["kind"] == "node/rename-rename")
    kept = graph_merge(base, ours, theirs,
                       overrides={"entities": {key: "ours"}})
    kept_remote = graph_merge(base, ours, theirs,
                              overrides={"entities": {key: "theirs"}})
    check("S5 rename/rename override selects the name",
          kept["status"] == "merged" and "B2" in kept["merged"]["node_uids"]
          and kept_remote["status"] == "merged"
          and "B3" in kept_remote["merged"]["node_uids"])

    ours = add_link(deepcopy(base), "B", "A", "Vector", "Value")
    theirs = add_link(deepcopy(base), "C", "A", "Result", "Value")
    res = graph_merge(base, ours, theirs)
    key = next(c["key"] for c in res["conflicts"]
               if c["kind"] == "link/contention")
    kept = graph_merge(base, ours, theirs,
                       overrides={"entities": {key: "ours"}})
    kept_remote = graph_merge(base, ours, theirs,
                              overrides={"entities": {key: "theirs"}})
    check("S5 link contention override selects the source",
          kept["status"] == "merged"
          and any(l["from_node"] == "B" and l["to_node"] == "A"
                  for l in kept["merged"]["links"])
          and kept_remote["status"] == "merged"
          and any(l["from_node"] == "C" and l["to_node"] == "A"
                  for l in kept_remote["merged"]["links"]))

    ours = add_node(deepcopy(base), "D", "uid-d")
    theirs = add_node(deepcopy(base), "D", "uid-d")
    theirs["nodes"][-1]["label"] = "different"
    res = graph_merge(base, ours, theirs)
    key = next(c["key"] for c in res["conflicts"]
               if c["kind"] == "node/field")
    kept = graph_merge(base, ours, theirs,
                       overrides={"entities": {key: "ours"}})
    check("S5 both-added override selects the content",
          kept["status"] == "merged"
          and next(n for n in kept["merged"]["nodes"]
                   if n["name"] == "D").get("label") == "")

    ours = add_node(deepcopy(base), "D", "uid-d")
    theirs = rename(deepcopy(base), "B", "B2")
    no_uids = [deepcopy(e) for e in (base, ours, theirs)]
    for entry in no_uids:
        entry.pop("node_uids", None)
    res = graph_merge(*no_uids)
    check("S3 legacy without maps keeps whole-side conflict",
          res["status"] == "conflicted"
          and any(c["kind"] == "structure" for c in res["conflicts"]))



def main():
    base = base_entry()

    side = rename(deepcopy(base), "B", "B2")
    matched = merge.match_entry_nodes(base, side)
    check("rename matched by UID", matched["matches"].get("B") == "B2")
    diff = merge.diff_entries(base, side)
    check("rename reported", diff["renamed"].get("B") == "B2")
    check("rename does not touch links", not diff["links_added"]
          and not diff["links_removed"])
    check("rename is a name field", diff["field_keys"]["uid-b"] == {"name"})
    check("UID links are stable", not diff["links_added"]
          and not diff["links_removed"])

    side = deepcopy(base)
    side["nodes"].append(node("D", "ShaderNodeMix",
                              [("Factor", "VALUE", "NodeSocketFloat", 0.0)],
                              [("Result", "VALUE", "NodeSocketFloat")]))
    side["node_uids"]["D"] = "uid-d"
    side["links"].append({"from_node": "C", "from_socket_name": "Result",
                          "to_node": "D", "to_socket_name": "Factor"})
    diff = merge.diff_entries(base, side)
    check("added node reported", diff["added"].get("uid-d") == "D")
    check("added link reported", len(diff["links_added"]) == 1)

    side = deepcopy(base)
    side["nodes"] = [n for n in side["nodes"] if n["name"] != "B"]
    side["links"] = [lk for lk in side["links"]
                     if lk["from_node"] != "B" and lk["to_node"] != "B"]
    side["node_uids"].pop("B")
    diff = merge.diff_entries(base, side)
    check("removed node reported", diff["removed"].get("uid-b") == "B")
    check("removed links reported", len(diff["links_removed"]) == 2)

    side = deepcopy(base)
    side["nodes"][0]["inputs"][0]["default_value"] = 3.5
    diff = merge.diff_entries(base, side)
    check("changed input reported", diff["changed"].get("uid-a") == ["input:Value"])

    side = deepcopy(base)
    side["nodes"][0]["properties"]["operation"] = "MULTIPLY"
    diff = merge.diff_entries(base, side)
    check("changed property reported",
          diff["changed"].get("uid-a") == ["prop:operation"])

    side = deepcopy(base)
    side["nodes"][0]["custom_properties"] = {"note": 1}
    diff = merge.diff_entries(base, side)
    check("changed custom prop reported",
          diff["changed"].get("uid-a") == ["custom:note"])

    side = deepcopy(base)
    dup = deepcopy(side["nodes"][0])
    dup["name"] = "A copy"
    side["nodes"].append(dup)
    side["node_uids"]["A copy"] = "uid-a"
    matched = merge.match_entry_nodes(base, side)
    check("duplicate UID is ambiguous",
          "A copy" in matched["ambiguous_side"])

    no_uids = deepcopy(base)
    no_uids.pop("node_uids")
    side = deepcopy(no_uids)
    rename(side, "B", "B2")
    matched = merge.match_entry_nodes(no_uids, side, use_uids=False)
    check("fallback matches a simple rename", matched["matches"].get("B") == "B2")

    side = deepcopy(no_uids)
    rename(side, "B", "B2")
    next(n for n in side["nodes"] if n["name"] == "B2")[
        "properties"]["operation"] = "SUBTRACT"
    matched = merge.match_entry_nodes(no_uids, side, use_uids=False)
    check("fallback uses topology after a property edit",
          matched["matches"].get("B") == "B2")

    first = merge.diff_entries(base, side)
    second = merge.diff_entries(base, side)
    check("diff is deterministic", first == second)

    test_graph_merge()

    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  - %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
