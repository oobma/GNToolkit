# -*- coding: utf-8 -*-
"""
GNToolkit — Blender Add-on Package

Flawless Post-Creation Sequential Mapping to prevent ID Collisions on
Volatile Nodes.  Now includes DNA/RNA synchronization for Geometry Nodes:
JSON (DNA) is the source of truth, .blend (RNA) is the working cache.
"""

import bpy

from .constants import ADDON_VERSION
from .operators import classes as operator_classes
from .sync_operators import classes as sync_operator_classes
from .sync_ui import classes as ui_classes
from .sync_manager import sync_manager, SyncStatus


# bl_info must be a literal dict (addon_utils parses it with
# ast.literal_eval; f-strings/comprehensions break Preferences).
# Keep "version" in sync with ADDON_VERSION in constants.py.
bl_info = {
    "name": "GNToolkit",
    "author": "oobma / AI assistant",
    "version": (0, 2, 2),
    "blender": (4, 0, 0),
    "location": "Node Editor > Sidebar > GN Tools",
    "description": "Geometry Nodes DNA/RNA sync: JSON-driven version control for node groups.",
    "category": "Node",
}


# ---------------------------------------------------------------------------
# Persistent handlers for load/save synchronization
# ---------------------------------------------------------------------------

@bpy.app.handlers.persistent
def _on_load_post(scene):
    """Load sync metadata when a .blend file is opened."""
    try:
        sync_manager.load()
        # Do NOT compute statuses on load — that's too expensive with 439 groups.
        # The user can press "Refresh Status" when ready.
    except Exception:
        pass


@bpy.app.handlers.persistent
def _on_save_post(scene):
    """Save sync metadata when the .blend file is saved."""
    try:
        sync_manager._relativize_json_paths()
        sync_manager.save()
    except Exception:
        pass


@bpy.app.handlers.persistent
def _on_undo_post(scene):
    """Invalidate the status cache after undo/redo."""
    try:
        sync_manager.invalidate_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_all_classes = list(operator_classes) + list(sync_operator_classes) + list(ui_classes)


def register():
    for cls in _all_classes:
        bpy.utils.register_class(cls)

    from .sync_ui import GN_SyncPrefs
    bpy.types.Scene.gnt_sync_prefs = bpy.props.PointerProperty(type=GN_SyncPrefs)

    bpy.app.handlers.load_post.append(_on_load_post)
    bpy.app.handlers.save_post.append(_on_save_post)
    bpy.app.handlers.undo_post.append(_on_undo_post)


def unregister():
    del bpy.types.Scene.gnt_sync_prefs

    for cls in reversed(_all_classes):
        bpy.utils.unregister_class(cls)

    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if _on_save_post in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_on_save_post)
    if _on_undo_post in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(_on_undo_post)