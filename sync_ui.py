# -*- coding: utf-8 -*-
"""
gn_toolkit.sync_ui — Sidebar panels for DNA/RNA sync (N-panel).

Two panels in the Node Editor sidebar:
  GN_PT_SyncPanel (bl_order=2):  Batch operations and summary
  GN_PT_IssuesPanel (bl_order=3): Scrollable list of all sync issues
"""

from __future__ import annotations

import bpy

from .sync_manager import sync_manager, SyncStatus
from .sync_metadata import is_ignored
from .geometry_validator import (
    IssueType,
    IssueSeverity,
    ValidationIssue,
    format_issue_report,
)


STATUS_ICONS = {
    SyncStatus.SYNCED: 'CHECKMARK',
    SyncStatus.BLEND_MODIFIED: 'LIGHT',
    SyncStatus.JSON_MODIFIED: 'FILE_REFRESH',
    SyncStatus.CONFLICT: 'ERROR',
    SyncStatus.ORPHAN: 'OUTLINER_OB_GROUP_INSTANCE',
    SyncStatus.UNTRACKED: 'PLUS',
    SyncStatus.JSON_MISSING: 'FILE',
    SyncStatus.NEW_TRACKED: 'FILE_TICK',
}

STATUS_LABELS = {
    SyncStatus.SYNCED: "Synced",
    SyncStatus.BLEND_MODIFIED: "Blend Modified",
    SyncStatus.JSON_MODIFIED: "JSON Modified",
    SyncStatus.CONFLICT: "Conflict",
    SyncStatus.ORPHAN: "Orphan",
    SyncStatus.UNTRACKED: "Untracked",
    SyncStatus.JSON_MISSING: "JSON Missing",
    SyncStatus.NEW_TRACKED: "New Tracked",
}

ISSUE_TYPE_ICONS = {
    IssueType.ATTRIBUTE_MISSING: 'ERROR',
    IssueType.GEOMETRY_DEGENERATE: 'ERROR',
    IssueType.OUTPUT_INVALID: 'SORT_DESC',
    IssueType.GROUP_NOT_FOUND: 'OUTLINER_OB_GROUP_INSTANCE',
    IssueType.INPUT_UNLINKED: 'UNLINKED',
    IssueType.TYPE_MISMATCH: 'COLOR',
}

ISSUE_TYPE_LABELS = {
    IssueType.ATTRIBUTE_MISSING: "Attribute not found",
    IssueType.GEOMETRY_DEGENERATE: "Degenerate geometry",
    IssueType.OUTPUT_INVALID: "Invalid output value",
    IssueType.GROUP_NOT_FOUND: "Group not found",
    IssueType.INPUT_UNLINKED: "Unlinked input",
    IssueType.TYPE_MISMATCH: "Type mismatch",
}

ISSUE_SEVERITY_ICONS = {
    IssueSeverity.ERROR: 'ERROR',
    IssueSeverity.WARNING: 'CANCEL',
    IssueSeverity.INFO: 'INFO',
}


# ---------------------------------------------------------------------------
# Panel 1: Batch Operations
# ---------------------------------------------------------------------------

