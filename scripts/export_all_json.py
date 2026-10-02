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
back later).  The converted .blend is only SAVED when --save is also
given — without it the conversion stays in memory and the source file
is left untouched.

--exclude-library FRAG (repeatable) skips linked trees whose library
path contains FRAG, both for the export and for --make-local: use it to
keep Blender's bundled assets (e.g. "datafiles\\assets") or third-party
toolsets out of the export/conversion.  The addon is enabled
automatically when missing (legacy install, extension module or the
repo copy next to this script).

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


def _make_linked_local(exclude_fragments=()):
    """Turn the library-linked trees that participate in the export into
    local copies.

    Covers: every linked GEOMETRY group, the transitive closure reached
    through group nodes from any geometry group, and any tree used by a
    Geometry Nodes modifier.  Copy-first / remap-after / remove-last, so
    cross-references between linked trees survive.  Linked trees whose
    library path contains one of *exclude_fragments* are left alone
    (e.g. Blender's bundled assets).  Returns the number of trees made
    local."""
    import bpy

    def library_path(tree):
        return getattr(tree, "library", None) is not None

    def is_excluded(tree):
        if not exclude_fragments:
            return False
        lib = getattr(tree, "library", None)
        path = (lib.filepath if lib else "").lower()
        return any(frag in path for frag in exclude_fragments)

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

    linked = [t for t in closure if library_path(t) and not is_excluded(t)]
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
    save = "--save" in argv
    exclude_fragments = [argv[i + 1].strip().lower()
                         for i, arg in enumerate(argv)
                         if arg == "--exclude-library" and i + 1 < len(argv)]
    exclude_fragments = [frag for frag in exclude_fragments if frag]

    if not _enable_addon():
        print("[ERROR] GNToolkit is not available — install it and retry.")
        sys.exit(2)

    if make_local:
        n = _make_linked_local(exclude_fragments)
        print(f"[INFO] made {n} library-linked tree(s) local")
        if n and save and bpy.data.filepath:
            bpy.ops.wm.save_mainfile()
            print(f"[INFO] saved {bpy.data.filepath}")
        elif n:
            print("[INFO] conversion kept in memory only — pass --save to "
                  "persist it in the .blend")
            if not bpy.data.filepath:
                print("[WARN] the .blend is unsaved; there is nothing to save")

    os.makedirs(out, exist_ok=True)
    stem = safe(os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "unsaved")
    pkg = os.path.join(out, f"{stem}.json")

    # exclude_libraries is only passed when used: older addon versions
    # (e.g. the one on the extension platform before 0.2.8) do not have
    # that operator property and would reject an unknown keyword.
    export_kwargs = {"filepath": pkg, "use_folder_structure": True,
                     "use_minify": minify}
    if exclude_fragments:
        export_kwargs["exclude_libraries"] = ";".join(exclude_fragments)
    result = bpy.ops.gn.export_batch_json(**export_kwargs)
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