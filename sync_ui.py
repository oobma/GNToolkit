# -*- coding: utf-8 -*-
"""
gn_toolkit.sync_ui — Sidebar panels for DNA/RNA sync (N-panel).

Panels in the Node Editor sidebar:
  GN_PT_SyncPanel (bl_order=2): Batch operations, summary and conflicts
  GN_PT_CollaborationPanel (bl_order=3): Git transport
  GN_PT_IssuesPanel (bl_order=4): Scrollable list of all sync issues
  GN_PT_GeometryIssuesPanel (bl_order=5): Geometry validation
"""

from __future__ import annotations

import os

import bpy

from . import audit
from .geometry_validator import (
    IssueType,
    IssueSeverity,
    ValidationIssue,
    format_issue_report,
)
from .sync_manager import (sync_manager, SyncStatus, entry_kind,
                           group_library_path, package_entry,
                           read_json_tolerant)
from .hash_utils import canonical_hash_from_json_data
from .sync_metadata import find_uuid_for_tree, is_ignored, resolve_json_path
from .sync_operators import _get_active_tree
from .serializer import tree_has_animation
from .graph_view import (GRAPH_TREE_NAME, _raw_name, clear_history_diff,
                         mark_history_diff)


STATUS_ICONS = {
    SyncStatus.SYNCED: 'CHECKMARK',
    SyncStatus.BLEND_MODIFIED: 'LIGHT',
    SyncStatus.JSON_MODIFIED: 'FILE_REFRESH',
    SyncStatus.CONFLICT: 'ERROR',
    SyncStatus.ORPHAN: 'OUTLINER_OB_GROUP_INSTANCE',
    SyncStatus.UNTRACKED: 'PLUS',
    SyncStatus.JSON_MISSING: 'FILE',
}

STATUS_LABELS = {
    SyncStatus.SYNCED: "Synced",
    SyncStatus.BLEND_MODIFIED: "Edited Locally",
    SyncStatus.JSON_MODIFIED: "Changed in JSON",
    SyncStatus.CONFLICT: "Conflict",
    SyncStatus.ORPHAN: "Missing in Blend",
    SyncStatus.UNTRACKED: "Untracked",
    SyncStatus.JSON_MISSING: "JSON File Missing",
}

# Leading icon of a tracked entry, by kind (groups are the historical
# default; embedded trees are owned by a material/world).
KIND_ICONS = {
    "material": 'MATERIAL',
    "world": 'WORLD',
}


def _layout_icon(*candidates: str) -> str:
    """First candidate that exists in this Blender's layout icon enum.

    Icon sets differ between versions (e.g. STATUS_WARNING exists in
    5.2 but not in 5.1, where WARNING is the valid one).  A hardcoded
    wrong name crashes panel draw at runtime only.
    """
    try:
        valid = {item.identifier for item in
                 bpy.types.UILayout.bl_rna.functions["label"]
                 .parameters["icon"].enum_items}
    except Exception:
        valid = set()
    for candidate in candidates:
        if candidate in valid:
            return candidate
    return 'INFO'


STATUS_WARNING_ICON = _layout_icon('STATUS_WARNING', 'WARNING', 'INFO')

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
# JSON remote helpers
# ---------------------------------------------------------------------------

def _json_remotes(tracked: dict) -> dict:
    """Map each distinct resolved JSON path to the number of groups using it."""
    remotes = {}
    blend_dir = sync_manager._blend_dir()
    for info in tracked.values():
        jp = info.get("json_path", "")
        if not jp:
            continue
        abs_path = resolve_json_path(jp, blend_dir)
        remotes[abs_path] = remotes.get(abs_path, 0) + 1
    return remotes


def _active_tracked_json(context):
    """Return (tree_name, json_basename, json_path) for the active tracked
    tree, or None when there is no active tracked tree."""
    tree = _get_active_tree(context)
    if tree is None:
        return None
    uid = find_uuid_for_tree(tree, sync_manager.metadata)
    if not uid:
        return None
    info = sync_manager.metadata.get("tracked_groups", {}).get(uid)
    if not info:
        return None
    jp = info.get("json_path", "")
    if not jp:
        return None
    abs_path = resolve_json_path(jp, sync_manager._blend_dir())
    return tree.name, os.path.basename(abs_path), abs_path


def _board_uid_for_name(name: str):
    """uid of the tracked group shown with *name* on the board, or None."""
    for uid, info in (sync_manager.metadata.get("tracked_groups")
                      or {}).items():
        if info.get("blend_name") == name and entry_kind(info) == "group":
            return uid
    return None


def _board_selection(context):
    """(name, uid) of the single tracked bar selected on the GNT board.

    None unless the active editor shows the board and exactly one group
    bar (not legend/reroute/frame) is selected and tracked.
    """
    space = getattr(context, "space_data", None)
    tree = getattr(space, "node_tree", None) if space is not None else None
    if tree is None or tree.name != GRAPH_TREE_NAME:
        return None
    selected = [n for n in (getattr(context, "selected_nodes", None) or ())
                if n.type == 'GROUP' and n.label
                and not n.name.startswith("legend_")]
    if len(selected) != 1:
        return None
    name = _raw_name(selected[0].label)
    uid = _board_uid_for_name(name)
    return (name, uid) if uid else None


# ---------------------------------------------------------------------------
# Board → history auto-follow: while the GNT Graph board is visible, a
# light timer watches the selected bar and opens its History box when the
# selection changes.  Closing the box stops following that selection
# until it changes.  Timers do not run in background contexts and
# ``bpy.app.timers.register`` may return None there, so registration is
# tracked with a flag (never with the handle).
# ---------------------------------------------------------------------------

_board_history_state: dict = {"last": None, "dismissed": None}
_board_history_pump_on: bool = False


def _board_history_step(state: dict, uid) -> bool:
    """Selection-transition state machine (pure): True → auto-open *uid*."""
    if uid == state.get("last"):
        return False
    state["last"] = uid
    if uid is None:
        state["dismissed"] = None
        return False
    if uid == state.get("dismissed"):
        return False
    state["dismissed"] = None
    return True


def _board_history_dismiss(uid) -> None:
    """Closing the box stops following *uid* while it stays selected."""
    if uid and _board_history_state.get("last") == uid:
        _board_history_state["dismissed"] = uid


def _board_history_tick():
    """Timer body: auto-open the History box for the selected board bar."""
    try:
        board = bpy.data.node_groups.get(GRAPH_TREE_NAME)
        if board is None:
            _board_history_state["last"] = None
            return 1.5
        screen = getattr(bpy.context, "screen", None)
        if screen is not None and not any(
                a.type == 'NODE_EDITOR'
                and getattr(a.spaces.active, "node_tree", None) is board
                for a in screen.areas):
            return 0.4
        selected = [n for n in board.nodes
                    if n.select and n.type == 'GROUP' and n.label
                    and not n.name.startswith("legend_")]
        if len(selected) > 1:
            # multi-selection: the user is doing something else, leave
            # the history state alone
            return 0.4
        uid = None
        if len(selected) == 1:
            uid = _board_uid_for_name(_raw_name(selected[0].label))
        prev = _board_history_state.get("last")
        if _board_history_step(_board_history_state, uid):
            bpy.ops.gn.sync_history_open(sync_uuid=uid)
        elif uid is None and prev is not None:
            # clicked outside (selection cleared): leave the time machine
            scene = getattr(bpy.context, "scene", None)
            state = (getattr(scene, "gnt_history_state", None)
                     if scene is not None else None)
            if state is not None and state.uid:
                bpy.ops.gn.sync_history_close()
    except Exception:
        pass
    return 0.4


def start_board_history_pump() -> None:
    """Start (once) the board-selection auto-follow timer."""
    global _board_history_pump_on
    if _board_history_pump_on:
        return
    try:
        bpy.app.timers.register(_board_history_tick, first_interval=1.0)
        _board_history_pump_on = True
    except Exception:
        _board_history_pump_on = False


def stop_board_history_pump() -> None:
    """Stop the auto-follow timer (add-on unregister)."""
    global _board_history_pump_on
    _board_history_pump_on = False
    try:
        if bpy.app.timers.is_registered(_board_history_tick):
            bpy.app.timers.unregister(_board_history_tick)
    except Exception:
        pass


def _notify_external_changes(count: int) -> None:
    msg = f"GNToolkit: {count} JSON file(s) changed on disk - Refresh Status to align"

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

    bpy.app.timers.register(_show, first_interval=0.1)
    bpy.app.timers.register(_hide, first_interval=6.0)


# ---------------------------------------------------------------------------
# Changes list (VS Code-style staging for the next save to JSON)
# ---------------------------------------------------------------------------

def _picker_item_icon(name: str, default: str) -> str:
    """Icon for a picker item label (kind-qualified embedded entries)."""
    if name.startswith("Material: "):
        return 'MATERIAL'
    if name.startswith("World: "):
        return 'WORLD'
    return default


