# -*- coding: utf-8 -*-
"""
gn_toolkit.sync_ui — Sidebar panels for DNA/RNA sync (N-panel).

Panels in the Node Editor sidebar (category "GNToolkit"):
  GN_PT_SyncPanel (bl_order=0): active entry, status, Changes, review
  GN_PT_TrackPanel (bl_order=1): tracking setup (context-aware)
  GN_PT_ExplorePanel (bl_order=2): graph board and JSON history
  GN_PT_CollaborationPanel (bl_order=3, label "Git"): git transport
  GN_PT_AdvancedPanel (bl_order=4): parent of Package/Project Health/
  Geometry Nodes Validation
"""

from __future__ import annotations

import os

import bpy

from . import audit
from .geometry_validator import (
    IssueType,
    IssueSeverity,
    ValidationIssue,
    validate_tracked_tree,
)
from .constants import is_graph_tree_name, is_trackable_tree_type
from .sync_manager import (sync_manager, SyncStatus, entry_kind,
                           group_library_path, package_entry,
                           read_json_tolerant, resolve_entry_tree)
from .hash_utils import canonical_hash_from_json_data
from .sync_metadata import (SYNC_UUID_PROP, find_uuid_for_tree, is_ignored,
                            resolve_json_path)
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
    SyncStatus.BLEND_MODIFIED: "Edited locally",
    SyncStatus.JSON_MODIFIED: "Changed in JSON",
    SyncStatus.CONFLICT: "Conflict",
    SyncStatus.ORPHAN: "Missing in blend",
    SyncStatus.UNTRACKED: "Untracked",
    SyncStatus.JSON_MISSING: "JSON missing",
}

STATUS_SHORT = {
    SyncStatus.BLEND_MODIFIED: "Edited",
    SyncStatus.JSON_MODIFIED: "In JSON",
    SyncStatus.CONFLICT: "Conflict",
    SyncStatus.ORPHAN: "Missing",
    SyncStatus.UNTRACKED: "Untracked",
    SyncStatus.JSON_MISSING: "JSON missing",
}

# Leading icon of a tracked entry, by kind (groups are the historical
# default; embedded trees are owned by a material/world).
KIND_ICONS = {
    "material": 'MATERIAL',
    "world": 'WORLD',
}


def _count_label(n, singular: str, plural: str | None = None) -> str:
    """Compact singular/plural label for panel text."""
    if plural is None:
        plural = singular + "s"
    return f"{n} {singular if n == 1 else plural}"


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


def _supported_editor(context) -> bool:
    space = getattr(context, "space_data", None)
    tree = getattr(space, "node_tree", None)
    return is_trackable_tree_type(getattr(tree, "type", ""))


def _active_entry(context):
    """Active group/material/world/compositor entry for the current editor.

    Resolves the UUID on the owner (material/world) as well as on a node
    group tree, so embedded entries get an Active row and History button.
    """
    metadata = sync_manager.metadata
    tracked = metadata.get("tracked_groups", {})
    space = getattr(context, "space_data", None)
    active_tree = getattr(space, "node_tree", None)
    tree_type = getattr(active_tree, "type", "")
    if is_graph_tree_name(getattr(active_tree, "name", "")):
        return None
    owner = None
    kind = "group"

    if tree_type == 'SHADER':
        mat = getattr(context, "material", None)
        if mat is not None and getattr(mat, "use_nodes", False):
            owner, kind = mat, "material"
        else:
            world = getattr(context, "world", None)
            if world is not None and getattr(world, "use_nodes", False):
                owner, kind = world, "world"

    if owner is None:
        tree = getattr(space, "node_tree", None)
        if tree is not None and is_trackable_tree_type(
                getattr(tree, "type", "")):
            owner, kind = tree, "group"

    if owner is None:
        return None

    name = owner.name
    library = bool(kind == "group" and group_library_path(owner))
    uid = None if library else owner.get(SYNC_UUID_PROP)
    info = tracked.get(uid) if uid else None
    if info is None and not library:
        for candidate_uid, candidate in tracked.items():
            if (candidate.get("blend_name") == name
                    and entry_kind(candidate) == kind):
                uid, info = candidate_uid, candidate
                break
    json_path = ""
    if info is not None:
        json_path = resolve_json_path(info.get("json_path", ""),
                                      sync_manager._blend_dir())
    return {
        "owner": owner,
        "kind": kind,
        "name": name,
        "uid": uid if info is not None else None,
        "info": info,
        "json_path": json_path,
        "library": library,
    }


def _draw_active_entry(layout, context):
    """Context row at the top of Sync/Track: active entry + actions."""
    entry = _active_entry(context)
    if entry is None:
        return None
    row = layout.row(align=True)
    icon = KIND_ICONS.get(entry["kind"], 'NODETREE')
    if entry["info"] is not None:
        row.label(text=f"Active: {entry['name']}", icon=icon)
        file_row = layout.row(align=True)
        file_row.label(text=os.path.basename(entry["json_path"]),
                       icon='FILE')
        copy_op = file_row.operator("gn.sync_copy_json_path", text="",
                                    icon='COPYDOWN')
        copy_op.json_path = entry["json_path"]
        hist_op = file_row.operator("gn.sync_history_open", text="",
                                    icon='TIME')
        hist_op.sync_uuid = entry["uid"]
        reveal_op = file_row.operator("gn.sync_reveal_json_path", text="",
                                      icon='FOLDER_REDIRECT')
        reveal_op.json_path = entry["json_path"]
    elif entry["library"]:
        row.label(text=f"Active: {entry['name']} - library-linked", icon=icon)
        row.label(text="Make it local to track", icon='INFO')
    else:
        row.label(text=f"Active: {entry['name']} - not tracked", icon=icon)
        op_id = {
            "group": "gn.sync_link",
            "material": "gn.sync_link_material",
            "world": "gn.sync_link_world",
        }.get(entry["kind"])
        if op_id:
            row.operator(op_id, text="Track", icon='LINKED')

    tree = entry["owner"] if entry["kind"] == "group" else None
    if tree is not None and tree_has_animation(tree):
        layout.label(text="Animation (keyframes/drivers) is not versioned",
                     icon=_layout_icon('STATUS_WARNING', 'WARNING'))
    return entry


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
        prefs = bpy.context.scene.gnt_sync_prefs
        if not getattr(prefs, "follow_board_selection", False):
            _board_history_state["last"] = None
            return 0.4
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
    msg = f"GNToolkit: {_count_label(count, 'JSON file')} changed on disk - Refresh Status to align"

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


def _draw_sync_alerts(layout, context) -> None:
    """Top-of-panel safety notices (pending save, failed imports)."""
    if sync_manager.has_pending_save():
        box = layout.box()
        box.label(text="Rebuild applied - save the .blend to persist (Ctrl+S)",
                  icon='ERROR')

    failed = getattr(context.scene, "gnt_failed_imports", None)
    if failed is not None and len(failed.items):
        box = layout.box()
        box.label(text=failed.summary or f"{len(failed.items)} failed",
                  icon='ERROR')
        for item in list(failed.items)[:5]:
            box.label(text=f"{item.group_name} - {item.reason}",
                      icon='NODETREE')
        if len(failed.items) > 5:
            box.label(text=f"... and {len(failed.items) - 5} more",
                      icon='INFO')
        btn_row = box.row(align=True)
        btn_row.operator("gn.sync_retry_failed", text="Retry failed",
                         icon='FILE_REFRESH')
        btn_row.operator("gn.sync_failures_clear", text="Dismiss", icon='X')


