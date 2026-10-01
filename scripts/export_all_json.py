# -*- coding: utf-8 -*-
r"""Export every Geometry Nodes group of the open .blend to a GNToolkit
folder export (one JSON file per group, plus Modifiers/).

Run from Blender's Text Editor (Scripting workspace) with the .blend open,
or headless:

    blender --background --factory-startup file.blend --python scripts\export_all_json.py -- --out <folder>

Without --out it writes next to the .blend (or to ./gnt_export when
unsaved).  --minify produces compact JSON.  The addon is enabled
automatically when missing (legacy install, extension module or the repo
copy next to this script).

The folder export can then be versioned and used with
'Track from Existing JSON' / 'Track Folder…' inside Blender, and checked
headless with gnt_check.py:

    python gnt_check.py <folder>\NodeGroups --strict
"""

import os
import re
import sys

ADDON_MODULES = ("GNToolkit", "bl_ext.user_default.gn_toolkit")
DEFAULT_OUT = os.path.join(os.getcwd(), "gnt_export")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_REPO_ROOT, os.path.dirname(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _enable_addon():
    import bpy
    if "export_batch_json" in dir(bpy.ops.gn):
        return True
    try:
        bpy.ops.preferences.addon_enable(module="GNToolkit")
        return True
    except Exception:
        pass
    for mod in ADDON_MODULES:
        try:
            __import__(mod)
            mod = sys.modules[mod]
            if hasattr(mod, "register"):
                mod.register()
            return True
        except Exception:
            continue
    return False


def safe(name):
    return (re.sub(r'[^A-Za-z0-9 _\-]', "_", name).strip() or "unnamed")


def main():
    import bpy

    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    out = argv[argv.index("--out") + 1] if "--out" in argv else DEFAULT_OUT
    minify = "--minify" in argv

    if not _enable_addon():
        print("[ERROR] GNToolkit is not available — install it and retry.")
        sys.exit(2)

    os.makedirs(out, exist_ok=True)
    stem = safe(os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "unsaved")
    pkg = os.path.join(out, f"{stem}.json")

    result = bpy.ops.gn.export_batch_json(
        filepath=pkg, use_folder_structure=True, use_minify=minify)
    if result != {'FINISHED'}:
        print(f"[ERROR] export failed: {result}")
        sys.exit(1)

    ng_dir = os.path.join(out, "NodeGroups")
    if os.path.isdir(ng_dir):
        print(f"[OK] {len(os.listdir(ng_dir))} group file(s) exported to {ng_dir}")
    else:
        print("[WARN] no NodeGroups folder was produced — check the console output")
    sys.exit(0)


main()