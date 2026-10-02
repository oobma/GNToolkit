# -*- coding: utf-8 -*-
r"""Build a master .blend from a GNToolkit JSON export.

The other half of the port workflow: export every source .blend read-only
with export_all_json.py (optionally --exclude-library for third-party
libraries), then import the union into ONE clean .blend where every group
is local.  That master is the file to track and version with GNToolkit.

Headless:
    blender --background --factory-startup --python scripts\import_all_json.py -- --in <folder> --out <master.blend>

<folder> may be a folder export (with NodeGroups/), its parent folder or
a package .json — the same resolution as the addon's "Import
Package/Folder".  Dependencies are rebuilt children-first; groups that
already exist in the target are never rebuilt (rebuilding renumbers the
interface and would break links already wired from other groups).
Modifier tasks are counted but not applied (a fresh master has no
objects).

Exit codes: 0 = imported, 1 = some group failed / unreadable input,
2 = GNToolkit not available.
"""

import os
import sys

ADDON_MODULES = ("GNToolkit", "bl_ext.user_default.gn_toolkit")

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


def _empty_file():
    import bpy
    try:
        bpy.ops.wm.read_homefile(use_empty=True)
    except Exception:
        bpy.ops.wm.read_factory_settings()


def build_master(input_path: str, out_path: str = "") -> dict:
    """Import every group of *input_path* into the current .blend.

    Returns ``{"groups": int, "rebuilt": int, "failed": [names],
    "errors": int, "modifiers": int}``.  When *out_path* is given the
    file is saved there (``.blend`` appended if missing).
    """
    import bpy

    from GNToolkit.error_tracker import ImportErrorTracker
    from GNToolkit.importer import import_node_tree_recursive
    from GNToolkit.operators import dependency_ordered_names, load_package_sources

    json_cache, mod_data = load_package_sources(input_path)
    if not json_cache:
        raise RuntimeError(f"No node groups found in '{input_path}'")

    tracker = ImportErrorTracker()
    shared_maps = {}
    rebuilt = 0
    failed = []
    for name in dependency_ordered_names(json_cache):
        if bpy.data.node_groups.get(name):
            continue
        import_node_tree_recursive(json_cache[name], json_cache,
                                   shared_maps, None, tracker)
        if bpy.data.node_groups.get(name):
            rebuilt += 1
        else:
            failed.append(name)

    if out_path:
        if not out_path.lower().endswith(".blend"):
            out_path += ".blend"
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=out_path)

    return {
        "groups": len(json_cache),
        "rebuilt": rebuilt,
        "failed": failed,
        "errors": tracker.error_count,
        "modifiers": len(mod_data),
    }


def main():
    import bpy

    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []

    def arg_value(flag):
        if flag in argv and argv.index(flag) + 1 < len(argv):
            return argv[argv.index(flag) + 1]
        return ""

    input_path = arg_value("--in")
    out_path = arg_value("--out")
    if not input_path:
        print("[ERROR] missing --in <folder-or-json>")
        sys.exit(2)

    _empty_file()
    if not _enable_addon():
        print("[ERROR] GNToolkit is not available — install it and retry.")
        sys.exit(2)

    try:
        result = build_master(input_path, out_path)
    except Exception as exc:
        print(f"[ERROR] import failed: {exc}")
        sys.exit(1)

    print(f"[OK] imported {result['rebuilt']}/{result['groups']} groups"
          + (f" ({result['modifiers']} modifier task(s) not applied)"
             if result["modifiers"] else ""))
    if result["failed"]:
        shown = ", ".join(result["failed"][:5])
        print(f"[WARN] {len(result['failed'])} group(s) failed to rebuild: "
              f"{shown}")
    if result["errors"]:
        print(f"[WARN] {result['errors']} error(s) — check the console output")
    if out_path:
        saved = out_path if out_path.lower().endswith(".blend") else out_path + ".blend"
        print(f"[OK] master saved to {os.path.abspath(saved)}")
        print("[INFO] open it and use 'Track from Existing JSON' / 'Track All' "
              "to start versioning")

    sys.exit(1 if (result["errors"] or result["failed"]) else 0)


if __name__ == "__main__":
    main()