def _draw_changes_list(layout, context) -> None:
    """One canonical Changes list (filters, marks, actions, deps)."""
    from .sync_operators import _change_rows

    prefs = context.scene.gnt_sync_prefs
    data = _change_rows()
    rows = data["rows"]
    ignored_rows = data.get("ignored_rows", [])
    deps = data["deps"]
    n_ignored = data.get("ignored", 0)
    staged_count = sum(1 for r in rows if r["staged"])
    total = len(rows) + len(deps)

    layout.separator()
    box = layout.box()
    head = box.row(align=True)
    head.prop(prefs, "expand_changes", text="", emboss=False,
              icon='DISCLOSURE_TRI_DOWN' if prefs.expand_changes
              else 'DISCLOSURE_TRI_RIGHT')
    head.label(text=f"Changes ({total})", icon='INFO')
    if staged_count:
        head.label(text=f"{staged_count} marked", icon='CHECKMARK')
    if not prefs.expand_changes:
        return

    if not rows and not deps:
        if ignored_rows:
            box.label(text=f"{_count_label(len(ignored_rows), 'ignored group')} "
                           "hidden", icon='HIDE_ON')
            box.prop(prefs, "show_ignored", text="Show ignored", toggle=True)
        elif not sync_manager._status_cache and not sync_manager.load_report:
            box.label(text="No status yet - click Refresh Status", icon='INFO')
        else:
            box.label(text="No changes - everything is saved",
                      icon='CHECKMARK')
        return

    filter_row = box.row(align=True)
    filter_row.prop(prefs, "expand_change_filters", text="Filter",
                    emboss=False,
                    icon='DISCLOSURE_TRI_DOWN' if prefs.expand_change_filters
                    else 'DISCLOSURE_TRI_RIGHT')
    if prefs.expand_change_filters:
        grid = box.grid_flow(row_major=True, columns=2, even_columns=True)
        grid.prop(prefs, "show_blend_modified", text="Edited locally",
                  toggle=True, icon='LIGHT')
        grid.prop(prefs, "show_json_modified", text="Changed in JSON",
                  toggle=True, icon='FILE_REFRESH')
        grid.prop(prefs, "show_conflict", text="Conflict", toggle=True,
                  icon='ERROR')
        grid.prop(prefs, "show_orphan", text="Missing in blend", toggle=True,
                  icon='GROUP')
        grid.prop(prefs, "show_json_missing", text="JSON missing", toggle=True,
                  icon='FILE')
        grid.prop(prefs, "show_ignored", text="Ignored", toggle=True,
                  icon='RESTRICT_VIEW_OFF')

    def _status_visible(r):
        s = r["status"]
        return {
            SyncStatus.BLEND_MODIFIED: prefs.show_blend_modified,
            SyncStatus.JSON_MODIFIED: prefs.show_json_modified,
            SyncStatus.CONFLICT: prefs.show_conflict,
            SyncStatus.ORPHAN: prefs.show_orphan,
            SyncStatus.JSON_MISSING: prefs.show_json_missing,
        }.get(s, True)

    shown = [r for r in rows if _status_visible(r)]
    n_local = sum(1 for r in rows
                  if r["status"] in (SyncStatus.BLEND_MODIFIED,
                                     SyncStatus.CONFLICT))
    control = box.row(align=True)
    control.label(text="Mark to save", icon='INFO')
    tools = control.row(align=True)
    tools.alignment = 'RIGHT'
    tools.operator("gn.sync_stage_all", text="Mark all", icon='CHECKMARK')
    tools.operator("gn.sync_unstage_all", text="Unmark all", icon='X')

    save_row = box.row(align=True)
    if staged_count:
        save_row.operator("gn.sync_save_staged",
                          text=f"Save selected ({staged_count})",
                          icon='EXPORT')
    elif n_local:
        save_row.operator("gn.sync_export_modified",
                          text=f"Save changed ({n_local})",
                          icon='EXPORT')
    else:
        save_row.enabled = False
        save_row.label(text="Nothing to save", icon='INFO')
    save_row.menu("GN_MT_SaveMenu", text="", icon='THREE_DOTS')

    n_animated = sum(1 for r in rows if r.get("animated"))
    if n_animated:
        box.label(text=f"{_count_label(n_animated, 'group')} "
                       f"{'has' if n_animated == 1 else 'have'} animation that "
                       "is not versioned",
                  icon=_layout_icon('STATUS_WARNING', 'WARNING'))

    if not shown and not deps and not (prefs.show_ignored and ignored_rows):
        box.label(text="No changes match current filters", icon='INFO')

    limit = len(shown) if prefs.show_all_changes else 50
    page = shown[:limit]
    if len(shown) > limit:
        row = box.row(align=True)
        row.label(text=f"Showing {limit} of {len(shown)}", icon='INFO')
        row.prop(prefs, "show_all_changes", text="Show all", toggle=True)

    def _primary(r):
        s = r["status"]
        if s == SyncStatus.CONFLICT and r["mergeable"]:
            return ("gn.sync_merge_conflict", "Merge", 'AUTOMERGE_ON')
        if s == SyncStatus.BLEND_MODIFIED:
            return ("gn.sync_export", "Save", 'EXPORT')
        if s == SyncStatus.JSON_MODIFIED:
            return ("gn.sync_import", "Pull", 'IMPORT')
        if s == SyncStatus.ORPHAN:
            return ("gn.sync_import", "Restore", 'IMPORT')
        if s == SyncStatus.JSON_MISSING:
            return ("gn.sync_export", "Re-create", 'EXPORT')
        return None

    def _draw_change_row(target, r):
        line = target.row(align=True)
        kind_icon = KIND_ICONS.get(r.get("kind", "group"))
        if kind_icon:
            line.label(text="", icon=kind_icon)
        line.label(text=r["name"],
                   icon=STATUS_ICONS.get(r["status"], 'QUESTION'))
        line.label(text=STATUS_SHORT.get(r["status"], r["status"].value))
        if r.get("ignored"):
            line.label(text="IGNORED", icon='HIDE_ON')
        if r.get("animated"):
            line.label(text="", icon=_layout_icon('STATUS_WARNING', 'WARNING'))

        actions = target.row(align=True)
        actions.alignment = 'RIGHT'
        if r.get("ignored"):
            op = actions.operator("gn.sync_unignore", text="Un-ignore",
                                  icon='RESTRICT_VIEW_OFF')
            op.sync_uuid = r["uid"]
        else:
            primary = _primary(r)
            if primary is not None:
                op_id, label, icon = primary
                op = actions.operator(op_id, text=label, icon=icon)
                op.sync_uuid = r["uid"]
            if r["saveable"]:
                op = actions.operator(
                    "gn.sync_change_unmark" if r["staged"]
                    else "gn.sync_change_mark",
                    text="Unmark" if r["staged"] else "Mark",
                    icon='REMOVE' if r["staged"] else 'CHECKMARK')
                op.sync_uuid = r["uid"]
            op = actions.operator("gn.sync_change_menu", text="",
                                  icon='THREE_DOTS')
            op.sync_uuid = r["uid"]

    staged_rows = [r for r in page if r["staged"]]
    plain_rows = [r for r in page if not r["staged"]]
    if staged_rows:
        box.label(text=f"Marked ({len(staged_rows)})", icon='CHECKMARK')
        for r in staged_rows:
            _draw_change_row(box, r)
    for r in plain_rows:
        _draw_change_row(box, r)

    if prefs.show_ignored and ignored_rows:
        box.label(text=f"Ignored ({len(ignored_rows)})", icon='HIDE_ON')
        for r in ignored_rows[:limit]:
            _draw_change_row(box, r)
        if len(ignored_rows) > limit:
            box.label(text=f"... and {len(ignored_rows) - limit} more",
                      icon='INFO')

    for d in deps:
        row = box.row(align=True)
        row.label(text=f"New dependency: {d['name']} (used by {d['parent']})",
                  icon='NODETREE')
        op = row.operator("gn.sync_track_deps", text="Track", icon='LINKED')
        op.group_name = d["name"]

    if n_ignored and not prefs.show_ignored:
        row = box.row(align=True)
        row.label(text=f"{_count_label(n_ignored, 'ignored group')} hidden",
                  icon='HIDE_ON')
        row.prop(prefs, "show_ignored", text="Show", toggle=True)


