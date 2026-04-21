# -*- coding: utf-8 -*-
"""
GN Batch JSON Toolkit v0.1.4 — Blender Add-on Package

Flawless Post-Creation Sequential Mapping to prevent ID Collisions on
Volatile Nodes.
"""

bl_info = {
    "name": "GN Batch JSON Toolkit v0.1.4",
    "author": "oobma/ Asistente IA",
    "version": (0, 1, 4),
    "blender": (4, 0, 0),
    "location": "Node Editor > Sidebar > GN Tools",
    "description": "Flawless Post-Creation Sequential Mapping to prevent ID Collisions on Volatile Nodes.",
    "category": "Node",
}

import bpy

from .operators import classes


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