def _draw_changes_list(layout, context) -> None:
    """Draw the collapsible Changes box.

    One row per out-of-sync tracked group with mark/unmark/discard/pull
    actions, plus one row per untracked dependency group with its Track
    button.  The mark state is session-scoped (see
    ``sync_manager.stage_change``); nothing is marked by default.
    """
    from .sync_operators import _change_rows

    prefs = context.scene.gnt_sync_prefs
    data = _change_rows()
    rows = data["rows"]
    deps = data["deps"]
    ignored = data["ignored"]
    staged_count = sum(1 for r in rows if r["staged"])

    layout.separator()
    box = layout.box()
    head = box.row(align=True)
    head.prop(prefs, "expand_changes", text="", emboss=False,
              icon='DISCLOSURE_TRI_DOWN' if prefs.expand_changes
              else 'DISCLOSURE_TRI_RIGHT')
    head.label(text=f"Changes ({len(rows) + len(deps)})", icon='INFO')
    if staged_count:
        head.label(text=f"{staged_count} marked", icon='CHECKMARK')
    if not prefs.expand_changes:
        return

    if not rows and not deps:
        if not sync_manager._status_cache and not sync_manager.load_report:
            box.label(text="No status yet — click Refresh Status", icon='INFO')
        else:
            box.label(text="No changes — everything is saved", icon='CHECKMARK')
        return

    control = box.row(align=True)
    control.label(text="Mark the changes and save them to JSON:")
    tools = control.row(align=True)
    tools.alignment = 'RIGHT'
    tools.operator("gn.sync_stage_all", text="", icon='PLUS')
    tools.operator("gn.sync_unstage_all", text="", icon='REMOVE')

    save_row = box.row()
    save_row.enabled = staged_count > 0
    save_label = (f"Save {staged_count} marked to JSON" if staged_count
                  else "Save marked to JSON")
    save_row.operator("gn.sync_save_staged", text=save_label, icon='EXPORT')

    n_animated = sum(1 for r in rows if r.get("animated"))
    if n_animated:
        box.label(text=f"{n_animated} group(s) have animation that is not "
                       "versioned",
                  icon=_layout_icon('STATUS_WARNING', 'WARNING'))

    def _draw_change_row(target, r):
        row = target.row(align=True)
        kind_icon = KIND_ICONS.get(r.get("kind", "group"))
        if kind_icon:
            row.label(text="", icon=kind_icon)
        row.label(text=r["name"],
                  icon=STATUS_ICONS.get(r["status"], 'QUESTION'))
        row.label(text=STATUS_LABELS.get(r["status"], r["status"].value))
        if r.get("animated"):
            row.label(text="", icon=_layout_icon('STATUS_WARNING', 'WARNING'))
        actions = row.row(align=True)
        actions.alignment = 'RIGHT'
        hist_op = actions.operator("gn.sync_history_open", text="",
                                   icon='TIME')
        hist_op.sync_uuid = r["uid"]
        if r.get("mergeable"):
            op = actions.operator("gn.sync_merge_conflict", text="",
                                  icon='AUTOMERGE_ON')
            op.sync_uuid = r["uid"]
        if r["saveable"]:
            if r["staged"]:
                op = actions.operator("gn.sync_change_unmark", text="",
                                      icon='REMOVE')
            else:
                op = actions.operator("gn.sync_change_mark", text="",
                                      icon='PLUS')
            op.sync_uuid = r["uid"]
        if r["discardable"]:
            op = actions.operator("gn.sync_discard_local", text="",
                                  icon='LOOP_BACK')
            op.sync_uuid = r["uid"]
        if r["pullable"]:
            op = actions.operator("gn.sync_import", text="", icon='IMPORT')
            op.sync_uuid = r["uid"]

    staged_rows = [r for r in rows if r["staged"]]
    plain_rows = [r for r in rows if not r["staged"]]
    if staged_rows:
        box.label(text=f"Marked ({len(staged_rows)})", icon='CHECKMARK')
        for r in staged_rows:
            _draw_change_row(box, r)
    for r in plain_rows:
        _draw_change_row(box, r)

    for d in deps:
        row = box.row(align=True)
        row.label(text=f"New dependency: {d['name']} (used by {d['parent']})",
                  icon='NODETREE')
        op = row.operator("gn.sync_track_deps", text="Track", icon='LINKED')
        op.group_name = d["name"]

    if ignored:
        box.label(text=f"{ignored} ignored group(s) hidden — manage them in "
                       "Sync Issues", icon='HIDE_ON')


def _draw_merge_report(layout, context) -> None:
    """Draw the last in-addon merge outcome (Scene.gnt_merge_state)."""
    state = getattr(context.scene, "gnt_merge_state", None)
    if state is None or not state.group_name:
        return
    layout.separator()
    box = layout.box()
    head = box.row(align=True)
    status = state.status
    icon = 'CHECKMARK' if status == "merged" else 'ERROR'
    head.label(text=f"Merge report: {state.group_name}", icon=icon)
    head.label(text=status)
    head.operator("gn.sync_merge_report_clear", text="", icon='X')
    if state.summary:
        box.label(text=state.summary, icon='INFO')
    editable = bool(state.uid)

    undecided = 0
    has_structure = False
    has_identity = False
    drawn_values = 0
    drawn_others = 0
    skipped = 0
    for item in state.items:
        kind = item.kind
        if kind == "structure":
            has_structure = True
        elif kind == "identity":
            has_identity = True
        if kind == "value":
            if drawn_values >= 20:
                skipped += 1
                continue
            drawn_values += 1
            box.label(text=f"{item.item}: base {item.base}, ours {item.ours}, "
                           f"JSON {item.theirs}", icon='ERROR')
            if editable:
                box.row(align=True).prop(item, "decision", expand=True)
                if item.decision == "custom" and item.value_type in (
                        "float", "int", "bool", "string"):
                    box.prop(item, "value_" + item.value_type)
                if item.decision == "none":
                    undecided += 1
            continue
        if drawn_others >= 20:
            skipped += 1
            continue
        drawn_others += 1
        if kind in ("structure", "identity"):
            box.label(text=item.item, icon='ERROR')
        elif kind == "applied":
            box.label(text=f"from JSON: {item.item} = {item.theirs}",
                      icon='IMPORT')
        else:
            box.label(text=item.item,
                      icon=_layout_icon('STATUS_WARNING', 'WARNING'))
    if skipped:
        box.label(text=f"... and {skipped} more", icon='INFO')

    if has_structure and editable:
        box.label(text="Structure changed on both sides - pick one side",
                  icon='ERROR')
        box.prop(state, "structure_decision", expand=True)
        if state.structure_decision == "none":
            undecided += 1
    if has_identity:
        box.label(text="Tree type mismatch: not mergeable - use Keep JSON "
                       "or Keep Blend", icon='ERROR')
    if status == "conflicted" and editable:
        apply_row = box.row()
        apply_row.enabled = not has_identity and undecided == 0
        apply_row.operator("gn.sync_merge_apply", text="Apply Chosen Merge",
                           icon='CHECKMARK')


def _draw_history_box(layout, context) -> None:
    """Draw the time-machine box (Scene.gnt_history_state)."""
    state = getattr(context.scene, "gnt_history_state", None)
    if state is None or not state.uid:
        return
    layout.separator()
    box = layout.box()
    head = box.row(align=True)
    ok = state.status in ("ok", "restored")
    head.label(text=f"History: {state.group_name}", icon='TIME')
    if state.status:
        head.label(text=state.status)
    head.operator("gn.sync_history_close", text="", icon='X')
    if state.message:
        box.label(text=state.message, icon='INFO' if ok else 'ERROR')

    for index, item in enumerate(state.items):
        row = box.row(align=True)
        text = f"{item.date} · {item.rev} · {item.subject}"
        op = row.operator("gn.sync_history_select", text=text[:110],
                          depress=item.selected)
        op.index = index

    if state.preview:
        lines = state.preview.split("\n")
        for line in lines[:15]:
            box.label(text=line)
        if len(lines) > 15:
            box.label(text=f"... and {len(lines) - 15} more", icon='INFO')

    if state.items:
        restore_row = box.row()
        restore_row.enabled = bool(state.selected_rev) \
            and not state.selected_current
        restore_row.operator("gn.sync_history_restore",
                             text="Restore This Revision", icon='LOOP_BACK')


# ---------------------------------------------------------------------------
# Panel 1: Batch Operations
# ---------------------------------------------------------------------------





