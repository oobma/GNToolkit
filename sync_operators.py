# -*- coding: utf-8 -*-
"""
gn_toolkit.sync_operators — Blender operators for DNA/RNA sync actions.

Provides operators for linking, unlinking, importing, exporting, ignoring,
resolving, and checking the sync state of Geometry Node groups.
"""

from __future__ import annotations

import bpy
from bpy.props import StringProperty, EnumProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .constants import ADDON_VERSION
from .importer import restore_zone_area, end_zone_session
from .sync_manager import read_json_tolerant, sync_manager, SyncStatus
from .sync_metadata import find_tree_by_uuid, find_uuid_for_tree, get_uuid_from_tree


# ---------------------------------------------------------------------------
# Helper: find active node tree
# ---------------------------------------------------------------------------

def _get_active_tree(context):
    """Return the active Geometry Nodes tree from the context, or None."""
    space = getattr(context, 'space_data', None)
    if space is not None:
        tree = getattr(space, 'node_tree', None)
        if tree is not None and hasattr(tree, 'type') and tree.type == 'GEOMETRY':
            return tree
    # Fallback: check the active object's modifier
    obj = context.active_object
    if obj:
        for mod in obj.modifiers:
            if mod.type == 'NODES' and mod.node_group:
                return mod.node_group
    return None


def _get_all_geometry_trees():
    """Return all Geometry Node trees in the current .blend."""
    return [t for t in bpy.data.node_groups if t.type == 'GEOMETRY']


# ---------------------------------------------------------------------------
# Operator: Track a group (write JSON from blend)
# ---------------------------------------------------------------------------

