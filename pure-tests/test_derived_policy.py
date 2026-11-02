# -*- coding: utf-8 -*-
"""Pure-Python tests for the external-dependency policy resolver (no bpy).

Usage: python pure-tests\\test_derived_policy.py
"""

import os
import sys

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


const = gnt_check._load_constants()

REFERENCE = const.DERIVED_POLICY_REFERENCE
EMBED = const.DERIVED_POLICY_EMBED
resolve = const.resolve_derived_policy

check("1: explicit reference wins", resolve({"derived_policy": REFERENCE}) == REFERENCE)
check("2: explicit embed wins over a folder layout",
      resolve({"derived_policy": EMBED, "layout": "folder"}) == EMBED)
check("3: folder without copies is reference",
      resolve({"layout": "folder"}) == REFERENCE)
check("4: package without copies defaults to embed",
      resolve({"layout": "package"}) == EMBED)
check("5: missing info defaults to embed", resolve({}) == EMBED)
check("6: legacy folder entry with copies stays embed",
      resolve({"layout": "folder"}, {"derived_node_groups": {"A": {}}}) == EMBED)
check("7: legacy folder entry without copies is reference",
      resolve({"layout": "folder"}, {"nodes": []}) == REFERENCE)
check("8: an invalid explicit value falls back to inference",
      resolve({"derived_policy": "nope", "layout": "folder"}) == REFERENCE)
check("9: policy version is an integer",
      isinstance(const.DERIVED_POLICY_VERSION, int)
      and const.DERIVED_POLICY_VERSION >= 1)
check("10: the two policies are the documented values",
      (REFERENCE, EMBED) == ("reference", "embed"))

print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
for name in FAILED:
    print(f"  - {name}")
sys.exit(1 if FAILED else 0)
