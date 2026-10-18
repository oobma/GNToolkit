# -*- coding: utf-8 -*-
"""Pure-Python tests for the shared sync-status rules (constants.py) — no bpy.

The "card" under test is constants.classify_sync_status / hashes_differ:
the single rule used by sync_manager's status checks and by gnt_check.

Usage: python tests\\test_sync_status_rules.py
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
SyncStatus = const.SyncStatus

# ---------------------------------------------------------------------------
# 1. Full four-hash truth table
# ---------------------------------------------------------------------------
check("1: equal/equal -> SYNCED",
      const.classify_sync_status("j1", "j1", "b1", "b1") == SyncStatus.SYNCED)
check("2: json differs -> JSON_MODIFIED",
      const.classify_sync_status("j2", "j1", "b1", "b1")
      == SyncStatus.JSON_MODIFIED)
check("3: blend differs -> BLEND_MODIFIED",
      const.classify_sync_status("j1", "j1", "b2", "b1")
      == SyncStatus.BLEND_MODIFIED)
check("4: both differ -> CONFLICT",
      const.classify_sync_status("j2", "j1", "b2", "b1") == SyncStatus.CONFLICT)

# ---------------------------------------------------------------------------
# 2. Missing baselines count as changed (first-tracking rule)
# ---------------------------------------------------------------------------
check("5: missing json baseline -> JSON_MODIFIED",
      const.classify_sync_status("j1", "", "b1", "b1")
      == SyncStatus.JSON_MODIFIED)
check("6: missing blend baseline -> BLEND_MODIFIED",
      const.classify_sync_status("j1", "j1", "b1", "")
      == SyncStatus.BLEND_MODIFIED)
check("7: both baselines missing -> CONFLICT",
      const.classify_sync_status("j1", "", "b1", "") == SyncStatus.CONFLICT)
check("8: None baselines behave like empty",
      const.classify_sync_status("j1", None, "b1", None) == SyncStatus.CONFLICT)

# ---------------------------------------------------------------------------
# 3. JSON-only mode (headless checker: no .blend side)
# ---------------------------------------------------------------------------
check("9: json-only equal -> SYNCED",
      const.classify_sync_status("j1", "j1", None, None) == SyncStatus.SYNCED)
check("10: json-only differ -> JSON_MODIFIED",
      const.classify_sync_status("j2", "j1", None, None)
      == SyncStatus.JSON_MODIFIED)
check("11: json-only missing baseline -> JSON_MODIFIED",
      const.classify_sync_status("j1", None, None, None)
      == SyncStatus.JSON_MODIFIED)

# ---------------------------------------------------------------------------
# 4. hashes_differ predicate (shared by the fast paths)
# ---------------------------------------------------------------------------
check("12: equal hashes -> no change", const.hashes_differ("a", "a") is False)
check("13: different hashes -> change", const.hashes_differ("a", "b") is True)
check("14: empty baseline -> change", const.hashes_differ("a", "") is True)
check("15: None baseline -> change", const.hashes_differ("a", None) is True)

print()
print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
if FAILED:
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
