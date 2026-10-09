# -*- coding: utf-8 -*-
"""Build the extension-platform package: dist/GNToolkit-<version>.zip.

Canonical, self-contained platform build for this repository: run it from
a clean checkout and you get the same package that is uploaded to the
Blender Extensions Platform.  No private tooling involved.

What it enforces (any problem fails the build):
  * versions agree across blender_manifest.toml, constants.py and bl_info;
  * only the files the extension needs are shipped (no docs, tools, tests,
    scripts or Git-CLI backend);
  * legacy-only blocks marked with ``# platform-strip:begin/end`` are
    removed from the shipped sources (the GitHub add-on build keeps them);
  * compliance guards: no ``sys.path``/``sys.modules`` writes, no
    threading/queue imports, no ctypes, no OS-level calls, no direct
    network/browser access, no bpy monkey-patching, no system-Git use, no
    dynamic code execution, no funding/promotion links, no references to
    other extensions;
  * manifest constraints beyond Blender's own validator: required fields
    non-empty, no "Blender" in the name, tagline and permission reasons
    within the terse-description limits, ``blender_version_min`` >= 4.2.0,
    the reserved ``[build.generated]`` table not declared;
  * every relative import is shipped (``git_backend_git`` is optional);
  * the wheels set is pure (``*-py3-none-any.whl``), complete, in sync with
    the manifest, and contains no compiled files or unsafe paths.

Usage:
    python build_platform.py [--out DIR]

Blender's own ``--command extension validate`` is not run here (it needs a
Blender executable); the maintainer gate runs it right after this script.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

INCLUDES = [
    "blender_manifest.toml",
    "LICENSE",
    "README.md",
    "audit.py", "codec.py", "constants.py", "credentials.py",
    "error_tracker.py",
    "file_utils.py",
    "geometry_validator.py",
    "git_backend_dulwich.py", "git_integration.py",
    "git_network_worker.py", "hash_utils.py",
    "importer.py",
    "modifier_utils.py", "operators.py",
    "serializer.py", "socket_utils.py", "sync_manager.py",
    "sync_metadata.py", "sync_operators.py", "sync_ui.py", "__init__.py",
]

OPTIONAL_MODULES = {"git_backend_git"}

EXPECTED_WHEELS = [
    "backports.tarfile-1.2.0-py3-none-any.whl",
    "dulwich-1.2.17-py3-none-any.whl",
    "importlib_metadata-9.0.1-py3-none-any.whl",
    "jaraco.classes-3.4.0-py3-none-any.whl",
    "jaraco_context-6.1.2-py3-none-any.whl",
    "jaraco_functools-4.6.0-py3-none-any.whl",
    "keyring-25.7.0-py3-none-any.whl",
    "more_itertools-11.1.0-py3-none-any.whl",
    "pywin32_ctypes-0.2.3-py3-none-any.whl",
    "typing_extensions-4.16.0-py3-none-any.whl",
    "urllib3-2.8.0-py3-none-any.whl",
    "zipp-4.1.1-py3-none-any.whl",
]

STRIP_RE = re.compile(
    r"(?ms)^[ \t]*#\s*platform-strip:begin\b.*?"
    r"^[ \t]*#\s*platform-strip:end\b[^\S\r\n]*\r?\n")

FORBIDDEN = [
    (r"sys\s*\.\s*path\s*(\.append|\.insert|\.extend|\+=|=)",
     "modifies sys.path"),
    (r"sys\s*\.\s*modules\s*(\[|\.update|\.pop|\.clear|=)",
     "modifies sys.modules"),
    (r"\b(import|from)\s+threading\b", "imports threading"),
    (r"\b(import|from)\s+queue\b", "imports queue"),
    (r"\bctypes\b", "uses ctypes"),
    (r"os\s*\.\s*(startfile|system|popen)\b", "OS-level call"),
    (r"urllib\s*\.\s*request\b|requests\s*\.|webbrowser",
     "direct network/browser access"),
    (r"setattr\s*\(\s*bpy\s*\.", "patches bpy"),
    (r'shutil\.which\(\s*[\'"]git[\'"]\s*\)', "uses the system Git"),
    (r"\beval\(|\bexec\(|pickle\.loads|__import__\(",
     "dynamic code execution"),
    (r"patreon|gumroad|paypal|ko-fi|buymeacoffee|donat|blendermarket"
     r"|superhive", "funding/promotion link (ToS 6.1)"),
    (r"bl_ext\.", "references another extension's namespace (ToS 5.3)"),
]

TERSE_MAX = 64
TERSE_END = ".!?:;,"

WHEEL_BINARY_SUFFIXES = (".so", ".pyd", ".dll", ".dylib", ".exe", ".pyc",
                         ".pyo", ".bin")


def fail(message: str):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def check_versions() -> str:
    manifest = read_text("blender_manifest.toml")
    constants = read_text("constants.py")
    init = read_text("__init__.py")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', manifest)
    manifest_ver = match.group(1) if match else ""
    match = re.search(r'ADDON_VERSION\s*=\s*"([^"]+)"', constants)
    const_ver = match.group(1) if match else ""
    match = re.search(
        r'"version":\s*\((\d+),\s*(\d+),\s*(\d+)\)', init)
    bl_info_ver = ".".join(match.groups()) if match else ""
    if not manifest_ver or manifest_ver != const_ver \
            or manifest_ver != bl_info_ver:
        fail(f"version mismatch: manifest={manifest_ver} "
             f"constants={const_ver} bl_info={bl_info_ver}")
    return manifest_ver


def check_files() -> None:
    missing = [n for n in INCLUDES if not (ROOT / n).is_file()]
    if missing:
        fail(f"missing files: {', '.join(missing)}")


def shipped_text(name: str) -> str:
    text = read_text(name)
    if not name.endswith(".py"):
        return text
    return STRIP_RE.sub("", text)


def check_imports() -> None:
    module_names = {Path(n).stem for n in INCLUDES if n.endswith(".py")}
    problems = []
    for name in INCLUDES:
        if not name.endswith(".py"):
            continue
        text = shipped_text(name)
        refs = re.findall(r"(?m)^\s*from\s+\.(\w+)\s+import", text)
        for group in re.findall(
                r"(?m)^\s*from\s+\.\s+import\s+([^\r\n#]+)", text):
            for part in group.split(","):
                clean = part.strip().split(" ")[0].strip("(),")
                if clean:
                    refs.append(clean)
        for ref in refs:
            if ref not in module_names and ref not in OPTIONAL_MODULES:
                problems.append(f"{name} -> {ref}")
    if problems:
        fail("relative imports not shipped: " + ", ".join(problems))


def check_compliance() -> None:
    for name in INCLUDES:
        if not name.endswith(".py"):
            continue
        text = shipped_text(name)
        if "platform-strip" in text:
            fail(f"unbalanced platform-strip marker in {name}")
        for pattern, reason in FORBIDDEN:
            if re.search(pattern, text):
                fail(f"forbidden pattern in {name}: {reason}")


def check_wheels() -> None:
    wheels_dir = ROOT / "wheels"
    if not wheels_dir.is_dir():
        fail(f"missing wheels directory: {wheels_dir}")
    actual = sorted(p.name for p in wheels_dir.glob("*.whl"))
    missing = [w for w in EXPECTED_WHEELS if w not in actual]
    extra = [w for w in actual if w not in EXPECTED_WHEELS]
    if missing:
        fail("missing wheels: " + ", ".join(missing))
    if extra:
        fail("unexpected wheels in wheels/: " + ", ".join(extra))
    compiled = [w for w in actual if not w.endswith("-py3-none-any.whl")]
    if compiled:
        fail("non-pure wheels not allowed: " + ", ".join(compiled))
    manifest = read_text("blender_manifest.toml")
    manifest_wheels = re.findall(
        r'(?m)^\s*"\./wheels/([^"]+)"\s*,?\s*$', manifest)
    if sorted(manifest_wheels) != sorted(EXPECTED_WHEELS):
        fail("blender_manifest.toml wheels do not match wheels/: "
             + ", ".join(manifest_wheels))


def check_manifest() -> None:
    manifest = read_text("blender_manifest.toml")

    def field(name):
        match = re.search(rf'(?m)^{name}\s*=\s*"([^"]*)"', manifest)
        return match.group(1) if match else None

    for key in ("schema_version", "id", "name", "version", "tagline",
                "maintainer", "type", "blender_version_min"):
        if not field(key):
            fail(f"manifest field missing or empty: {key}")

    license_match = re.search(r'(?m)^license\s*=\s*\[([^\]]*)\]', manifest)
    if not license_match or not license_match.group(1).strip():
        fail("manifest license list missing or empty")

    if re.search(r"(?i)blender", field("name")):
        fail("manifest name must not contain 'Blender' (ToS 2.1)")

    tagline = field("tagline")
    if len(tagline) > TERSE_MAX or tagline[-1] in TERSE_END:
        fail(f"tagline must be <= {TERSE_MAX} chars without end punctuation")

    version_min = field("blender_version_min")
    try:
        parts = tuple(int(p) for p in version_min.split("."))
    except ValueError:
        fail(f"blender_version_min is not a version: {version_min!r}")
    if parts < (4, 2, 0):
        fail("blender_version_min must be at least 4.2.0")

    perms = re.findall(r'(?m)^(files|network|clipboard|camera|microphone)'
                       r'\s*=\s*"([^"]*)"', manifest)
    if not perms:
        fail("manifest declares no [permissions] entries")
    for _key, reason in perms:
        if len(reason) > TERSE_MAX or reason[-1] in TERSE_END:
            fail(f"permission reason must be <= {TERSE_MAX} chars without "
                 f"end punctuation: {reason!r}")

    if "build.generated" in manifest:
        fail("manifest declares the reserved [build.generated] table")


def check_wheel_contents() -> None:
    for name in EXPECTED_WHEELS:
        with zipfile.ZipFile(ROOT / "wheels" / name) as zf:
            for entry in zf.namelist():
                if entry.startswith("/") or ".." in entry:
                    fail(f"{name}: unsafe wheel path {entry!r}")
                if entry.lower().endswith(WHEEL_BINARY_SUFFIXES):
                    fail(f"{name}: non-Python file in a pure wheel: {entry}")


def build_zip(version: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"GNToolkit-{version}.zip"
    if zip_path.exists():
        zip_path.unlink()
    entries = INCLUDES + [f"wheels/{w}" for w in EXPECTED_WHEELS]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in entries:
            if name.endswith(".py"):
                payload = shipped_text(name).encode("utf-8")
            else:
                payload = (ROOT / name).read_bytes()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, payload)
    return zip_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_platform",
        description="Build the extension-platform package "
                    "(dist/GNToolkit-<version>.zip).")
    parser.add_argument("--out", default=str(ROOT / "dist"),
                        help="output directory (default: dist/)")
    args = parser.parse_args(argv)

    version = check_versions()
    check_files()
    check_imports()
    check_compliance()
    check_wheels()
    check_manifest()
    check_wheel_contents()
    zip_path = build_zip(version, Path(args.out))
    print(f"Built {zip_path} ({len(INCLUDES)} files + "
          f"{len(EXPECTED_WHEELS)} wheels, version {version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
