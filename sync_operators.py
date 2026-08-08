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
from .sync_manager import read_json_tolerant, sync_manager, SyncStatus
from .sync_metadata import find_tree_by_uuid, find_uuid_for_tree, get_uuid_from_tree


# ---------------------------------------------------------------------------
# Helper: find active node tree
# ---------------------------------------------------------------------------

def _get_active_tree(context):
    """Return the active Geometry Nodes tree from the context, or None."""
    space = getattr(context, 'space_data', None)
    if space is None:
        return None
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
# Operator: Link a group to JSON
# ---------------------------------------------------------------------------

class GN_OT_SyncLink(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.sync_link"
    bl_label = "Link Group to JSON (DNA)"
    bl_description = "Link the active Geometry Node group to a JSON file for DNA/RNA tracking"
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

        # Check if already linked
        existing_uuid = get_uuid_from_tree(tree)
        if existing_uuid:
            existing_info = sync_manager.get_tracked_group(existing_uuid)
            if existing_info is not None:
                self.report({'WARNING'},
                            f"'{tree.name}' is already linked (UUID: {existing_uuid[:8]}...)")
                return {'CANCELLED'}

        try:
            sync_uuid = sync_manager.link_group(tree, self.filepath)
            sync_manager.save()
            self.report({'INFO'}, f"Linked '{tree.name}' as DNA (UUID: {sync_uuid[:8]}...)")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to link: {e}")
            return {'CANCELLED'}

        context.window_manager.progress_end()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Unlink a group from tracking
# ---------------------------------------------------------------------------

class GN_OT_SyncUnlink(bpy.types.Operator):
    bl_idname = "gn.sync_unlink"
    bl_label = "Unlink Group"
    bl_description = "Remove DNA/RNA tracking for this node group (keeps the JSON and node tree)"
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
        self.report({'INFO'}, f"Unlinked '{blend_name}' from DNA/RNA tracking")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Import from JSON (DNA → RNA)

class GN_OT_SyncImport(bpy.types.Operator):
    bl_idname = "gn.sync_import"
    bl_label = "Import from JSON (DNA → RNA)"
    bl_description = "Overwrite the .blend node group with JSON data (DNA wins)"
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
        sync_manager.save()
        # Force UI redraw so issue disappears immediately
        for area in context.screen.areas:
            area.tag_redraw()

        if tracker.has_errors:
            self.report({'WARNING'},
                        f"Import completed with {tracker.count} warnings — check console")
        else:
            self.report({'INFO'}, "Import from JSON completed")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Export to JSON (RNA → DNA)

class GN_OT_SyncExport(bpy.types.Operator):
    bl_idname = "gn.sync_export"
    bl_label = "Export to JSON (RNA → DNA)"
    bl_description = "Overwrite the JSON file with .blend data (RNA wins)"
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
            self.report({'INFO'}, "Export to JSON completed")
            return {'FINISHED'}
        else:
            self.report({'ERROR'},
                        "Export blocked — JSON was modified externally. "
                        "Import first or resolve the conflict.")
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
            ('blend', 'Keep .blend (RNA)', 'Keep the .blend version and update JSON'),
            ('json', 'Keep JSON (DNA)', 'Keep the JSON version and update .blend'),
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
    bl_label = "Check Sync Status"
    bl_description = "Force a sync status check for all tracked groups"
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
# Operator: Link dependencies
# ---------------------------------------------------------------------------

class GN_OT_SyncLinkDeps(bpy.types.Operator):
    bl_idname = "gn.sync_link_deps"
    bl_label = "Link Dependencies"
    bl_description = "Auto-link all untracked groups that share the same JSON file"
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
            self.report({'INFO'}, f"Linked {len(new_uuids)} dependency group(s)")
        else:
            self.report({'INFO'}, "No untracked dependencies found")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Link all groups (batch)
# ---------------------------------------------------------------------------

class GN_OT_SyncLinkAll(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.sync_link_all"
    bl_label = "Link All Groups"
    bl_description = "Link ALL Geometry Node groups in the current .blend to a single master JSON file"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def execute(self, context):
        gn_groups = [ng for ng in bpy.data.node_groups if ng.type == 'GEOMETRY']
        if not gn_groups:
            self.report({'ERROR'}, "No Geometry Node groups found in this .blend")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, len(gn_groups))

        try:
            result = sync_manager.link_all_groups(self.filepath, context)
            sync_manager.save()

            msg = (f"Linked {result['linked']} groups, "
                   f"updated {result['skipped']} existing, "
                   f"{result['errors']} errors")
            context.window_manager.progress_end()
            self.report({'INFO'}, msg)
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Batch link failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Export all groups (batch)
# ---------------------------------------------------------------------------

class GN_OT_SyncExportAll(bpy.types.Operator):
    bl_idname = "gn.sync_export_all"
    bl_label = "Export All (RNA → DNA)"
    bl_description = "Export all tracked groups to their JSON files (force overwrites)"
    bl_options = {'REGISTER'}

    force: bpy.props.BoolProperty(
        name="Force Overwrite",
        description="Overwrite JSON even if it was modified externally",
        default=True,
    )

    def execute(self, context):
        tracked = sync_manager.metadata.get("tracked_groups", {})
        if not tracked:
            self.report({'ERROR'}, "No groups tracked. Use 'Link All Groups' first.")
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
                        f"Exported {result['exported']} groups, "
                        f"skipped {result['skipped']}, "
                        f"errors {result['errors']}")
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Batch export failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Export only modified groups (batch)
# ---------------------------------------------------------------------------