_GRAPH_CONFLICT_KINDS = frozenset((
    "node/delete-edit", "node/field", "node/rename-rename",
    "link/contention", "link/order", "identity/name",
))


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
    head.label(text="Merged" if status == "merged" else "Conflicted")
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
        if kind in _GRAPH_CONFLICT_KINDS:
            if drawn_others >= 20:
                skipped += 1
                continue
            drawn_others += 1
            box.label(text=f"{item.item}: base {item.base}, ours "
                           f"{item.ours}, JSON {item.theirs}", icon='ERROR')
            if editable:
                box.row(align=True).prop(item, "decision", expand=True)
                if item.decision == "none":
                    undecided += 1
            continue
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
        head.label(text={"ok": "Ready", "restored": "Restored"}.get(
            state.status, state.status))
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


def _draw_refresh_button(layout, context):
    """Refresh Status / Checking x/y in a layout row."""
    if sync_manager.check_busy():
        done, total = sync_manager.check_progress()
        wait = layout.row(align=True)
        wait.enabled = False
        wait.operator("gn.sync_check", text=f"Checking {done}/{total}...",
                      icon='FILE_REFRESH')
    else:
        layout.operator("gn.sync_check", text="Refresh Status",
                        icon='FILE_REFRESH')


def _draw_status_summary(layout, context):
    """Compact status line; the Changes list owns the issue count."""
    if sync_manager.check_busy():
        done, total = sync_manager.check_progress()
        row = layout.row(align=True)
        row.label(text=f"Checking {done}/{total}...", icon='TIME')
        row.operator("gn.sync_cancel_check", text="Cancel", icon='X')
        return
    if not sync_manager._status_cache:
        row = layout.row(align=True)
        row.label(text="Status not checked", icon='INFO')
        _draw_refresh_button(row, context)
        if sync_manager.load_report:
            layout.label(
                text=f"{_count_label(len(sync_manager.load_report), 'JSON change')}"
                     " detected - Refresh Status to compare",
                icon='FILE_REFRESH')
        return
    summary = sync_manager.get_status_summary()
    n_total = len(sync_manager.metadata.get("tracked_groups", {}))
    n_cached = sum(v for k, v in summary.items() if k != "ignored")
    n_synced = summary.get("synced", 0)
    partial = n_cached < n_total
    row = layout.row(align=True)
    row.label(text=f"{n_total} tracked", icon='NODETREE')
    row.label(text=f"{n_synced} synced", icon='CHECKMARK')
    _draw_refresh_button(row, context)
    if partial:
        layout.label(text="Status partially checked - Refresh Status",
                     icon='INFO')

    counters = (
        ("To save", summary.get("blend_modified", 0), 'LIGHT'),
        ("To pull", summary.get("json_modified", 0), 'FILE_REFRESH'),
        ("Conflicts", summary.get("conflict", 0), 'ERROR'),
        ("Missing", summary.get("orphan", 0), 'GROUP'),
        ("JSON missing", summary.get("json_missing", 0), 'FILE'),
    )
    active = [(label, count, icon) for label, count, icon in counters if count]
    if active:
        grid = layout.grid_flow(row_major=True, columns=2, even_columns=True)
        for label, count, icon in active:
            grid.label(text=f"{label}: {count}", icon=icon)
    elif not partial:
        layout.label(text="All synced", icon='CHECKMARK')


def _draw_review(layout, context):
    """Conflict banner + last merge report; the Changes list has actions."""
    summary = sync_manager.get_status_summary()
    n_conflict = summary.get("conflict", 0)
    if n_conflict:
        box = layout.box()
        box.label(text=f"{_count_label(n_conflict, 'conflict')} - resolve in "
                       "Changes", icon='ERROR')
    _draw_merge_report(layout, context)


# ---------------------------------------------------------------------------
# Panel 1: Batch Operations
# ---------------------------------------------------------------------------





class GN_PT_SyncPanel(bpy.types.Panel):
    bl_label = "Sync"
    bl_idname = "GN_PT_SyncPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GNToolkit'
    bl_order = 0

    @classmethod
    def poll(cls, context):
        return _supported_editor(context)

    def draw(self, context):
        layout = self.layout
        _draw_sync_alerts(layout, context)
        mismatches = sync_manager.uuid_mismatch_count()
        if mismatches:
            box = layout.box()
            box.label(text=f"{_count_label(mismatches, 'entry')} matched by "
                           "name but with a different UUID", icon='ERROR')
            box.label(text="Restore the matching sidecar or re-track these "
                           "groups", icon='INFO')
        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})
        if tracked:
            changed = sync_manager.scan_external_json_changes()
            if changed:
                _notify_external_changes(len(changed))
        _draw_active_entry(layout, context)
        if not tracked:
            layout.separator()
            layout.label(text="Nothing tracked yet", icon='INFO')
            layout.label(text="Use Track to add a group, material or world.",
                         icon='INFO')
            return
        layout.separator()
        _draw_status_summary(layout, context)
        summary = sync_manager.get_status_summary()
        pullable = (summary.get("json_modified", 0)
                    + summary.get("conflict", 0)
                    + summary.get("orphan", 0))
        if pullable:
            batch = layout.row(align=True)
            batch.operator("gn.sync_import_modified",
                           text=f"Pull from JSON ({pullable})",
                           icon='IMPORT')
        if sync_manager._status_cache or sync_manager.load_report:
            _draw_changes_list(layout, context)
        _draw_review(layout, context)


class GN_PT_TrackPanel(bpy.types.Panel):
    bl_label = "Track"
    bl_idname = "GN_PT_TrackPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GNToolkit'
    bl_order = 1

    @classmethod
    def poll(cls, context):
        return _supported_editor(context)

    def draw(self, context):
        layout = self.layout
        entry = _draw_active_entry(layout, context)
        layout.separator()
        layout.label(text="Add more", icon='ADD')
        row = layout.row(align=True)
        row.operator("gn.sync_link_all", text="Track all groups...",
                     icon='FILE_TICK')
        row = layout.row(align=True)
        row.operator("gn.sync_link_all_materials",
                     text="Track all materials...", icon='MATERIAL')
        row = layout.row(align=True)
        row.operator("gn.sync_link_all_worlds",
                     text="Track all worlds...", icon='WORLD')
        row = layout.row(align=True)
        row.operator("gn.sync_link_folder", text="Track from folder...",
                     icon='FILE_FOLDER')
        row = layout.row(align=True)
        row.operator("gn.sync_initialize", text="Adopt existing JSON...",
                     icon='FILE_FOLDER')
        comp = getattr(context.scene, "compositing_node_group", None)
        if comp is not None:
            row = layout.row(align=True)
            row.operator("gn.sync_link_scene_compositor",
                         text="Track scene compositor...",
                         icon=_layout_icon('COMPOSITING', 'NODETREE'))
        layout.separator()
        if entry is not None and entry["info"] is not None:
            stop = layout.operator("gn.sync_unlink",
                                   text=f"Stop tracking {entry['name']}",
                                   icon='X')
            stop.sync_uuid = entry["uid"]
        layout.operator("gn.sync_unlink_all", text="Stop tracking all...",
                        icon='X')
        layout.label(text="Tracking writes the JSON and metadata; "
                          "Save writes later changes.", icon='INFO')


