# -*- coding: utf-8 -*-
"""Pure tests for the graph matcher and per-entity diff (S2, no bpy).

Usage: python pure-tests\\test_merge_graph.py
"""

import json
import os
import random
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

    legacy_base = deepcopy(base)
    legacy_base.pop("node_uids", None)
    res = graph_merge(legacy_base, ours, theirs)
    check("S3 missing base identities are explained",
          res["status"] == "conflicted"
          and any("base has no node identities" in n
                  for n in res.get("notices") or []))



def multi_entry():
    """A base with three links feeding the same (multi-input) socket."""
    base = base_entry()
    add_node(base, "D", "uid-d")
    add_link(base, "A", "D", "Value", "Value")
    add_link(base, "B", "D", "Vector", "Value")
    add_link(base, "C", "D", "Result", "Value")
    return base


def reorder_target(entry, target, names):
    """Put the target's incoming links in *names* order (rest untouched)."""
    rest = [l for l in entry["links"] if l["to_node"] != target]
    by_name = {l["from_node"]: l for l in entry["links"]
               if l["to_node"] == target}
    entry["links"] = rest + [by_name[name] for name in names]
    return entry


def target_order(entry, target="D"):
    return [l["from_node"] for l in entry.get("links", [])
            if l["to_node"] == target]


def test_link_order():
    base = multi_entry()
    target_key = "link:" + repr(("uid-d", "Value"))

    ours = reorder_target(deepcopy(base), "D", ["B", "C", "A"])
    res = graph_merge(base, ours, deepcopy(base))
    merged = res.get("merged") or {}
    check("S6 one-sided link reorder is applied",
          res["status"] == "merged" and not res["conflicts"]
          and target_order(merged) == ["B", "C", "A"],
          str(target_order(merged)))
    check("S6 reorder is reported as applied",
          any("link order" in a.get("item", "")
              for a in res.get("applied") or []))

    res = graph_merge(base, deepcopy(base), ours)
    merged = res.get("merged") or {}
    check("S6 one-sided link reorder (theirs) is applied",
          res["status"] == "merged"
          and target_order(merged) == ["B", "C", "A"])

    theirs = reorder_target(deepcopy(base), "D", ["C", "A", "B"])
    res = graph_merge(base, ours, theirs)
    order_conflicts = [c for c in res.get("conflicts") or []
                       if c.get("kind") == "link/order"]
    check("S6 divergent link reorder conflicts",
          res["status"] == "conflicted" and len(order_conflicts) == 1
          and order_conflicts[0].get("key") == target_key)
    check("S6 order conflict shows the three orders",
          bool(order_conflicts and order_conflicts[0].get("base")
               and order_conflicts[0].get("ours")
               and order_conflicts[0].get("theirs")))

    res = graph_merge(base, ours, deepcopy(ours))
    merged = res.get("merged") or {}
    check("S6 identical reorder on both sides merges",
          res["status"] == "merged"
          and target_order(merged) == ["B", "C", "A"])

    for decision, expected in (("ours", ["B", "C", "A"]),
                               ("theirs", ["C", "A", "B"]),
                               ("base", ["A", "B", "C"])):
        res = graph_merge(base, ours, theirs,
                          overrides={"entities": {target_key: decision}})
        merged = res.get("merged") or {}
        check("S6 order override %s" % decision,
              res["status"] == "merged"
              and target_order(merged) == expected,
              str(target_order(merged)))

    theirs_add = add_node(deepcopy(base), "E", "uid-e")
    add_link(theirs_add, "E", "D", "Result", "Value")
    res = graph_merge(base, ours, theirs_add)
    merged = res.get("merged") or {}
    check("S6 reorder + disjoint add keeps both",
          res["status"] == "merged"
          and target_order(merged) == ["B", "C", "A", "E"],
          str(target_order(merged)))


def _rand_node(name, value):
    return node(name, "ShaderNodeMath",
                [("Value", "VALUE", "NodeSocketFloat", value)],
                [("Value", "VALUE", "NodeSocketFloat")])