class GN_OT_SyncLink(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.sync_link"
    bl_label = "Track Group"
    bl_description = "Start tracking the active group: writes its JSON from the current .blend content (first commit)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    tree_name: StringProperty(name="Node Group", description="Name of the node group to link")

    def invoke(self, context, event):
        tree = _get_active_tree(context)
        if tree is None:
            self.report({'ERROR'}, "No active Geometry Node group found")
            return {'CANCELLED'}
        self.tree_name = tree.name
        self.filepath = tree.name + ".json"
        context.window_manager.progress_begin(0, 100)
        return super().invoke(context, event)

    def execute(self, context):
        if not self.tree_name:
            self.report({'ERROR'}, "No node group specified")
            return {'CANCELLED'}

        tree = bpy.data.node_groups.get(self.tree_name)
        if tree is None:
            self.report({'ERROR'}, f"Node group '{self.tree_name}' not found")
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
            self.report({'INFO'}, f"Now tracking '{tree.name}' (UUID: {sync_uuid[:8]}...)")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to link: {e}")
            return {'CANCELLED'}

        context.window_manager.progress_end()
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
        for area in context.screen.areas:
            area.tag_redraw()
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
            self.report({'INFO'}, "Already synced — nothing to import")
            return {'CANCELLED'}

        tracker = sync_manager.import_from_json(self.sync_uuid, context)
        try:
            sync_manager.save()
        finally:
            end_zone_session()
            restore_zone_area()
        # Force UI redraw so issue disappears immediately
        for area in context.screen.areas:
            area.tag_redraw()

        if tracker.has_errors:
            self.report({'WARNING'},
                        f"Pull completed with {tracker.warn_count} warnings — check console")
        else:
            self.report({'INFO'}, "Pull from JSON completed")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Commit a group to JSON

class GN_OT_SyncExport(bpy.types.Operator):
    bl_idname = "gn.sync_export"
    bl_label = "Commit to JSON"
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

        success = sync_manager.export_to_json(self.sync_uuid)
        if success:
            sync_manager.save()
            # Force UI redraw so issue disappears immediately
            for area in context.screen.areas:
                area.tag_redraw()
            self.report({'INFO'}, "Commit to JSON completed")
            return {'FINISHED'}
        else:
            self.report({'ERROR'},
                        "Commit blocked — the JSON was modified externally. "
                        "Pull first or resolve the conflict.")
            return {'CANCELLED'}


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
        self.report({'INFO'}, "Issue ignored — status still tracked")
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
# Operator: Resolve conflict
# ---------------------------------------------------------------------------

class GN_OT_SyncResolve(bpy.types.Operator):
    bl_idname = "gn.sync_resolve"
    bl_label = "Resolve Conflict"
    bl_description = "Resolve a sync conflict by choosing which side to keep"
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: StringProperty(name="UUID", description="UUID of the tracked group")
    keep: EnumProperty(
        name="Keep Side",
        description="Which version to keep",
        items=[
            ('blend', 'Keep .blend', 'Keep the .blend version and update the JSON'),
            ('json', 'Keep JSON', 'Keep the JSON version and update the .blend'),
        ],
    )

    def execute(self, context):
        if not self.sync_uuid:
            tree = _get_active_tree(context)
            if tree:
                self.sync_uuid = find_uuid_for_tree(tree, sync_manager.metadata) or ""
        if not self.sync_uuid:
            self.report({'ERROR'}, "No tracked group found")
            return {'CANCELLED'}

        sync_manager.resolve_conflict(self.sync_uuid, self.keep)
        sync_manager.save()
        restore_zone_area()
        # Force UI redraw so issue disappears immediately
        for area in context.screen.areas:
            area.tag_redraw()

        side = ".blend" if self.keep == "blend" else "JSON"
        self.report({'INFO'}, f"Conflict resolved — kept {side} version")
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
        sync_manager.invalidate_cache()
        statuses = sync_manager.check_all_statuses()
        # Force UI redraw to show updated statuses
        for area in context.screen.areas:
            area.tag_redraw()
        n_synced = sum(1 for s in statuses.values() if s == SyncStatus.SYNCED)
        n_issues = len(statuses) - n_synced
        if n_issues == 0:
            self.report({'INFO'}, f"All {len(statuses)} groups synced")
        else:
            self.report({'WARNING'},
                        f"{n_issues} of {len(statuses)} groups need attention")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Track dependencies
# ---------------------------------------------------------------------------

class GN_OT_SyncLinkDeps(bpy.types.Operator):
    bl_idname = "gn.sync_link_deps"
    bl_label = "Track Dependencies"
    bl_description = "Start tracking untracked groups that share the same JSON file"
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: StringProperty(name="UUID", description="UUID of the parent tracked group")

    def execute(self, context):
        if not self.sync_uuid:
            tree = _get_active_tree(context)
            if tree:
                self.sync_uuid = find_uuid_for_tree(tree, sync_manager.metadata) or ""
        if not self.sync_uuid:
            self.report({'ERROR'}, "No tracked group found")
            return {'CANCELLED'}

        new_uuids = sync_manager.link_dependencies(self.sync_uuid)
        sync_manager.save()

        if new_uuids:
            self.report({'INFO'}, f"Now tracking {len(new_uuids)} dependency group(s)")
        else:
            self.report({'INFO'}, "No untracked dependencies found")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Track all groups (batch)
# ---------------------------------------------------------------------------

class GN_OT_SyncLinkAll(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.sync_link_all"
    bl_label = "Track All"
    bl_description = "Track every Geometry Nodes group in one master JSON, written from the current .blend (first commit)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({'INFO'},
                        "The .blend is not saved yet — tracking is kept in memory "
                        "until you save. Save the file to persist the sidecar and "
                        "relative JSON paths.")

        gn_groups = [ng for ng in bpy.data.node_groups if ng.type == 'GEOMETRY']
        if not gn_groups:
            self.report({'ERROR'}, "No Geometry Node groups found in this .blend")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, len(gn_groups))

        try:
            result = sync_manager.link_all_groups(self.filepath, context)
            sync_manager.save()

            msg = (f"Tracking {result['linked']} groups, "
                   f"{result['skipped']} already tracked, "
                   f"{result['errors']} errors")
            context.window_manager.progress_end()
            self.report({'INFO'}, msg)
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Batch link failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Commit all groups (batch)
# ---------------------------------------------------------------------------

class GN_OT_SyncExportAll(bpy.types.Operator):
    bl_idname = "gn.sync_export_all"
    bl_label = "Commit All to JSON"
    bl_description = "Write every tracked group's .blend content into its JSON file"
    bl_options = {'REGISTER'}

    force: bpy.props.BoolProperty(
        name="Force Overwrite",
        description="Overwrite JSON even if it was modified externally",
        default=True,
    )

    def execute(self, context):
        tracked = sync_manager.metadata.get("tracked_groups", {})
        if not tracked:
            self.report({'ERROR'}, "No groups tracked. Use 'Track All' or 'Track from Existing JSON' first.")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, len(tracked))
        try:
            result = sync_manager.export_all(force=self.force, context=context)
            sync_manager.save()
            context.window_manager.progress_end()
            # Force UI redraw so issues disappear immediately
            for area in context.screen.areas:
                area.tag_redraw()
            self.report({'INFO'},
                        f"Committed {result['exported']} groups, "
                        f"skipped {result['skipped']}, "
                        f"errors {result['errors']}")
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Commit failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Commit only modified groups (batch)
# ---------------------------------------------------------------------------