class GN_PT_SyncPanel(bpy.types.Panel):
    bl_label = "DNA/RNA Sync"
    bl_idname = "GN_PT_SyncPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GN Tools'
    bl_order = 2

    def draw(self, context):
        layout = self.layout
        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})

        row = layout.row(align=True)
        row.operator("gn.sync_link", text="Link Group", icon='LINKED')
        row.operator("gn.sync_link_all", text="Link All", icon='FILE_TICK')

        if not tracked:
            layout.separator()
            layout.label(text="No groups tracked", icon='INFO')
            layout.separator()
            layout.operator("gn.sync_initialize", text="Initialize Sync from JSON", icon='FILE_FOLDER')
            layout.label(text="Link an existing JSON file and create tracking metadata")
            layout.separator()
            layout.label(text="Or use the buttons below to create new links:")
            return

        layout.separator()
        layout.label(text="Batch Operations:", icon='ACTION')

        batch_row = layout.row(align=True)
        batch_row.operator("gn.sync_export_modified", text="Export Modified", icon='EXPORT')
        batch_row.operator("gn.sync_import_modified", text="Import Modified", icon='IMPORT')

        batch_row2 = layout.row(align=True)
        batch_row2.operator("gn.sync_export_all", text="Export All", icon='EXPORT')

        check_row = layout.row(align=True)
        check_row.operator("gn.sync_check", text="Refresh Status", icon='FILE_REFRESH')

        has_cache = bool(sync_manager._status_cache)
        if has_cache:
            layout.separator()
            summary = sync_manager.get_status_summary()
            n_total = sum(v for k, v in summary.items() if k != "ignored")
            n_synced = summary.get("synced", 0)
            n_issues = n_total - n_synced
            n_ignored = summary.get("ignored", 0)

            summary_row = layout.row()
            summary_row.label(text=f"Total: {n_total}  |  ", icon='NODETREE')
            summary_row.label(text=f"Synced: {n_synced}")

            n_blend = summary.get("blend_modified", 0)
            n_json = summary.get("json_modified", 0)
            n_conflict = summary.get("conflict", 0)
            n_orphan = summary.get("orphan", 0)
            n_missing = summary.get("json_missing", 0)

            if n_blend:
                summary_row.label(text=f"  Modified: {n_blend}", icon='LIGHT')
            if n_json:
                summary_row.label(text=f"  JSON↓: {n_json}", icon='FILE_REFRESH')
            if n_conflict:
                summary_row.label(text=f"  Conflict: {n_conflict}", icon='ERROR')
            if n_orphan:
                summary_row.label(text=f"  Orphan: {n_orphan}", icon='GROUP')
            if n_missing:
                summary_row.label(text=f"  Missing: {n_missing}", icon='FILE')

            if n_issues:
                info_row = layout.row()
                info_row.label(text=f"{n_issues} issues", icon='ERROR')
                if n_ignored:
                    info_row.label(text=f"({n_ignored} ignored)", icon='HIDE_ON')
            else:
                layout.label(text="All synced", icon='CHECKMARK')


# ---------------------------------------------------------------------------
# Panel 2: Issues List
# ---------------------------------------------------------------------------

