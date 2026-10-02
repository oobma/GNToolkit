# -*- coding: utf-8 -*-
r"""Export every Geometry Nodes group of the open .blend to a GNToolkit
folder export (one JSON file per group, plus Modifiers/).

Run from Blender's Text Editor (Scripting workspace) with the .blend open,
or headless:

    blender --background --factory-startup file.blend --python scripts\export_all_json.py -- --out <folder>

Without --out it writes next to the .blend (or to ./gnt_export when
unsaved).  --minify produces compact JSON.  --make-local turns every
library-linked tree that participates in the export (geometry groups,
their transitive group-node references and geometry-nodes modifiers)
into a local copy BEFORE exporting, so the folder export is fully
self-contained and trackable (library-linked trees cannot be pulled
back later).  The addon is enabled automatically when missing (legacy
install, extension module or the repo copy next to this script).

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


def _make_linked_local():
    """Turn the library-linked trees that participate in the export into
    local copies.

    Covers: every linked GEOMETRY group, the transitive closure reached
    through group nodes from any geometry group, and any tree used by a
    Geometry Nodes modifier.  Copy-first / remap-after / remove-last, so
    cross-references between linked trees survive.  Returns the number of
    trees made local."""
    import bpy

    def library_path(tree):
        lib = getattr(tree, "library", None)
        return lib is not None

    roots = {ng for ng in bpy.data.node_groups if ng.type == 'GEOMETRY'}
    closure = set(roots)
    changed = True
    while changed:
        changed = False
        for tree in bpy.data.node_groups:
            if tree in closure:
                continue
            if any(n.type == 'GROUP'
                   and getattr(n, "node_tree", None) in closure
                   for n in tree.nodes):
                closure.add(tree)
                changed = True
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if mod.type == 'NODES' and getattr(mod, "node_group", None) is not None:
                closure.add(mod.node_group)

    linked = [t for t in closure if library_path(t)]
    if not linked:
        return 0

    copies = {}
    failed = []
    for tree in linked:
        try:
            copies[tree] = tree.copy()
        except Exception as e:
            failed.append(f"{tree.name} ({e})")

    for tree in bpy.data.node_groups:
        for node in tree.nodes:
            target = getattr(node, "node_tree", None)
            if target in copies:
                node.node_tree = copies[target]
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            target = getattr(mod, "node_group", None)
            if target in copies:
                mod.node_group = copies[target]

    names = {copy: tree.name for tree, copy in copies.items()}
    for tree, copy in copies.items():
        bpy.data.node_groups.remove(tree)
    for copy, original_name in names.items():
        copy.name = original_name

    if failed:
        print(f"[WARN] could not make local: {', '.join(failed)}")
    return len(copies)


def safe(name):
    return (re.sub(r'[^A-Za-z0-9 _\-]', "_", name).strip() or "unnamed")


def main():
    import bpy

    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    out = DEFAULT_OUT
    if "--out" in argv and argv.index("--out") + 1 < len(argv):
        out = argv[argv.index("--out") + 1]
    minify = "--minify" in argv
    make_local = "--make-local" in argv

    if not _enable_addon():
        print("[ERROR] GNToolkit is not available — install it and retry.")
        sys.exit(2)

    if make_local:
        n = _make_linked_local()
        print(f"[INFO] made {n} library-linked tree(s) local")
        if n and bpy.data.filepath:
            bpy.ops.wm.save_mainfile()
        elif n:
            print("[WARN] the .blend is unsaved — the made-local state is "
                  "in memory only; save the file to keep it")

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


if __name__ == "__main__":
    main()