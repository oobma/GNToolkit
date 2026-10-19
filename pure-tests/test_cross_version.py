# -*- coding: utf-8 -*-
"""gnt_check --cross-version: name-aware comparison of two exports.

Usage:
    python pure-tests\\test_cross_version.py
"""

import contextlib
import io
import json
import os
import shutil
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


def socket(name, socket_type="NodeSocketFloat", bl_idname="NodeSocketFloat",
           default=None):
    item = {"name": name, "identifier": name, "type": "VALUE",
            "bl_idname": bl_idname}
    if socket_type == "NodeSocketBool":
        item["type"] = "BOOLEAN"
    elif socket_type == "NodeSocketInt":
        item["type"] = "INT"
    if default is not None:
        item["default_value"] = default
    return item


def node(name="Math", node_type="ShaderNodeMath", inputs=None, outputs=None,
         properties=None, **extra):
    item = {
        "name": name,
        "type": node_type,
        "inputs": inputs if inputs is not None else [socket("Value", default=1.0)],
        "outputs": outputs if outputs is not None else [socket("Value", default=0.0)],
        "properties": properties if properties is not None else {"operation": "ADD"},
    }
    item.update(extra)
    return item


def group(name="G", nodes=None, links=None, interface_items=None,
          tree_type=None):
    item = {
        "name": name,
        "nodes": nodes if nodes is not None else [node()],
        "links": links if links is not None else [],
        "interface_items": interface_items if interface_items is not None else [],
    }
    if tree_type:
        item["tree_type"] = tree_type
    return item


def write_folder(path, groups):
    os.makedirs(path, exist_ok=True)
    for name, entry in groups.items():
        with open(os.path.join(path, name + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"node_groups": {name: entry}}, fh)


def run_cross(old, new, *extra):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = gnt_check.main(["--cross-version", old, new, *extra])
    return code, out.getvalue()