class GN_PT_IssuesPanel(bpy.types.Panel):
    bl_label = "DNA/RNA Sync Issues"
    bl_idname = "GN_PT_IssuesPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GN Tools'
    bl_order = 3

    @classmethod
    def poll(cls, context):
        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})
        return bool(tracked)

    def draw_header(self, context):
        self.layout.label(text="", icon='FILE_REFRESH')

    def draw(self, context):
        layout = self.layout
        prefs = context.scene.gnt_sync_prefs

        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})

        if not tracked:
            layout.label(text="No groups tracked", icon='INFO')
            return

        has_cache = bool(sync_manager._status_cache)

        if not has_cache:
            layout.label(text=f"{len(tracked)} groups tracked", icon='NODETREE')
            layout.label(text="Click 'Refresh Status' to check", icon='INFO')
            layout.operator("gn.sync_check", text="Refresh Status", icon='FILE_REFRESH')
            return

        summary = sync_manager.get_status_summary()
        n_total = sum(v for k, v in summary.items() if k != "ignored")
        n_synced = summary.get("synced", 0)
        n_issues = n_total - n_synced
        n_ignored = summary.get("ignored", 0)

        header_row = layout.row()
        header_row.label(text=f"{n_issues} issues", icon='ERROR')
        if n_ignored:
            header_row.label(text=f"({n_ignored} ignored)", icon='HIDE_ON')

        layout.separator()

        filters_row1 = layout.row(align=True)
        filters_row1.prop(prefs, "show_blend_modified", text="Blend", toggle=True, icon='LIGHT')
        filters_row1.prop(prefs, "show_json_modified", text="JSON", toggle=True, icon='FILE_REFRESH')
        filters_row1.prop(prefs, "show_conflict", text="Conflict", toggle=True, icon='ERROR')

        filters_row2 = layout.row(align=True)
        filters_row2.prop(prefs, "show_orphan", text="Orphan", toggle=True, icon='GROUP')
        filters_row2.prop(prefs, "show_json_missing", text="Missing", toggle=True, icon='FILE')
        filters_row2.prop(prefs, "show_ignored", text="Ignored", toggle=True, icon='RESTRICT_VIEW_OFF')

        layout.separator()

        filtered_items = self._build_filtered_list(prefs)

        if not filtered_items:
            layout.label(text="No issues match current filters", icon='INFO')
            return

        for uid, blend_name, status, ignored in filtered_items:
            box = layout.box()
            icon = STATUS_ICONS.get(status, 'QUESTION')
            label = STATUS_LABELS.get(status, status.value)

            header = box.row()
            header.label(text=blend_name, icon=icon)

            if ignored:
                header.label(text=label, icon='HIDE_ON')
                badge = header.row(align=True)
                badge.enabled = False
                badge.label(text="IGNORED", icon='RESTRICT_VIEW_ON')
            else:
                header.label(text=label)

            actions = box.row(align=True)
            actions.alignment = 'RIGHT'

            if ignored:
                actions.operator("gn.sync_unignore", text="Un-ignore", icon='RESTRICT_VIEW_OFF').sync_uuid = uid
            elif status == SyncStatus.BLEND_MODIFIED:
                actions.operator("gn.sync_export", text="Export", icon='EXPORT').sync_uuid = uid
                actions.operator("gn.sync_ignore", text="Ignore", icon='HIDE_ON').sync_uuid = uid
            elif status == SyncStatus.JSON_MODIFIED:
                actions.operator("gn.sync_import", text="Import (DNA→RNA)", icon='IMPORT').sync_uuid = uid
                actions.operator("gn.sync_ignore", text="Ignore", icon='HIDE_ON').sync_uuid = uid
            elif status == SyncStatus.CONFLICT:
                actions.operator("gn.sync_resolve_json", text="Keep DNA", icon='FILE_REFRESH').sync_uuid = uid
                actions.operator("gn.sync_resolve_blend", text="Keep RNA", icon='LIGHT').sync_uuid = uid
                actions.operator("gn.sync_ignore", text="Ignore", icon='HIDE_ON').sync_uuid = uid
            elif status == SyncStatus.ORPHAN:
                actions.operator("gn.sync_import", text="Re-import", icon='IMPORT').sync_uuid = uid
                actions.operator("gn.sync_unlink", text="Unlink", icon='X').sync_uuid = uid
            elif status == SyncStatus.JSON_MISSING:
                actions.operator("gn.sync_export", text="Re-create JSON", icon='EXPORT').sync_uuid = uid
                actions.operator("gn.sync_unlink", text="Unlink", icon='X').sync_uuid = uid

        layout.separator()
        layout.operator("gn.sync_check", text="Refresh Status", icon='FILE_REFRESH')

    def _build_filtered_list(self, prefs):
        items = []
        for uid, status in sync_manager._status_cache.items():
            if status == SyncStatus.SYNCED:
                continue

            ignored = is_ignored(sync_manager.metadata, uid)
            if ignored and not prefs.show_ignored:
                continue

            info = sync_manager.metadata.get("tracked_groups", {}).get(uid, {})
            blend_name = info.get("blend_name", "?")

            if status == SyncStatus.BLEND_MODIFIED and not prefs.show_blend_modified:
                continue
            if status == SyncStatus.JSON_MODIFIED and not prefs.show_json_modified:
                continue
            if status == SyncStatus.CONFLICT and not prefs.show_conflict:
                continue
            if status == SyncStatus.ORPHAN and not prefs.show_orphan:
                continue
            if status == SyncStatus.JSON_MISSING and not prefs.show_json_missing:
                continue

            items.append((uid, blend_name, status, ignored))

        items.sort(key=lambda x: (x[3], x[2].value))
        return items


# ---------------------------------------------------------------------------
# Conflict resolution operators
# ---------------------------------------------------------------------------

class GN_OT_SyncResolveBlend(bpy.types.Operator):
    bl_idname = "gn.sync_resolve_blend"
    bl_label = "Resolve: Keep RNA"
    bl_description = "Keep the .blend (RNA) version and overwrite JSON (DNA)"
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: bpy.props.StringProperty(name="UUID")

    def execute(self, context):
        sync_manager.resolve_conflict(self.sync_uuid, "blend")
        sync_manager.save()
        self.report({'INFO'}, "Conflict resolved — kept RNA (.blend) version")
        return {'FINISHED'}


class GN_OT_SyncResolveJSON(bpy.types.Operator):
    bl_idname = "gn.sync_resolve_json"
    bl_label = "Resolve: Keep DNA"
    bl_description = "Keep the JSON (DNA) version and overwrite .blend (RNA)"
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: bpy.props.StringProperty(name="UUID")

    def execute(self, context):
        sync_manager.resolve_conflict(self.sync_uuid, "json")
        sync_manager.save()
        self.report({'INFO'}, "Conflict resolved — kept DNA (JSON) version")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Preferences PropertyGroup
# ---------------------------------------------------------------------------

