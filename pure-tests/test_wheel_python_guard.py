# -*- coding: utf-8 -*-
"""Pure-Python tests for the wheel Python guard in build_platform.py.

Every bundled wheel must stay installable when Blender changes its Python
version (4.2 ships 3.11, 5.1+ ships 3.13).  A ``Requires-Python`` ceiling
(``<``, ``<=``, ``~=``, ``==``) makes pip refuse the wheel on the new
interpreter even when the wheel itself is pure, so the builder rejects any
upper bound and keeps only lower bounds and exclusions.

Usage: python pure-tests\\test_wheel_python_guard.py
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location(
    "gnt_build_platform", os.path.join(_ROOT, "build_platform.py"))
build_platform = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_platform)

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"[PASS] {name} {detail}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name} {detail}")


def make_wheel(path, requires_python=""):
    dist = os.path.basename(path)[:-4]
    metadata = "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"
    if requires_python:
        metadata += f"Requires-Python: {requires_python}\n"
    metadata += "\nBody text.\n"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{dist}.dist-info/METADATA", metadata)


def accepts(requires_python):
    with tempfile.TemporaryDirectory(prefix="gnt_wheelpy_") as tmp:
        wheel = os.path.join(tmp, "demo-1.0-py3-none-any.whl")
        make_wheel(wheel, requires_python)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                build_platform.check_wheel_requires_python(Path(wheel))
        except SystemExit:
            return False, err.getvalue()
        return True, err.getvalue()


def build_synthetic_root(tmp, ceiling_name=None):
    wheels = os.path.join(tmp, "wheels")
    os.makedirs(wheels)
    lines = []
    for name in build_platform.EXPECTED_WHEELS:
        spec = "~=3.11.0" if name == ceiling_name else ">=3.10"
        make_wheel(os.path.join(wheels, name), spec)
        lines.append(f'    "./wheels/{name}",')
    manifest = "[build]\nwheels = [\n" + "\n".join(lines) + "\n]\n"
    with open(os.path.join(tmp, "blender_manifest.toml"), "w",
              encoding="utf-8") as fh:
        fh.write(manifest)


def run_check_wheels():
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            build_platform.check_wheels()
        return True, err.getvalue()
    except SystemExit:
        return False, err.getvalue()


# ---------------------------------------------------------------------------
# A. The wheels shipped today
# ---------------------------------------------------------------------------
problems = []
for name in build_platform.EXPECTED_WHEELS:
    _spec_path = Path(_ROOT) / "wheels" / name
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            build_platform.check_wheel_requires_python(_spec_path)
    except SystemExit:
        problems.append(name)
check("A1: the bundled wheels declare no Python ceiling",
      not problems and len(build_platform.EXPECTED_WHEELS) == 12,
      str(problems))

# ---------------------------------------------------------------------------
# B. Allowed and rejected specifiers
# ---------------------------------------------------------------------------
ok, _ = accepts(">=3.10")
check("B1: lower bound allowed", ok)
ok, _ = accepts(">=3.8,!=3.9.*")
check("B2: lower bound plus exclusion allowed", ok)
ok, _ = accepts("")
check("B3: no Requires-Python allowed", ok)

ok, msg = accepts("~=3.11.0")
check("B4: tilde patch pin rejected (~=3.11.0)",
      not ok and "~=3.11.0" in msg, msg.strip())
ok, msg = accepts("~=3.11")
check("B5: tilde minor pin rejected (~=3.11)",
      not ok and "~=3.11" in msg, msg.strip())
ok, _ = accepts(">=3.10,<3.14")
check("B6: upper bound rejected (>=3.10,<3.14)", not ok)
ok, _ = accepts("==3.12.*")
check("B7: exact pin rejected (==3.12.*)", not ok)
ok, _ = accepts("<=3.13")
check("B8: ceiling-only spec rejected (<=3.13)", not ok)
ok, _ = accepts("3.11")
check("B9: bare version rejected (3.11)", not ok)

# ---------------------------------------------------------------------------
# C. check_wheels wiring (synthetic wheels + manifest in a temp root)
# ---------------------------------------------------------------------------
original_root = build_platform.ROOT
try:
    with tempfile.TemporaryDirectory(prefix="gnt_wheelroot_") as tmp:
        build_synthetic_root(tmp)
        build_platform.ROOT = Path(tmp)
        passed, err = run_check_wheels()
    check("C1: check_wheels accepts a ceiling-free wheel set", passed,
          err.strip())

    with tempfile.TemporaryDirectory(prefix="gnt_wheelroot_") as tmp:
        ceiling = build_platform.EXPECTED_WHEELS[0]
        build_synthetic_root(tmp, ceiling_name=ceiling)
        build_platform.ROOT = Path(tmp)
        passed, err = run_check_wheels()
    check("C2: check_wheels rejects a wheel with a Python ceiling",
          not passed and ceiling in err and "Requires-Python" in err,
          err.strip())
finally:
    build_platform.ROOT = original_root

print()
print(f"=== RESULTS: {len(PASSED)} passed, {len(FAILED)} failed ===")
if FAILED:
    for name in FAILED:
        print(f"  - {name}")
sys.exit(1 if FAILED else 0)