class GN_PT_ExplorePanel(bpy.types.Panel):
    bl_label = "Explore"
    bl_idname = "GN_PT_ExplorePanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GNToolkit'
    bl_order = 2

    @classmethod
    def poll(cls, context):
        return _supported_editor(context)

    def draw(self, context):
        from . import graph_view as gv
        layout = self.layout
        row = layout.row(align=True)
        row.operator("gn.graph_view", text="Graph view...", icon='NODETREE')
        clusters = row.operator("gn.graph_view", text="Clusters...",
                                icon='NODETREE')
        clusters.mode = 'CLUSTERS'
        row = layout.row(align=True)
        row.operator("gn.graph_poster", text="Graph poster...", icon='EXPORT')
        row.operator("gn.graph_clear", text="Clear graph", icon='X')
        report = getattr(gv, "last_report", None)
        if report:
            unit = "clusters" if report.get("mode") == "CLUSTERS" else "groups"
            text = (f"{report.get('nodes', 0)} {unit}, "
                    f"{report.get('links', 0) - report.get('reroutes', 0)} "
                    "links, "
                    f"{report.get('reroutes', 0)} routing")
            if report.get("clipped"):
                text += f", {report['clipped']} clipped"
            layout.label(text=text, icon='INFO')
            if report.get("clipped"):
                layout.label(text="Graph incomplete: increase the port cap "
                                  "or use a neighborhood.", icon='ERROR')
        state = getattr(context.scene, "gnt_history_state", None)
        history_open = bool(state is not None and state.uid)
        if report and report.get("stale") and not history_open:
            row = layout.row(align=True)
            row.label(text="Board may be out of date",
                      icon=STATUS_WARNING_ICON)
            row.operator("gn.graph_rebuild", text="Rebuild",
                         icon='FILE_REFRESH')
        layout.label(text="Bar marks: ~ edited | J JSON | ! conflict | "
                          "M missing | F JSON missing", icon='INFO')
        prefs = context.scene.gnt_sync_prefs
        layout.prop(prefs, "follow_board_selection",
                    text="Follow board selection")
        board = _board_selection(context)
        if board:
            name, uid = board
            row = layout.row(align=True)
            row.label(text=f"Board selection: {name}", icon='NODETREE')
            op = row.operator("gn.sync_history_open", text="", icon='TIME')
            op.sync_uuid = uid
        if state is not None and state.uid:
            _draw_history_box(layout, context)
        else:
            layout.label(text="Select a row's History or a board bar.",
                         icon='INFO')


class GN_PT_IssuesPanel(bpy.types.Panel):
    bl_label = "Project Health"
    bl_idname = "GN_PT_IssuesPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GNToolkit'
    bl_parent_id = "GN_PT_AdvancedPanel"
    bl_order = 1
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return (bool(sync_manager.metadata.get("tracked_groups"))
                and _supported_editor(context))

    def draw(self, context):
        layout = self.layout
        audit_state = context.scene.gnt_audit_state
        audit_box = layout.box()
        head = audit_box.row(align=True)
        head.label(text="Project audit", icon='VIEWZOOM')
        if _audit_busy():
            done, total = _audit_progress()
            head.label(text=f"{done}/{total}", icon='TIME')
            head.operator("gn.cancel_project_audit", text="Cancel", icon='X')
            audit_box.label(text="Auditing in the background...", icon='INFO')
            return
        head.operator("gn.audit_project", text="Run", icon='PLAY')
        head.operator("gn.audit_clear", text="", icon='X')
        if audit_state.summary:
            audit_box.label(text=audit_state.summary, icon='INFO')
        for item in audit_state.details[:30]:
            audit_box.label(text=item.text,
                            icon=AUDIT_KIND_ICONS.get(item.kind, 'INFO'))
        extra = len(audit_state.details) - 30
        if extra > 0:
            audit_box.label(text=f"... and {extra} more", icon='INFO')

        deps = sync_manager.find_untracked_dependencies()
        if deps:
            layout.label(text=f"{_count_label(len(deps), 'untracked dependency')}"
                              " - see Changes", icon='PLUS')
        else:
            layout.label(text="No untracked dependencies", icon='CHECKMARK')


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
    if self.kind == "identity/name":
        return [("none", "...", "Not decided yet"),
                ("delete", "Delete", "Drop the colliding node")]
    items = [("none", "...", "Not decided yet"),
             ("ours", "Ours", "Keep the local .blend value"),
             ("base", "Base", "Restore the pre-edit value"),
             ("theirs", "JSON", "Take the value from the JSON file")]
    if self.kind in _GRAPH_CONFLICT_KINDS and self.kind != "link/order":
        items.append(("delete", "Delete", "Remove the node or link"))
    elif self.value_type in ("float", "int", "bool", "string"):
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


_audit_state = {
    "running": False,
    "items": [],
    "index": 0,
    "records": [],
    "counts": {},
    "target": "",
    "blend_dir": "",
}


def _audit_busy() -> bool:
    return bool(_audit_state["running"])


def _audit_progress() -> tuple[int, int]:
    return int(_audit_state["index"]), len(_audit_state["items"])


def _audit_target(context, metadata, group_name: str) -> str:
    target = (group_name or "").strip()
    if target:
        return target
    tree = _get_active_tree(context)
    if tree is not None:
        uid = find_uuid_for_tree(tree, metadata)
        if uid:
            return metadata.get("tracked_groups", {}).get(uid, {}).get(
                "blend_name", "")
    return ""


def _audit_process(state, items, start, end, records, counts, blend_dir):
    for uid, info in items[start:end]:
        name = info.get("blend_name") or uid
        tree = bpy.data.node_groups.get(name)
        if tree is not None and group_library_path(tree):
            counts["linked"] += 1
            _audit_add(state, "linked",
                       f"{name} - linked from {group_library_path(tree)} "
                       "(make it local to track)")
            continue
        json_path = resolve_json_path(info.get("json_path", ""), blend_dir)
        if not json_path or not os.path.isfile(json_path):
            counts["missing"] += 1
            _audit_add(state, "missing", f"{name} - file not found")
            continue
        kind = info.get("kind", "group")
        if kind != "group":
            entry = package_entry(read_json_tolerant(json_path), info)
            if entry is None:
                counts["missing"] += 1
                _audit_add(state, "missing",
                           f"{name} - {kind} entry not found inside "
                           f"{os.path.basename(json_path)}")
            else:
                records.append({
                    "name": f"{kind}: {name}",
                    "hash": canonical_hash_from_json_data(entry)})
            continue
        data, err = audit.group_data_from_file(json_path, name)
        if err == "conflict":
            counts["conflict"] += 1
            _audit_add(state, "conflict", f"{name} - git merge markers")
        elif err == "unreadable":
            counts["unreadable"] += 1
            _audit_add(state, "unreadable",
                       f"{name} - not valid UTF-8/JSON")
        elif err == "missing-group":
            counts["missing"] += 1
            _audit_add(state, "missing",
                       f"{name} - group not found inside "
                       f"{os.path.basename(json_path)}")
        else:
            records.append({"name": name,
                            "hash": audit.content_hash_from_json_data(data)})


