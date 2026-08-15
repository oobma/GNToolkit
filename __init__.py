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

_check_timer: object = None


@bpy.app.handlers.persistent
def _on_load_post(scene):
    """Load sync metadata when a .blend file is opened."""
    try:
        sync_manager.load()
        # Do NOT compute full statuses on load — tree hashing is too
        # expensive with many groups.  A deferred JSON-side-only check
        # (pure Python, chunked via timers) detects files that changed
        # outside Blender and shows a non-intrusive notice.
        _schedule_json_check()
    except Exception:
        pass


def _schedule_json_check():
    """Schedule the deferred JSON-side sync check shown after load."""
    global _check_timer
    if _check_timer is not None:
        return
    try:
        if not sync_manager.metadata.get("tracked_groups"):
            return
        prefs = bpy.context.scene.gnt_sync_prefs
        if not getattr(prefs, "check_on_load", True):
            return
    except Exception:
        return

    sync_manager.start_json_check()

    def _tick():
        global _check_timer
        try:
            done = sync_manager.step_json_check(200)
        except Exception:
            done = True
        if not done:
            return 0.05
        _check_timer = None
        report = sync_manager.load_report
        if report:
            n_changed = sum(1 for e in report.values()
                            if e.get("status") == SyncStatus.JSON_MISSING)
            msg = f"GNToolkit: {len(report)} group(s) out of sync with JSON"
            if n_changed:
                msg += f" ({n_changed} JSON file(s) missing)"

            def _show_status(text=msg):
                try:
                    bpy.context.workspace.status_text_set(text)
                except Exception:
                    pass

            def _hide_status():
                try:
                    bpy.context.workspace.status_text_set(None)
                except Exception:
                    pass

            bpy.app.timers.register(_show_status, first_interval=0.1)
            bpy.app.timers.register(_hide_status, first_interval=6.0)
        return None

    _check_timer = bpy.app.timers.register(_tick, first_interval=1.0)


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
    global _check_timer
    if _check_timer is not None:
        try:
            bpy.app.timers.unregister(_check_timer)
        except Exception:
            pass
        _check_timer = None

    del bpy.types.Scene.gnt_sync_prefs

    for cls in reversed(_all_classes):
        bpy.utils.unregister_class(cls)

    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if _on_save_post in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_on_save_post)
    if _on_undo_post in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(_on_undo_post)