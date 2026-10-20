# -*- coding: utf-8 -*-
"""
gn_toolkit.sync_operators — Blender operators for DNA/RNA sync actions.

Provides operators for linking, unlinking, importing, exporting, ignoring,
resolving, and checking the sync state of tracked node groups.
"""

from __future__ import annotations

import bpy
import os
from bpy.props import StringProperty, EnumProperty, BoolProperty, CollectionProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .constants import (ADDON_VERSION, EMBEDDED_KINDS,
                        is_group_node_bl_idname, is_trackable_tree_type)
from .serializer import serialize_node_tree, tree_has_animation
from .socket_utils import custom_inner_tree_names
from .sync_manager import (
    read_json_tolerant, json_read_failure_reason, sync_manager, SyncStatus,
    group_library_path, resolve_json_path, resolve_entry_tree,
)
from .sync_metadata import (SYNC_UUID_PROP, find_tree_by_uuid,
                            find_uuid_for_tree, get_uuid_from_tree)


# ---------------------------------------------------------------------------
# Helper: find active node tree
# ---------------------------------------------------------------------------

def _get_active_tree(context):
    """Return the active supported node tree from the context, or None."""
    space = getattr(context, 'space_data', None)
    if space is not None:
        tree = getattr(space, 'node_tree', None)
        if (tree is not None and hasattr(tree, 'type')
                and is_trackable_tree_type(tree.type)):
            return tree
    # Fallback: check the active object's modifier
    obj = context.active_object
    if obj:
        for mod in obj.modifiers:
            if mod.type == 'NODES' and mod.node_group:
                return mod.node_group
    return None


def _get_scene_compositor(context):
    """Return the active scene's compositor group (5.0+), or None."""
    scene = getattr(context, 'scene', None)
    if scene is None or not hasattr(scene, 'compositing_node_group'):
        return None
    tree = scene.compositing_node_group
    if tree is not None and is_trackable_tree_type(getattr(tree, 'type', '')):
        return tree
    return None


def _get_active_material(context):
    """Active material in the Shader Editor (or the object's active slot)."""
    mat = getattr(context, 'material', None)
    if isinstance(mat, bpy.types.Material):
        return mat
    space = getattr(context, 'space_data', None)
    if space is not None and getattr(space, 'tree_type', '') == 'ShaderNodeTree':
        idblock = getattr(space, 'id', None)
        if isinstance(idblock, bpy.types.Material):
            return idblock
        if isinstance(idblock, bpy.types.Object) and idblock.active_material:
            return idblock.active_material
    obj = getattr(context, 'active_object', None)
    if obj is not None and getattr(obj, 'active_material', None) is not None:
        return obj.active_material
    return None


def _get_active_world(context):
    """Active world in the Shader Editor (or the scene's world)."""
    world = getattr(context, 'world', None)
    if isinstance(world, bpy.types.World):
        return world
    space = getattr(context, 'space_data', None)
    if space is not None and getattr(space, 'tree_type', '') == 'ShaderNodeTree':
        idblock = getattr(space, 'id', None)
        if isinstance(idblock, bpy.types.World):
            return idblock
    scene = getattr(context, 'scene', None)
    if scene is not None and scene.world is not None:
        return scene.world
    return None


def _untracked_deps_note() -> str:
    """Report suffix for saves that leave untracked dependencies behind."""
    try:
        count = len(sync_manager.find_untracked_dependencies())
    except Exception:
        return ""
    if not count:
        return ""
    return (f" - warning: {count} untracked dependency group(s) were NOT "
            "saved (Sync Issues -> Track)")


def _refresh_after_sync_change(context=None):
    """Recalculate statuses and git state after tracking/commit operations.

    The panels draw from caches; without this the user sees stale numbers
    until pressing Refresh Status manually.
    """
    sync_manager.invalidate_cache()
    sync_manager.check_all_statuses()
    try:
        from .git_integration import invalidate_git_state
        invalidate_git_state()
    except Exception:
        pass
    if context is not None:
        try:
            for area in context.screen.areas:
                area.tag_redraw()
        except Exception:
            pass


def _collect_commit_suggestion():
    """Build a compact message describing what a commit is about to write.

    Stored on the sync manager so "Git Commit…" can pre-fill its message.
    """
    from . import semantic_diff
    try:
        statuses = sync_manager.check_all_statuses()
        tracked = sync_manager.metadata.get("tracked_groups", {})
        changed = []
        new_groups = []
        for uid, status in statuses.items():
            if status == SyncStatus.SYNCED:
                continue
            info = tracked.get(uid)
            if not info:
                continue
            blend_name = info.get("blend_name", "")
            tree = bpy.data.node_groups.get(blend_name)
            if tree is None:
                continue
            entry = None
            json_path = resolve_json_path(info.get("json_path", ""),
                                          sync_manager._blend_dir())
            if json_path and os.path.isfile(json_path):
                data = read_json_tolerant(json_path)
                if isinstance(data, dict):
                    if data.get("type") == "GN_UNIFIED_PACKAGE":
                        entry = data.get("node_groups", {}).get(blend_name)
                    elif "nodes" in data:
                        entry = data
            if entry is None:
                new_groups.append((blend_name, len(tree.nodes)))
                continue
            result = semantic_diff.compare_groups(entry,
                                                  serialize_node_tree(tree))
            if result is not None:
                changed.append(semantic_diff.summarize_group(blend_name,
                                                             result))
        message = semantic_diff.suggest_message(changed, new_groups)
    except Exception:
        message = ""
    sync_manager.commit_suggestion = message
    return message


# ---------------------------------------------------------------------------
# Operator: Track a group (write JSON from blend)
# ---------------------------------------------------------------------------