def _audit_finish(state, records, counts, target, metadata):
    buckets = audit.duplicates_from_records(records)
    for bucket in buckets:
        _audit_add(state, "duplicate",
                   f'{len(bucket["names"])} groups with identical logic: '
                   + ", ".join(bucket["names"]))

    ext = audit.external_refs(metadata)
    for entry in ext:
        _audit_add(state, "external",
                   f'{entry["name"]} - {len(entry["missing_uuids"])} '
                   'untracked reference(s)')

    if target:
        imp = audit.impact(metadata, target)
        if imp is None:
            _audit_add(state, "impact", f"{target} - not tracked")
        else:
            consumers = []
            for obj in bpy.data.objects:
                for mod in obj.modifiers:
                    if (mod.type == 'NODES'
                            and mod.node_group is not None
                            and mod.node_group.name == imp["name"]):
                        consumers.append(f"{obj.name} ({mod.name})")
            head = ", ".join(e["name"] for e in imp["direct"][:4])
            more = len(imp["direct"]) - 4
            suffix = f" - {head}" if head else ""
            if more > 0:
                suffix += f" (+{more} more)"
            line = (f'{imp["name"]}: {len(imp["direct"])} direct, '
                    f'{len(imp["transitive"])} total dependents{suffix}')
            if consumers:
                extra = len(consumers) - 4
                line += (f" - used by {len(consumers)} object modifier(s): "
                         + ", ".join(consumers[:4]))
                if extra > 0:
                    line += f" (+{extra} more)"
            _audit_add(state, "impact", line)

    if not state.details:
        state.summary = "No findings - project looks healthy"
    else:
        state.summary = (
            f"{_count_label(len(buckets), 'duplicated group')} - "
            f"{_count_label(len(ext), 'external ref')} - "
            f"{counts['missing']} missing - {counts['unreadable']} unreadable - "
            f"{_count_label(counts['conflict'], 'conflict')} - "
            f"{_count_label(counts['linked'], 'library-linked group')}")


def _audit_redraw() -> None:
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:
        pass


def _audit_tick():
    state = _audit_state
    if not state["running"]:
        return None
    total = len(state["items"])
    end = min(state["index"] + 10, total)
    _audit_process(state["scene_state"], state["items"], state["index"], end,
                   state["records"], state["counts"], state["blend_dir"])
    state["index"] = end
    _audit_redraw()
    if end >= total:
        _audit_finish(state["scene_state"], state["records"],
                      state["counts"], state["target"],
                      sync_manager.metadata)
        state["running"] = False
        _audit_redraw()
        return None
    return 0.05


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
        if _audit_busy():
            self.report({'WARNING'}, "An audit is already running")
            return {'CANCELLED'}
        scene_state = context.scene.gnt_audit_state
        scene_state.summary = ""
        scene_state.details.clear()
        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})
        if not tracked:
            self.report({'INFO'}, "No tracked groups")
            return {'FINISHED'}
        target = _audit_target(context, metadata, self.group_name)
        items = list(tracked.items())
        _audit_state.update(
            running=True, items=items, index=0, records=[],
            counts={"missing": 0, "unreadable": 0, "conflict": 0, "linked": 0},
            target=target, blend_dir=sync_manager._blend_dir(),
            scene_state=scene_state)
        if bpy.app.background:
            _audit_process(scene_state, items, 0, len(items),
                           _audit_state["records"], _audit_state["counts"],
                           _audit_state["blend_dir"])
            _audit_state["index"] = len(items)
            _audit_finish(scene_state, _audit_state["records"],
                          _audit_state["counts"], target, metadata)
            _audit_state["running"] = False
            return {'FINISHED'}
        try:
            bpy.app.timers.register(_audit_tick, first_interval=0.05,
                                    persistent=True)
        except Exception:
            _audit_process(scene_state, items, 0, len(items),
                           _audit_state["records"], _audit_state["counts"],
                           _audit_state["blend_dir"])
            _audit_state["index"] = len(items)
            _audit_finish(scene_state, _audit_state["records"],
                          _audit_state["counts"], target, metadata)
            _audit_state["running"] = False
        self.report({'INFO'}, f"Auditing {len(items)} tracked entries...")
        return {'FINISHED'}


class GN_OT_CancelProjectAudit(bpy.types.Operator):
    """Cancel a running project audit."""

    bl_idname = "gn.cancel_project_audit"
    bl_label = "Cancel Audit"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        _audit_state["running"] = False
        state = context.scene.gnt_audit_state
        state.summary = "Audit cancelled"
        state.details.clear()
        _audit_redraw()
        return {'FINISHED'}


class GN_OT_AuditClear(bpy.types.Operator):
    """Clear the audit report."""

    bl_idname = "gn.audit_clear"
    bl_label = "Clear Audit Report"
    bl_options = {'REGISTER'}

    def execute(self, context):
        _audit_state["running"] = False
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
        sync_manager.refresh_cached_statuses({self.sync_uuid})
        for area in context.screen.areas:
            area.tag_redraw()
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
        sync_manager.refresh_cached_statuses({self.sync_uuid})
        sync_manager.refresh_history_after_sync(self.sync_uuid, "Kept JSON")
        for area in context.screen.areas:
            area.tag_redraw()
        self.report({'INFO'}, "Conflict resolved - kept the JSON version")
        return {'FINISHED'}


class GN_OT_SyncMergeConflict(bpy.types.Operator):
    bl_idname = "gn.sync_merge_conflict"
    bl_label = "Merge with JSON"
    bl_description = ("Merge this conflicted entry per item (values, and "
                      "nodes/links when the sides carry identities): local "
                      ".blend edits are kept and the JSON changes applied; "
                      "the common base comes from the JSON file's git "
                      "history. Nothing is written when items conflict")
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
        from .codec import _round_float
        return _round_float(float(item.value_float))
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
        entities = {}
        undecided = 0
        has_structure = False
        for item in state.items:
            if item.kind in _GRAPH_CONFLICT_KINDS:
                if item.key and item.decision in ("ours", "base", "theirs",
                                                  "delete"):
                    entities[item.key] = item.decision
                else:
                    undecided += 1
            elif item.kind == "value":
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
        if entities:
            overrides["entities"] = entities
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