class GN_PT_SyncPanel(bpy.types.Panel):
    bl_label = "Sync"
    bl_idname = "GN_PT_SyncPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GN Tools'
    bl_order = 2

    def draw(self, context):
        layout = self.layout
        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})

        if tracked:
            changed = sync_manager.scan_external_json_changes()
            if changed:
                _notify_external_changes(len(changed))

        if tracked:
            active = _active_tracked_json(context)
            if active:
                tree_name, json_base, json_path = active
                active_row = layout.row(align=True)
                active_row.label(text=f"Active: {tree_name} → {json_base}", icon='NODETREE')
                copy_op = active_row.operator("gn.sync_copy_json_path", text="", icon='COPYDOWN')
                copy_op.json_path = json_path
                active_tree = bpy.data.node_groups.get(tree_name)
                if active_tree is not None:
                    uid = find_uuid_for_tree(active_tree, metadata)
                    if uid:
                        hist_op = active_row.operator(
                            "gn.sync_history_open", text="", icon='TIME')
                        hist_op.sync_uuid = uid
                if active_tree is not None and tree_has_animation(active_tree):
                    layout.label(text="Animation (keyframes/drivers) is not "
                                      "versioned",
                                 icon=_layout_icon('STATUS_WARNING', 'WARNING'))
                layout.separator()

        board = _board_selection(context)
        if board:
            name, uid = board
            board_row = layout.row(align=True)
            board_row.label(text=f"Board selection: {name}", icon='NODETREE')
            hist_op = board_row.operator("gn.sync_history_open", text="",
                                         icon='TIME')
            hist_op.sync_uuid = uid

        row = layout.row(align=True)
        row.operator("gn.sync_link", text="Track Group", icon='LINKED')
        row.operator("gn.sync_link_all", text="Track All", icon='FILE_TICK')
        row.operator("gn.sync_link_folder", text="Track Folder…", icon='FILE_FOLDER')

        mat_row = layout.row(align=True)
        mat_row.operator("gn.sync_link_material", text="Track Material",
                         icon='MATERIAL')
        mat_row.operator("gn.sync_link_all_materials", text="Track All Materials",
                         icon='FILE_TICK')
        mat_row.operator("gn.sync_link_world", text="Track World", icon='WORLD')

        if not tracked:
            layout.separator()
            layout.label(text="Nothing tracked yet", icon='INFO')
            layout.separator()
            layout.operator("gn.sync_initialize", text="Track from Existing JSON", icon='FILE_FOLDER')
            layout.label(text="Reads the JSON as the source of truth — it is NOT modified")
            layout.separator()
            layout.label(text="Or create the JSON from this .blend with the buttons above:")
            return

        layout.separator()
        layout.label(text="Sync Actions:", icon='ACTION')

        batch_row = layout.row(align=True)
        batch_row.operator("gn.sync_export_modified", text="Save Modified to JSON", icon='EXPORT')
        batch_row.operator("gn.sync_import_modified", text="Pull from JSON", icon='IMPORT')

        batch_row2 = layout.row(align=True)
        batch_row2.operator("gn.sync_export_all", text="Save All to JSON", icon='EXPORT')

        _draw_changes_list(layout, context)

        graph_row = layout.row(align=True)
        graph_row.operator("gn.graph_view", text="Graph View…",
                           icon='NODETREE')
        graph_row.operator("gn.graph_poster", text="", icon='EXPORT')
        graph_row.operator("gn.graph_clear", text="", icon='X')

        check_row = layout.row(align=True)
        if sync_manager.check_busy():
            done, total = sync_manager.check_progress()
            wait_row = check_row.row(align=True)
            wait_row.enabled = False
            wait_row.operator("gn.sync_check", text=f"Checking {done}/{total}…",
                              icon='FILE_REFRESH')
        else:
            check_row.operator("gn.sync_check", text="Refresh Status", icon='FILE_REFRESH')
        prefs = context.scene.gnt_sync_prefs
        check_row.prop(prefs, "check_on_load", text="", icon='PLUGIN',
                       toggle=True, expand=True)

        import_row = layout.row(align=True)
        import_row.operator("gn.sync_import_group_file", text="Import Group from JSON…",
                            icon='IMPORT')

        state = getattr(context.scene, "gnt_import_state", None)
        if state and state.open:
            layout.separator()
            picker_box = layout.box()
            picker_box.label(text=f"Package: {os.path.basename(state.filepath)}",
                             icon='PACKAGE')
            picker_box.label(text=state.filepath, icon='FILE')
            picker_box.label(text="Use the search field to filter the group list:",
                             icon='VIEWZOOM')
            srow = picker_box.row(align=True)
            srow.prop_search(state, "group_name", state, "items",
                             text="", icon='VIEWZOOM')
            import_row = srow.row()
            import_row.enabled = bool(state.group_name)
            imp = import_row.operator("gn.sync_import_group", text="Import",
                                      icon='IMPORT')
            imp.filepath = state.filepath
            imp.group_name = state.group_name
            track_row = picker_box.row(align=True)
            _is_group_item = (bool(state.group_name)
                              and not state.group_name.startswith(
                                  ("Material: ", "World: ")))
            track_row.enabled = _is_group_item and \
                bpy.data.node_groups.get(state.group_name) is not None
            track_op = track_row.operator("gn.sync_import_group_track",
                                          text="Track to JSON…", icon='LINKED')
            track_op.group_name = state.group_name
            if state.group_name:
                from .sync_operators import (_compute_selection_plan,
                                             _tracked_elsewhere_notes)
                plan = _compute_selection_plan(state)
                if plan:
                    if plan["existing"]:
                        picker_box.label(text="Already in the .blend (not touched):",
                                         icon='INFO')
                        for name in plan["existing"]:
                            picker_box.label(text=f"  {name}",
                                             icon=_picker_item_icon(
                                                 name, 'CHECKMARK'))
                    if plan["missing"]:
                        picker_box.label(text="Will be imported:", icon='IMPORT')
                        for name in plan["missing"]:
                            picker_box.label(text=f"  {name}",
                                             icon=_picker_item_icon(
                                                 name, 'NODETREE'))
                    if plan["divergent"]:
                        picker_box.label(
                            text=f"Differ from {os.path.basename(state.filepath)}: "
                                 f"{', '.join(plan['divergent'])}",
                            icon='ERROR')
                    if plan["external"]:
                        picker_box.label(
                            text=f"Unconnected refs: {', '.join(plan['external'])}",
                            icon=STATUS_WARNING_ICON)
                    for name, other_pkg in _tracked_elsewhere_notes(state, plan):
                        picker_box.label(
                            text=f"{name} — tracked to: {other_pkg} (not this package)",
                            icon='INFO')
                else:
                    picker_box.label(text="Not found in this package",
                                     icon='ERROR')
            picker_box.label(text="Existing groups are never touched — only missing "
                                  "dependencies are imported.", icon='INFO')
            picker_box.label(text="To update an existing group: Import Package/Folder "
                                  "with 'Update existing groups' (or Pull from JSON "
                                  "when tracked).", icon='INFO')
            picker_box.row(align=True).operator("gn.sync_import_group_close",
                                                text="Close Picker", icon='X')

        remotes = _json_remotes(tracked)
        if remotes:
            layout.separator()
            rem_box = layout.box()
            rem_head = rem_box.row(align=True)
            rem_head.prop(prefs, "expand_remotes", text="", emboss=False,
                          icon='DISCLOSURE_TRI_DOWN' if prefs.expand_remotes
                          else 'DISCLOSURE_TRI_RIGHT')
            rem_head.label(text=f"JSON files: {len(remotes)}", icon='FILE_FOLDER')
            if prefs.expand_remotes:
                for abs_path, count in sorted(remotes.items()):
                    remote_row = rem_box.row(align=True)
                    icon = 'FILE' if os.path.isfile(abs_path) else 'ERROR'
                    remote_row.label(text=abs_path, icon=icon)
                    remote_row.label(text=f"{count} groups")
                    copy_op = remote_row.operator("gn.sync_copy_json_path", text="", icon='COPYDOWN')
                    copy_op.json_path = abs_path
                    reveal_op = remote_row.operator("gn.sync_reveal_json_path", text="", icon='FOLDER_REDIRECT')
                    reveal_op.json_path = abs_path

        has_cache = bool(sync_manager._status_cache)

        load_report = sync_manager.load_report
        if load_report:
            n_changed = sum(1 for e in load_report.values()
                            if e.get("status") == SyncStatus.JSON_MISSING)
            layout.separator()
            warn_row = layout.row(align=True)
            warn_row.label(text=f"{len(load_report)} group(s) out of sync with JSON",
                           icon='FILE_REFRESH')
            if n_changed:
                warn_row.label(text=f"({n_changed} file(s) missing)", icon='FILE')
            warn_row.operator("gn.sync_check", text="", icon='FILE_REFRESH')

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
                summary_row.label(text=f"  To save: {n_blend}", icon='LIGHT')
            if n_json:
                summary_row.label(text=f"  To pull: {n_json}", icon='FILE_REFRESH')
            if n_conflict:
                summary_row.label(text=f"  Conflict: {n_conflict}", icon='ERROR')
            if n_orphan:
                summary_row.label(text=f"  Missing: {n_orphan}", icon='GROUP')
            if n_missing:
                summary_row.label(text=f"  JSON file: {n_missing}", icon='FILE')

            if n_issues:
                info_row = layout.row()
                info_row.label(text=f"{n_issues} issues", icon='ERROR')
                if n_ignored:
                    info_row.label(text=f"({n_ignored} ignored)", icon='HIDE_ON')
            else:
                layout.label(text="All synced", icon='CHECKMARK')

            layout.separator()
            conf_box = layout.box()
            if n_conflict:
                conf_box.label(text="Conflicts — resolve per group:", icon='ERROR')
                shown = 0
                for uid, status in sync_manager._status_cache.items():
                    if status != SyncStatus.CONFLICT:
                        continue
                    info = sync_manager.metadata.get("tracked_groups", {}).get(uid, {})
                    crow = conf_box.row(align=True)
                    crow.label(text=info.get("blend_name", uid), icon='NODETREE')
                    if info.get("kind", "group") == "group":
                        merge_op = crow.operator("gn.sync_merge_conflict",
                                                 text="Merge",
                                                 icon='AUTOMERGE_ON')
                        merge_op.sync_uuid = uid
                    keep_json = crow.operator("gn.sync_resolve_json", text="Keep JSON",
                                              icon='FILE_REFRESH')
                    keep_json.sync_uuid = uid
                    keep_blend = crow.operator("gn.sync_resolve_blend", text="Keep Blend",
                                               icon='LIGHT')
                    keep_blend.sync_uuid = uid
                    shown += 1
                    if shown >= 8:
                        rest = n_conflict - shown
                        if rest > 0:
                            conf_box.label(text=f"…and {rest} more — resolve in Sync Issues",
                                           icon='INFO')
                        break
            else:
                btn_row = conf_box.row(align=True)
                btn_row.enabled = False
                keep_json = btn_row.operator("gn.sync_resolve_json", text="Keep JSON",
                                             icon='FILE_REFRESH')
                keep_blend = btn_row.operator("gn.sync_resolve_blend", text="Keep Blend",
                                              icon='LIGHT')
                hint_row = conf_box.row()
                hint_row.label(text="when a group differs on both sides", icon='INFO')
            _draw_merge_report(layout, context)
        else:
            layout.label(text="No status yet — click Refresh Status", icon='INFO')

        _draw_history_box(layout, context)

        if sync_manager.has_pending_save():
            layout.separator()
            save_row = layout.row(align=True)
            save_row.label(text="Pull applied — save the .blend to persist (Ctrl+S)",
                           icon='ERROR')

        failed = getattr(context.scene, "gnt_failed_imports", None)
        if failed is not None and len(failed.items):
            layout.separator()
            fail_box = layout.box()
            fail_box.label(text=failed.summary or f"{len(failed.items)} failed",
                           icon='ERROR')
            for item in list(failed.items)[:8]:
                fail_box.label(text=f"{item.group_name} — {item.reason}", icon='NODETREE')
            if len(failed.items) > 8:
                fail_box.label(text=f"…and {len(failed.items) - 8} more", icon='INFO')
            btn_row = fail_box.row(align=True)
            btn_row.operator("gn.sync_retry_failed", text="Retry Failed",
                             icon='FILE_REFRESH')
            btn_row.operator("gn.sync_failures_clear", text="Dismiss", icon='X')

        layout.separator()
        stop_row = layout.row(align=True)
        stop_row.operator("gn.sync_unlink_all", text="Stop Tracking All", icon='X')


