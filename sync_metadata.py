# -*- coding: utf-8 -*-
"""
gn_toolkit.sync_metadata — Sidecar file and UUID tracking for DNA/RNA sync.

Manages the .gntsync sidecar file that stores synchronization metadata
between .blend (RNA) and JSON (DNA) sources of truth.
"""

from __future__ import annotations

import json
import os
import uuid as _uuid

import bpy

from .constants import SIDECAR_EXTENSION, SIDECAR_TEXT_BLOCK_NAME, ADDON_VERSION, HASH_VERSION


# ---------------------------------------------------------------------------
# UUID generation
# ---------------------------------------------------------------------------

def generate_uuid() -> str:
    """Generate a new UUID v4 string."""
    return str(_uuid.uuid4())


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_json_path(json_path: str, blend_dir: str) -> str:
    """Convert relative paths (starting with '//') to absolute paths.

    Parameters
    ----------
    json_path : str
        The stored JSON path, which may be relative (//prefix) or absolute.
    blend_dir : str
        The directory containing the .blend file, used to resolve
        relative paths.
    """
    if json_path.startswith("//"):
        relative = json_path[2:]
        return os.path.normpath(os.path.join(blend_dir, relative))
    return os.path.normpath(json_path)


def make_json_path_relative(json_path: str, blend_dir: str) -> str:
    """Convert an absolute path to a relative path with // prefix if possible.

    If the path is under blend_dir, returns a relative path prefixed with //.
    Otherwise returns the absolute path unchanged.
    """
    json_path = os.path.normpath(json_path)
    blend_dir = os.path.normpath(blend_dir)
    try:
        rel = os.path.relpath(json_path, blend_dir)
        if not rel.startswith(".."):
            return "//" + rel.replace(os.sep, "/")
    except ValueError:
        pass
    return json_path


# ---------------------------------------------------------------------------
# Sidecar file I/O
# ---------------------------------------------------------------------------

def _sidecar_path(blend_filepath: str) -> str:
    """Derive the sidecar file path from the .blend file path."""
    return blend_filepath + SIDECAR_EXTENSION


def load_sync_metadata_from_sidecar(blend_filepath: str | None = None) -> dict:
    """Load sync metadata from the sidecar file.

    Returns an empty dict with version info if the file does not exist.
    """
    if blend_filepath is None:
        blend_filepath = bpy.data.filepath
    if not blend_filepath:
        return _empty_metadata()

    sidecar = _sidecar_path(blend_filepath)
    if not os.path.isfile(sidecar):
        return _empty_metadata()

    try:
        with open(sidecar, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict) or "tracked_groups" not in data:
            return _empty_metadata()
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_metadata()