class GN_OT_SyncExportModified(bpy.types.Operator):
    bl_idname = "gn.sync_export_modified"
    bl_label = "Commit Modified to JSON"
    bl_description = "Write only edited or conflicting groups into their JSON files"
    bl_options = {'REGISTER'}

    def execute(self, context):
        tracked = sync_manager.metadata.get("tracked_groups", {})
        if not tracked:
            self.report({'ERROR'}, "No groups tracked. Use 'Track All' or 'Track from Existing JSON' first.")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, len(tracked))
        try:
            result = sync_manager.export_all_modified(force=True, context=context)
            sync_manager.save()
            context.window_manager.progress_end()
            # Force UI redraw so issues disappear immediately
            for area in context.screen.areas:
                area.tag_redraw()
            self.report({'INFO'},
                        f"Committed {result['exported']} groups, "
                        f"skipped {result['skipped']}, "
                        f"errors {result['errors']}")
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Commit failed: {e}")
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
            try:
                sync_manager.save()
            finally:
                end_zone_session()
                restore_zone_area()
            context.window_manager.progress_end()
            # Force UI redraw so issues disappear immediately
            for area in context.screen.areas:
                area.tag_redraw()
            self.report({'INFO'},
                        f"Pulled {result['imported']}, "
                        f"skipped {result['skipped']}, "
                        f"errors {result['errors']}, "
                        f"auto-tracked {result['auto_linked']}"
                        + (f", {result.get('still_differ', 0)} still differ from JSON"
                           if result.get('still_differ') else ""))
        except Exception as e:
            end_zone_session()
            restore_zone_area()
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Pull failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Track from existing JSON (no sidecar)
# ---------------------------------------------------------------------------

class GN_OT_SyncInitialize(bpy.types.Operator, ImportHelper):
    bl_idname = "gn.sync_initialize"
    bl_label = "Track from Existing JSON"
    bl_description = ("Start tracking all groups using an existing JSON as the source of truth — "
                      "the JSON is read only and is NOT modified")
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
                        "The .blend is not saved yet — tracking is kept in memory "
                        "until you save. Save the file to persist the sidecar and "
                        "relative JSON paths.")

        json_path = self.filepath
        if not os.path.isfile(json_path):
            self.report({'ERROR'}, f"JSON file not found: {json_path}")
            return {'CANCELLED'}

        # Load JSON
        data = read_json_tolerant(json_path)
        if data is None:
            self.report({'ERROR'}, "Failed to read JSON (unreadable or concurrent write)")
            return {'CANCELLED'}

        groups = data.get("node_groups", {})
        if not groups:
            self.report({'ERROR'}, "No node groups found in JSON")
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

        for gname in groups:
            tree = bpy.data.node_groups.get(gname)
            if tree is None:
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
                blend_hash, json_hash, mtime
            )
            store_uuid_on_tree(tree, uid)
            linked += 1

        sync_manager._dirty = True
        sync_manager.save()

        # Refresh status cache
        sync_manager.invalidate_cache()
        sync_manager.check_all_statuses()

        msg = f"Tracking started: {linked} groups, {skipped} skipped (already tracked or not in .blend)"
        if divergent:
            msg += f", {divergent} differ from the JSON (use Pull to apply)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    GN_OT_SyncInitialize,
    GN_OT_SyncLink,
    GN_OT_SyncUnlink,
    GN_OT_SyncUnlinkAll,
    GN_OT_SyncImport,
    GN_OT_SyncExport,
    GN_OT_SyncIgnore,
    GN_OT_SyncUnignore,
    GN_OT_SyncResolve,
    GN_OT_SyncCheck,
    GN_OT_SyncLinkDeps,
    GN_OT_SyncLinkAll,
    GN_OT_SyncExportAll,
    GN_OT_SyncExportModified,
    GN_OT_SyncImportModified,
)