# ---------------------------------------------------------------------------
# Panel 2: Issues List
# ---------------------------------------------------------------------------

class GN_PT_IssuesPanel(bpy.types.Panel):
    bl_label = "Sync Issues"
    bl_idname = "GN_PT_IssuesPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GN Tools'
    bl_order = 4

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

        # Project audit (works without a status refresh)
        audit_state = context.scene.gnt_audit_state
        audit_box = layout.box()
        audit_head = audit_box.row(align=True)
        audit_head.label(text="Audit", icon='VIEWZOOM')
        audit_head.operator("gn.audit_project", text="Run", icon='PLAY')
        audit_head.operator("gn.audit_clear", text="", icon='X')
        if audit_state.summary:
            audit_box.label(text=audit_state.summary, icon='INFO')
        for item in audit_state.details[:30]:
            audit_box.label(text=item.text,
                            icon=AUDIT_KIND_ICONS.get(item.kind, 'INFO'))
        extra = len(audit_state.details) - 30
        if extra > 0:
            audit_box.label(text=f"... and {extra} more", icon='INFO')
        layout.separator()

        has_cache = bool(sync_manager._status_cache)
        load_report = sync_manager.load_report

        if not has_cache and not load_report:
            layout.label(text=f"{len(tracked)} groups tracked", icon='NODETREE')
            layout.label(text="Statuses not checked yet", icon='INFO')
            layout.operator("gn.sync_check", text="Check Statuses", icon='FILE_REFRESH')
            return

        summary = sync_manager.get_status_summary()
        n_total = sum(v for k, v in summary.items() if k != "ignored")
        n_synced = summary.get("synced", 0)
        n_issues = n_total - n_synced + len(sync_manager.load_report)
        n_ignored = summary.get("ignored", 0)

        header_row = layout.row()
        header_row.label(text=f"{n_issues} issues", icon='ERROR')
        if n_ignored:
            header_row.label(text=f"({n_ignored} ignored)", icon='HIDE_ON')

        untracked_deps = sync_manager.find_untracked_dependencies()
        if untracked_deps:
            deps_box = layout.box()
            deps_box.label(text=f"{len(untracked_deps)} untracked dependency group(s)",
                           icon='PLUS')
            for dep in untracked_deps:
                row = deps_box.row(align=True)
                row.label(text=f"{dep['child_name']}  (used by {dep['parent_name']})",
                          icon='NODETREE')
                track_op = row.operator("gn.sync_track_deps", text="Track",
                                        icon='LINKED')
                track_op.group_name = dep["child_name"]
            deps_box.label(text="Save them before (or with) their parents", icon='INFO')

        layout.separator()

        filters_row1 = layout.row(align=True)
        filters_row1.prop(prefs, "show_blend_modified", text="Edited", toggle=True, icon='LIGHT')
        filters_row1.prop(prefs, "show_json_modified", text="In JSON", toggle=True, icon='FILE_REFRESH')
        filters_row1.prop(prefs, "show_conflict", text="Conflict", toggle=True, icon='ERROR')

        filters_row2 = layout.row(align=True)
        filters_row2.prop(prefs, "show_orphan", text="Missing", toggle=True, icon='GROUP')
        filters_row2.prop(prefs, "show_json_missing", text="JSON File", toggle=True, icon='FILE')
        filters_row2.prop(prefs, "show_ignored", text="Ignored", toggle=True, icon='RESTRICT_VIEW_OFF')

        layout.separator()

        filtered_items = self._build_filtered_list(prefs)

        if not filtered_items:
            layout.label(text="No issues match current filters", icon='INFO')
            return

        iss_box = layout.box()
        iss_head = iss_box.row(align=True)
        iss_head.prop(prefs, "expand_issues", text="", emboss=False,
                      icon='DISCLOSURE_TRI_DOWN' if prefs.expand_issues
                      else 'DISCLOSURE_TRI_RIGHT')
        iss_head.label(text=f"{len(filtered_items)} group(s) out of sync",
                       icon='ERROR')
        if not prefs.expand_issues:
            layout.separator()
            layout.operator("gn.sync_check", text="Re-check Issues", icon='FILE_REFRESH')
            return

        max_rows = 200
        for uid, blend_name, status, ignored, kind in filtered_items[:max_rows]:
            box = iss_box.box()
            icon = STATUS_ICONS.get(status, 'QUESTION')
            label = STATUS_LABELS.get(status, status.value)

            header = box.row()
            kind_icon = KIND_ICONS.get(kind)
            if kind_icon:
                header.label(text="", icon=kind_icon)
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
                actions.operator("gn.sync_export", text="Save to JSON", icon='EXPORT').sync_uuid = uid
                actions.operator("gn.sync_ignore", text="Ignore", icon='HIDE_ON').sync_uuid = uid
            elif status == SyncStatus.JSON_MODIFIED:
                actions.operator("gn.sync_import", text="Pull", icon='IMPORT').sync_uuid = uid
                actions.operator("gn.sync_export", text="Save to JSON", icon='EXPORT').sync_uuid = uid
                actions.operator("gn.sync_ignore", text="Ignore", icon='HIDE_ON').sync_uuid = uid
                box.label(text="The JSON differs from the tracked baseline - "
                               "Pull applies the JSON, Save keeps the .blend "
                               "version", icon='INFO')
            elif status == SyncStatus.CONFLICT:
                actions.operator("gn.sync_resolve_json", text="Keep JSON", icon='FILE_REFRESH').sync_uuid = uid
                actions.operator("gn.sync_resolve_blend", text="Keep Blend", icon='LIGHT').sync_uuid = uid
                actions.operator("gn.sync_ignore", text="Ignore", icon='HIDE_ON').sync_uuid = uid
            elif status == SyncStatus.ORPHAN:
                actions.operator("gn.sync_import", text="Restore from JSON", icon='IMPORT').sync_uuid = uid
                actions.operator("gn.sync_unlink", text="Stop Tracking", icon='X').sync_uuid = uid
            elif status == SyncStatus.JSON_MISSING:
                actions.operator("gn.sync_export", text="Re-create JSON", icon='EXPORT').sync_uuid = uid
                actions.operator("gn.sync_unlink", text="Stop Tracking", icon='X').sync_uuid = uid

        hidden = len(filtered_items) - max_rows
        if hidden > 0:
            iss_box.label(text=f"…and {hidden} more — narrow down with the filters",
                          icon='INFO')

        layout.separator()
        layout.operator("gn.sync_check", text="Refresh Status", icon='FILE_REFRESH')

    def _build_filtered_list(self, prefs):
        items = []
        statuses = dict(sync_manager._status_cache)
        for uid, entry in sync_manager.load_report.items():
            statuses.setdefault(uid, entry.get("status"))
        for uid, status in statuses.items():
            if status == SyncStatus.SYNCED:
                continue

            ignored = is_ignored(sync_manager.metadata, uid)
            if ignored and not prefs.show_ignored:
                continue

            info = sync_manager.metadata.get("tracked_groups", {}).get(uid, {})
            blend_name = info.get("blend_name", "?")
            kind = info.get("kind", "group")

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

            items.append((uid, blend_name, status, ignored, kind))

        items.sort(key=lambda x: (x[3], x[2].value))
        return items