def _focus_board_nodes(names, context) -> None:
    """Select and frame the marked bars when the GNT Graph is visible."""
    tree = bpy.data.node_groups.get(GRAPH_TREE_NAME)
    if tree is None:
        return
    selected = False
    for node in tree.nodes:
        if (node.type != 'GROUP' or not node.label
                or node.name.startswith("legend_")):
            continue
        hit = _raw_name(node.label) in names
        node.select = hit
        selected = selected or hit
    if not selected:
        return
    for area in getattr(context.screen, "areas", []):
        if area.type != 'NODE_EDITOR':
            continue
        if area.spaces.active.node_tree is not tree:
            continue
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        try:
            with bpy.context.temp_override(area=area, region=region,
                                           screen=context.screen,
                                           window=getattr(context, "window", None)):
                bpy.ops.node.view_selected()
        except Exception:
            pass
        return


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
            note = (f"{_count_label(marked, 'group')} of this file differ in this "
                    "revision - marked on the board")
            _focus_board_nodes(names, context)
        elif names:
            note = (f"{_count_label(len(names), 'group')} differ in this revision "
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
            state.message = _count_label(len(state.items), 'revision')
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

_git_log_cache: dict = {}


def _cached_git_log(root, rel_path=None, count=10):
    """Cached ``git log`` for panel draws (no subprocess on every redraw)."""
    key = (root, rel_path or "", int(count))
    if key not in _git_log_cache:
        from .git_integration import git_log
        _git_log_cache[key] = git_log(root, rel_path, count)
    return _git_log_cache[key]


def _invalidate_git_log_cache() -> None:
    _git_log_cache.clear()


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

    bl_label = "Git"
    bl_idname = "GN_PT_CollaborationPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GNToolkit'
    bl_order = 3

    @classmethod
    def poll(cls, context):
        return (bool(sync_manager.metadata.get("tracked_groups"))
                and _supported_editor(context))

    def draw(self, context):
        from .git_integration import (
            get_git_state, ensure_status_job, git_busy,
        )

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

        summary = sync_manager.get_status_summary()
        n_total = sum(v for k, v in summary.items() if k != "ignored")
        all_synced = bool(n_total) and summary.get("synced", 0) == n_total

        for root, st in sorted(repos.items()):
            box = layout.box()
            head = box.row(align=True)
            if not st.get("ok"):
                head.label(text=f"Repository: {st.get('name', root)}",
                           icon='ERROR')
                head.label(text=st.get("error", "git status failed"))
                continue
            if st["tracked_changed"]:
                icon = 'FILE_REFRESH'
            elif st["ahead"] or st["behind"]:
                icon = 'NETWORK_DRIVE'
            else:
                icon = 'CHECKMARK'
            head.label(text=f"Repository: {st['name']}", icon=icon)

            head_hash = st.get("head") or ""
            head_subject = st.get("head_subject") or ""
            if head_hash:
                if len(head_subject) > 42:
                    head_subject = head_subject[:39] + "..."
                head_text = f"HEAD: {head_hash}"
                if head_subject:
                    head_text += f" — {head_subject}"
            else:
                head_text = "HEAD: (no commits yet)"
            box.label(text=head_text, icon='DOT')

            branch = st.get("branch") or "(detached)"
            upstream = st.get("upstream") or ""
            if upstream:
                branch_text = f"Branch: {branch}  ·  upstream: {upstream}"
                branch_text += (f"  ·  ahead {st.get('ahead', 0)}, "
                                f"behind {st.get('behind', 0)}")
            else:
                branch_text = f"Branch: {branch}  ·  upstream: (none)"
            box.label(text=branch_text, icon='NETWORK_DRIVE')

            if st["tracked_changed"]:
                working = (f"{len(st['tracked_changed'])} tracked JSON "
                           "changed")
                working_icon = 'FILE_REFRESH'
            else:
                working = "clean (tracked JSON)"
                working_icon = 'CHECKMARK'
            box.label(text=f"Working tree: {working}", icon=working_icon)
            if st["tracked_changed"] and all_synced:
                box.label(text="Git sees file changes; GNToolkit is synced "
                               "with the working JSON", icon='INFO')
                box.label(text="The JSON may contain uncommitted changes.",
                          icon='INFO')
                discard = box.operator("gn.git_discard_layout",
                                       text="Discard cosmetic-only changes",
                                       icon='LOOP_BACK')
                discard.repo = root

            actions = box.row(align=True)
            actions.enabled = not busy
            commit_op = actions.operator("gn.git_commit", text="Git commit...",
                                         icon='EXPORT')
            commit_op.repo = root
            sync_op = actions.operator("gn.git_sync", text="Git sync",
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

        if len(repos) == 1:
            repo_box = layout.box()
            repo_head = repo_box.row(align=True)
            repo_head.prop(prefs, "expand_git_repo_log", text="", emboss=False,
                           icon='DISCLOSURE_TRI_DOWN'
                           if prefs.expand_git_repo_log
                           else 'DISCLOSURE_TRI_RIGHT')
            repo_head.label(text="Repository history", icon='TIME')
            repo_head.operator("gn.git_history_refresh", text="",
                               icon='FILE_REFRESH')
            if prefs.expand_git_repo_log:
                entries = _cached_git_log(next(iter(repos)), None, 10)
                if entries:
                    _draw_git_log_box(repo_box, entries)
                    repo_box.label(text="Shows the last 10 commits (newest first)",
                                   icon='INFO')
                else:
                    repo_box.label(text="No commits yet", icon='INFO')

        layout.separator()
        settings_op = layout.operator("preferences.addon_show",
                                      text="Credentials & Git settings...",
                                      icon='PREFERENCES')
        settings_op.module = __package__


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

    check_on_load: bpy.props.BoolProperty(
        name="Check JSON on open",
        description="After loading a .blend, compare the JSON hashes in the "
                    "background and show a notice when files changed outside "
                    "Blender",
        default=True,
    )
    fetch_on_load: bpy.props.BoolProperty(
        name="Fetch remotes on open",
        description="After loading a .blend, contact the Git remotes of the "
                    "tracked repositories in the background (only when online "
                    "access is enabled)",
        default=True,
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
        layout.label(text="Git engine", icon='SETTINGS')
        layout.prop(self, "git_backend", text="")
        layout.label(text="Network", icon='URL')
        layout.prop(self, "network_timeout")
        layout.label(text="On file open", icon='FILE_REFRESH')
        layout.prop(self, "check_on_load")
        layout.prop(self, "fetch_on_load")
        layout.separator()
        layout.label(text="Credentials", icon='LOCKED')
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
    follow_board_selection: bpy.props.BoolProperty(
        name="Follow board selection",
        description="Open History automatically when a GNT Graph bar is "
                    "selected (otherwise use the bar's History button)",
        default=False,
    )
    expand_change_filters: bpy.props.BoolProperty(
        name="Show change filters",
        description="Show the status filters in the Changes list",
        default=False,
    )
    show_all_changes: bpy.props.BoolProperty(
        name="Show all changes",
        description="List every out-of-sync group instead of the first 50",
        default=False,
    )

# ---------------------------------------------------------------------------
# Panel 3: Geometry Validation Issues
# ---------------------------------------------------------------------------

class GN_PT_GeometryIssuesPanel(bpy.types.Panel):
    """Panel showing geometry validation issues for tracked groups."""

    bl_label = "Geometry Nodes Validation"
    bl_idname = "GN_PT_GeometryIssuesPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GNToolkit'
    bl_parent_id = "GN_PT_AdvancedPanel"
    bl_order = 2
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "node_tree", None)
        return (bool(sync_manager.metadata.get("tracked_groups"))
                and getattr(tree, "type", "") == 'GEOMETRY')

    def draw_header(self, context):
        if _validation_busy():
            done, total = _validation_progress()
            self.layout.label(text=f"Validating {done}/{total}", icon='TIME')
            return
        if not sync_manager.has_geometry_validation_result():
            self.layout.label(text="Not run", icon='INFO')
            return
        count = sync_manager.get_geometry_issue_count()
        if count:
            self.layout.label(text=f"({count})", icon='ERROR')
        else:
            self.layout.label(text="Clean", icon='CHECKMARK')

    def draw(self, context):
        layout = self.layout
        if _validation_busy():
            done, total = _validation_progress()
            row = layout.row(align=True)
            row.label(text=f"Validating {done}/{total}...", icon='TIME')
            row.operator("gn.cancel_geometry_validation", text="Cancel",
                         icon='X')
            layout.label(text="Validation runs in the background; you can "
                              "close Advanced.", icon='INFO')
            return
        issues = sync_manager.get_geometry_issues()

        # Button to run validation
        row = layout.row()
        row.operator("gn.validate_geometry", text="Validate", icon='VIEWZOOM')

        if not issues:
            if sync_manager.has_geometry_validation_result():
                layout.label(text="No geometry issues found", icon='CHECKMARK')
            else:
                layout.label(text="Click 'Validate' to check", icon='INFO')
            return

        layout.separator()
        layout.label(text=f"{_count_label(len(issues), 'issue')} found", icon='ERROR')
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
            rec_row = box.row(align=True)
            rec_row.label(text=f"Recommendation: {issue.recommendation}", icon='INFO')
            if issue.sync_uuid and issue.node_name:
                op = rec_row.operator("gn.validate_focus_issue",
                                      text="Go to node", icon='VIEWZOOM')
                op.sync_uuid = issue.sync_uuid
                op.node_name = issue.node_name

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

class GN_OT_FocusValidationIssue(bpy.types.Operator):
    """Open the validated group and select the offending node."""

    bl_idname = "gn.validate_focus_issue"
    bl_label = "Go to Node"
    bl_description = "Open the group and select the node with this issue"
    bl_options = {'REGISTER'}

    sync_uuid: bpy.props.StringProperty()
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        result = bpy.ops.gn.sync_focus_group(sync_uuid=self.sync_uuid)
        if 'FINISHED' not in result:
            return result
        info = sync_manager.metadata.get("tracked_groups", {}).get(
            self.sync_uuid)
        tree = resolve_entry_tree(self.sync_uuid, info) if info else None
        node = tree.nodes.get(self.node_name) if tree is not None else None
        if node is None:
            self.report({'WARNING'}, "The node no longer exists")
            return {'CANCELLED'}
        for other in tree.nodes:
            other.select = other == node
        for area in context.screen.areas:
            if area.type != 'NODE_EDITOR':
                continue
            if area.spaces.active.node_tree is not tree:
                continue
            region = next((r for r in area.regions if r.type == 'WINDOW'),
                          None)
            try:
                with bpy.context.temp_override(
                        area=area, region=region, screen=context.screen,
                        window=getattr(context, "window", None)):
                    bpy.ops.node.view_selected()
            except Exception:
                pass
            area.tag_redraw()
            break
        return {'FINISHED'}


_validation_state = {
    "running": False,
    "uids": [],
    "index": 0,
    "issues": {},
    "total": 0,
}


def _validation_busy() -> bool:
    return bool(_validation_state["running"])


def _validation_progress() -> tuple[int, int]:
    return int(_validation_state["index"]), int(_validation_state["total"])


def _validation_redraw() -> None:
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:
        pass


def _validation_tick():
    state = _validation_state
    if not state["running"]:
        return None
    metadata = sync_manager.metadata.get("tracked_groups", {})
    batch = 2
    while batch > 0 and state["index"] < state["total"]:
        uid = state["uids"][state["index"]]
        info = metadata.get(uid, {})
        try:
            issues = validate_tracked_tree(uid, info)
        except Exception:
            issues = []
        if issues:
            state["issues"][uid] = issues
        state["index"] += 1
        batch -= 1
    sync_manager._geometry_issues_cache = dict(state["issues"])
    _validation_redraw()
    if state["index"] >= state["total"]:
        state["running"] = False
        sync_manager._geometry_checked = True
        return None
    return 0.05


def _start_validation() -> int:
    sync_manager.invalidate_geometry_cache()
    uids = list(sync_manager.metadata.get("tracked_groups", {}))
    _validation_state.update(running=True, uids=uids, index=0,
                             issues={}, total=len(uids))
    if uids and not bpy.app.background:
        try:
            bpy.app.timers.register(_validation_tick, first_interval=0.05,
                                    persistent=True)
        except Exception:
            _run_validation_sync()
    elif not uids:
        _validation_state["running"] = False
        sync_manager._geometry_checked = True
    return len(uids)


def _run_validation_sync() -> int:
    sync_manager.invalidate_geometry_cache()
    issues_by_uuid = sync_manager.validate_geometry()
    _validation_state["running"] = False
    return sum(len(v) for v in issues_by_uuid.values())


class GN_OT_CancelGeometryValidation(bpy.types.Operator):
    """Cancel a running background validation."""

    bl_idname = "gn.cancel_geometry_validation"
    bl_label = "Cancel Validation"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        _validation_state["running"] = False
        sync_manager._geometry_issues_cache.clear()
        sync_manager._geometry_checked = False
        _validation_redraw()
        return {'FINISHED'}


class GN_OT_ValidateGeometry(bpy.types.Operator):
    """Run geometry validation on all tracked groups."""

    bl_idname = "gn.validate_geometry"
    bl_label = "Validate Geometry"
    bl_description = "Check all tracked groups for geometry issues"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if _validation_busy():
            self.report({'WARNING'}, "Validation is already running")
            return {'CANCELLED'}
        if bpy.app.background:
            total = _run_validation_sync()
            if total:
                self.report({'WARNING'},
                            f"Validation found {_count_label(total, 'issue')} - "
                            "see Advanced > Geometry Nodes Validation.")
            else:
                self.report({'INFO'}, "No geometry issues found")
            return {'FINISHED'}
        total = _start_validation()
        self.report({'INFO'}, f"Validating {total} group(s) in the background...")
        return {'FINISHED'}


class GN_OT_GitHistoryRefresh(bpy.types.Operator):
    """Reload the cached repository history."""

    bl_idname = "gn.git_history_refresh"
    bl_label = "Refresh Git History"
    bl_description = "Reload the cached repository history"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        _invalidate_git_log_cache()
        for area in context.screen.areas:
            area.tag_redraw()
        return {'FINISHED'}


class GN_OT_SyncImportGroupDialog(bpy.types.Operator):
    """Modal, wide import picker (replaces the inline sidebar box)."""

    bl_idname = "gn.sync_import_group_dialog"
    bl_label = "Import Group from JSON"
    bl_description = "Pick a group and preview its missing dependencies"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        state = context.scene.gnt_import_state
        if not state.filepath:
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(
            self, width=620, confirm_text="Import")

    def draw(self, context):
        from .sync_operators import (_compute_selection_plan,
                                     _tracked_elsewhere_notes)
        state = context.scene.gnt_import_state
        layout = self.layout
        layout.label(text=f"Package: {os.path.basename(state.filepath)}",
                     icon='PACKAGE')
        layout.label(text=state.filepath, icon='FILE')
        layout.prop_search(state, "group_name", state, "items", text="Group")
        if not state.group_name:
            layout.label(text="Search and select a group to see the plan.",
                         icon='INFO')
            return
        plan = _compute_selection_plan(state)
        if not plan:
            layout.label(text="Not found in this package", icon='ERROR')
            return
        if plan["existing"]:
            layout.label(text="Already in the .blend (not touched):",
                         icon='INFO')
            for name in plan["existing"]:
                layout.label(text=f"  {name}",
                             icon=_picker_item_icon(name, 'CHECKMARK'))
        if plan["missing"]:
            layout.label(text="Will be imported:", icon='IMPORT')
            for name in plan["missing"]:
                layout.label(text=f"  {name}",
                             icon=_picker_item_icon(name, 'NODETREE'))
        if plan["divergent"]:
            layout.label(text="Differ from package: "
                              + ", ".join(plan["divergent"]), icon='ERROR')
        if plan["external"]:
            layout.label(text="Unconnected refs: "
                              + ", ".join(plan["external"]),
                         icon=STATUS_WARNING_ICON)
        notes = _tracked_elsewhere_notes(state, plan)
        for name, other_pkg in notes:
            layout.label(text=f"{name} - tracked to: {other_pkg} "
                              "(not this package)", icon='INFO')
        row = layout.row(align=True)
        track_row = row.row()
        track_row.enabled = bool(bpy.data.node_groups.get(state.group_name))
        op = track_row.operator("gn.sync_import_group_track",
                                text="Track to JSON...", icon='LINKED')
        op.group_name = state.group_name
        layout.label(text="Existing groups are never touched - only missing "
                          "dependencies are imported.", icon='INFO')

    def execute(self, context):
        state = context.scene.gnt_import_state
        if not (state.filepath and state.group_name):
            self.report({'ERROR'}, "Select a group first")
            return {'CANCELLED'}
        result = bpy.ops.gn.sync_import_group(
            filepath=state.filepath, group_name=state.group_name)
        state.open = False
        return result


class GN_OT_SyncFocusGroup(bpy.types.Operator):
    """Open the entry's node tree in a node editor."""

    bl_idname = "gn.sync_focus_group"
    bl_label = "Go to Item"
    bl_description = "Open this group/material/world tree in a node editor"
    bl_options = {'REGISTER'}

    sync_uuid: bpy.props.StringProperty()

    def execute(self, context):
        info = sync_manager.metadata.get("tracked_groups", {}).get(
            self.sync_uuid)
        if info is None:
            self.report({'ERROR'}, "This entry is no longer tracked")
            return {'CANCELLED'}
        tree = resolve_entry_tree(self.sync_uuid, info)
        if tree is None:
            self.report({'ERROR'}, "The node tree was not found")
            return {'CANCELLED'}
        wanted = {
            "GEOMETRY": 'GeometryNodeTree',
            "SHADER": 'ShaderNodeTree',
            "COMPOSITING": 'CompositorNodeTree',
        }.get(getattr(tree, "type", ""), 'GeometryNodeTree')
        for area in context.screen.areas:
            if area.type != 'NODE_EDITOR':
                continue
            space = area.spaces.active
            try:
                space.tree_type = wanted
                space.pin = True
                space.path.clear()
                space.path.start(tree)
            except Exception:
                try:
                    space.node_tree = tree
                except Exception:
                    continue
            area.tag_redraw()
            return {'FINISHED'}
        self.report({'ERROR'}, "No Node Editor area is available")
        return {'CANCELLED'}


class GN_OT_ChangeMenu(bpy.types.Operator):
    """Per-row action menu for the unified Changes list."""

    bl_idname = "gn.sync_change_menu"
    bl_label = "Change Actions"
    bl_options = {'INTERNAL'}

    sync_uuid: bpy.props.StringProperty()

    def invoke(self, context, event):
        if not self.sync_uuid:
            return {'CANCELLED'}
        info = sync_manager.metadata.get("tracked_groups", {}).get(
            self.sync_uuid)
        if info is None:
            return {'CANCELLED'}
        context.window_manager.popup_menu(
            self._draw_menu, title=info.get("blend_name", "Actions"))
        return {'FINISHED'}

    def _draw_menu(self, menu, context):
        layout = menu.layout
        uid = self.sync_uuid
        info = sync_manager.metadata.get("tracked_groups", {}).get(uid, {})
        status = sync_manager.check_status(uid)
        ignored = is_ignored(sync_manager.metadata, uid)
        saveable = status in (SyncStatus.BLEND_MODIFIED, SyncStatus.CONFLICT,
                              SyncStatus.JSON_MODIFIED, SyncStatus.JSON_MISSING)
        pullable = status in (SyncStatus.JSON_MODIFIED, SyncStatus.CONFLICT,
                              SyncStatus.ORPHAN)
        discardable = status in (SyncStatus.BLEND_MODIFIED,
                                 SyncStatus.CONFLICT)

        op = layout.operator("gn.sync_focus_group", text="Go to item",
                             icon='VIEWZOOM')
        op.sync_uuid = uid
        op = layout.operator("gn.sync_history_open", text="History",
                             icon='TIME')
        op.sync_uuid = uid
        layout.separator()

        if ignored:
            op = layout.operator("gn.sync_unignore", text="Un-ignore",
                                 icon='RESTRICT_VIEW_OFF')
            op.sync_uuid = uid
            return

        if saveable:
            op = layout.operator(
                "gn.sync_change_unmark" if sync_manager.is_staged(uid)
                else "gn.sync_change_mark",
                text="Unmark" if sync_manager.is_staged(uid) else "Mark",
                icon='REMOVE' if sync_manager.is_staged(uid) else 'CHECKMARK')
            op.sync_uuid = uid
        if saveable:
            op = layout.operator("gn.sync_export", text="Save to JSON",
                                 icon='EXPORT')
            op.sync_uuid = uid
        if pullable:
            op = layout.operator("gn.sync_import", text="Pull from JSON",
                                 icon='IMPORT')
            op.sync_uuid = uid
        if status == SyncStatus.CONFLICT:
            op = layout.operator("gn.sync_merge_conflict",
                                 text="Merge with JSON",
                                 icon='AUTOMERGE_ON')
            op.sync_uuid = uid
            op = layout.operator("gn.sync_resolve_json", text="Keep JSON",
                                 icon='FILE_REFRESH')
            op.sync_uuid = uid
            op = layout.operator("gn.sync_resolve_blend", text="Keep Blend",
                                 icon='LIGHT')
            op.sync_uuid = uid
        if discardable:
            layout.separator()
            op = layout.operator("gn.sync_discard_local",
                                 text="Discard local changes",
                                 icon='LOOP_BACK')
            op.sync_uuid = uid
        layout.separator()
        op = layout.operator("gn.sync_ignore", text="Ignore", icon='HIDE_ON')
        op.sync_uuid = uid
        if status in (SyncStatus.ORPHAN, SyncStatus.JSON_MISSING):
            op = layout.operator("gn.sync_unlink", text="Stop tracking",
                                 icon='X')
            op.sync_uuid = uid


class GN_MT_SaveMenu(bpy.types.Menu):
    """Secondary save actions (never the primary path)."""

    bl_idname = "GN_MT_SaveMenu"
    bl_label = "Save"

    def draw(self, context):
        layout = self.layout
        layout.operator("gn.sync_export_all",
                        text="Save all tracked groups...", icon='FILE_TICK')


classes = (
    GN_MT_SaveMenu,
    GN_OT_GitHistoryRefresh,
    GN_OT_SyncImportGroupDialog,
    GN_OT_SyncFocusGroup,
    GN_OT_FocusValidationIssue,
    GN_OT_CancelGeometryValidation,
    GN_OT_CancelProjectAudit,
    GN_OT_ChangeMenu,
    GN_PT_SyncPanel,
    GN_PT_TrackPanel,
    GN_PT_ExplorePanel,
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
