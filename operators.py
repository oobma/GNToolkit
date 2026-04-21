# -*- coding: utf-8 -*-
"""
gn_toolkit.operators — Blender Operator and Panel classes.

Operators orchestrate the import/export workflow.  They no longer contain
data-processing logic; that lives in ``serializer`` and ``importer``.
"""

from __future__ import annotations

import bpy
import json
import os
import traceback
from bpy_extras.io_utils import ExportHelper, ImportHelper
from bpy.props import StringProperty, BoolProperty

from .codec import clean_value, unclean_value
from .constants import ADDON_VERSION
from .error_tracker import ImportErrorTracker
from .serializer import serialize_node_tree
from .socket_utils import get_tree_dependencies
from .importer import import_node_tree_recursive


class GN_OT_ExportBatchJSON(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.export_batch_json"
    bl_label = "Export Full Project"
    bl_description = "Export all Geometry Nodes groups and Modifier setups to JSON."
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    use_folder_structure: BoolProperty(name="Use Folder Structure", default=False)
    use_minify: BoolProperty(name="Minify JSON", default=False)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "use_folder_structure")
        layout.prop(self, "use_minify")

    def execute(self, context):
        base_dir = os.path.dirname(self.filepath)
        trees = [t for t in bpy.data.node_groups if t.type == 'GEOMETRY']
        context.window_manager.progress_begin(0, len(trees))

        dump_args = {'separators': (',', ':')} if self.use_minify else {'indent': 4}

        try:
            if self.use_folder_structure:
                if not os.path.exists(base_dir):
                    os.makedirs(base_dir)
                ng_dir = os.path.join(base_dir, "NodeGroups")
                if not os.path.exists(ng_dir):
                    os.makedirs(ng_dir)

                for i, tree in enumerate(trees):
                    context.window_manager.progress_update(i)
                    context.workspace.status_text_set(f"Exporting: {tree.name}")
                    data = serialize_node_tree(tree)
                    safe_name = "".join(c if c.isalnum() or c in (' ', '_') else '_' for c in tree.name)
                    with open(os.path.join(ng_dir, f"{safe_name}.json"), 'w', encoding='utf-8') as f:
                        json.dump(data, f, **dump_args)

                mod_dir = os.path.join(base_dir, "Modifiers")
                if not os.path.exists(mod_dir):
                    os.makedirs(mod_dir)
                count_mod = 0
                for obj in bpy.data.objects:
                    for mod in obj.modifiers:
                        if mod.type == 'NODES':
                            data = {
                                "object": obj.name,
                                "modifier_name": mod.name,
                                "node_group": mod.node_group.name if mod.node_group else None,
                                "inputs": {k: clean_value(v) for k, v in dict(mod).items()},
                            }
                            safe_name = "".join(c if c.isalnum() or c in (' ', '_') else '_' for c in f"{obj.name}_{mod.name}")
                            with open(os.path.join(mod_dir, f"{safe_name}.json"), 'w', encoding='utf-8') as f:
                                json.dump(data, f, **dump_args)
                            count_mod += 1
                self.report({'INFO'}, f"Exported {len(trees)} Groups and {count_mod} Modifiers.")
            else:
                master_data = {
                    "version": ADDON_VERSION,
                    "type": "GN_UNIFIED_PACKAGE",
                    "export_method": "BATCH",
                    "node_groups": {},
                    "modifiers": [],
                }
                for i, tree in enumerate(trees):
                    context.window_manager.progress_update(i)
                    master_data["node_groups"][tree.name] = serialize_node_tree(tree)

                for obj in bpy.data.objects:
                    for mod in obj.modifiers:
                        if mod.type == 'NODES':
                            master_data["modifiers"].append({
                                "object": obj.name,
                                "modifier_name": mod.name,
                                "node_group": mod.node_group.name if mod.node_group else None,
                                "inputs": {k: clean_value(v) for k, v in dict(mod).items()},
                            })
                with open(self.filepath, 'w', encoding='utf-8') as f:
                    json.dump(master_data, f, **dump_args)
                self.report({'INFO'}, "Batch Export Completed.")
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {e}")
            traceback.print_exc()
            return {'CANCELLED'}
        finally:
            context.window_manager.progress_end()
            context.workspace.status_text_set(None)
        return {'FINISHED'}


class GN_OT_ExportActiveJSON(bpy.types.Operator, ExportHelper):
    bl_idname = "gn.export_active_json"
    bl_label = "Export Active Group"
    bl_description = "Export the currently active Geometry Node group to a standalone JSON file"
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    use_minify: BoolProperty(name="Minify JSON", default=False)
    tree_name: StringProperty()

    def invoke(self, context, event):
        if not context.space_data or getattr(context.space_data, "type", "") != 'NODE_EDITOR':
            self.report({'ERROR'}, "Must be in Node Editor.")
            return {'CANCELLED'}

        tree = getattr(context.space_data, "node_tree", None)
        if not tree:
            self.report({'ERROR'}, "No active node tree.")
            return {'CANCELLED'}

        self.tree_name = tree.name
        self.filepath = tree.name + ".json"
        return super().invoke(context, event)

    def draw(self, context):
        self.layout.prop(self, "use_minify")

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.tree_name)
        if not tree:
            self.report({'ERROR'}, f"Tree '{self.tree_name}' not found.")
            return {'CANCELLED'}

        dump_args = {'separators': (',', ':')} if self.use_minify else {'indent': 4}
        deps = get_tree_dependencies(tree)

        master_data = {
            "version": ADDON_VERSION,
            "type": "GN_UNIFIED_PACKAGE",
            "export_method": "ACTIVE",
            "node_groups": {},
            "modifiers": [],
        }

        for d_name, d_tree in deps.items():
            master_data["node_groups"][d_name] = serialize_node_tree(d_tree)

        obj = context.active_object
        if obj:
            for mod in obj.modifiers:
                if mod.type == 'NODES' and mod.node_group and mod.node_group.name == tree.name:
                    master_data["modifiers"].append({
                        "object": obj.name,
                        "modifier_name": mod.name,
                        "node_group": mod.node_group.name,
                        "inputs": {k: clean_value(v) for k, v in dict(mod).items()},
                    })

        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(master_data, f, **dump_args)
            self.report({'INFO'}, f"Exported '{tree.name}' successfully.")
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {e}")
            traceback.print_exc()
            return {'CANCELLED'}

        return {'FINISHED'}