# ---------------------------------------------------------------------------
# Project audit (duplicates, JSON health, dependency impact)
# ---------------------------------------------------------------------------

AUDIT_KIND_ICONS = {
    "duplicate": 'DUPLICATE',
    "missing": 'ERROR',
    "unreadable": 'ERROR',
    "conflict": 'ERROR',
    "external": 'LINK_BLEND',
    "linked": 'LIBRARY_DATA_OVERRIDE',
    "impact": 'FORWARD',
}


class GN_AuditItem(bpy.types.PropertyGroup):
    """One audit finding (Scene.gnt_audit_state.details)."""
    kind: bpy.props.StringProperty()
    text: bpy.props.StringProperty()


class GN_AuditState(bpy.types.PropertyGroup):
    """Last project-audit report (Scene.gnt_audit_state)."""
    summary: bpy.props.StringProperty()
    details: bpy.props.CollectionProperty(type=GN_AuditItem)


def _merge_decision_items(self, context):
    items = [("none", "...", "Not decided yet"),
             ("ours", "Ours", "Keep the local .blend value"),
             ("base", "Base", "Restore the pre-edit value"),
             ("theirs", "JSON", "Take the value from the JSON file")]
    if self.value_type in ("float", "int", "bool", "string"):
        items.append(("custom", "Custom", "Type your own value"))
    return items


class GN_MergeItem(bpy.types.PropertyGroup):
    """One line of the last in-addon merge report."""
    kind: bpy.props.StringProperty()
    item: bpy.props.StringProperty()
    base: bpy.props.StringProperty()
    ours: bpy.props.StringProperty()
    theirs: bpy.props.StringProperty()
    key: bpy.props.StringProperty()
    value_type: bpy.props.StringProperty()
    decision: bpy.props.EnumProperty(items=_merge_decision_items,
                                     default=0)
    value_float: bpy.props.FloatProperty(name="Value")
    value_int: bpy.props.IntProperty(name="Value")
    value_bool: bpy.props.BoolProperty(name="Value")
    value_str: bpy.props.StringProperty(name="Value")


class GN_MergeState(bpy.types.PropertyGroup):
    """Last in-addon merge outcome (Scene.gnt_merge_state)."""
    group_name: bpy.props.StringProperty()
    status: bpy.props.StringProperty()
    summary: bpy.props.StringProperty()
    uid: bpy.props.StringProperty()
    structure_decision: bpy.props.EnumProperty(
        items=[("none", "...", "Not decided yet"),
               ("ours", "Ours", "Keep the local .blend structure"),
               ("theirs", "JSON", "Take the structure from the JSON file")],
        default="none")
    items: bpy.props.CollectionProperty(type=GN_MergeItem)


class GN_HistoryItem(bpy.types.PropertyGroup):
    """One revision row of the time-machine box."""
    rev: bpy.props.StringProperty()
    rev_full: bpy.props.StringProperty()
    date: bpy.props.StringProperty()
    author: bpy.props.StringProperty()
    subject: bpy.props.StringProperty()
    selected: bpy.props.BoolProperty()


class GN_HistoryState(bpy.types.PropertyGroup):
    """Time-machine box state (Scene.gnt_history_state)."""
    uid: bpy.props.StringProperty()
    group_name: bpy.props.StringProperty()
    status: bpy.props.StringProperty()
    message: bpy.props.StringProperty()
    preview: bpy.props.StringProperty()
    selected_rev: bpy.props.StringProperty()
    selected_current: bpy.props.BoolProperty()
    items: bpy.props.CollectionProperty(type=GN_HistoryItem)


def _store_merge_report(context, report) -> None:
    """Park a merge_conflict report in the scene for the panel to draw."""
    state = context.scene.gnt_merge_state
    state.group_name = report.get("name", "")
    state.status = report.get("status", "")
    state.uid = str(report.get("uid", ""))
    state.structure_decision = "none"
    applied = list(report.get("applied") or [])
    conflicts = list(report.get("conflicts") or [])
    state.summary = (f"{len(applied)} item(s) from JSON, "
                     f"{len(conflicts)} conflict(s)")
    state.items.clear()
    if report.get("reason"):
        row = state.items.add()
        row.kind = "notice"
        row.item = str(report["reason"])
    for notice in report.get("notices") or []:
        row = state.items.add()
        row.kind = "notice"
        row.item = str(notice)
    for applied_item in applied:
        row = state.items.add()
        row.kind = "applied"
        row.item = str(applied_item.get("item", ""))
        row.theirs = str(applied_item.get("value", ""))
    for conflict in conflicts:
        row = state.items.add()
        row.kind = str(conflict.get("kind", "value"))
        row.item = str(conflict.get("item", ""))
        row.base = str(conflict.get("base", ""))
        row.ours = str(conflict.get("ours", ""))
        row.theirs = str(conflict.get("theirs", ""))
        row.key = str(conflict.get("key", ""))
        row.value_type = str(conflict.get("value_type", "other"))
        row.decision = "none"
        row.value_float = 0.0
        row.value_int = 0
        row.value_bool = False
        row.value_str = ""


def _audit_add(state, kind: str, text: str) -> None:
    item = state.details.add()
    item.kind = kind
    item.text = text


class GN_OT_AuditProject(bpy.types.Operator):
    """Audit the project: duplicate logic, JSON health, dependency impact."""

    bl_idname = "gn.audit_project"
    bl_label = "Run Project Audit"
    bl_description = (
        "Check every tracked JSON for duplicate logic, missing/unreadable/"
        "conflicted files and untracked references, and report the "
        "dependency impact of the selected group")
    bl_options = {'REGISTER'}

    group_name: bpy.props.StringProperty(
        name="Group", default="",
        description="Tracked group for the impact report (defaults to the "
                    "active node editor tree)")

    def execute(self, context):
        state = context.scene.gnt_audit_state
        state.summary = ""
        state.details.clear()

        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})
        if not tracked:
            self.report({'INFO'}, "No tracked groups")
            return {'FINISHED'}

        blend_dir = sync_manager._blend_dir()
        records = []
        n_missing = n_unreadable = n_conflict = n_linked = 0
        for uid, info in tracked.items():
            name = info.get("blend_name") or uid
            tree = bpy.data.node_groups.get(name)
            if tree is not None and group_library_path(tree):
                n_linked += 1
                _audit_add(state, "linked",
                           f"{name} — linked from {group_library_path(tree)} "
                           "(make it local to track)")
                continue
            json_path = resolve_json_path(info.get("json_path", ""), blend_dir)
            if not json_path or not os.path.isfile(json_path):
                n_missing += 1
                _audit_add(state, "missing", f"{name} — file not found")
                continue
            kind = info.get("kind", "group")
            if kind != "group":
                entry = package_entry(read_json_tolerant(json_path), info)
                if entry is None:
                    n_missing += 1
                    _audit_add(state, "missing",
                               f"{name} — {kind} entry not found inside "
                               f"{os.path.basename(json_path)}")
                else:
                    records.append({
                        "name": f"{kind}: {name}",
                        "hash": canonical_hash_from_json_data(entry)})
                continue
            data, err = audit.group_data_from_file(json_path, name)
            if err == "conflict":
                n_conflict += 1
                _audit_add(state, "conflict", f"{name} — git merge markers")
            elif err == "unreadable":
                n_unreadable += 1
                _audit_add(state, "unreadable", f"{name} — not valid UTF-8/JSON")
            elif err == "missing-group":
                n_missing += 1
                _audit_add(state, "missing",
                           f"{name} — group not found inside "
                           f"{os.path.basename(json_path)}")
            else:
                records.append({"name": name,
                                "hash": audit.content_hash_from_json_data(data)})

        buckets = audit.duplicates_from_records(records)
        for b in buckets:
            _audit_add(state, "duplicate",
                       f'{len(b["names"])} groups with identical logic: '
                       + ", ".join(b["names"]))

        ext = audit.external_refs(metadata)
        for e in ext:
            _audit_add(state, "external",
                       f'{e["name"]} — {len(e["missing_uuids"])} '
                       f'untracked reference(s)')

        target = self.group_name.strip()
        if not target:
            tree = _get_active_tree(context)
            if tree is not None:
                uid = find_uuid_for_tree(tree, metadata)
                if uid:
                    target = metadata["tracked_groups"].get(
                        uid, {}).get("blend_name", "")
        if target:
            imp = audit.impact(metadata, target)
            if imp is None:
                _audit_add(state, "impact", f"{target} — not tracked")
            else:
                consumers = []
                all_objects = bpy.data.objects
                for obj in all_objects:
                    for mod in obj.modifiers:
                        if (mod.type == 'NODES'
                                and mod.node_group is not None
                                and mod.node_group.name == imp["name"]):
                            consumers.append(f"{obj.name} ({mod.name})")
                head = ", ".join(e["name"] for e in imp["direct"][:4])
                more = len(imp["direct"]) - 4
                suffix = f" — {head}" if head else ""
                if more > 0:
                    suffix += f" (+{more} more)"
                line = (f'{imp["name"]}: {len(imp["direct"])} direct, '
                        f'{len(imp["transitive"])} total dependents{suffix}')
                if consumers:
                    extra = len(consumers) - 4
                    line += (f" · used by {len(consumers)} object modifier(s): "
                             + ", ".join(consumers[:4]))
                    if extra > 0:
                        line += f" (+{extra} more)"
                _audit_add(state, "impact", line)

        state.summary = (
            f'{len(buckets)} duplicate bucket(s) · {len(ext)} external ref(s) '
            f'· {n_missing} missing · {n_unreadable} unreadable '
            f'· {n_conflict} conflict(s) · {n_linked} library-linked')

        n_findings = len(state.details)
        if n_findings:
            self.report({'WARNING'}, f"Audit: {n_findings} finding(s)")
        else:
            self.report({'INFO'}, "Audit: no findings")
        return {'FINISHED'}


