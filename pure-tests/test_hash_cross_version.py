# -*- coding: utf-8 -*-
"""Pure tests: version-gated properties fold to absent at their default,
so a group serialized by Blender 4.2 hashes identically to the same
group serialized by 5.x (constants.CROSS_VERSION_DEFAULTS).

Non-default values must still move the hash (they are real content).

Usage: python tests\\test_hash_cross_version.py
"""

import copy
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _ROOT)

import gnt_check

hu = gnt_check._load_hash_utils()
constants = sys.modules["gnt_core.constants"]

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"[PASS] {name} {detail}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name} {detail}")


BASE = {
    "name": "T",
    "tree_type": "ShaderNodeTree",
    "interface_items": [
        {"item_type": "SOCKET", "name": "Value", "in_out": "INPUT",
         "socket_type": "NodeSocketFloat",
         "properties": {"default_value": 0.5}},
    ],
    "nodes": [
        {"name": "Normal Map", "type": "ShaderNodeNormalMap",
         "properties": {"uv_map": "", "space": "TANGENT"},
         "inputs": [], "outputs": [],
         "location": [0.0, 0.0]},
        {"name": "Math", "type": "ShaderNodeMath",
         "properties": {}, "inputs": [], "outputs": [],
         "location": [100.0, 0.0]},
    ],
    "links": [],
    "tree_properties": {},
}

base_hash = hu.canonical_hash_from_json_data(copy.deepcopy(BASE))


def hashed(mutate):
    data = copy.deepcopy(BASE)
    mutate(data)
    return hu.canonical_hash_from_json_data(data)


def with_node_prop(key, value):
    def mutate(data):
        data["nodes"][1]["properties"][key] = value
    return mutate


def with_iface_prop(key, value):
    def mutate(data):
        data["interface_items"][0]["properties"][key] = value
    return mutate


def with_tree_prop(key, value):
    def mutate(data):
        data["tree_properties"][key] = value
    return mutate


check("0: hash_utils loaded pure (no bpy)", "bpy" not in sys.modules)

check("1: table covers all measured scopes",
      set(constants.CROSS_VERSION_DEFAULTS) == {
          ("interface", "is_panel_toggle"),
          ("interface", "structure_type"),
          ("node", "warning_propagation"),
          ("node", "base"),
          ("node", "convention"),
          ("tree", "bl_use_group_interface"),
          ("tree", "default_group_node_width"),
          ("tree", "grease_pencil"),
          ("owner", "shadow_method"),
          ("interface", "default_input"),
          ("tree", "color_tag"),
      },
      f"({len(constants.CROSS_VERSION_DEFAULTS)} entries)")

for key, value in (("warning_propagation", "ALL"),
                   ("base", "DISPLACED"),
                   ("convention", "OPENGL")):
    check(f"2.{key}: default folds to absent",
          hashed(with_node_prop(key, value)) == base_hash)

for key, value in (("is_panel_toggle", False),
                   ("structure_type", "AUTO")):
    check(f"3.{key}: default folds to absent",
          hashed(with_iface_prop(key, value)) == base_hash)

for key, value in (("bl_use_group_interface", True),
                   ("default_group_node_width", 140),
                   ("grease_pencil", None)):
    check(f"4.{key}: default folds to absent",
          hashed(with_tree_prop(key, value)) == base_hash)

check("5: all version-gated defaults together fold to absent",
      hashed(lambda d: (
          d["nodes"][1]["properties"].update(
              {"warning_propagation": "ALL", "base": "DISPLACED",
               "convention": "OPENGL"}),
          d["interface_items"][0]["properties"].update(
              {"is_panel_toggle": False, "structure_type": "AUTO"}),
          d["tree_properties"].update(
              {"bl_use_group_interface": True,
               "default_group_node_width": 140, "grease_pencil": None}),
      )) == base_hash)

for key, value in (("warning_propagation", "ERRORS"),
                   ("base", "ORIGINAL"),
                   ("convention", "DIRECTX")):
    check(f"6.{key}: non-default stays a difference",
          hashed(with_node_prop(key, value)) != base_hash)

for key, value in (("is_panel_toggle", True),
                   ("structure_type", "FIELD")):
    check(f"7.{key}: non-default stays a difference",
          hashed(with_iface_prop(key, value)) != base_hash)

for key, value in (("bl_use_group_interface", False),
                   ("default_group_node_width", 110)):
    check(f"8.{key}: non-default stays a difference",
          hashed(with_tree_prop(key, value)) != base_hash)

check("9: tree bl_* class text is not content",
      hashed(with_tree_prop(
          "bl_description",
          "Edit materials, lights, and world shading using nodes")) == base_hash
      and hashed(with_tree_prop("bl_description", "Shader nodes")) == base_hash
      and hashed(with_tree_prop("bl_label", "Shader Editor")) == base_hash)

check("10: tree description stays content",
      hashed(with_tree_prop("description", "my group")) != base_hash)

check("11: default_input folds 'VALUE' and the sanitized ''",
      hashed(with_iface_prop("default_input", "VALUE")) == base_hash
      and hashed(with_iface_prop("default_input", "")) == base_hash
      and hashed(with_iface_prop("default_input", "ATTRIBUTE")) != base_hash)

check("12: color_tag folds 'NONE' and the sanitized ''",
      hashed(with_tree_prop("color_tag", "NONE")) == base_hash
      and hashed(with_tree_prop("color_tag", "")) == base_hash
      and hashed(with_tree_prop("color_tag", "COLOR_04")) != base_hash)

print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
for name in FAILED:
    print(f"  - {name}")
sys.exit(1 if FAILED else 0)
