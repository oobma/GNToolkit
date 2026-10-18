# -*- coding: utf-8 -*-
"""Pure-Python test for the declarative exclusion tables (M1, decisions 1-2).

Checks, without bpy (constants.py is loaded directly from disk):
  1. every exclusion entry has a non-empty rationale;
  2. the derived frozensets match the historical membership exactly
     (accidental drift fails here);
  3. the struct-dump allowlist invariants (paired_output absent);
  4. the reinstatement invariants (is_panel_toggle & co. must stay serialized);
  5. serializer.py consumes the table (no local re-hardcode).

Usage: python tests\\test_exclusions_rationale.py
"""

import importlib.util
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)

_spec = importlib.util.spec_from_file_location(
    "gnt_constants_probe", os.path.join(_ROOT, "constants.py"))
constants = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(constants)

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"[PASS] {name} {detail}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name} {detail}")


EXPECTED_NODE = {
    'name', 'label', 'location', 'width', 'type', 'inputs', 'outputs',
    'node_tree', 'repeat_items', 'simulation_items', 'state_items',
    'input_items', 'main_items', 'generation_items', 'capture_items',
    'active_item', 'parent', 'menu_items', 'enum_items',
    'index_switch_items', 'list_items', 'asset_data', 'is_preview',
    'rna_type', 'bytecode', 'bytecode_hash',
}

EXPECTED_INTERFACE = {
    'name', 'item_type', 'in_out', 'socket_type', 'bl_socket_idname',
    'identifier', 'parent', 'rna_type', 'enum_items', 'is_inspect_output',
    'layer_selection_field', 'select',
}

EXPECTED_TREE = {
    'name', 'type', 'rna_type', 'library', 'tag', 'is_embedded_data',
    'is_embedded_id', 'users', 'parent', 'nodes', 'links', 'inputs',
    'outputs', 'interface', 'asset_data', 'is_preview', 'annotation',
    'node_tool_idname', 'use_fake_user', 'use_extra_user',
}

check("table has the 3 scopes",
      set(constants.EXCLUSIONS) == {"node", "interface", "tree"})
check("node membership frozen",
      set(constants.EXCLUSIONS["node"]) == EXPECTED_NODE,
      f"({len(constants.EXCLUSIONS['node'])})")
check("interface membership frozen",
      set(constants.EXCLUSIONS["interface"]) == EXPECTED_INTERFACE,
      f"({len(constants.EXCLUSIONS['interface'])})")
check("tree membership frozen",
      set(constants.EXCLUSIONS["tree"]) == EXPECTED_TREE,
      f"({len(constants.EXCLUSIONS['tree'])})")

for scope, table in sorted(constants.EXCLUSIONS.items()):
    for prop, rationale in sorted(table.items()):
        ok = isinstance(rationale, str) and len(rationale.strip()) >= 10
        check(f"rationale {scope}.{prop}", ok)

check("NODE_PROPS_TO_SKIP derived",
      constants.NODE_PROPS_TO_SKIP == frozenset(EXPECTED_NODE))
check("INTERFACE_SKIP_PROPS derived",
      constants.INTERFACE_SKIP_PROPS == frozenset(EXPECTED_INTERFACE))
check("TREE_PROPS_TO_SKIP derived",
      constants.TREE_PROPS_TO_SKIP == frozenset(EXPECTED_TREE))

check("struct allowlist membership",
      set(constants.STRUCT_DUMP_TYPES) ==
      {"ColorRamp", "CurveMapping", "CurveProfile", "ImageUser"})
check("paired_output absent from allowlist",
      "paired_output" not in constants.STRUCT_DUMP_TYPES)
for name, rationale in sorted(constants.STRUCT_DUMP_TYPES.items()):
    check(f"rationale struct.{name}",
          isinstance(rationale, str) and len(rationale.strip()) >= 10)

check("is_panel_toggle stays serialized",
      "is_panel_toggle" not in constants.INTERFACE_SKIP_PROPS)
check("structure_type stays serialized",
      "structure_type" not in constants.INTERFACE_SKIP_PROPS)
check("dimensions conditionally serialized (F2)",
      "dimensions" not in constants.INTERFACE_SKIP_PROPS)
check("menu_expanded stays serialized",
      "menu_expanded" not in constants.INTERFACE_SKIP_PROPS)
check("optional_label stays serialized",
      "optional_label" not in constants.INTERFACE_SKIP_PROPS)

with open(os.path.join(_ROOT, "serializer.py"), encoding="utf-8") as f:
    ser = f.read()
check("serializer imports STRUCT_DUMP_TYPES table",
      "STRUCT_DUMP_TYPES as _STRUCT_DUMP_TYPES" in ser)
check("serializer has no local struct literal",
      'frozenset({"ColorRamp"' not in ser)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
if FAILED:
    print("FAILED:", ", ".join(FAILED))
sys.exit(1 if FAILED else 0)