class GN_OT_AuditClear(bpy.types.Operator):
    """Clear the audit report."""

    bl_idname = "gn.audit_clear"
    bl_label = "Clear Audit Report"
    bl_options = {'REGISTER'}

    def execute(self, context):
        state = context.scene.gnt_audit_state
        state.summary = ""
        state.details.clear()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# JSON path utility operators
# ---------------------------------------------------------------------------

class GN_OT_CopyJSONPath(bpy.types.Operator):
    bl_idname = "gn.sync_copy_json_path"
    bl_label = "Copy JSON Path"
    bl_description = "Copy the full JSON file path to the clipboard"
    bl_options = {'REGISTER'}

    json_path: bpy.props.StringProperty()

    def execute(self, context):
        if not self.json_path:
            self.report({'ERROR'}, "No JSON path to copy")
            return {'CANCELLED'}
        context.window_manager.clipboard = self.json_path
        self.report({'INFO'}, "JSON path copied to clipboard")
        return {'FINISHED'}


class GN_OT_RevealJSONPath(bpy.types.Operator):
    bl_idname = "gn.sync_reveal_json_path"
    bl_label = "Reveal JSON in File Browser"
    bl_description = "Open the JSON file's folder in the system file browser"
    bl_options = {'REGISTER'}

    json_path: bpy.props.StringProperty()

    def execute(self, context):
        if not self.json_path:
            return {'CANCELLED'}
        folder = os.path.dirname(self.json_path)
        if not os.path.isdir(folder):
            self.report({'ERROR'}, f"Folder not found: {folder}")
            return {'CANCELLED'}
        try:
            opened = bpy.ops.wm.path_open(filepath=folder)
        except Exception:
            opened = set()
        if "FINISHED" not in opened:
            self.report({'WARNING'}, "Could not open the system file browser")
            return {'CANCELLED'}
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Conflict resolution operators
# ---------------------------------------------------------------------------

class GN_OT_SyncResolveBlend(bpy.types.Operator):
    bl_idname = "gn.sync_resolve_blend"
    bl_label = "Resolve: Keep Blend"
    bl_description = "Keep the .blend version and overwrite the JSON"
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: bpy.props.StringProperty(name="UUID")

    def execute(self, context):
        ok = sync_manager.resolve_conflict(self.sync_uuid, "blend")
        sync_manager.save()
        if ok is False:
            self.report({'ERROR'},
                        "Could not write the JSON - check the console/log "
                        "(e.g. unwritable file)")
            return {'CANCELLED'}
        self.report({'INFO'}, "Conflict resolved - kept the .blend version")
        return {'FINISHED'}


class GN_OT_SyncResolveJSON(bpy.types.Operator):
    bl_idname = "gn.sync_resolve_json"
    bl_label = "Resolve: Keep JSON"
    bl_description = "Keep the JSON version and overwrite the .blend"
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: bpy.props.StringProperty(name="UUID")

    def execute(self, context):
        tracker = sync_manager.resolve_conflict(self.sync_uuid, "json")
        sync_manager.save()
        if tracker is not None and tracker.error_count:
            self.report({'ERROR'},
                        "Could not apply the JSON side - check the console/log "
                        "(library-linked groups must be made local first)")
            return {'CANCELLED'}
        self.report({'INFO'}, "Conflict resolved - kept the JSON version")
        return {'FINISHED'}


class GN_OT_SyncMergeConflict(bpy.types.Operator):
    bl_idname = "gn.sync_merge_conflict"
    bl_label = "Merge with JSON"
    bl_description = ("Merge this conflicted entry per item (socket defaults, "
                      "node labels/properties, custom properties, owner "
                      "settings): local .blend edits are kept and the JSON "
                      "changes applied; the common base comes from the JSON "
                      "file's git history. Nothing is written when items "
                      "conflict")
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: bpy.props.StringProperty(name="UUID")

    def execute(self, context):
        report = sync_manager.merge_conflict(self.sync_uuid)
        _store_merge_report(context, report)
        status = report.get("status")
        name = report.get("name") or "the group"
        sync_manager.save()
        from .sync_operators import _tag_ui_redraw
        _tag_ui_redraw(context)
        if status == "merged":
            self.report(
                {'INFO'},
                "Merged '%s': %d item(s) from JSON, local edits kept"
                % (name, len(report.get("applied") or [])))
            return {'FINISHED'}
        if status == "unchanged":
            self.report({'INFO'}, "'%s' is already in sync" % name)
            return {'FINISHED'}
        if status == "conflicted":
            self.report(
                {'WARNING'},
                "Merge conflicts in '%s' (%d) - decide them in the merge "
                "report (nothing was written yet)"
                % (name, len(report.get("conflicts") or [])))
            return {'FINISHED'}
        reason = report.get("reason") or "; ".join(report.get("notices") or [])
        self.report({'ERROR'}, "Merge not applied: %s" % (reason or status))
        return {'CANCELLED'}


def _merge_custom_value(item):
    if item.value_type == "float":
        return float(item.value_float)
    if item.value_type == "int":
        return int(item.value_int)
    if item.value_type == "bool":
        return bool(item.value_bool)
    return str(item.value_str)


class GN_OT_SyncMergeApply(bpy.types.Operator):
    bl_idname = "gn.sync_merge_apply"
    bl_label = "Apply Chosen Merge"
    bl_description = ("Write the merged package with the per-item decisions "
                      "chosen in the merge report, rebuild the entry in the "
                      ".blend and mark it Synced; nothing is written while "
                      "any conflict is undecided")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        state = context.scene.gnt_merge_state
        if not state.uid:
            self.report({'ERROR'}, "No editable merge report - press Merge "
                                   "in the entry row first")
            return {'CANCELLED'}
        values = {}
        undecided = 0
        has_structure = False
        for item in state.items:
            if item.kind == "value":
                if item.key and item.decision in ("ours", "base", "theirs"):
                    values[item.key] = item.decision
                elif item.key and item.decision == "custom":
                    values[item.key] = {"custom": _merge_custom_value(item)}
                else:
                    undecided += 1
            elif item.kind == "structure":
                has_structure = True
            elif item.kind == "identity":
                self.report({'ERROR'}, "A tree type mismatch cannot be "
                                       "merged - use Keep JSON or Keep Blend")
                return {'CANCELLED'}
        overrides = {"values": values}
        if has_structure:
            if state.structure_decision in ("ours", "theirs"):
                overrides["structure"] = state.structure_decision
            else:
                undecided += 1
        if undecided:
            self.report({'WARNING'},
                        "%d conflict(s) still have no decision" % undecided)
            return {'CANCELLED'}
        report = sync_manager.merge_conflict(state.uid, overrides=overrides)
        _store_merge_report(context, report)
        sync_manager.save()
        from .sync_operators import _tag_ui_redraw
        _tag_ui_redraw(context)
        status = report.get("status")
        name = report.get("name") or "the group"
        if status == "merged":
            self.report({'INFO'},
                        "Merged '%s': %d item(s) applied"
                        % (name, len(report.get("applied") or [])))
            return {'FINISHED'}
        if status == "conflicted":
            self.report({'WARNING'},
                        "Still %d unresolved conflict(s) in '%s' - nothing "
                        "was written"
                        % (len(report.get("conflicts") or []), name))
            return {'FINISHED'}
        reason = report.get("reason") or "; ".join(report.get("notices") or [])
        self.report({'ERROR'}, "Merge not applied: %s" % (reason or status))
        return {'CANCELLED'}