def _random_entry(rng, count):
    names = ["N%d" % i for i in range(count)]
    nodes = [_rand_node(name, round(rng.uniform(0.0, 1.0), 3))
             for name in names]
    uids = {name: "uid-%d" % i for i, name in enumerate(names)}
    multi = names[1]
    links = []
    used = set()
    for _ in range(rng.randint(0, count)):
        target = rng.choice(names)
        if target != multi and target in used:
            continue
        source = rng.choice([n for n in names if n != target])
        links.append({"from_node": source, "from_socket_name": "Value",
                      "to_node": target, "to_socket_name": "Value"})
        used.add(target)
    for _ in range(rng.randint(0, 2)):
        source = rng.choice([n for n in names if n != multi])
        if not any(l["from_node"] == source and l["to_node"] == multi
                   for l in links):
            links.append({"from_node": source, "from_socket_name": "Value",
                          "to_node": multi, "to_socket_name": "Value"})
    return {"name": "G", "nodes": nodes, "links": links,
            "interface_items": [], "tree_properties": {},
            "node_uids": uids}, multi


def _mutate_entry(rng, base, multi):
    entry = deepcopy(base)
    for _ in range(rng.randint(0, 3)):
        op = rng.choice(["add", "remove", "rename", "value", "link",
                         "unlink", "reorder"])
        names = [n["name"] for n in entry["nodes"]]
        if op == "add":
            name = "X%d" % rng.randint(0, 100000)
            while name in names:
                name += "_"
            entry["nodes"].append(_rand_node(name, 0.5))
            entry["node_uids"][name] = "uid-" + name
            source = rng.choice(names)
            entry["links"].append({"from_node": source,
                                   "from_socket_name": "Value",
                                   "to_node": name, "to_socket_name": "Value"})
        elif op == "remove" and len(names) > 2:
            candidates = [n for n in names if n != multi]
            if candidates:
                name = rng.choice(candidates)
                entry["nodes"] = [n for n in entry["nodes"]
                                  if n["name"] != name]
                entry["links"] = [l for l in entry["links"]
                                  if l["from_node"] != name
                                  and l["to_node"] != name]
                entry["node_uids"].pop(name, None)
        elif op == "rename" and entry["nodes"]:
            n = rng.choice(entry["nodes"])
            old, new = n["name"], n["name"] + "_r"
            if new in names:
                continue
            n["name"] = new
            for l in entry["links"]:
                if l["from_node"] == old:
                    l["from_node"] = new
                if l["to_node"] == old:
                    l["to_node"] = new
            uid = entry["node_uids"].pop(old, None)
            if uid:
                entry["node_uids"][new] = uid
            if multi == old:
                multi = new
        elif op == "value" and entry["nodes"]:
            rng.choice(entry["nodes"])["inputs"][0]["default_value"] = \
                round(rng.uniform(0.0, 2.0), 3)
        elif op == "link" and len(names) > 1:
            target = rng.choice(names)
            if target != multi:
                entry["links"] = [l for l in entry["links"]
                                  if l["to_node"] != target]
            source = rng.choice([n for n in names if n != target])
            if not any(l["from_node"] == source and l["to_node"] == target
                       for l in entry["links"]):
                entry["links"].append({"from_node": source,
                                       "from_socket_name": "Value",
                                       "to_node": target,
                                       "to_socket_name": "Value"})
        elif op == "unlink" and entry["links"]:
            entry["links"].pop(rng.randrange(len(entry["links"])))
        elif op == "reorder":
            target_links = [l for l in entry["links"] if l["to_node"] == multi]
            if len(target_links) > 1:
                rest = [l for l in entry["links"] if l["to_node"] != multi]
                rng.shuffle(target_links)
                entry["links"] = rest + target_links
    return entry


def test_merge_identity():
    rng = random.Random(20261024)
    failures = 0
    cases = 0
    for _ in range(40):
        base, multi = _random_entry(rng, rng.randint(3, 7))
        side = _mutate_entry(rng, base, multi)
        for args in ((base, base, side), (base, side, base)):
            res = merge.merge_group_entries(*args)
            cases += 1
            merged = res.get("merged")
            if merged is None or (
                    hu.canonical_hash_from_json_data(merged)
                    != hu.canonical_hash_from_json_data(side)):
                failures += 1
    check("S7 identity merge(base,base,X) == X and merge(base,X,base) == X",
          failures == 0, "%d/%d cases" % (failures, cases))


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
    test_link_order()
    test_merge_identity()

    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  - %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