class GN_OT_ImportBatchJSON(bpy.types.Operator, ImportHelper):
    bl_idname = "gn.import_batch_json"
    bl_label = "Import Data Package"
    bl_description = "Import Geometry Nodes hierarchies from JSON."
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    _timer = None
    task_queue = []
    json_cache = {}
    group_interface_maps = {}
    _tracker: ImportErrorTracker = None  # type: ignore[assignment]

    def cancel_modal(self, context):
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
        context.window_manager.progress_end()
        context.workspace.status_text_set(None)
        context.window.cursor_modal_restore()

    def execute(self, context):
        self._tracker = ImportErrorTracker()

        filepath = self.filepath
        self.json_cache = {}
        self.group_interface_maps = {}
        mod_data_list = []

        try:
            if os.path.isdir(filepath):
                ng_dir = os.path.join(filepath, "NodeGroups")
                mod_dir = os.path.join(filepath, "Modifiers")
                if os.path.exists(ng_dir):
                    for f in os.listdir(ng_dir):
                        if f.endswith('.json'):
                            with open(os.path.join(ng_dir, f), 'r', encoding='utf-8') as file:
                                data = json.load(file)
                                if "name" in data:
                                    self.json_cache[data["name"]] = data
                if os.path.exists(mod_dir):
                    for f in os.listdir(mod_dir):
                        if f.endswith('.json'):
                            with open(os.path.join(mod_dir, f), 'r', encoding='utf-8') as file:
                                mod_data_list.append(json.load(file))
            else:
                with open(filepath, 'r', encoding='utf-8') as f:
                    master_data = json.load(f)
                if master_data.get("type") == "GN_UNIFIED_PACKAGE":
                    self.json_cache.update(master_data.get("node_groups", {}))
                    mod_data_list = master_data.get("modifiers", [])
                elif "name" in master_data and "nodes" in master_data:
                    self.json_cache[master_data["name"]] = master_data

        except Exception as e:
            self.report({'ERROR'}, f"Read error: {str(e)}")
            return {'CANCELLED'}

        self.task_queue = [("NODEGROUP", n) for n in self.json_cache] + [("MODIFIER", m) for m in mod_data_list]
        if not self.task_queue:
            self.report({'ERROR'}, "No data found.")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, len(self.task_queue))
        self._timer = context.window_manager.event_timer_add(0.05, window=context.window)
        context.window.cursor_modal_set('WAIT')

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            self.report({'WARNING'}, "Cancelled by user")
            self.cancel_modal(context)
            return {'CANCELLED'}

        if event.type == 'TIMER':
            if not self.task_queue:
                msg = "Import finished successfully."
                if self._tracker and self._tracker.has_errors:
                    msg = f"Import finished with {self._tracker.count} warnings (Check Console)."
                    self.report({'WARNING'}, msg)
                else:
                    self.report({'INFO'}, msg)
                self.cancel_modal(context)
                return {'FINISHED'}

            context.window.cursor_modal_set('WAIT')

            task_type, data = self.task_queue.pop(0)
            try:
                if task_type == "NODEGROUP":
                    name = data
                    if not bpy.data.node_groups.get(name):
                        import_node_tree_recursive(
                            self.json_cache[name],
                            self.json_cache,
                            self.group_interface_maps,
                            context,
                            self._tracker,
                        )

                elif task_type == "MODIFIER":
                    m = data
                    obj = bpy.data.objects.get(m.get("object", ""))
                    if obj:
                        mod = obj.modifiers.get(m["modifier_name"])
                        if not mod:
                            mod = obj.modifiers.new(m["modifier_name"], 'NODES')
                        if m.get("node_group"):
                            ng = bpy.data.node_groups.get(m["node_group"])
                            if ng:
                                mod.node_group = ng
                        for k, v in m.get("inputs", {}).items():
                            try:
                                mod[k] = unclean_value(v)
                            except (TypeError, AttributeError, ValueError, RuntimeError):
                                pass
            except Exception as e:
                print(f"[CRITICAL] Task failed: {e}")
                traceback.print_exc()

            progress = len(self.json_cache) - len([t for t in self.task_queue if t[0] == 'NODEGROUP'])
            context.window_manager.progress_update(progress)

        return {'PASS_THROUGH'}


class GN_PT_MainPanel(bpy.types.Panel):
    bl_label = "GN Batch Toolkit v0.1.4"
    bl_idname = "GN_PT_MainPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GN Tools'

    def draw(self, context):
        layout = self.layout
        layout.label(text="Pipeline Tools", icon='TOOL_SETTINGS')

        row = layout.row(align=True)
        row.operator("gn.export_batch_json", text="Batch Export All", icon='EXPORT')
        row.operator("gn.import_batch_json", text="Batch Import", icon='IMPORT')

        row2 = layout.row(align=True)
        row2.operator("gn.export_active_json", text="Export Active Group", icon='FILE_BACKUP')


classes = (GN_OT_ExportBatchJSON, GN_OT_ExportActiveJSON, GN_OT_ImportBatchJSON, GN_PT_MainPanel)