class GN_SyncPrefs(bpy.types.PropertyGroup):
    """Persistent preferences for the issues panel."""

    show_blend_modified = bpy.props.BoolProperty(
        name="Show Blend Modified", default=True,
    )
    show_json_modified = bpy.props.BoolProperty(
        name="Show JSON Modified", default=True,
    )
    show_conflict = bpy.props.BoolProperty(
        name="Show Conflict", default=True,
    )
    show_orphan = bpy.props.BoolProperty(
        name="Show Orphan", default=True,
    )
    show_json_missing = bpy.props.BoolProperty(
        name="Show JSON Missing", default=True,
    )
    show_ignored = bpy.props.BoolProperty(
        name="Show Ignored", default=False,
    )


# ---------------------------------------------------------------------------
# Panel 3: Geometry Validation Issues
# ---------------------------------------------------------------------------

class GN_PT_GeometryIssuesPanel(bpy.types.Panel):
    """Panel showing geometry validation issues for tracked groups."""

    bl_label = "Geometry Validation"
    bl_idname = "GN_PT_GeometryIssuesPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GN Tools'
    bl_order = 4
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})
        return bool(tracked)

    def draw_header(self, context):
        count = sync_manager.get_geometry_issue_count()
        if count:
            self.layout.label(text=f"({count})", icon='ERROR')
        else:
            self.layout.label(text="", icon='CHECKMARK')

    def draw(self, context):
        layout = self.layout
        issues = sync_manager.get_geometry_issues()

        # Button to run validation
        row = layout.row()
        row.operator("gn.validate_geometry", text="Validate", icon='VIEWZOOM')

        if not issues:
            has_cache = sync_manager.has_geometry_issues()
            if has_cache:
                layout.label(text="No geometry issues found", icon='CHECKMARK')
            else:
                layout.label(text="Click 'Validate' to check", icon='INFO')
            return

        layout.separator()
        layout.label(text=f"{len(issues)} issue(s) found", icon='ERROR')
        layout.separator()

        for issue in issues:
            box = layout.box()

            # Header: severity icon + type label
            header = box.row()
            sev_icon = ISSUE_SEVERITY_ICONS.get(issue.severity, 'INFO')
            type_icon = ISSUE_TYPE_ICONS.get(issue.issue_type, 'QUESTION')
            header.label(text="", icon=sev_icon)
            header.label(text=ISSUE_TYPE_LABELS.get(issue.issue_type, issue.issue_type.value))

            # Details
            detail_row = box.row()
            detail_row.label(text=f"Group: {issue.tree_name}")
            detail_row = box.row()
            detail_row.label(text=f"Node: {issue.node_name}")
            if issue.socket_name:
                detail_row = box.row()
                detail_row.label(text=f"Socket: {issue.socket_name}")

            # Problem description
            box.label(text=f"Problem: {issue.details}")

            # Recommendation
            rec_row = box.row()
            rec_row.label(text=f"Recommendation: {issue.recommendation}", icon='INFO')

            # Available options (if any)
            if issue.available_options:
                opts = ", ".join(issue.available_options[:5])
                if len(issue.available_options) > 5:
                    opts += f" ... (+{len(issue.available_options) - 5})"
                opt_row = box.row()
                opt_row.label(text=f"Options: {opts}")


# ---------------------------------------------------------------------------
# Geometry validation operator
# ---------------------------------------------------------------------------

class GN_OT_ValidateGeometry(bpy.types.Operator):
    """Run geometry validation on all tracked groups."""

    bl_idname = "gn.validate_geometry"
    bl_label = "Validate Geometry"
    bl_description = "Check all tracked groups for geometry issues"
    bl_options = {'REGISTER'}

    def execute(self, context):
        sync_manager.invalidate_geometry_cache()
        issues_by_uuid = sync_manager.validate_geometry()

        total = sum(len(issues) for issues in issues_by_uuid.values())
        if total:
            # Build report
            all_issues = []
            for issues in issues_by_uuid.values():
                all_issues.extend(issues)
            report = format_issue_report(all_issues)
            self.report({'WARNING'}, f"Validation found {total} issue(s). See console for details.")
            print(report)
        else:
            self.report({'INFO'}, "No geometry issues found")

        return {'FINISHED'}


classes = (
    GN_PT_SyncPanel,
    GN_PT_IssuesPanel,
    GN_PT_GeometryIssuesPanel,
    GN_OT_SyncResolveBlend,
    GN_OT_SyncResolveJSON,
    GN_OT_ValidateGeometry,
    GN_SyncPrefs,
)
