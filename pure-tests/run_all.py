#!/usr/bin/env python3
"""Run the pure-Python test suite of GNToolkit (no Blender required).

These checks exercise the JSON-side logic shipped with the add-on: the
canonical hashes, the semantic diff and merge cores, the audit helpers and
the ``gnt_check.py`` CLI.  Every test builds its own tiny fixtures in a
temporary directory — nothing from a real project is needed, and nothing
is modified outside the system temp folder.

Usage:
    python pure-tests/run_all.py

Exits 0 when every suite passes.  The same command runs in CI on every
push (see .github/workflows/pure-tests.yml).
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CASES = [
    ("gnt_check selftest", "gnt_check.py"),
    ("audit", "test_audit.py"),
    ("cross-project identity", "test_cross_project.py"),
    ("sync status rules", "test_sync_status_rules.py"),
    ("merge values", "test_merge_values.py"),
    ("merge package", "test_merge_package.py"),
    ("merge overrides", "test_merge_overrides.py"),
    ("gnt_check version guard", "test_gnt_check_version_guard.py"),
    ("gnt_check diff", "test_gnt_check_diff.py"),
    ("gnt_check merge", "test_gnt_check_merge.py"),
    ("cross-version", "test_cross_version.py"),
    ("gnt_check render (dot-only)", "test_gnt_check_render.py"),
    ("exclusions rationale", "test_exclusions_rationale.py"),
    ("hash cross-version", "test_hash_cross_version.py"),
    ("wheel python guard", "test_wheel_python_guard.py"),
]


def run_case(label, filename):
    if filename == "gnt_check.py":
        cmd = [sys.executable, os.path.join(ROOT, filename), "--selftest"]
    else:
        cmd = [sys.executable, os.path.join(HERE, filename)]
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    summary = ""
    for line in reversed((res.stdout or "").splitlines()):
        low = line.lower()
        if "passed" in low and "failed" in low:
            summary = line.strip()
            break
    ok = res.returncode == 0
    print("%s %-32s %s" % ("[OK]  " if ok else "[FAIL]", label, summary))
    if not ok:
        tail = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
        print("-------- output tail --------")
        print(tail[-2500:])
        print("-----------------------------")
    return ok


def main():
    results = [run_case(label, filename) for label, filename in CASES]
    passed = sum(1 for r in results if r)
    print()
    print("=== pure-tests: %d/%d suites passed ===" % (passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