class GN_OT_SyncLink(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.sync_link"
    bl_label = "Track Group"
    bl_description = "Start tracking the active group: writes its JSON from the current .blend content (first save)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    tree_name: StringProperty(name="Node Group", description="Name of the node group to link")

    _progress_started = False

    def _resolve_tree(self, context):
        return _get_active_tree(context)

    def invoke(self, context, event):
        tree = self._resolve_tree(context)
        if tree is None:
            self.report({'ERROR'}, "No active node group found")
            return {'CANCELLED'}
        self.tree_name = tree.name
        self.filepath = tree.name + ".json"
        context.window_manager.progress_begin(0, 100)
        self._progress_started = True
        result = super().invoke(context, event)
        if 'RUNNING_MODAL' not in result:
            context.window_manager.progress_end()
            self._progress_started = False
        return result

    def cancel(self, context):
        if self._progress_started:
            context.window_manager.progress_end()
            self._progress_started = False

    def execute(self, context):
        try:
            return self._run_link(context)
        finally:
            if self._progress_started:
                context.window_manager.progress_end()
                self._progress_started = False

    def _run_link(self, context):
        if not self.tree_name:
            self.report({'ERROR'}, "No node group specified")
            return {'CANCELLED'}

        tree = bpy.data.node_groups.get(self.tree_name)
        if tree is None:
            self.report({'ERROR'}, f"Node group '{self.tree_name}' not found")
            return {'CANCELLED'}
        if group_library_path(tree):
            self.report({'ERROR'},
                        f"'{tree.name}' is linked from a library - "
                        "make it local to track it")
            return {'CANCELLED'}

        # Check if already tracked
        existing_uuid = get_uuid_from_tree(tree)
        if existing_uuid:
            existing_info = sync_manager.get_tracked_group(existing_uuid)
            if existing_info is not None:
                self.report({'WARNING'},
                            f"'{tree.name}' is already tracked (UUID: {existing_uuid[:8]}...)")
                return {'CANCELLED'}

        try:
            sync_uuid = sync_manager.link_group(tree, self.filepath)
            sync_manager.save()
            self.report({'INFO'},
                        f"Now tracking '{tree.name}' to {os.path.basename(self.filepath)} "
                        f"(UUID: {sync_uuid[:8]}...)")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to link: {e}")
            return {'CANCELLED'}

        _refresh_after_sync_change(context)
        return {'FINISHED'}


class GN_OT_SyncLinkSceneCompositor(GN_OT_SyncLink):
    bl_idname = "gn.sync_link_scene_compositor"
    bl_label = "Track Scene Compositor"
    bl_description = ("Start tracking the active scene's compositor group "
                      "(Compositor Nodes, Blender 5.0+): writes its JSON from "
                      "the current .blend content (first save)")

    def _resolve_tree(self, context):
        return _get_scene_compositor(context)

    def invoke(self, context, event):
        if self._resolve_tree(context) is None:
            self.report({'ERROR'},
                        "The active scene has no compositor node group")
            return {'CANCELLED'}
        return super().invoke(context, event)


class GN_OT_SyncLinkMaterial(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.sync_link_material"
    bl_label = "Track Material"
    bl_description = ("Start tracking the active material: writes its JSON "
                      "(node tree + versioned material settings) from the "
                      "current .blend content (first save)")
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    material_name: StringProperty(name="Material")

    def invoke(self, context, event):
        mat = _get_active_material(context)
        if mat is None:
            self.report({'ERROR'},
                        "No active material found (open a material in the "
                        "Shader Editor)")
            return {'CANCELLED'}
        self.material_name = mat.name
        self.filepath = mat.name + ".json"
        return super().invoke(context, event)

    def execute(self, context):
        mat = (bpy.data.materials.get(self.material_name)
               if self.material_name else None)
        if mat is None:
            self.report({'ERROR'}, "Material not found")
            return {'CANCELLED'}
        if mat.library is not None:
            self.report({'ERROR'},
                        f"'{mat.name}' is linked from a library - "
                        "make it local to track it")
            return {'CANCELLED'}
        existing = mat.get(SYNC_UUID_PROP)
        if existing and sync_manager.get_tracked_group(existing) is not None:
            self.report({'WARNING'},
                        f"'{mat.name}' is already tracked "
                        f"(UUID: {existing[:8]}...)")
            return {'CANCELLED'}
        if not getattr(mat, "use_nodes", False) or mat.node_tree is None:
            self.report({'ERROR'},
                        f"'{mat.name}' has no node tree (enable 'Use Nodes')")
            return {'CANCELLED'}
        try:
            sync_uuid = sync_manager.link_embedded(mat, "material", self.filepath)
            sync_manager.save()
        except Exception as e:
            self.report({'ERROR'}, f"Failed to link: {e}")
            return {'CANCELLED'}
        if not sync_uuid:
            self.report({'ERROR'}, f"Could not track '{mat.name}'")
            return {'CANCELLED'}
        self.report({'INFO'},
                    f"Now tracking '{mat.name}' to "
                    f"{os.path.basename(self.filepath)} "
                    f"(UUID: {sync_uuid[:8]}...)")
        _refresh_after_sync_change(context)
        return {'FINISHED'}


class GN_OT_SyncLinkWorld(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.sync_link_world"
    bl_label = "Track World"
    bl_description = ("Start tracking the active world: writes its JSON "
                      "(node tree + versioned world settings) from the "
                      "current .blend content (first save)")
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    world_name: StringProperty(name="World")

    def invoke(self, context, event):
        world = _get_active_world(context)
        if world is None:
            self.report({'ERROR'}, "No world found")
            return {'CANCELLED'}
        self.world_name = world.name
        self.filepath = world.name + ".json"
        return super().invoke(context, event)

    def execute(self, context):
        world = (bpy.data.worlds.get(self.world_name)
                 if self.world_name else None)
        if world is None:
            self.report({'ERROR'}, "World not found")
            return {'CANCELLED'}
        if world.library is not None:
            self.report({'ERROR'},
                        f"'{world.name}' is linked from a library - "
                        "make it local to track it")
            return {'CANCELLED'}
        existing = world.get(SYNC_UUID_PROP)
        if existing and sync_manager.get_tracked_group(existing) is not None:
            self.report({'WARNING'},
                        f"'{world.name}' is already tracked "
                        f"(UUID: {existing[:8]}...)")
            return {'CANCELLED'}
        if not getattr(world, "use_nodes", False) or world.node_tree is None:
            self.report({'ERROR'},
                        f"'{world.name}' has no node tree (enable 'Use Nodes')")
            return {'CANCELLED'}
        try:
            sync_uuid = sync_manager.link_embedded(world, "world", self.filepath)
            sync_manager.save()
        except Exception as e:
            self.report({'ERROR'}, f"Failed to link: {e}")
            return {'CANCELLED'}
        if not sync_uuid:
            self.report({'ERROR'}, f"Could not track '{world.name}'")
            return {'CANCELLED'}
        self.report({'INFO'},
                    f"Now tracking '{world.name}' to "
                    f"{os.path.basename(self.filepath)} "
                    f"(UUID: {sync_uuid[:8]}...)")
        _refresh_after_sync_change(context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Stop tracking a group
# ---------------------------------------------------------------------------

class GN_OT_SyncUnlink(bpy.types.Operator):
    bl_idname = "gn.sync_unlink"
    bl_label = "Stop Tracking"
    bl_description = "Stop tracking this group — the JSON file and the node tree are kept"
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: StringProperty(name="UUID", description="UUID of the tracked group")

    def execute(self, context):
        if not self.sync_uuid:
            tree = _get_active_tree(context)
            if tree:
                self.sync_uuid = find_uuid_for_tree(tree, sync_manager.metadata) or ""
        if not self.sync_uuid:
            self.report({'ERROR'}, "No tracked group found")
            return {'CANCELLED'}

        info = sync_manager.get_tracked_group(self.sync_uuid)
        if info is None:
            self.report({'ERROR'}, "Group not found in metadata")
            return {'CANCELLED'}

        blend_name = info.get("blend_name", "?")
        sync_manager.unlink_group(self.sync_uuid)
        sync_manager.save()
        self.report({'INFO'}, f"Stopped tracking '{blend_name}'")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Stop tracking all groups (batch)
# ---------------------------------------------------------------------------

class GN_OT_SyncUnlinkAll(bpy.types.Operator):
    bl_idname = "gn.sync_unlink_all"
    bl_label = "Stop Tracking All"
    bl_description = "Stop tracking every group — the JSON files and the node trees are kept"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        n = len(sync_manager.metadata.get("tracked_groups", {}))
        if n == 0:
            self.report({'INFO'}, "Nothing tracked")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(
            self, event,
            title=f"Stop tracking {n} groups?",
            message=(
                f"All {n} tracked groups will lose their tracking metadata.\n\n"
                "The JSON files and the node trees are kept."
            ),
            icon='WARNING',
        )

    def execute(self, context):
        n = sync_manager.unlink_all_groups()
        sync_manager.save()
        _refresh_after_sync_change(context)
        self.report({'INFO'}, f"Stopped tracking {n} groups")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Pull a group from JSON

class GN_OT_SyncImport(bpy.types.Operator):
    bl_idname = "gn.sync_import"
    bl_label = "Pull from JSON"
    bl_description = "Overwrite this group in the .blend with the JSON version (JSON wins)"
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: StringProperty(name="UUID", description="UUID of the tracked group")

    def execute(self, context):
        if not self.sync_uuid:
            tree = _get_active_tree(context)
            if tree:
                self.sync_uuid = find_uuid_for_tree(tree, sync_manager.metadata) or ""
        if not self.sync_uuid:
            self.report({'ERROR'}, "No tracked group found")
            return {'CANCELLED'}

        status = sync_manager.check_status(self.sync_uuid)
        if status == SyncStatus.SYNCED:
            self.report({'INFO'}, "Already synced - nothing to import")
            return {'CANCELLED'}

        tracker = sync_manager.import_from_json(self.sync_uuid, context)
        sync_manager.save()
        # Force UI redraw so issue disappears immediately
        for area in context.screen.areas:
            area.tag_redraw()

        if tracker.error_count:
            self.report({'ERROR'},
                        f"Pull failed ({tracker.error_count} error(s)) - "
                        "check the console/log")
            return {'CANCELLED'}
        if tracker.warn_count:
            self.report({'WARNING'},
                        f"Pull completed with {tracker.warn_count} warning(s) - check console")
        else:
            self.report({'INFO'}, "Pull from JSON completed")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Commit a group to JSON

class GN_OT_SyncExport(bpy.types.Operator):
    bl_idname = "gn.sync_export"
    bl_label = "Save to JSON"
    bl_description = "Write this group's .blend content into its JSON file (blend wins)"
    bl_options = {'REGISTER'}

    sync_uuid: StringProperty(name="UUID", description="UUID of the tracked group")

    def execute(self, context):
        if not self.sync_uuid:
            tree = _get_active_tree(context)
            if tree:
                self.sync_uuid = find_uuid_for_tree(tree, sync_manager.metadata) or ""
        if not self.sync_uuid:
            self.report({'ERROR'}, "No tracked group found")
            return {'CANCELLED'}

        try:
            _collect_commit_suggestion()
            force = (sync_manager.check_status(self.sync_uuid)
                     == SyncStatus.JSON_MODIFIED)
            success = sync_manager.export_to_json(self.sync_uuid, force=force)
        except PermissionError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        if success:
            sync_manager.save()
            _refresh_after_sync_change(context)
            note = _untracked_deps_note()
            if note:
                self.report({'WARNING'}, "Saved to JSON" + note)
            else:
                self.report({'INFO'}, "Saved to JSON")
            return {'FINISHED'}
        else:
            self.report({'ERROR'},
                        "Save blocked - the JSON was modified externally. "
                        "Pull first or resolve the conflict.")
            return {'CANCELLED'}


# ---------------------------------------------------------------------------
# Changes list: staging (VS Code-style mark/unmark/discard/pull)
# ---------------------------------------------------------------------------

def _change_rows() -> dict:
    """Build the Changes list: out-of-sync tracked groups (ignored ones
    excluded) plus untracked dependency groups.

    Reads the cached statuses only (no recompute); prunes the staged set
    so it never carries groups that left the list.  Returns
    ``{"rows": [...], "deps": [...], "ignored": n}`` where each row has
    ``uid/name/status/saveable/pullable/discardable/staged``.
    """
    from .sync_metadata import is_ignored
    statuses = dict(sync_manager._status_cache)
    for uid, entry in sync_manager.load_report.items():
        statuses.setdefault(uid, entry.get("status"))
    saveable = (SyncStatus.BLEND_MODIFIED, SyncStatus.CONFLICT,
                SyncStatus.JSON_MODIFIED, SyncStatus.JSON_MISSING)
    pullable = (SyncStatus.JSON_MODIFIED, SyncStatus.CONFLICT,
                SyncStatus.ORPHAN)
    discardable = (SyncStatus.BLEND_MODIFIED, SyncStatus.CONFLICT)
    rows = []
    ignored = 0
    for uid, status in statuses.items():
        if status == SyncStatus.SYNCED:
            continue
        if is_ignored(sync_manager.metadata, uid):
            ignored += 1
            continue
        info = sync_manager.metadata.get("tracked_groups", {}).get(uid, {})
        tree = resolve_entry_tree(uid, info)
        rows.append({
            "uid": uid,
            "name": info.get("blend_name", "?"),
            "kind": info.get("kind", "group"),
            "status": status,
            "saveable": status in saveable,
            "pullable": status in pullable,
            "discardable": status in discardable,
            "mergeable": (status == SyncStatus.CONFLICT
                          and info.get("kind", "group") == "group"),
            "staged": sync_manager.is_staged(uid),
            "animated": bool(tree is not None and tree_has_animation(tree)),
        })
    rows.sort(key=lambda r: (r["status"].value, r["name"]))
    sync_manager.prune_staged(r["uid"] for r in rows if r["saveable"])
    deps = [{"name": d["child_name"], "parent": d["parent_name"]}
            for d in sync_manager.find_untracked_dependencies()]
    return {"rows": rows, "deps": deps, "ignored": ignored}


def _tag_ui_redraw(context):
    try:
        for area in context.screen.areas:
            area.tag_redraw()
    except Exception:
        pass


class GN_OT_SyncStageChange(bpy.types.Operator):
    bl_idname = "gn.sync_change_mark"
    bl_label = "Mark Change"
    bl_description = "Include this group in the next save to JSON"
    bl_options = {'REGISTER'}

    sync_uuid: StringProperty(name="UUID")

    def execute(self, context):
        if not self.sync_uuid:
            return {'CANCELLED'}
        sync_manager.stage_change(self.sync_uuid)
        _tag_ui_redraw(context)
        return {'FINISHED'}


class GN_OT_SyncUnstageChange(bpy.types.Operator):
    bl_idname = "gn.sync_change_unmark"
    bl_label = "Unmark Change"
    bl_description = "Exclude this group from the next save to JSON"
    bl_options = {'REGISTER'}

    sync_uuid: StringProperty(name="UUID")

    def execute(self, context):
        if not self.sync_uuid:
            return {'CANCELLED'}
        sync_manager.unstage_change(self.sync_uuid)
        _tag_ui_redraw(context)
        return {'FINISHED'}


class GN_OT_SyncStageAll(bpy.types.Operator):
    bl_idname = "gn.sync_stage_all"
    bl_label = "Mark All Changes"
    bl_description = "Mark every change for the next save to JSON"
    bl_options = {'REGISTER'}

    def execute(self, context):
        data = _change_rows()
        sync_manager.stage_all(r["uid"] for r in data["rows"] if r["saveable"])
        _tag_ui_redraw(context)
        return {'FINISHED'}


class GN_OT_SyncUnstageAll(bpy.types.Operator):
    bl_idname = "gn.sync_unstage_all"
    bl_label = "Unmark All Changes"
    bl_description = "Unmark every change"
    bl_options = {'REGISTER'}

    def execute(self, context):
        sync_manager.unstage_all()
        _tag_ui_redraw(context)
        return {'FINISHED'}


class GN_OT_SyncSaveStaged(bpy.types.Operator):
    bl_idname = "gn.sync_save_staged"
    bl_label = "Save Marked to JSON"
    bl_description = ("Write the marked groups' .blend content into their "
                      "JSON files (blend wins)")
    bl_options = {'REGISTER'}

    def execute(self, context):
        staged = sync_manager.staged_uuids()
        if not staged:
            self.report({'INFO'}, "Nothing marked - mark changes first")
            return {'FINISHED'}
        _collect_commit_suggestion()
        saved = blocked = errors = 0
        first_error = ""
        for uid in sorted(staged):
            status = sync_manager.check_status(uid)
            force = status in (SyncStatus.JSON_MODIFIED, SyncStatus.CONFLICT)
            try:
                ok = sync_manager.export_to_json(uid, force=force)
            except PermissionError as exc:
                errors += 1
                first_error = first_error or str(exc)
                continue
            if ok:
                sync_manager.unstage_change(uid)
                saved += 1
            else:
                blocked += 1
        sync_manager.save()
        _refresh_after_sync_change(context)
        msg = f"Saved {saved} group(s) to JSON"
        if blocked:
            msg += f", {blocked} blocked (JSON modified externally)"
        if errors:
            msg += f", {errors} failed (unwritable)"
        if first_error:
            msg += f" - {first_error}"
        note = _untracked_deps_note()
        if note:
            msg += note
        level = 'WARNING' if (blocked or errors) else 'INFO'
        self.report({level}, msg)
        return {'FINISHED'}


class GN_OT_SyncDiscardLocal(bpy.types.Operator):
    bl_idname = "gn.sync_discard_local"
    bl_label = "Discard Local Changes"
    bl_description = ("Throw away this group's local edits and take the "
                      "JSON version (the saved one wins)")
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: StringProperty(name="UUID")

    def invoke(self, context, event):
        info = sync_manager.metadata.get("tracked_groups", {}).get(
            self.sync_uuid, {})
        name = info.get("blend_name", "this group")
        return context.window_manager.invoke_confirm(
            self, event,
            title="Discard local changes?",
            message=(f"Discard local changes of '{name}' and take the JSON "
                     "version?"),
            confirm_text="Discard",
        )

    def execute(self, context):
        sync_manager.unstage_change(self.sync_uuid)
        tracker = sync_manager.resolve_conflict(self.sync_uuid, "json")
        sync_manager.save()
        if tracker is not None and tracker.error_count:
            self.report({'ERROR'},
                        "Could not apply the JSON side - check the console/log "
                        "(library-linked groups must be made local first)")
            return {'CANCELLED'}
        _refresh_after_sync_change(context)
        self.report({'INFO'}, "Local changes discarded - JSON version applied")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Graph view: disposable dependency board in the node editor
# ---------------------------------------------------------------------------

class GN_OT_GraphView(bpy.types.Operator):
    bl_idname = "gn.graph_view"
    bl_label = "Graph View"
    bl_description = ("Draw the dependency graph as a disposable node tree, "
                      "coloured by sync status (nothing is saved)")
    bl_options = {'REGISTER'}

    mode: EnumProperty(
        name="Scope",
        items=(('NEIGHBORHOOD', "Around a group",
                "The selected/active group and its dependencies/dependents"),
               ('PROJECT', "Whole project", "Every tracked group")),
        default='NEIGHBORHOOD',
    )
    hops: bpy.props.IntProperty(name="Hops", default=2, min=1, max=6)
    include_externals: BoolProperty(
        name="Include untracked references", default=True)
    focus_name: StringProperty(name="Focus group", default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "mode")
        if self.mode == 'NEIGHBORHOOD':
            col.prop(self, "hops")
        col.prop(self, "include_externals")

    def execute(self, context):
        from .graph_view import (GRAPH_TREE_NAME, PORT_TREE_PREFIX,
                                 build_graph_tree, focus_from_board,
                                 show_in_editor)
        if not sync_manager.metadata.get("tracked_groups"):
            self.report({'ERROR'}, "No groups tracked.")
            return {'CANCELLED'}
        focus = self.focus_name
        if self.mode == 'NEIGHBORHOOD' and not focus:
            active = _get_active_tree(context)
            if active is not None:
                if (active.name == GRAPH_TREE_NAME
                        or active.name.startswith(PORT_TREE_PREFIX)):
                    focus = focus_from_board(active)
                else:
                    focus = active.name
            if not focus:
                self.report({'ERROR'},
                            "Select a tracked group in a Node Editor (or "
                            "switch to the whole project scope).")
                return {'CANCELLED'}
        report = build_graph_tree(mode=self.mode, focus=focus,
                                  hops=self.hops,
                                  include_externals=self.include_externals)
        tree = bpy.data.node_groups.get(report["tree"])
        shown = show_in_editor(context, tree) if tree is not None else False
        cables = report["links"] - report.get("reroutes", 0)
        msg = f"Graph: {report['nodes']} nodes, {cables} links"
        if report.get("reroutes"):
            msg += f", {report['reroutes']} routing nodes"
        if report["externals"]:
            msg += f", {report['externals']} untracked reference(s)"
        if report["clipped"]:
            msg += f" ({report['clipped']} link(s) clipped)"
        if not shown:
            msg += " - open 'GNT Graph' from the editor header"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class GN_OT_GraphPoster(bpy.types.Operator):
    bl_idname = "gn.graph_poster"
    bl_label = "Graph Poster"
    bl_description = ("Export the board as a vector SVG poster (no Graphviz "
                      "needed; opens in any browser)")
    bl_options = {'REGISTER'}

    mode: EnumProperty(
        name="Scope",
        items=(('NEIGHBORHOOD', "Around a group",
                "The selected/active group and its dependencies/dependents"),
               ('PROJECT', "Whole project", "Every tracked group")),
        default='NEIGHBORHOOD',
    )
    hops: bpy.props.IntProperty(name="Hops", default=2, min=1, max=6)
    include_externals: BoolProperty(
        name="Include untracked references", default=True)
    focus_name: StringProperty(name="Focus group", default="")
    filepath: StringProperty(name="SVG file", subtype='FILE_PATH',
                             default="//GNT_graph.svg")
    source: EnumProperty(
        name="Layout",
        items=(('COMPACT', "Compact auto lay-out",
                "Recompute the compact layered layout"),
               ('BOARD', "As arranged on the board",
                "Use the node editor board's current positions, e.g. "
                "after an external node arrangement")),
        default='COMPACT',
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "mode")
        if self.mode == 'NEIGHBORHOOD':
            col.prop(self, "hops")
        col.prop(self, "include_externals")
        col.prop(self, "source")
        col.prop(self, "filepath")

    def execute(self, context):
        from .graph_utils import render_svg
        from .graph_view import (GRAPH_TREE_NAME, PORT_TREE_PREFIX,
                                 collect_board_model, focus_from_board)
        if not sync_manager.metadata.get("tracked_groups"):
            self.report({'ERROR'}, "No groups tracked.")
            return {'CANCELLED'}
        focus = self.focus_name
        if self.mode == 'NEIGHBORHOOD' and not focus:
            active = _get_active_tree(context)
            if active is not None:
                if (active.name == GRAPH_TREE_NAME
                        or active.name.startswith(PORT_TREE_PREFIX)):
                    focus = focus_from_board(active)
                else:
                    focus = active.name
            if not focus:
                self.report({'ERROR'},
                            "Select a tracked group in a Node Editor (or "
                            "switch to the whole project scope).")
                return {'CANCELLED'}
        model = collect_board_model(self.mode, focus, self.hops,
                                    self.include_externals)
        positions = routes = None
        if self.source == 'BOARD':
            from .graph_view import board_positions
            positions, routes = board_positions()
            if not positions:
                self.report({'ERROR'},
                            "No board on screen — build the graph view "
                            "first, or use the compact auto lay-out.")
                return {'CANCELLED'}
            missing = sorted(n for n in model["names"]
                             if n not in positions)
            if missing:
                self.report({'ERROR'},
                            "The board shows a different scope (missing: "
                            + ", ".join(missing[:3]) + ("…" if
                                                        len(missing) > 3
                                                        else "") + "); "
                            "rebuild it for this scope first.")
                return {'CANCELLED'}
        title = f"GNToolkit graph - {len(model['names'])} groups"
        if model["focus"]:
            title += f" - around {model['focus']} (+-{self.hops} hops)"
        svg = render_svg(model["names"], model["edges"],
                         model["status_keys"], focus=model["focus"],
                         externals=model["externals"], title=title,
                         positions=positions, routes=routes)
        path = bpy.path.abspath(self.filepath)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
        except OSError as exc:
            self.report({'ERROR'}, f"Could not write {path}: {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Poster written: {path}")
        return {'FINISHED'}


class GN_OT_GraphClear(bpy.types.Operator):
    bl_idname = "gn.graph_clear"
    bl_label = "Clear Graph"
    bl_description = "Remove the disposable graph board and its helper trees"
    bl_options = {'REGISTER'}

    def execute(self, context):
        from .graph_view import clear_graph_trees, unpin_graph_editors
        unpin_graph_editors(context)
        removed = clear_graph_trees()
        _tag_ui_redraw(context)
        self.report({'INFO'}, f"Graph cleared ({removed} tree(s) removed)")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Ignore changes (accept current state)
# ---------------------------------------------------------------------------

class GN_OT_SyncIgnore(bpy.types.Operator):
    bl_idname = "gn.sync_ignore"
    bl_label = "Ignore Changes"
    bl_description = "Hide this issue from the list — the real status is still tracked"
    bl_options = {'REGISTER'}
    sync_uuid: StringProperty(name="UUID", description="UUID of the tracked group")

    def execute(self, context):
        if not self.sync_uuid:
            tree = _get_active_tree(context)
            if tree:
                self.sync_uuid = find_uuid_for_tree(tree, sync_manager.metadata) or ""
        if not self.sync_uuid:
            self.report({'ERROR'}, "No tracked group found")
            return {'CANCELLED'}

        sync_manager.ignore_changes(self.sync_uuid)
        sync_manager.save()
        for area in context.screen.areas:
            area.tag_redraw()
        self.report({'INFO'}, "Issue ignored - status still tracked")
        return {'FINISHED'}


class GN_OT_SyncUnignore(bpy.types.Operator):
    bl_idname = "gn.sync_unignore"
    bl_label = "Un-ignore Changes"
    bl_description = "Show this issue in the list again"
    bl_options = {'REGISTER'}

    sync_uuid: StringProperty(name="UUID", description="UUID of the tracked group")

    def execute(self, context):
        if not self.sync_uuid:
            tree = _get_active_tree(context)
            if tree:
                self.sync_uuid = find_uuid_for_tree(tree, sync_manager.metadata) or ""
        if not self.sync_uuid:
            self.report({'ERROR'}, "No tracked group found")
            return {'CANCELLED'}

        sync_manager.unignore_changes(self.sync_uuid)
        sync_manager.save()
        for area in context.screen.areas:
            area.tag_redraw()
        self.report({'INFO'}, "Issue un-ignored")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Check all statuses
# ---------------------------------------------------------------------------

class GN_OT_SyncCheck(bpy.types.Operator):
    bl_idname = "gn.sync_check"
    bl_label = "Refresh Status"
    bl_description = "Recompute the sync status of every tracked group"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if sync_manager.check_busy():
            self.report({'WARNING'}, "Status check already running")
            return {'CANCELLED'}
        state = getattr(context.scene, "gnt_merge_state", None)
        if state is not None and state.group_name:
            state.group_name = ""
            state.status = ""
            state.summary = ""
            state.items.clear()
        total = sync_manager.start_check_all()
        if not total:
            self.report({'INFO'}, "No tracked groups")
            return {'FINISHED'}
        ensure_check_pump()
        self.report({'INFO'}, f"Checking {total} groups...")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Chunked status check pump — keeps the UI responsive on huge projects
# ---------------------------------------------------------------------------

_check_pump_handle = None


def ensure_check_pump():
    global _check_pump_handle
    if _check_pump_handle is not None:
        return
    handle = bpy.app.timers.register(_check_pump_tick, first_interval=0.05)
    if handle is None:
        # No timers in this context (background/script): run synchronously
        # so the caller still gets a complete result.
        sync_manager.step_check_all(10 ** 9)
        try:
            from .graph_view import recolor_board
            recolor_board()
        except Exception:
            pass
        return
    _check_pump_handle = handle


def stop_check_pump():
    global _check_pump_handle
    if _check_pump_handle is not None:
        try:
            bpy.app.timers.unregister(_check_pump_tick)
        except Exception:
            pass
        _check_pump_handle = None
    sync_manager.cancel_check_all()


def _check_pump_tick():
    global _check_pump_handle
    try:
        done, total = sync_manager.step_check_all(20)
    except Exception:
        sync_manager.cancel_check_all()
        _check_pump_handle = None
        return None
    if total and done < total:
        _tag_git_redraw()
        return 0.05
    _check_pump_handle = None
    statuses = sync_manager._status_cache
    n_synced = sum(1 for s in statuses.values() if s == SyncStatus.SYNCED)
    n_issues = len(statuses) - n_synced
    if n_issues == 0:
        _show_status_message(f"GNToolkit: all {len(statuses)} groups synced")
    else:
        _show_status_message(
            f"GNToolkit: {n_issues} of {len(statuses)} groups need attention")
    _tag_git_redraw()
    try:
        from .graph_view import recolor_board
        recolor_board()
    except Exception:
        pass
    try:
        from .git_integration import queue_status_refresh
        queue_status_refresh()
        ensure_git_pump()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Operators: Git transport (thin layer over the git CLI)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Git job pump — advances the pipeline stages and delivers results
# ---------------------------------------------------------------------------

_git_pump_handle = None


def ensure_git_pump():
    global _git_pump_handle
    if _git_pump_handle is not None:
        return
    handle = bpy.app.timers.register(_git_pump_tick, first_interval=0.1)
    if handle is None:
        return
    _git_pump_handle = handle


def stop_git_pump():
    global _git_pump_handle
    if _git_pump_handle is not None:
        try:
            bpy.app.timers.unregister(_git_pump_tick)
        except Exception:
            pass
        _git_pump_handle = None
    from .git_integration import shutdown_git_jobs
    shutdown_git_jobs()


def _git_pump_tick():
    global _git_pump_handle
    from . import git_integration as gi
    gi.advance_git_jobs()
    for kind, payload, result in gi.drain_git_results():
        try:
            handle_git_job_done(kind, payload, result)
        except Exception:
            pass
    if gi.git_busy():
        return 0.1
    _git_pump_handle = None
    return None


def _network_timeout() -> int:
    """Remote-operation timeout (seconds) from the add-on preferences."""
    prefs = _addon_preferences(bpy.context)
    try:
        value = int(getattr(prefs, "network_timeout", 300) or 300)
    except (TypeError, ValueError):
        value = 300
    return max(10, min(3600, value))


def git_submit(kind, **payload):
    ensure_git_pump()
    from .git_integration import submit_git_job
    if kind == "sync":
        payload.setdefault("network_timeout", _network_timeout())
    submit_git_job(kind, **payload)


def git_online_allowed() -> bool:
    return getattr(bpy.app, "online_access", True)


def _show_status_message(msg, duration=6.0):
    def _show(text=msg):
        try:
            bpy.context.workspace.status_text_set(text)
        except Exception:
            pass

    def _hide():
        try:
            bpy.context.workspace.status_text_set(None)
        except Exception:
            pass

    try:
        bpy.app.timers.register(_show, first_interval=0.1)
        bpy.app.timers.register(_hide, first_interval=duration)
    except Exception:
        pass


def _tag_git_redraw():
    try:
        for area in bpy.context.screen.areas:
            area.tag_redraw()
    except Exception:
        pass


def _affected_uids_for_files(repo, files):
    from .sync_metadata import resolve_json_path
    affected = set()
    if not files:
        return affected
    blend_dir = sync_manager._blend_dir()
    for uid, info in sync_manager.metadata.get("tracked_groups", {}).items():
        jp = info.get("json_path", "")
        if not jp:
            continue
        abs_path = os.path.normcase(os.path.normpath(
            resolve_json_path(jp, blend_dir)))
        for rel in files:
            repo_abs = os.path.normcase(os.path.normpath(
                os.path.join(repo, rel.replace("/", os.sep))))
            if abs_path == repo_abs:
                affected.add(uid)
    return affected


def handle_git_job_done(kind, payload, result):
    from .git_integration import (
        set_git_state, clear_status_job_flag, queue_status_refresh,
    )
    if kind == "status":
        clear_status_job_flag()
        set_git_state(result)
        _tag_git_redraw()
        return
    if kind == "commit":
        if result.get("ok"):
            _show_status_message(
                f"Git: committed {len(result['committed'])} file(s)")
        elif result.get("nothing"):
            _show_status_message(
                "Git: nothing to commit — no tracked JSON changed")
        else:
            _show_status_message(
                f"Git commit failed: {result.get('detail', 'unknown error')}",
                duration=10.0)
        queue_status_refresh()
        _tag_git_redraw()
        return
    if kind == "sync":
        status = result.get("status")
        detail = result.get("detail", "")
        if status in ("ok", "merged"):
            affected = _affected_uids_for_files(
                payload["repo"], result.get("files", []))
            if affected:
                for uid in affected:
                    sync_manager.invalidate_cache(uid)
                sync_manager.check_all_statuses()
            if status == "merged":
                _show_status_message(
                    f"Git sync: merged {len(result.get('files', []))} "
                    "file(s) with the remote changes and pushed")
            elif affected:
                _show_status_message(
                    f"Git sync complete — {len(affected)} group(s) "
                    "refreshed from remote")
            else:
                _show_status_message("Git sync complete")
        elif status == "merge_conflict":
            report = result.get("report")
            if report:
                from .sync_ui import _store_merge_report
                _store_merge_report(bpy.context, report)
            _show_status_message(
                f"Git merge: {detail} — see the merge report",
                duration=10.0)
        elif status in ("dirty", "merge_blocked"):
            _show_status_message(detail, duration=10.0)
        elif status == "diverged":
            _show_status_message(
                "Git: versions diverged — resolve with your Git tooling "
                "(pull could not fast-forward)", duration=10.0)
        else:
            _show_status_message(
                f"Git sync failed: {detail}", duration=10.0)
        queue_status_refresh()
        _tag_git_redraw()


class GN_OT_GitCommit(bpy.types.Operator):
    bl_idname = "gn.git_commit"
    bl_label = "Git Commit"
    bl_description = ("Commit the tracked JSON files that changed to the local git "
                      "repository — only files managed by this addon are staged")
    bl_options = {'REGISTER'}

    repo: StringProperty(name="Repository")
    message: StringProperty(
        name="Message",
        description="Commit message (single line, like git commit -m)",
        default="Update node groups",
    )

    def invoke(self, context, event):
        if not self.repo or not os.path.isdir(self.repo):
            self.report({'ERROR'}, "Repository not found")
            return {'CANCELLED'}
        suggestion = getattr(sync_manager, "commit_suggestion", "")
        if suggestion:
            self.message = suggestion[:300]
        return context.window_manager.invoke_props_dialog(self, width=520)

    def execute(self, context):
        if not self.repo or not os.path.isdir(self.repo):
            self.report({'ERROR'}, "Repository not found")
            return {'CANCELLED'}
        git_submit("commit", repo=self.repo,
                   message=self.message.strip() or "Update node groups")
        self.report({'INFO'}, "Git commit started in background...")
        return {'FINISHED'}


class GN_OT_GitSync(bpy.types.Operator):
    bl_idname = "gn.git_sync"
    bl_label = "Git Sync"
    bl_description = ("Bring remote changes in and push local commits; when "
                      "the branch diverged, the tracked JSON files merge per "
                      "item (nothing is written if anything conflicts)")
    bl_options = {'REGISTER'}

    repo: StringProperty(name="Repository")

    def execute(self, context):
        if not self.repo or not os.path.isdir(self.repo):
            self.report({'ERROR'}, "Repository not found")
            return {'CANCELLED'}
        if not git_online_allowed():
            self.report({'ERROR'},
                        "Online access is disabled in Blender preferences - "
                        "Git Sync needs it to push/pull. Git Commit still "
                        "works locally")
            return {'CANCELLED'}
        git_submit("sync", repo=self.repo)
        self.report({'INFO'}, "Git sync started in background...")
        return {'FINISHED'}


class GN_OT_RevealRepo(bpy.types.Operator):
    bl_idname = "gn.git_reveal_repo"
    bl_label = "Reveal Repository"
    bl_description = "Open the git repository folder in the system file browser"
    bl_options = {'REGISTER'}

    repo: StringProperty(name="Repository")

    def execute(self, context):
        if not self.repo or not os.path.isdir(self.repo):
            self.report({'ERROR'}, "Repository not found")
            return {'CANCELLED'}
        try:
            opened = bpy.ops.wm.path_open(filepath=self.repo)
        except Exception:
            opened = set()
        if "FINISHED" not in opened:
            self.report({'WARNING'}, "Could not open the system file browser")
            return {'CANCELLED'}
        return {'FINISHED'}


def _addon_preferences(context):
    try:
        return context.preferences.addons[__package__].preferences
    except Exception:
        return None


class GN_OT_GitSaveCredentials(bpy.types.Operator):
    bl_idname = "gn.git_save_credentials"
    bl_label = "Save Credentials"
    bl_description = ("Store the token for the host: the OS vault when "
                      "available, otherwise the add-on preferences")
    bl_options = {'REGISTER'}

    def execute(self, context):
        from . import credentials
        prefs = _addon_preferences(context)
        if prefs is None:
            self.report({'ERROR'}, "Add-on preferences are not available")
            return {'CANCELLED'}
        host = (prefs.git_host or "").strip()
        token = (prefs.git_token or "").strip()
        if not host:
            self.report({'ERROR'}, "Set the host first (e.g. github.com)")
            return {'CANCELLED'}
        if not token:
            self.report({'ERROR'}, "Enter a token to save")
            return {'CANCELLED'}
        source = credentials.save(f"https://{host}/", prefs.git_username,
                                  token)
        if source == "vault":
            self.report({'INFO'}, f"Token stored in the OS vault for {host}")
        elif source == "preferences":
            self.report({'WARNING'},
                        f"Token stored UNENCRYPTED in Blender preferences for "
                        f"{host} (no OS vault available)")
        else:
            self.report({'INFO'}, f"Token kept for this session ({host})")
        return {'FINISHED'}


class GN_OT_GitForgetCredentials(bpy.types.Operator):
    bl_idname = "gn.git_forget_credentials"
    bl_label = "Forget Credentials"
    bl_description = "Delete the stored token for the host"
    bl_options = {'REGISTER'}

    def execute(self, context):
        from . import credentials
        prefs = _addon_preferences(context)
        if prefs is None:
            self.report({'ERROR'}, "Add-on preferences are not available")
            return {'CANCELLED'}
        host = (prefs.git_host or "").strip()
        if not host:
            self.report({'ERROR'}, "Set the host first (e.g. github.com)")
            return {'CANCELLED'}
        credentials.forget(f"https://{host}/")
        self.report({'INFO'}, f"Stored credentials for {host} removed")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Track untracked dependencies (contextual)
# ---------------------------------------------------------------------------

class GN_OT_SyncTrackDeps(bpy.types.Operator):
    bl_idname = "gn.sync_track_deps"
    bl_label = "Track Dependencies"
    bl_description = ("Start tracking untracked dependency groups found in the .blend: "
                      "each one is written to its own JSON file in the folder (or into "
                      "the master package) and recorded with its own entry")
    bl_options = {'REGISTER', 'UNDO'}

    group_name: StringProperty(
        name="Group",
        description="Track only this dependency group (empty = every untracked one)",
    )

    def execute(self, context):
        only = [self.group_name] if self.group_name else None
        result = sync_manager.track_untracked_dependencies(only_names=only)
        sync_manager.save()
        _refresh_after_sync_change(context)
        if result["errors"]:
            self.report({'WARNING'},
                        f"Tracked {result['tracked']} dependency group(s), "
                        f"{result['errors']} error(s) - see the log")
        elif result["tracked"]:
            self.report({'INFO'},
                        f"Now tracking {result['tracked']} dependency group(s)")
        else:
            self.report({'INFO'}, "No untracked dependencies found")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Track all groups (batch)
# ---------------------------------------------------------------------------

def _mass_counts_link_all() -> dict:
    inner_names = custom_inner_tree_names()
    groups = [ng for ng in bpy.data.node_groups
              if is_trackable_tree_type(ng.type)
              and ng.name not in inner_names]
    linked = sum(1 for ng in groups if group_library_path(ng))
    tracked = len(sync_manager.metadata.get("tracked_groups", {}))
    return {"total": len(groups), "linked": linked, "tracked": tracked}


class GN_OT_SyncLinkAll(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.sync_link_all"
    bl_label = "Track All"
    bl_description = ("Track every node group (Geometry Nodes, Shader and Compositor), writing one master JSON from the "
                      "current .blend — the addon CREATES the JSON here (first save). "
                      "Use 'Track from Existing JSON' instead when the JSON already exists "
                      "elsewhere and must not be rewritten")
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def invoke(self, context, event):
        counts = _mass_counts_link_all()
        if counts["total"] - counts["linked"] <= 1:
            return super().invoke(context, event)
        if not self.filepath:
            stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "nodegroups"
            base = sync_manager._blend_dir() or os.getcwd()
            self.filepath = os.path.join(base, f"{stem}.json")
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        layout = self.layout
        counts = _mass_counts_link_all()
        layout.label(text=f"Track {counts['total'] - counts['linked']} group(s)?",
                     icon='FILE_TICK')
        col = layout.column(align=True)
        col.label(text=f"Groups in the .blend: {counts['total']}")
        if counts["linked"]:
            col.label(text=f"Library-linked, skipped: {counts['linked']}",
                      icon='LINK_BLEND')
        col.label(text=f"Already tracked (re-written): {counts['tracked']}")
        if self.filepath and os.path.isfile(self.filepath):
            col.label(text="The existing JSON will be overwritten — a copy is kept "
                           "in backups/", icon='ERROR')
        layout.separator()
        layout.prop(self, "filepath")
        layout.label(text="A backup of the destination file is written to backups/",
                     icon='INFO')

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({'INFO'},
                        "The .blend is not saved yet - tracking is kept in memory "
                        "until you save. Save the file to persist the sidecar and "
                        "relative JSON paths.")

        if self.filepath and not self.filepath.lower().endswith(".json"):
            self.filepath += ".json"

        inner_names = custom_inner_tree_names()
        gn_groups = [ng for ng in bpy.data.node_groups
                     if is_trackable_tree_type(ng.type)
                     and ng.name not in inner_names]
        if not gn_groups:
            self.report({'ERROR'},
                        "No trackable node groups found in this .blend")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, len(gn_groups))

        try:
            result = sync_manager.link_all_groups(self.filepath, context)
            sync_manager.save()

            msg = (f"Tracking {result['linked']} groups, "
                   f"{result['skipped']} already tracked, "
                   f"{result['errors']} errors")
            if result.get("linked_skipped"):
                msg += (f", {result['linked_skipped']} library-linked skipped - "
                        "make them local to track")
            context.window_manager.progress_end()
            self.report({'INFO'}, msg)
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Batch link failed: {e}")
            return {'CANCELLED'}

        _refresh_after_sync_change(context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Track all materials (batch)
# ---------------------------------------------------------------------------

def _mass_counts_link_all_materials() -> dict:
    mats = [m for m in bpy.data.materials
            if getattr(m, "use_nodes", False) and m.node_tree is not None]
    linked = sum(1 for m in mats if m.library is not None)
    tracked = sum(1 for _uid, info in
                  sync_manager.metadata.get("tracked_groups", {}).items()
                  if info.get("kind") == "material")
    return {"total": len(mats), "linked": linked, "tracked": tracked}


class GN_OT_SyncLinkAllMaterials(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.sync_link_all_materials"
    bl_label = "Track All Materials"
    bl_description = ("Track every material with a node tree, writing one "
                      "master JSON from the current .blend — the addon "
                      "CREATES the JSON here (first save). Versioned per "
                      "material: its node tree plus the curated settings "
                      "(mix mode, culling, viewport display...)")
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def invoke(self, context, event):
        counts = _mass_counts_link_all_materials()
        if not self.filepath:
            stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "file"
            base = sync_manager._blend_dir() or os.getcwd()
            self.filepath = os.path.join(base, f"{stem}_materials.json")
        if counts["total"] - counts["linked"] <= 1:
            return super().invoke(context, event)
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        counts = _mass_counts_link_all_materials()
        layout.label(text=f"Track {counts['total'] - counts['linked']} material(s)?",
                     icon='MATERIAL')
        col = layout.column(align=True)
        col.label(text=f"Materials with nodes: {counts['total']}")
        if counts["linked"]:
            col.label(text=f"Library-linked, skipped: {counts['linked']}",
                      icon='LINK_BLEND')
        col.label(text=f"Already tracked (re-written): {counts['tracked']}")
        layout.separator()
        layout.prop(self, "filepath")
        layout.label(text="Each entry versions the node tree plus the curated "
                          "material settings", icon='INFO')

    def execute(self, context):
        if self.filepath and not self.filepath.lower().endswith(".json"):
            self.filepath += ".json"
        try:
            result = sync_manager.link_all_materials(self.filepath, context)
            sync_manager.save()
        except Exception as e:
            self.report({'ERROR'}, f"Batch link failed: {e}")
            return {'CANCELLED'}
        msg = (f"Tracking {result['linked']} material(s), "
               f"{result['skipped']} already tracked, "
               f"{result['errors']} errors")
        if result.get("linked_skipped"):
            msg += (f", {result['linked_skipped']} library-linked skipped - "
                    "make them local to track")
        self.report({'INFO'}, msg)
        _refresh_after_sync_change(context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Track all groups from a folder of per-group exports
# ---------------------------------------------------------------------------

class GN_OT_SyncLinkFolder(bpy.types.Operator, ImportHelper):
    bl_idname = "gn.sync_link_folder"
    bl_label = "Track All from Folder"
    bl_description = ("Track every per-group JSON export found in a folder ('Export package' "
                      "with folder structure): pick any file inside the exported folder, and "
                      "each JSON whose group exists in the .blend is tracked against its own "
                      "file. The JSONs are read only and are NOT modified")
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def execute(self, context):
        from .socket_utils import get_tree_dependencies
        from .sync_metadata import (
            generate_uuid, add_tracked_group, store_uuid_on_tree,
            make_json_path_relative,
        )
        from .hash_utils import (
            canonical_hash_from_tree, canonical_hash_from_json_group,
            canonical_hash_from_json_path,
        )

        chosen = self.filepath
        if not chosen:
            self.report({'ERROR'}, "Select any file inside the exported folder")
            return {'CANCELLED'}

        scan_dir = os.path.dirname(chosen)
        ng_dir = os.path.join(scan_dir, "NodeGroups")
        if os.path.isdir(ng_dir):
            scan_dir = ng_dir

        try:
            files = sorted(
                os.path.join(scan_dir, f) for f in os.listdir(scan_dir)
                if f.lower().endswith(".json")
            )
        except OSError:
            self.report({'ERROR'}, f"Cannot read folder: {scan_dir}")
            return {'CANCELLED'}
        if not files:
            self.report({'ERROR'}, f"No JSON files found in '{scan_dir}'")
            return {'CANCELLED'}

        if not sync_manager.metadata.get("tracked_groups"):
            sync_manager.metadata = {
                "version": ADDON_VERSION,
                "tracked_groups": {},
            }

        # First pass: tracking entries for every group with a matching tree
        linked_by_name: dict[str, str] = {}
        skipped = 0
        errors = 0
        linked_skipped = 0
        for fp in files:
            data = read_json_tolerant(fp)
            if data is None:
                reason = json_read_failure_reason(fp)
                if reason == "encoding":
                    self.report({'ERROR'},
                                f"'{os.path.basename(fp)}' is not valid UTF-8 - "
                                "re-save it as UTF-8 and retry")
                else:
                    self.report({'ERROR'}, f"Failed to read '{os.path.basename(fp)}'")
                errors += 1
                continue
            if not isinstance(data, dict):
                errors += 1
                continue
            groups = data.get("node_groups", {})
            if not groups and "nodes" in data and data.get("name"):
                groups = {data["name"]: data}
            if not groups:
                # Embedded-entry files (Materials/Worlds) are handled below.
                if not any(isinstance(data.get(s), dict)
                           for s in EMBEDDED_KINDS.values()):
                    skipped += 1
                continue

            for gname in groups:
                tree = bpy.data.node_groups.get(gname)
                if tree is None:
                    skipped += 1
                    continue
                if group_library_path(tree):
                    linked_skipped += 1
                    skipped += 1
                    continue
                if get_uuid_from_tree(tree) or find_uuid_for_tree(tree, sync_manager.metadata):
                    skipped += 1
                    continue

                uid = generate_uuid()
                blend_hash = canonical_hash_from_tree(tree)
                json_hash = canonical_hash_from_json_group(fp, gname)
                if json_hash is None:
                    json_hash = canonical_hash_from_json_path(fp)
                mtime = os.path.getmtime(fp)
                stored_path = make_json_path_relative(fp, sync_manager._saved_blend_dir())
                add_tracked_group(
                    sync_manager.metadata, uid, gname, stored_path,
                    blend_hash, json_hash, mtime, layout="folder",
                )
                store_uuid_on_tree(tree, uid)
                linked_by_name[gname] = uid

        # Embedded trees: the folder's Materials/ and Worlds/ subfolders
        # (each file is a package-shaped single-owner entry).  Picking a
        # file inside those subfolders scans them directly.
        from .hash_utils import (canonical_hash_from_json_embedded,
                                 canonical_hash_from_json_path)
        from .serializer import canonical_hash_from_embedded
        if os.path.basename(scan_dir) in ("Materials", "Worlds"):
            embed_dirs = [(scan_dir, os.path.basename(scan_dir))]
        else:
            parent = (os.path.dirname(scan_dir)
                      if os.path.basename(scan_dir) == "NodeGroups"
                      else scan_dir)
            embed_dirs = [(os.path.join(parent, sub), sub)
                          for sub in ("Materials", "Worlds")]

        embedded_linked = 0
        for sub_dir, sub in embed_dirs:
            if not os.path.isdir(sub_dir):
                continue
            kind = "material" if sub == "Materials" else "world"
            section = EMBEDDED_KINDS[kind]
            for fname in sorted(os.listdir(sub_dir)):
                if not fname.lower().endswith(".json"):
                    continue
                fp = os.path.join(sub_dir, fname)
                data = read_json_tolerant(fp)
                if not isinstance(data, dict):
                    errors += 1
                    continue
                entries = data.get(section)
                if not isinstance(entries, dict):
                    if "nodes" in data and data.get("name"):
                        entries = {data["name"]: data}
                    else:
                        continue
                for ename in entries:
                    coll = (bpy.data.materials if kind == "material"
                            else bpy.data.worlds)
                    owner = coll.get(ename)
                    if (owner is None or not getattr(owner, "use_nodes", False)
                            or getattr(owner, "node_tree", None) is None):
                        skipped += 1
                        continue
                    if getattr(owner, "library", None) is not None:
                        linked_skipped += 1
                        skipped += 1
                        continue
                    if owner.get(SYNC_UUID_PROP):
                        skipped += 1
                        continue
                    uid = generate_uuid()
                    blend_hash = canonical_hash_from_embedded(owner, kind)
                    json_hash = canonical_hash_from_json_embedded(fp, kind,
                                                                  ename)
                    if json_hash is None:
                        json_hash = canonical_hash_from_json_path(fp)
                    mtime = os.path.getmtime(fp)
                    stored_path = make_json_path_relative(
                        fp, sync_manager._saved_blend_dir())
                    add_tracked_group(
                        sync_manager.metadata, uid, ename, stored_path,
                        blend_hash, json_hash, mtime, layout="folder",
                        kind=kind,
                    )
                    owner[SYNC_UUID_PROP] = uid
                    try:
                        owner.use_fake_user = True
                    except Exception:
                        pass
                    embedded_linked += 1

        # Second pass: dependency edges (mirrors Track All's two-pass logic)
        for gname, uid in linked_by_name.items():
            tree = bpy.data.node_groups.get(gname)
            if tree is None:
                continue
            dep_uuids = []
            for dep_name in get_tree_dependencies(tree):
                dep_tree = bpy.data.node_groups.get(dep_name)
                if dep_tree is not None:
                    dep_uid = get_uuid_from_tree(dep_tree)
                    if dep_uid and dep_uid != uid:
                        dep_uuids.append(dep_uid)
            info = sync_manager.metadata.get("tracked_groups", {}).get(uid)
            if info is not None:
                info["depends_on"] = dep_uuids

        sync_manager._dirty = True
        sync_manager.save()
        _refresh_after_sync_change(context)

        msg = (f"Tracked {len(linked_by_name)} group(s) from folder, "
               f"{skipped} skipped (already tracked or not in .blend)")
        if embedded_linked:
            msg += f", {embedded_linked} material/world(s)"
        if linked_skipped:
            msg += f", {linked_skipped} library-linked (make them local to track)"
        if errors:
            msg += f", {errors} unreadable file(s)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Save all groups (batch)
# ---------------------------------------------------------------------------

class GN_OT_SyncExportAll(bpy.types.Operator):
    bl_idname = "gn.sync_export_all"
    bl_label = "Save All to JSON"
    bl_description = "Write every tracked group's .blend content into its JSON file"
    bl_options = {'REGISTER'}

    force: bpy.props.BoolProperty(
        name="Force Overwrite",
        description="Overwrite JSON even if it was modified externally",
        default=True,
    )

    def invoke(self, context, event):
        tracked = sync_manager.metadata.get("tracked_groups", {})
        if len(tracked) <= 1:
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        tracked = sync_manager.metadata.get("tracked_groups", {})
        files = len({info.get("json_path", "") for info in tracked.values()
                     if info.get("json_path")})
        layout.label(text=f"Save all {len(tracked)} tracked groups to JSON?", icon='EXPORT')
        layout.label(text=f"{files} JSON file(s) will be overwritten — a backup "
                          "copy is kept in backups/")
        layout.label(text="Groups missing from the .blend are skipped", icon='INFO')

    def execute(self, context):
        tracked = sync_manager.metadata.get("tracked_groups", {})
        if not tracked:
            self.report({'ERROR'}, "No groups tracked. Use 'Track All' or 'Track from Existing JSON' first.")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, len(tracked))
        try:
            _collect_commit_suggestion()
            result = sync_manager.export_all(force=self.force, context=context)
            sync_manager.save()
            context.window_manager.progress_end()
            _refresh_after_sync_change(context)
            note = _untracked_deps_note()
            self.report({'WARNING' if note else 'INFO'},
                        f"Saved {result['exported']} groups, "
                        f"skipped {result['skipped']}, "
                        f"errors {result['errors']}" + note)
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Save failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Save only modified groups (batch)
# ---------------------------------------------------------------------------

class GN_OT_SyncExportModified(bpy.types.Operator):
    bl_idname = "gn.sync_export_modified"
    bl_label = "Save Modified to JSON"
    bl_description = "Write only edited or conflicting groups into their JSON files"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        summary = sync_manager.get_status_summary()
        affected = summary.get("blend_modified", 0) + summary.get("conflict", 0)
        if affected <= 1:
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        summary = sync_manager.get_status_summary()
        affected = summary.get("blend_modified", 0) + summary.get("conflict", 0)
        layout.label(text=f"Save {affected} modified group(s) to JSON?", icon='EXPORT')
        layout.label(text="Their JSON files will be overwritten — a backup copy "
                          "is kept in backups/")

    def execute(self, context):
        tracked = sync_manager.metadata.get("tracked_groups", {})
        if not tracked:
            self.report({'ERROR'}, "No groups tracked. Use 'Track All' or 'Track from Existing JSON' first.")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, len(tracked))
        try:
            _collect_commit_suggestion()
            result = sync_manager.export_all_modified(force=True, context=context)
            sync_manager.save()
            context.window_manager.progress_end()
            _refresh_after_sync_change(context)
            note = _untracked_deps_note()
            self.report({'WARNING' if note else 'INFO'},
                        f"Saved {result['exported']} groups, "
                        f"skipped {result['skipped']}, "
                        f"errors {result['errors']}" + note)
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Save failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Pull all modified groups (batch)
# ---------------------------------------------------------------------------

class GN_OT_SyncImportModified(bpy.types.Operator):
    bl_idname = "gn.sync_import_modified"
    bl_label = "Pull from JSON"
    bl_description = ("Apply JSON changes to the .blend for every group that changed in the JSON; "
                      "warns before overwriting local edits")
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        # Check for blend_modified groups to warn user
        blend_modified_count = sync_manager.count_local_changes()

        if blend_modified_count > 0:
            return context.window_manager.invoke_confirm(
                self, event,
                title=f"Overwrite {blend_modified_count} local change(s)?",
                message=(
                    f"{blend_modified_count} group(s) have local modifications that will be "
                    f"permanently overwritten with the JSON version.\n\n"
                    f"Are you sure you want to continue?"
                ),
                icon='WARNING',
            )
        return self.execute(context)

    def execute(self, context):
        tracked = sync_manager.metadata.get("tracked_groups", {})
        if not tracked:
            self.report({'ERROR'}, "No groups tracked. Use 'Track All' or 'Track from Existing JSON' first.")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, len(tracked))
        try:
            result = sync_manager.import_all_modified(context)
            sync_manager.save()
            context.window_manager.progress_end()
            _set_failed_imports(context.scene, result.get("failures", []))
            # Force UI redraw so issues disappear immediately
            for area in context.screen.areas:
                area.tag_redraw()
            self.report({'INFO'},
                        f"Pulled {result['imported']}, "
                        f"skipped {result['skipped']}, "
                        f"errors {result['errors']}, "
                        f"auto-tracked {result['auto_linked']}"
                        + (f", {result.get('still_differ', 0)} still differ from JSON"
                           if result.get('still_differ') else "")
                        + (f", {result.get('linked_skipped', 0)} library-linked skipped"
                           if result.get('linked_skipped') else "")
                        + (f" - {len(result.get('failures', []))} failed, "
                           "see the panel to retry"
                           if result.get('failures') else ""))
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Pull failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


class GN_OT_SyncRetryFailed(bpy.types.Operator):
    bl_idname = "gn.sync_retry_failed"
    bl_label = "Retry Failed"
    bl_description = "Import again only the groups that failed in the last batch pull"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        state = getattr(context.scene, "gnt_failed_imports", None)
        names = [item.group_name for item in state.items] if state else []
        if not names:
            self.report({'INFO'}, "Nothing to retry")
            return {'CANCELLED'}
        context.window_manager.progress_begin(0, len(names))
        try:
            result = sync_manager.import_all_modified(context, only_names=names)
            sync_manager.save()
            context.window_manager.progress_end()
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Retry failed: {e}")
            return {'CANCELLED'}
        _set_failed_imports(context.scene, result.get("failures", []))
        for area in context.screen.areas:
            area.tag_redraw()
        if result.get("failures"):
            self.report({'WARNING'},
                        f"{len(result['failures'])} group(s) still failing - see the panel")
        else:
            self.report({'INFO'},
                        f"Retry complete - {result['imported']} group(s) imported")
        return {'FINISHED'}


class GN_OT_SyncFailuresClear(bpy.types.Operator):
    bl_idname = "gn.sync_failures_clear"
    bl_label = "Dismiss"
    bl_description = "Clear the failed-import list"
    bl_options = {'REGISTER'}

    def execute(self, context):
        _set_failed_imports(context.scene, [])
        for area in context.screen.areas:
            area.tag_redraw()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Track from existing JSON (no sidecar)
# ---------------------------------------------------------------------------

class GN_OT_SyncInitialize(bpy.types.Operator, ImportHelper):
    bl_idname = "gn.sync_initialize"
    bl_label = "Track from Existing JSON"
    bl_description = ("Start tracking groups using an existing JSON as the source of truth — "
                      "a unified package (one file) or a single-group export ('Export package' "
                      "with folder structure). The JSON is read only and is NOT modified; "
                      "use 'Track All' instead to write a new master JSON from the current .blend")
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def execute(self, context):
        import json
        import os
        from .sync_manager import SyncManager, SyncStatus
        from .sync_metadata import (
            generate_uuid, add_tracked_group, store_uuid_on_tree,
            save_sync_metadata,
        )
        from .hash_utils import canonical_hash_from_tree, canonical_hash_from_json_data

        if not bpy.data.filepath:
            self.report({'INFO'},
                        "The .blend is not saved yet - tracking is kept in memory "
                        "until you save. Save the file to persist the sidecar and "
                        "relative JSON paths.")

        json_path = self.filepath
        if not os.path.isfile(json_path):
            self.report({'ERROR'}, f"JSON file not found: {json_path}")
            return {'CANCELLED'}

        # Load JSON
        data = read_json_tolerant(json_path)
        if data is None:
            reason = json_read_failure_reason(json_path)
            if reason == "encoding":
                self.report({'ERROR'},
                            f"'{os.path.basename(json_path)}' is not valid UTF-8 - "
                            "re-save it as UTF-8 in your editor (File > Save As > "
                            "UTF-8) and try again")
            elif reason == "conflict":
                self.report({'ERROR'},
                            f"'{os.path.basename(json_path)}' has merge conflicts - "
                            "resolve them with your Git tooling")
            else:
                self.report({'ERROR'}, "Failed to read JSON (unreadable or concurrent write)")
            return {'CANCELLED'}

        if not isinstance(data, dict):
            self.report({'ERROR'},
                        f"'{os.path.basename(json_path)}' is not a node group package - "
                        "pick the unified package (single file) or a per-group export "
                        "(NodeGroups folder)")
            return {'CANCELLED'}

        groups = data.get("node_groups", {})
        if not groups and "nodes" in data and data.get("name"):
            groups = {data["name"]: data}
        if not groups:
            if "modifier_name" in data:
                self.report({'ERROR'},
                            f"'{os.path.basename(json_path)}' is a modifier export, "
                            "not a node group package - pick the unified package "
                            "(single file) or a per-group export (NodeGroups folder)")
            else:
                self.report({'ERROR'},
                            f"No node groups found in '{os.path.basename(json_path)}' - "
                            "pick the unified package (single file) or a per-group "
                            "export (NodeGroups folder)")
            return {'CANCELLED'}

        # Initialize sync manager if needed
        if not sync_manager.metadata.get("tracked_groups"):
            sync_manager.metadata = {
                "version": ADDON_VERSION,
                "tracked_groups": {},
            }

        linked = 0
        skipped = 0
        divergent = 0
        linked_skipped = 0
        file_layout = ("package" if data.get("type") == "GN_UNIFIED_PACKAGE"
                       and len(groups) > 1 else "folder")

        for gname in groups:
            tree = bpy.data.node_groups.get(gname)
            if tree is None:
                skipped += 1
                continue
            if group_library_path(tree):
                linked_skipped += 1
                skipped += 1
                continue

            # Skip if already tracked
            existing_uid = get_uuid_from_tree(tree)
            if existing_uid:
                existing_info = sync_manager.get_tracked_group(existing_uid)
                if existing_info is not None:
                    skipped += 1
                    continue

            # Create tracking entry with current hashes
            uid = generate_uuid()
            blend_hash = canonical_hash_from_tree(tree)
            json_hash = canonical_hash_from_json_data(groups[gname])
            mtime = os.path.getmtime(json_path)

            # When the existing JSON already differs from the .blend, the
            # divergence must stay visible: store the BLEND hash as the
            # baseline for both sides (so check_status reports
            # JSON_MODIFIED — "the JSON has changes to pull") and force the
            # mtime fast-path off so the JSON hash is actually compared.
            if json_hash != blend_hash:
                json_hash = blend_hash
                mtime = 0.0
                divergent += 1

            add_tracked_group(
                sync_manager.metadata, uid, gname, json_path,
                blend_hash, json_hash, mtime, layout=file_layout,
            )
            store_uuid_on_tree(tree, uid)
            linked += 1

        sync_manager._dirty = True
        sync_manager.save()

        # Refresh status cache
        _refresh_after_sync_change(context)

        msg = f"Tracking started: {linked} groups, {skipped} skipped (already tracked or not in .blend)"
        if linked_skipped:
            msg += f", {linked_skipped} library-linked (make them local to track)"
        if divergent:
            msg += f", {divergent} differ from the JSON (use Pull to apply)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Operator: selective group import with dependency closure (F3)
# ---------------------------------------------------------------------------

def _load_import_package(filepath: str) -> dict:
    """Read a JSON package into (data, groups, embedded) with one read."""
    import os
    data = None
    groups = {}
    embedded = []
    mtime = None
    if filepath and os.path.isfile(filepath):
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            mtime = None
        data = read_json_tolerant(filepath)
        if isinstance(data, dict):
            if data.get("type") == "GN_UNIFIED_PACKAGE":
                groups = data.get("node_groups", {})
                for kind, section in EMBEDDED_KINDS.items():
                    entries = data.get(section)
                    if isinstance(entries, dict):
                        for name, entry in entries.items():
                            embedded.append({"kind": kind, "name": name,
                                             "data": entry})
            elif "nodes" in data:
                groups = {data.get("name", "?"): data}
    return {"filepath": filepath, "mtime": mtime, "data": data,
            "groups": groups, "embedded": embedded}


def _get_import_package(cache: dict, filepath: str) -> dict:
    """Return the parsed package for *filepath*, re-reading only on change.

    The picker's draw() runs on every UI redraw, and parsing a 439-group
    package takes ~a second — so the result is cached keyed by (path,
    mtime).  *cache* is a plain dict owned by the caller.  The cached
    plan also carries the per-group in-blend map (see
    ``_ensure_import_exists``).
    """
    import os
    mtime = None
    if filepath and os.path.isfile(filepath):
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            mtime = None
    cached = cache.get("plan")
    if cached is not None and cached.get("filepath") == filepath and cached.get("mtime") == mtime:
        return cached
    plan = _load_import_package(filepath)
    plan["exists"] = {}
    plan["ng_len"] = -1
    cache["plan"] = plan
    return plan


def _ensure_import_exists(plan: dict) -> None:
    """Refresh the in-blend map of the package without per-draw lookups.

    Querying ``bpy.data.node_groups`` for 439 names on every draw is the
    picker's cost driver; instead the map is rebuilt only when the node
    group count changes (importing through the picker bumps it).
    """
    ng_len = len(bpy.data.node_groups)
    if plan.get("ng_len") == ng_len:
        return
    groups = plan.get("groups", {})
    plan["exists"] = {gname: bpy.data.node_groups.get(gname) is not None
                      for gname in groups}
    plan["ng_len"] = ng_len


# Module-level cache for the picker panel (draw() runs on every redraw;
# keyed by path + mtime via _get_import_package).
_import_package_cache: dict = {}


def _compute_import_plan(groups: dict, group_name: str) -> dict | None:
    """Analyse what importing *group_name* over *groups* would do.

    Returns ``{"ordered", "existing", "missing", "divergent",
    "external"}`` — the closure (dependencies first), the members
    already present in the .blend, the missing ones, the existing ones
    whose .blend version differs from the package, and the referenced
    groups that are not in the package.  Returns None when the group is
    not in the package.
    """
    from .hash_utils import canonicalize_node_tree_data
    from .importer import json_group_closure
    from .serializer import serialize_node_tree


    if group_name not in groups:
        return None
    ordered, external = json_group_closure(groups, group_name)
    external_set = set(external)
    existing = []
    missing = []
    divergent = []
    for name in ordered:
        tree = bpy.data.node_groups.get(name)
        if tree is None:
            missing.append(name)
            continue
        existing.append(name)
        entry = groups.get(name)
        if entry is None:
            continue
        try:
            # Group nodes whose reference is NOT in the package carry
            # socket records that depend on whatever tree is attached at
            # import time — not verifiable content, so they are dropped
            # from the divergence comparison.
            def _canon_stripped(raw):
                canon = canonicalize_node_tree_data(raw)
                canon.pop("derived_node_groups", None)
                for nd in canon.get("nodes", []):
                    if (is_group_node_bl_idname(nd.get("type"))
                            and nd.get("node_tree_reference") in external_set):
                        nd.pop("inputs", None)
                        nd.pop("outputs", None)
                return canon
            tree_canon = _canon_stripped(
                serialize_node_tree(tree, derived=False))
            json_canon = _canon_stripped(entry)
            if tree_canon != json_canon:
                divergent.append(name)
        except Exception:
            pass
    return {
        "ordered": ordered,
        "existing": existing,
        "missing": missing,
        "divergent": divergent,
        "external": external,
    }


class GN_ImportItem(bpy.types.PropertyGroup):
    """One group name offered by the picker's native search widget."""
    name: StringProperty()


class GN_ImportState(bpy.types.PropertyGroup):
    """State of the panel-based import picker (Scene.gnt_import_state).

    The group search uses Blender's native ``prop_search`` widget (its
    popup filters live as you type) and the in-blend markers are shown
    in the plan preview under the field.
    """
    open: BoolProperty(default=False)
    filepath: StringProperty()
    group_name: StringProperty()
    items: CollectionProperty(type=GN_ImportItem)


class GN_FailedImport(bpy.types.PropertyGroup):
    """One group that failed in a batch pull (Scene.gnt_failed_imports)."""
    group_name: StringProperty()
    reason: StringProperty()


class GN_FailedImportsState(bpy.types.PropertyGroup):
    """Failed groups of the last batch pull, offered for retry."""
    items: CollectionProperty(type=GN_FailedImport)
    summary: StringProperty()


def _set_failed_imports(scene, failures) -> None:
    state = getattr(scene, "gnt_failed_imports", None)
    if state is None:
        return
    state.items.clear()
    for name, reason in failures:
        item = state.items.add()
        item.group_name = name
        item.reason = reason
    state.summary = (f"{len(failures)} group(s) failed to import"
                     if failures else "")


_EMBEDDED_PREFIXES = {"material": "Material: ", "world": "World: "}


def _qualified_embedded_name(kind: str, name: str) -> str:
    """Picker display name of an embedded entry (kind-qualified)."""
    return _EMBEDDED_PREFIXES.get(kind, "Material: ") + name


def _parse_qualified_embedded(name: str):
    """Split a qualified embedded item name into (kind, plain_name)."""
    for kind, prefix in _EMBEDDED_PREFIXES.items():
        if name.startswith(prefix):
            return kind, name[len(prefix):]
    return None, name


def _fill_import_items(state) -> None:
    """(Re)build the searchable list from the selected package.

    Groups keep their plain names; embedded entries are kind-qualified
    ("Material: X" / "World: Y") so the flat search list stays readable.
    """
    state.items.clear()
    package = _get_import_package(_import_package_cache, state.filepath)
    for gname in sorted(package.get("groups", {})):
        item = state.items.add()
        item.name = gname
    for entry in sorted(package.get("embedded", []),
                        key=lambda e: (e["kind"], e["name"])):
        item = state.items.add()
        item.name = _qualified_embedded_name(entry["kind"], entry["name"])


# Live refresh while the picker is open: panels only redraw on UI
# events, so a JSON edit on disk would not show until the mouse moves.
# A lightweight timer redraws the areas every 0.5s while open (and
# stops itself when the picker closes).  The draw itself stays cached —
# no file reads or hashing unless the file actually changed.  The
# registration is tracked with a flag: ``bpy.app.timers.register`` may
# return None (background/script contexts), so the handle is unusable.
_picker_pump_active: bool = False


def _ensure_picker_pump() -> None:
    """Start (once) the picker refresh timer; stops when it closes."""
    global _picker_pump_active
    if _picker_pump_active:
        return

    def _pump():
        global _picker_pump_active
        try:
            state = bpy.context.scene.gnt_import_state
            if not state or not state.open:
                _picker_pump_active = False
                return None
            for win in bpy.context.window_manager.windows:
                for area in win.screen.areas:
                    area.tag_redraw()
        except Exception:
            _picker_pump_active = False
            return None
        return 0.5

    bpy.app.timers.register(_pump, first_interval=0.5)
    _picker_pump_active = True


# Preview of the selected group's import plan, keyed by
# (filepath, package mtime, group_name, node-group count) so it is
# computed once per selection instead of on every draw.  The mtime makes
# a JSON edit on disk refresh the preview (e.g. to test divergence).
_selection_plan_cache: dict = {}


def _compute_embedded_plan(package, kind: str, name: str) -> dict | None:
    """Plan for an embedded picker selection (owner presence/divergence)."""
    entry = None
    for item in package.get("embedded", []):
        if item["kind"] == kind and item["name"] == name:
            entry = item["data"]
            break
    if entry is None:
        return None
    coll = (bpy.data.materials if kind == "material" else bpy.data.worlds)
    owner = coll.get(name)
    if (owner is None or not getattr(owner, "use_nodes", False)
            or getattr(owner, "node_tree", None) is None):
        return {"ordered": [name], "existing": [], "missing": [name],
                "divergent": [], "external": [],
                "embedded": {"kind": kind, "name": name}}
    divergent = []
    try:
        from .hash_utils import canonical_hash_from_json_data
        from .serializer import canonical_hash_from_embedded
        if (canonical_hash_from_embedded(owner, kind)
                != canonical_hash_from_json_data(entry)):
            divergent.append(name)
    except Exception:
        pass
    return {"ordered": [name], "existing": [name], "missing": [],
            "divergent": divergent, "external": [],
            "embedded": {"kind": kind, "name": name}}


def _compute_selection_plan(state) -> dict | None:
    """Return the cached closure plan shown under the search field."""
    package = _get_import_package(_import_package_cache, state.filepath)
    key = (state.filepath, package.get("mtime"), state.group_name,
           len(bpy.data.node_groups))
    cached = _selection_plan_cache.get(key)
    if cached is not None:
        return cached
    kind, plain = _parse_qualified_embedded(state.group_name)
    if kind:
        plan = _compute_embedded_plan(package, kind, plain)
    else:
        plan = _compute_import_plan(package.get("groups", {}),
                                    state.group_name)
    if plan is None:
        return None
    _selection_plan_cache.clear()
    _selection_plan_cache[key] = plan
    return plan


def _divergence_parts(divergent, metadata, pkg_name=None) -> list:
    """Actionable divergence hints: tracked vs untracked members.

    Untracked groups need Track first (Track Group/Track All), tracked
    ones align directly with Pull / Keep JSON.
    """
    from .sync_metadata import find_uuid_for_tree
    tracked = []
    untracked = []
    for name in divergent:
        tree = bpy.data.node_groups.get(name)
        uid = find_uuid_for_tree(tree, metadata) if tree is not None else None
        (tracked if uid else untracked).append(name)
    base = "Divergent" + (f" vs {pkg_name}" if pkg_name else "")
    parts = []
    if untracked:
        parts.append(f"{base}: {', '.join(untracked)} - not tracked yet; "
                     "Track Group/Track All, then Pull to align")
    if tracked:
        parts.append(f"{base}: {', '.join(tracked)} - Pull or Keep JSON to align")
    return parts


def _tracked_elsewhere_notes(state, plan) -> list:
    """Members tracked to a JSON different from the picker's package."""
    import os
    from .sync_metadata import find_uuid_for_tree, resolve_json_path
    notes = []
    for name in plan.get("existing", []):
        tree = bpy.data.node_groups.get(name)
        if tree is None:
            continue
        uid = find_uuid_for_tree(tree, sync_manager.metadata)
        if not uid:
            continue
        info = sync_manager.metadata.get("tracked_groups", {}).get(uid, {})
        jp = resolve_json_path(info.get("json_path", ""), sync_manager._blend_dir())
        if jp and os.path.normpath(jp) != os.path.normpath(state.filepath):
            notes.append((name, os.path.basename(jp)))
    return notes


class GN_OT_SyncImportGroupClose(bpy.types.Operator):
    bl_idname = "gn.sync_import_group_close"
    bl_label = "Close Import Picker"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        state = context.scene.gnt_import_state
        state.open = False
        state.group_name = ""
        state.filepath = ""
        state.items.clear()
        _selection_plan_cache.clear()
        for area in context.screen.areas:
            area.tag_redraw()
        return {'FINISHED'}


class GN_OT_SyncImportGroupFile(bpy.types.Operator, ImportHelper):
    bl_idname = "gn.sync_import_group_file"
    bl_label = "Import Group from JSON…"
    bl_description = ("Pick a JSON package and import one group plus its missing "
                      "dependencies. Groups already present in the .blend are never touched.")
    bl_options = {'REGISTER'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def execute(self, context):
        import os
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({'ERROR'}, f"JSON file not found: {self.filepath}")
            return {'CANCELLED'}
        state = context.scene.gnt_import_state
        state.filepath = self.filepath
        state.group_name = ""
        state.open = True
        _fill_import_items(state)
        if len(state.items) == 0:
            reason = json_read_failure_reason(self.filepath)
            if reason in ("encoding", "conflict", "json"):
                state.open = False
                if reason == "encoding":
                    self.report({'ERROR'},
                                f"'{os.path.basename(self.filepath)}' is not valid UTF-8 - "
                                "re-save it as UTF-8 in your editor (File > Save As > "
                                "UTF-8) and try again")
                elif reason == "conflict":
                    self.report({'ERROR'},
                                f"'{os.path.basename(self.filepath)}' has merge conflicts - "
                                "resolve them with your Git tooling")
                else:
                    self.report({'ERROR'},
                                f"'{os.path.basename(self.filepath)}' is not valid JSON")
                return {'CANCELLED'}
        _selection_plan_cache.clear()
        _ensure_picker_pump()
        for area in context.screen.areas:
            area.tag_redraw()
        return {'FINISHED'}


class GN_OT_SyncImportGroup(bpy.types.Operator):
    bl_idname = "gn.sync_import_group"
    bl_label = "Import Group from JSON"
    bl_description = ("Import one node group plus its missing dependencies from a JSON "
                      "package. Groups already present in the .blend are never touched.")
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(
        name="Package",
        description="JSON package to import from",
    )
    group_name: StringProperty(
        name="Group",
        description="Node group to import from the selected JSON package",
    )

    def execute(self, context):
        import os
        from .codec import coercion_count, reset_coercion_count
        from .error_tracker import ImportErrorTracker
        from .importer import import_node_tree_recursive

        reset_coercion_count()

        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({'ERROR'}, "Select a JSON package file first")
            return {'CANCELLED'}
        if not self.group_name:
            self.report({'ERROR'}, "Pick a group from the list first")
            return {'CANCELLED'}

        # Embedded entries are kind-qualified ("Material: X" / "World: Y"):
        # import (creating the owner when missing) or report untouched.
        emb_kind, emb_name = _parse_qualified_embedded(self.group_name)
        if emb_kind:
            from .importer import import_embedded_entry
            package = _get_import_package(_import_package_cache, self.filepath)
            entry = next((i["data"] for i in package.get("embedded", [])
                          if i["kind"] == emb_kind and i["name"] == emb_name),
                         None)
            if entry is None:
                self.report({'ERROR'},
                            f"{emb_kind.capitalize()} '{emb_name}' not found "
                            f"in the package")
                return {'CANCELLED'}
            coll = (bpy.data.materials if emb_kind == "material"
                    else bpy.data.worlds)
            owner = coll.get(emb_name)
            if (owner is not None and getattr(owner, "use_nodes", False)
                    and getattr(owner, "node_tree", None) is not None):
                _selection_plan_cache.clear()
                self.report({'INFO'},
                            f"{emb_kind.capitalize()} '{emb_name}' already "
                            "exists (untouched) - use Import Package/Folder "
                            "with 'Update existing groups', or track it and "
                            "Pull from JSON")
                return {'FINISHED'}
            tracker = ImportErrorTracker()
            owner = import_embedded_entry(entry, emb_kind,
                                          package.get("groups", {}),
                                          context, tracker)
            _selection_plan_cache.clear()
            for area in context.screen.areas:
                area.tag_redraw()
            if owner is None:
                self.report({'WARNING'},
                            f"Could not import {emb_kind} '{emb_name}' "
                            f"({tracker.issue_count} issue(s) - check console)")
                return {'FINISHED'}
            sync_manager.mark_pending_save()
            self.report({'INFO'},
                        f"Imported {emb_kind} '{emb_name}' (tree + versioned "
                        "settings) - not tracked yet; use Track Material/World "
                        "to sync")
            return {'FINISHED'}

        package = _get_import_package(_import_package_cache, self.filepath)
        groups = package.get("groups", {})
        target_data = groups.get(self.group_name)
        if target_data is None:
            self.report({'ERROR'}, f"Group '{self.group_name}' not found in the package")
            return {'CANCELLED'}
        plan = _compute_import_plan(groups, self.group_name)
        if plan is None:
            self.report({'ERROR'}, f"Group '{self.group_name}' not found in the package")
            return {'CANCELLED'}

        tracker = ImportErrorTracker()
        imported = []
        if bpy.data.node_groups.get(self.group_name) is None:
            # Fresh import: the recursive importer pulls the whole
            # missing dependency closure from the full package cache.
            tree = import_node_tree_recursive(target_data, groups, {}, context, tracker)
            if tree is not None:
                imported.append(self.group_name)
        else:
            # Skip mode: existing groups are never touched; only the
            # missing closure members are imported.
            for name in plan["missing"]:
                entry = groups.get(name)
                if entry is None:
                    continue
                tree = import_node_tree_recursive(entry, groups, {}, context, tracker)
                if tree is not None:
                    imported.append(name)
        _selection_plan_cache.clear()

        if imported:
            sync_manager.mark_pending_save()

        for area in context.screen.areas:
            area.tag_redraw()

        parts = []
        if imported:
            parts.append(f"Imported {len(imported)}: {', '.join(imported)}")
        if plan["existing"]:
            parts.append(f"{len(plan['existing'])} already existed (untouched)")
            parts.append("to update an existing group use Import Package/Folder "
                         "with 'Update existing groups' (or Pull from JSON when "
                         "tracked)")
        if plan["divergent"]:
            parts.extend(_divergence_parts(plan["divergent"], sync_manager.metadata,
                                           os.path.basename(self.filepath)))
        if plan["external"]:
            parts.append(f"Unconnected refs: {', '.join(plan['external'])}")
        if tracker.has_issues:
            parts.append(f"{tracker.issue_count} import issue(s) - check console")
        coercions = coercion_count()
        if coercions:
            parts.append(f"{coercions} value coercion(s) (GNT_DEBUG=1 for details)")
        msg = " - ".join(parts) or "Nothing to import"
        if imported:
            msg += " - not tracked yet; use Track Group / Track All to sync"
        self.report({'WARNING'} if (plan["divergent"] or plan["external"]
                                    or tracker.has_issues) else {'INFO'}, msg)
        return {'FINISHED'}


class GN_OT_SyncImportGroupTrack(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.sync_import_group_track"
    bl_label = "Track Imported Group to JSON…"
    bl_description = ("Track the imported group (and its untracked dependencies) to a JSON "
                      "package: the group is written into the JSON and becomes sync-tracked")
    bl_options = {'REGISTER'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    group_name: StringProperty(
        name="Group",
        description="Node group to track",
    )

    def execute(self, context):
        import os
        from .sync_metadata import find_uuid_for_tree
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({'ERROR'}, "Choose a JSON package file first")
            return {'CANCELLED'}
        tree = bpy.data.node_groups.get(self.group_name)
        if tree is None:
            self.report({'ERROR'}, f"Group '{self.group_name}' not found in the .blend")
            return {'CANCELLED'}
        uid = find_uuid_for_tree(tree, sync_manager.metadata)
        if uid:
            info = sync_manager.metadata.get("tracked_groups", {}).get(uid, {})
            self.report({'WARNING'},
                        f"'{self.group_name}' is already tracked to "
                        f"{info.get('json_path', '?')} - use Save/Pull instead")
            return {'CANCELLED'}
        sync_manager.link_group(tree, self.filepath)
        sync_manager.save()
        for area in context.screen.areas:
            area.tag_redraw()
        self.report({'INFO'},
                    f"Tracked '{self.group_name}' to {os.path.basename(self.filepath)}")
        return {'FINISHED'}


classes = (
    GN_ImportItem,
    GN_ImportState,
    GN_FailedImport,
    GN_FailedImportsState,
    GN_OT_SyncInitialize,
    GN_OT_SyncLink,
    GN_OT_SyncLinkSceneCompositor,
    GN_OT_SyncLinkMaterial,
    GN_OT_SyncLinkWorld,
    GN_OT_SyncLinkAllMaterials,
    GN_OT_SyncUnlink,
    GN_OT_SyncUnlinkAll,
    GN_OT_SyncImport,
    GN_OT_SyncExport,
    GN_OT_SyncStageChange,
    GN_OT_SyncUnstageChange,
    GN_OT_SyncStageAll,
    GN_OT_SyncUnstageAll,
    GN_OT_SyncSaveStaged,
    GN_OT_SyncDiscardLocal,
    GN_OT_SyncIgnore,
    GN_OT_SyncUnignore,
    GN_OT_SyncCheck,
    GN_OT_SyncTrackDeps,
    GN_OT_SyncLinkAll,
    GN_OT_SyncLinkFolder,
    GN_OT_SyncExportAll,
    GN_OT_SyncExportModified,
    GN_OT_SyncImportModified,
    GN_OT_SyncRetryFailed,
    GN_OT_SyncFailuresClear,
    GN_OT_GraphView,
    GN_OT_GraphPoster,
    GN_OT_GraphClear,
    GN_OT_SyncImportGroup,
    GN_OT_SyncImportGroupFile,
    GN_OT_SyncImportGroupTrack,
    GN_OT_SyncImportGroupClose,
    GN_OT_GitCommit,
    GN_OT_GitSync,
    GN_OT_RevealRepo,
    GN_OT_GitSaveCredentials,
    GN_OT_GitForgetCredentials,
)