class GN_OT_SyncExportModified(bpy.types.Operator):
    bl_idname = "gn.sync_export_modified"
    bl_label = "Export Modified (RNA → DNA)"
    bl_description = "Export only groups with BLEND_MODIFIED or CONFLICT status to JSON"
    bl_options = {'REGISTER'}

    def execute(self, context):
        tracked = sync_manager.metadata.get("tracked_groups", {})
        if not tracked:
            self.report({'ERROR'}, "No groups tracked. Use 'Link All Groups' first.")
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
                        f"Exported {result['exported']} groups, "
                        f"skipped {result['skipped']}, "
                        f"errors {result['errors']}")
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Batch export failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Import all modified groups (batch)
# ---------------------------------------------------------------------------

class GN_OT_SyncImportModified(bpy.types.Operator):
    bl_idname = "gn.sync_import_modified"
    bl_label = "Import Modified (DNA → RNA)"
    bl_description = "Import groups with JSON_MODIFIED, BLEND_MODIFIED, or CONFLICT status. BLEND_MODIFIED changes will be overwritten."
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
            self.report({'ERROR'}, "No groups tracked. Use 'Link All Groups' first.")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, len(tracked))
        try:
            result = sync_manager.import_all_modified(context)
            sync_manager.save()
            context.window_manager.progress_end()
            # Force UI redraw so issues disappear immediately
            for area in context.screen.areas:
                area.tag_redraw()
            self.report({'INFO'},
                        f"Imported {result['imported']}, "
                        f"skipped {result['skipped']}, "
                        f"errors {result['errors']}, "
                        f"auto-linked {result['auto_linked']}")
        except Exception as e:
            context.window_manager.progress_end()
            self.report({'ERROR'}, f"Batch import failed: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator: Initialize sync from existing JSON (no sidecar)
# ---------------------------------------------------------------------------

class GN_OT_SyncInitialize(bpy.types.Operator, ImportHelper):
    bl_idname = "gn.sync_initialize"
    bl_label = "Initialize Sync from JSON"
    bl_description = "Link an existing JSON file and create tracking metadata for all matching groups in this .blend"
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

        self.report(
            {'INFO'},
            f"Initialized sync: {linked} groups linked, {skipped} skipped (already tracked or not in .blend)"
        )
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    GN_OT_SyncInitialize,
    GN_OT_SyncLink,
    GN_OT_SyncUnlink,
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