tmp = tempfile.mkdtemp(prefix="gnt_cross_version_")
try:
    # --- identical ---
    old_dir = os.path.join(tmp, "identical_old")
    new_dir = os.path.join(tmp, "identical_new")
    write_folder(old_dir, {"G": group()})
    write_folder(new_dir, {"G": group()})
    code, out = run_cross(old_dir, new_dir)
    check("identical exit 0", code == 0, f"exit={code}")
    check("identical report", "1 identical" in out and "0 changed" in out)

    # --- additive socket ---
    old_dir = os.path.join(tmp, "add_old")
    new_dir = os.path.join(tmp, "add_new")
    write_folder(old_dir, {"G": group()})
    added = node(inputs=[socket("Value", default=1.0),
                         socket("Miter Scale", "NodeSocketBool",
                                "NodeSocketBool", False)])
    write_folder(new_dir, {"G": group(nodes=[added])})
    code, out = run_cross(old_dir, new_dir)
    check("additive exit 0", code == 0, f"exit={code}")
    check("additive count", "1 changed (1 additive, 0 destructive)" in out)
    check("additive socket reported",
          "Miter Scale" in out and "default False" in out)
    code_strict, _ = run_cross(old_dir, new_dir, "--strict")
    check("strict turns additive into exit 1", code_strict == 1,
          f"exit={code_strict}")
    code_json, out_json = run_cross(old_dir, new_dir, "--json")
    payload = json.loads(out_json)
    check("json counts", payload["counts"]["additive"] == 1
          and payload["counts"]["destructive"] == 0)
    check("json added socket entry",
          payload["added_sockets"][0]["socket"] == "Miter Scale"
          and payload["added_sockets"][0]["count"] == 1)

    # --- removed socket (destructive) ---
    new_dir = os.path.join(tmp, "removed_new")
    write_folder(new_dir, {"G": group(nodes=[node(inputs=[])])})
    code, out = run_cross(old_dir, new_dir)
    check("removed socket exit 1", code == 1, f"exit={code}")
    check("removed socket reported", "removed sockets" in out
          and "Value" in out)

    # --- shared default change ---
    new_dir = os.path.join(tmp, "default_new")
    write_folder(new_dir, {"G": group(nodes=[node(
        inputs=[socket("Value", default=2.0)])])})
    code, out = run_cross(old_dir, new_dir)
    check("shared default exit 1", code == 1, f"exit={code}")
    check("shared default reported",
          "changes on sockets present in both" in out)

    # --- socket type change ---
    new_dir = os.path.join(tmp, "type_new")
    write_folder(new_dir, {"G": group(nodes=[node(
        inputs=[socket("Value", "NodeSocketInt", "NodeSocketInt", 1)])])})
    code, out = run_cross(old_dir, new_dir)
    check("socket type exit 1", code == 1, f"exit={code}")

    # --- node added / removed / property change ---
    new_dir = os.path.join(tmp, "node_added_new")
    write_folder(new_dir, {"G": group(nodes=[node(), node("Extra")])})
    code, out = run_cross(old_dir, new_dir)
    check("node added exit 1", code == 1, f"exit={code}")

    new_dir = os.path.join(tmp, "node_removed_new")
    write_folder(new_dir, {"G": group(nodes=[])})
    code, out = run_cross(old_dir, new_dir)
    check("node removed exit 1", code == 1, f"exit={code}")

    new_dir = os.path.join(tmp, "prop_new")
    write_folder(new_dir, {"G": group(nodes=[node(
        properties={"operation": "MULTIPLY"})])})
    code, out = run_cross(old_dir, new_dir)
    check("node property exit 1", code == 1, f"exit={code}")

    # --- link change ---
    new_dir = os.path.join(tmp, "link_new")
    write_folder(new_dir, {"G": group(links=[{
        "from_node": "Math", "from_socket": "Value",
        "to_node": "Math", "to_socket": "Value"}])})
    code, out = run_cross(old_dir, new_dir)
    check("link change exit 1", code == 1, f"exit={code}")

    # --- interface changes ---
    new_dir = os.path.join(tmp, "iface_new")
    write_folder(new_dir, {"G": group(interface_items=[{
        "name": "Out", "in_out": "OUTPUT",
        "socket_type": "NodeSocketFloat",
        "bl_socket_idname": "NodeSocketFloat",
        "properties": {"default_value": 0.0}}])})
    code, out = run_cross(old_dir, new_dir)
    check("interface added exit 1", code == 1, f"exit={code}")

    # --- groups only on one side ---
    new_dir = os.path.join(tmp, "group_new")
    write_folder(new_dir, {"G": group(), "New": group("New")})
    code, out = run_cross(old_dir, new_dir)
    check("group only-new exit 1", code == 1, f"exit={code}")
    check("group only-new reported", "only in new: New" in out)

    # --- unreadable input ---
    bad_dir = os.path.join(tmp, "bad")
    os.makedirs(bad_dir, exist_ok=True)
    with open(os.path.join(bad_dir, "broken.json"), "w",
              encoding="utf-8") as fh:
        fh.write("{not json")
    code, out = run_cross(old_dir, bad_dir)
    check("unreadable exit 2", code == 2, f"exit={code}")
    check("unreadable reported", "UNREADABLE" in out)

    # --- missing target ---
    code, out = run_cross(old_dir, os.path.join(tmp, "missing"))
    check("missing target exit 2", code == 2, f"exit={code}")

    # --- mixed additive + destructive in one group ---
    new_dir = os.path.join(tmp, "mixed_new")
    write_folder(new_dir, {"G": group(nodes=[node(
        inputs=[socket("Miter Scale", "NodeSocketBool",
                       "NodeSocketBool", False)],
        properties={"operation": "MULTIPLY"})])})
    code, out = run_cross(old_dir, new_dir)
    check("mixed change is destructive", code == 1, f"exit={code}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
for name in FAILED:
    print(f"  - {name}")
sys.exit(1 if FAILED else 0)