class GN_OT_SyncMergeReportClear(bpy.types.Operator):
    bl_idname = "gn.sync_merge_report_clear"
    bl_label = "Clear Merge Report"
    bl_description = "Hide the last merge report"
    bl_options = {'REGISTER'}

    def execute(self, context):
        state = context.scene.gnt_merge_state
        state.group_name = ""
        state.status = ""
        state.summary = ""
        state.items.clear()
        return {'FINISHED'}


def _select_history_revision(context, index) -> None:
    """Mark one revision row and refresh its preview (shared by operators)."""
    state = context.scene.gnt_history_state
    if index < 0 or index >= len(state.items):
        return
    for i, item in enumerate(state.items):
        item.selected = (i == index)
    selected = state.items[index]
    state.selected_rev = selected.rev_full
    report = sync_manager.history_preview(state.uid, selected.rev_full)
    state.selected_current = bool(report.get("identical"))
    lines = [f"{selected.date} · {selected.author} — {selected.subject}"]
    if report.get("status") == "ok":
        if report.get("identical"):
            lines.append("(identical to the current JSON entry)")
        else:
            lines += list(report.get("lines") or [])
    else:
        lines.append(report.get("reason") or "no preview available")
    diff = sync_manager.history_revision_diff(state.uid, selected.rev_full)
    note = ""
    if diff.get("status") == "ok":
        names = list(diff.get("names") or [])
        marked = mark_history_diff(
            names,
            label=f"HISTORY MODE - {selected.rev} · {selected.date}")
        if marked:
            note = (f"{marked} group(s) of this file differ in this "
                    "revision - marked on the board")
        elif names:
            note = (f"{len(names)} group(s) differ in this revision "
                    "(none on the current board)")
    else:
        clear_history_diff()
    if note:
        lines.insert(1, note)
    state.preview = "\n".join(lines)


class GN_OT_SyncHistoryOpen(bpy.types.Operator):
    bl_idname = "gn.sync_history_open"
    bl_label = "History"
    bl_description = ("Browse this entry's JSON revisions (git history) and "
                      "restore an old version into the JSON - the .blend is "
                      "not touched")
    bl_options = {'REGISTER'}

    sync_uuid: bpy.props.StringProperty(name="UUID")

    def execute(self, context):
        state = context.scene.gnt_history_state
        clear_history_diff()
        report = sync_manager.history_revisions(self.sync_uuid)
        state.uid = self.sync_uuid
        state.group_name = report.get("name", "")
        state.status = report.get("status", "")
        state.message = report.get("reason", "")
        state.selected_rev = ""
        state.selected_current = False
        state.preview = ""
        state.items.clear()
        revisions = list(report.get("revisions") or [])
        for rev in revisions:
            item = state.items.add()
            item.rev = rev.get("hash", "")[:10]
            item.rev_full = rev.get("hash", "")
            item.date = rev.get("date", "")
            item.author = rev.get("author", "")
            item.subject = rev.get("subject", "")
            item.selected = False
        if state.status == "ok" and len(state.items):
            state.message = f"{len(state.items)} revision(s)"
            _select_history_revision(context, 0)
        from .sync_operators import _tag_ui_redraw
        _tag_ui_redraw(context)
        return {'FINISHED'}


class GN_OT_SyncHistoryClose(bpy.types.Operator):
    bl_idname = "gn.sync_history_close"
    bl_label = "Close History"
    bl_description = "Hide the history box"
    bl_options = {'REGISTER'}

    def execute(self, context):
        state = context.scene.gnt_history_state
        _board_history_dismiss(state.uid)
        clear_history_diff()
        state.uid = ""
        state.group_name = ""
        state.status = ""
        state.message = ""
        state.preview = ""
        state.selected_rev = ""
        state.selected_current = False
        state.items.clear()
        return {'FINISHED'}


class GN_OT_SyncHistorySelect(bpy.types.Operator):
    bl_idname = "gn.sync_history_select"
    bl_label = "Select Revision"
    bl_description = "Preview this revision against the current JSON entry"
    bl_options = {'REGISTER'}

    index: bpy.props.IntProperty(name="Index")

    def execute(self, context):
        _select_history_revision(context, int(self.index))
        return {'FINISHED'}


class GN_OT_SyncHistoryRestore(bpy.types.Operator):
    bl_idname = "gn.sync_history_restore"
    bl_label = "Restore This Revision"
    bl_description = ("Write this entry as it was in the selected revision "
                      "into the JSON file; the .blend is not touched - Pull "
                      "rebuilds the group from the JSON")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        state = context.scene.gnt_history_state
        if not state.uid or not state.selected_rev:
            self.report({'ERROR'}, "Select a revision first")
            return {'CANCELLED'}
        report = sync_manager.history_restore(state.uid, state.selected_rev)
        name = report.get("name") or "the entry"
        short = state.selected_rev[:10]
        if report.get("status") != "restored":
            state.message = report.get("reason") or report.get("status", "")
            self.report({'ERROR'}, "Restore failed: %s" % state.message)
            return {'CANCELLED'}
        state.status = "restored"
        state.message = (f"Restored '{name}' from {short} - the JSON changed; "
                         "Pull to rebuild the .blend")
        selected = next((i for i, item in enumerate(state.items)
                         if item.selected), -1)
        if selected >= 0:
            _select_history_revision(context, selected)
        from .sync_operators import _tag_ui_redraw
        _tag_ui_redraw(context)
        self.report({'INFO'}, "Restored '%s' from %s" % (name, short))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel 2.5: Collaboration (thin git transport)
# ---------------------------------------------------------------------------

def _draw_git_log_box(box, entries):
    for e in entries:
        row = box.row(align=True)
        row.label(text=f"{e['date']} · {e['author'][:24]}", icon='DOT')
        row.label(text=e['subject'])


def _addon_prefs(context):
    try:
        return context.preferences.addons[__package__].preferences
    except Exception:
        return None