def save_sync_metadata_to_sidecar(metadata: dict, blend_filepath: str | None = None) -> bool:
    """Write sync metadata to the sidecar file.

    Returns True on success, False on failure.
    """
    if blend_filepath is None:
        blend_filepath = bpy.data.filepath
    if not blend_filepath:
        return False

    sidecar = _sidecar_path(blend_filepath)
    try:
        with open(sidecar, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Text block cache
# ---------------------------------------------------------------------------

def _sync_metadata_to_text_block(metadata: dict) -> None:
    """Write metadata to a Blender text block for in-session access."""
    text_name = SIDECAR_TEXT_BLOCK_NAME
    content = json.dumps(metadata, indent=2, ensure_ascii=False)

    if text_name in bpy.data.texts:
        bpy.data.texts[text_name].clear()
        bpy.data.texts[text_name].write(content)
    else:
        bpy.data.texts.new(text_name)
        bpy.data.texts[text_name].write(content)


def _load_metadata_from_text_block() -> dict | None:
    """Read metadata from the Blender text block cache.

    Returns None if the text block does not exist.
    """
    text_name = SIDECAR_TEXT_BLOCK_NAME
    if text_name not in bpy.data.texts:
        return None
    try:
        content = bpy.data.texts[text_name].as_string()
        data = json.loads(content)
        if isinstance(data, dict) and "tracked_groups" in data:
            return data
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


# ---------------------------------------------------------------------------
# Unified load / save
# ---------------------------------------------------------------------------

def load_sync_metadata(blend_filepath: str | None = None) -> dict:
    """Load sync metadata, trying sidecar file first, then text block cache.

    Returns a metadata dict (never None).
    """
    data = load_sync_metadata_from_sidecar(blend_filepath)
    if data.get("tracked_groups"):
        _sync_metadata_to_text_block(data)
        return data

    text_data = _load_metadata_from_text_block()
    if text_data is not None and text_data.get("tracked_groups"):
        return text_data

    return _empty_metadata()


def save_sync_metadata(metadata: dict, blend_filepath: str | None = None) -> bool:
    """Save metadata to both sidecar file and text block."""
    ok = save_sync_metadata_to_sidecar(metadata, blend_filepath)
    _sync_metadata_to_text_block(metadata)
    return ok


# ---------------------------------------------------------------------------
# UUID stored on the Blender node tree
# ---------------------------------------------------------------------------

SYNC_UUID_PROP = "gnt_sync_id"


def store_uuid_on_tree(tree, sync_uuid: str) -> None:
    """Store the sync UUID as a custom property on the node tree."""
    tree[SYNC_UUID_PROP] = sync_uuid


def get_uuid_from_tree(tree) -> str | None:
    """Read the sync UUID from a node tree's custom properties."""
    return tree.get(SYNC_UUID_PROP)


def find_tree_by_uuid(blend_name: str, sync_uuid: str):
    """Return the node tree by blend name, falling back to a scan by
    sync UUID custom property. Returns None if the tree is not found.

    The fallback scan is O(n) over all node groups and should be
    avoided on hot paths; prefer passing a resolvable blend name.
    """
    tree = bpy.data.node_groups.get(blend_name)
    if tree is not None:
        return tree
    for ng in bpy.data.node_groups:
        if ng.get(SYNC_UUID_PROP) == sync_uuid:
            return ng
    return None


def find_uuid_for_tree(tree, metadata: dict) -> str | None:
    """Find the UUID for a tree by checking its custom property first,
    then searching metadata by name.

    Returns None if the tree is not tracked.
    """
    direct = get_uuid_from_tree(tree)
    if direct:
        return direct

    tree_name = tree.name
    for uid, info in metadata.get("tracked_groups", {}).items():
        if info.get("blend_name") == tree_name:
            return uid
    return None


# ---------------------------------------------------------------------------
# CRUD operations on tracked groups
# ---------------------------------------------------------------------------

def get_tracked_group(metadata: dict, sync_uuid: str) -> dict | None:
    """Return the info dict for a tracked group, or None."""
    return metadata.get("tracked_groups", {}).get(sync_uuid)


def get_tracked_group_by_name(metadata: dict, blend_name: str) -> list[tuple[str, dict]]:
    """Return all (uuid, info) pairs whose blend_name matches."""
    results = []
    for uid, info in metadata.get("tracked_groups", {}).items():
        if info.get("blend_name") == blend_name:
            results.append((uid, info))
    return results


def add_tracked_group(
    metadata: dict,
    sync_uuid: str,
    blend_name: str,
    json_path: str,
    blend_hash: str,
    json_hash: str,
    json_mtime: float,
    depends_on: list[str] | None = None,
) -> None:
    """Add a new tracked group to the metadata dict (in-place)."""
    if "tracked_groups" not in metadata:
        metadata["tracked_groups"] = {}
    metadata["tracked_groups"][sync_uuid] = {
        "blend_name": blend_name,
        "json_path": json_path,
        "last_blend_hash": blend_hash,
        "last_json_hash": json_hash,
        "last_json_mtime": json_mtime,
        "last_sync_time": _now_timestamp(),
        "depends_on": depends_on or [],
    }


def update_tracked_group(metadata: dict, sync_uuid: str, **kwargs) -> None:
    """Update specific fields on a tracked group."""
    info = metadata.get("tracked_groups", {}).get(sync_uuid)
    if info is None:
        return
    for key, value in kwargs.items():
        if key in ("blend_name", "json_path", "last_blend_hash",
                    "last_json_hash", "last_json_mtime", "last_sync_time",
                    "depends_on", "ignored"):
            info[key] = value


def set_ignored(metadata: dict, sync_uuid: str, value: bool) -> None:
    """Set the ignored flag for a tracked group."""
    update_tracked_group(metadata, sync_uuid, ignored=value)


def is_ignored(metadata: dict, sync_uuid: str) -> bool:
    """Check if a tracked group is marked as ignored."""
    info = get_tracked_group(metadata, sync_uuid)
    if info is None:
        return False
    return bool(info.get("ignored", False))


def remove_tracked_group(metadata: dict, sync_uuid: str) -> None:
    """Remove a tracked group from the metadata dict (in-place)."""
    metadata.get("tracked_groups", {}).pop(sync_uuid, None)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_metadata(metadata: dict, blend_dir: str) -> list[str]:
    """Validate metadata integrity. Returns a list of warning strings."""
    warnings = []
    groups = metadata.get("tracked_groups", {})

    for uid, info in groups.items():
        json_path = resolve_json_path(info.get("json_path", ""), blend_dir)
        if not json_path or not os.path.isfile(json_path):
            warnings.append(f"JSON file not found for '{info.get('blend_name', '?')}': {json_path}")

        blend_name = info.get("blend_name", "")
        if blend_name not in bpy.data.node_groups:
            tree = None
        else:
            tree = bpy.data.node_groups[blend_name]
        if tree is None:
            warnings.append(f"Node tree '{blend_name}' not found in .blend (orphan)")

        stored_uuid = None
        if tree is not None:
            stored_uuid = get_uuid_from_tree(tree)
        if stored_uuid and stored_uuid != uid:
            warnings.append(
                f"UUID mismatch for '{blend_name}': "
                f"metadata has {uid}, tree has {stored_uuid}"
            )

    return warnings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _empty_metadata() -> dict:
    """Return a fresh metadata dict with version info."""
    return {
        "version": ADDON_VERSION,
        "hash_version": HASH_VERSION,
        "tracked_groups": {},
    }


def _now_timestamp() -> float:
    """Return current time as a Unix timestamp."""
    import time
    return time.time()