class GN_PT_CollaborationPanel(bpy.types.Panel):
    """Git transport for the tracked JSON files."""

    bl_label = "Collaboration"
    bl_idname = "GN_PT_CollaborationPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GN Tools'
    bl_order = 3

    @classmethod
    def poll(cls, context):
        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})
        return bool(tracked)

    def draw(self, context):
        from .git_integration import (
            get_git_state, ensure_status_job, git_busy, git_log, find_git_repo,
            active_backend_name,
        )
        from . import credentials

        layout = self.layout
        prefs = context.scene.gnt_sync_prefs

        state = get_git_state()
        if not state:
            ensure_status_job()
            from .sync_operators import ensure_git_pump
            ensure_git_pump()
            layout.label(text="Checking git state…", icon='TIME')
            return

        if not state.get("available"):
            layout.label(text="No Git engine available — collaboration is optional",
                         icon='INFO')
            layout.label(text="Everything else works without it")
            return

        busy = git_busy()
        repos = state.get("repos", {})
        if not repos:
            layout.label(text="Tracked JSONs are not inside a git repository",
                         icon='INFO')
            layout.label(text="Init one with your Git tooling, then refresh",
                         icon='INFO')
            return

        for root, st in sorted(repos.items()):
            box = layout.box()
            head = box.row(align=True)
            if not st.get("ok"):
                head.label(text=f"Repo: {st.get('name', root)}", icon='ERROR')
                head.label(text=st.get("error", "git status failed"))
                continue
            if st["tracked_changed"]:
                icon = 'FILE_REFRESH'
            elif st["ahead"] or st["behind"]:
                icon = 'NETWORK_DRIVE'
            else:
                icon = 'CHECKMARK'
            head.label(text=f"Repo: {st['name']}", icon=icon)
            parts = []
            if st["tracked_changed"]:
                parts.append(f"{len(st['tracked_changed'])} to commit")
            if st["ahead"]:
                parts.append(f"ahead {st['ahead']}")
            if st["behind"]:
                parts.append(f"behind {st['behind']}")
            head.label(text=" · ".join(parts) if parts else "clean")

            actions = box.row(align=True)
            actions.enabled = not busy
            commit_op = actions.operator("gn.git_commit", text="Git Commit…",
                                         icon='EXPORT')
            commit_op.repo = root
            sync_op = actions.operator("gn.git_sync", text="Sync Changes",
                                       icon='FILE_REFRESH')
            sync_op.repo = root
            reveal_op = actions.operator("gn.git_reveal_repo", text="",
                                         icon='FOLDER_REDIRECT')
            reveal_op.repo = root

        if busy:
            layout.label(text="Git operation in progress…", icon='TIME')

        for path in state.get("conflicts", []):
            row = layout.row(align=True)
            row.label(text=f"Merge conflict: {os.path.basename(path)}",
                      icon='ERROR')
            reveal_op = row.operator("gn.sync_reveal_json_path", text="Reveal")
            reveal_op.json_path = path
        if state.get("conflicts"):
            layout.label(text="Resolve the conflicts with your Git tooling",
                         icon='INFO')

        addon_prefs = _addon_prefs(context)
        if addon_prefs is not None:
            hosts = []
            for st in repos.values():
                host = credentials.host_for(st.get("remote_url") or "")
                if host and host not in hosts:
                    hosts.append(host)
            box = layout.box()
            head = box.row(align=True)
            head.label(text="Credentials", icon='LOCKED')
            if hosts:
                head.label(text="detected: " + ", ".join(hosts))
            if not (addon_prefs.git_host or "").strip() and hosts:
                addon_prefs.git_host = hosts[0]
            box.prop(addon_prefs, "git_host", text="Host")
            box.prop(addon_prefs, "git_username", text="User")
            box.prop(addon_prefs, "git_token", text="Token")
            actions = box.row(align=True)
            actions.operator("gn.git_save_credentials", text="Save",
                             icon='CHECKMARK')
            actions.operator("gn.git_forget_credentials", text="Forget",
                             icon='TRASH')
            source = credentials.status_for_host(
                (addon_prefs.git_host or "").strip())
            labels = {
                "vault": "Stored in the OS vault",
                "preferences": "Stored in Blender preferences (no OS vault)",
                "session": "In memory for this session",
                "environment": "Provided by environment variables",
                "missing": "No token stored for this host",
            }
            box.label(text=labels.get(source, source), icon='INFO')
            if source == "preferences":
                box.label(text="Warning: the token is stored unencrypted in "
                               "Blender preferences", icon='ERROR')

        if len(repos) == 1:
            repo_box = layout.box()
            repo_head = repo_box.row(align=True)
            repo_head.prop(prefs, "expand_git_repo_log", text="", emboss=False,
                           icon='DISCLOSURE_TRI_DOWN'
                           if prefs.expand_git_repo_log
                           else 'DISCLOSURE_TRI_RIGHT')
            repo_head.label(text="Repository history", icon='TIME')
            if prefs.expand_git_repo_log:
                entries = git_log(next(iter(repos)), None, 10)
                if entries:
                    _draw_git_log_box(repo_box, entries)
                    repo_box.label(text="Shows the last 10 commits (newest first)",
                                   icon='INFO')
                else:
                    repo_box.label(text="No commits yet", icon='INFO')

        active = _active_tracked_json(context)
        if active:
            tree_name, _json_base, json_path = active
            root = find_git_repo(json_path)
            if root:
                act_box = layout.box()
                act_head = act_box.row(align=True)
                act_head.prop(prefs, "expand_git_active_log", text="",
                              emboss=False,
                              icon='DISCLOSURE_TRI_DOWN'
                              if prefs.expand_git_active_log
                              else 'DISCLOSURE_TRI_RIGHT')
                act_head.label(text=f"Active: {tree_name} — history",
                               icon='NODETREE')
                if prefs.expand_git_active_log:
                    entries = git_log(root, os.path.relpath(json_path, root), 10)
                    if entries:
                        _draw_git_log_box(act_box, entries)
                        act_box.label(text="Shows the last 10 commits (newest first)",
                                      icon='INFO')
                    else:
                        act_box.label(text="No commits for this file",
                                      icon='INFO')

        layout.separator()
        engine = active_backend_name()
        if engine:
            label = "Engine: Git (system)" if engine == "git" \
                else "Engine: embedded (dulwich)"
            layout.label(text=label, icon='INFO')
        layout.prop(prefs, "fetch_on_load", toggle=True, icon='URL')
        if addon_prefs is not None:
            layout.prop(addon_prefs, "network_timeout", text="Network timeout (s)")


# ---------------------------------------------------------------------------
# Preferences PropertyGroup
# ---------------------------------------------------------------------------

def _on_git_backend_changed(self, context):
    try:
        from .git_integration import clear_backend_caches
        clear_backend_caches()
    except Exception:
        pass


def _git_backend_items():
    """Engine options for this build: the system-Git engine only exists in
    the GitHub/release builds (the platform build ships the embedded
    engine alone)."""
    items = [
        ("auto", "Automatic",
         "Use the embedded engine bundled with the add-on"),
        ("dulwich", "Embedded (dulwich)",
         "Pure-Python engine bundled with the add-on — no external "
         "program needed"),
    ]
    try:
        from . import git_backend_git  # noqa: F401
    except Exception:
        return items
    items[0] = ("auto", "Automatic",
                "Use the system Git when installed, otherwise the embedded "
                "engine")
    items.append(("git", "Git (system)",
                  "Use the Git program installed on this computer"))
    return items


class GN_AddonPrefs(bpy.types.AddonPreferences):
    """Global add-on preferences (per user, not per .blend file)."""

    bl_idname = __package__

    git_backend: bpy.props.EnumProperty(
        name="Git engine",
        description="Engine that runs the collaboration operations",
        items=_git_backend_items(),
        default="auto",
        update=_on_git_backend_changed,
    )

    network_timeout: bpy.props.IntProperty(
        name="Network Timeout (s)",
        description="Maximum seconds to wait for remote Git operations "
                    "(fetch, pull, push) before cancelling them",
        default=300,
        min=10,
        max=3600,
    )

    git_host: bpy.props.StringProperty(
        name="Host",
        description="Host the credentials apply to (e.g. github.com) — "
                    "filled from the tracked remotes",
        default="",
    )
    git_username: bpy.props.StringProperty(
        name="Git user",
        description="Username for token authentication (any non-empty name "
                    "works with token logins)",
        default="",
    )
    git_token: bpy.props.StringProperty(
        name="Token",
        description="Personal access token — stored in the OS vault when "
                    "available",
        subtype='PASSWORD',
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "git_backend")
        layout.prop(self, "network_timeout")
        layout.separator()
        layout.label(text="Credentials")
        layout.prop(self, "git_host")
        layout.prop(self, "git_username")
        layout.prop(self, "git_token")
        row = layout.row(align=True)
        row.operator("gn.git_save_credentials", text="Save", icon='CHECKMARK')
        row.operator("gn.git_forget_credentials", text="Forget", icon='TRASH')


class GN_SyncPrefs(bpy.types.PropertyGroup):
    """Persistent preferences for the issues panel.

    NOTE: properties must use the annotation syntax (``name: bpy.props.X``)
    — Blender 4.x/5.x silently ignores the assignment syntax.
    """

    show_blend_modified: bpy.props.BoolProperty(
        name="Show Edited Locally", default=True,
    )
    show_json_modified: bpy.props.BoolProperty(
        name="Show Changed in JSON", default=True,
    )
    show_conflict: bpy.props.BoolProperty(
        name="Show Conflict", default=True,
    )
    show_orphan: bpy.props.BoolProperty(
        name="Show Missing in Blend", default=True,
    )
    show_json_missing: bpy.props.BoolProperty(
        name="Show JSON File Missing", default=True,
    )
    show_ignored: bpy.props.BoolProperty(
        name="Show Ignored", default=False,
    )
    expand_remotes: bpy.props.BoolProperty(
        name="Show JSON Files",
        description="List every JSON file that tracked groups point to "
                    "(folder exports can produce one file per group)",
        default=False,
    )
    expand_issues: bpy.props.BoolProperty(
        name="Show Issue List",
        description="List each out-of-sync group with its actions",
        default=True,
    )
    expand_changes: bpy.props.BoolProperty(
        name="Show Changes",
        description="List every out-of-sync group with mark/unmark/discard/"
                    "pull actions (the staging list for the next save)",
        default=True,
    )
    expand_git_repo_log: bpy.props.BoolProperty(
        name="Show Repository History",
        description="List the latest commits of the git repository",
        default=False,
    )
    expand_git_active_log: bpy.props.BoolProperty(
        name="Show Active Group History",
        description="List the latest commits of the active group's JSON file",
        default=False,
    )
    check_on_load: bpy.props.BoolProperty(
        name="Check JSON on open",
        description="After loading a .blend, compare the JSON hashes in the background "
                    "and show a notice when files changed outside Blender",
        default=True,
    )
    fetch_on_load: bpy.props.BoolProperty(
        name="Fetch remotes on open",
        description="After loading a .blend, contact the Git remotes of the tracked "
                    "repositories in the background (only when online access is enabled)",
        default=True,
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
    bl_order = 5
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
    GN_PT_CollaborationPanel,
    GN_PT_GeometryIssuesPanel,
    GN_OT_SyncResolveBlend,
    GN_OT_SyncResolveJSON,
    GN_OT_SyncMergeConflict,
    GN_OT_SyncMergeApply,
    GN_OT_SyncMergeReportClear,
    GN_OT_SyncHistoryOpen,
    GN_OT_SyncHistoryClose,
    GN_OT_SyncHistorySelect,
    GN_OT_SyncHistoryRestore,
    GN_OT_CopyJSONPath,
    GN_OT_RevealJSONPath,
    GN_OT_ValidateGeometry,
    GN_SyncPrefs,
    GN_AddonPrefs,
    GN_AuditItem,
    GN_AuditState,
    GN_MergeItem,
    GN_MergeState,
    GN_HistoryItem,
    GN_HistoryState,
    GN_OT_AuditProject,
    GN_OT_AuditClear,
)
