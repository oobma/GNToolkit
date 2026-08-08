# -*- coding: utf-8 -*-
"""
gn_toolkit.sync_manager — State detection and reconciliation for DNA/RNA sync.

Orchestrates the comparison of .blend and JSON states, provides actions
for import/export/ignore, and manages the SyncStatus lifecycle.

Supports per-group hashing within unified JSON packages (Solution 3),
cascade updates for dependencies, and auto-linking of imported groups.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from enum import Enum

import bpy

from .constants import ADDON_VERSION, LOCK_TIMEOUT_SECONDS, PACKAGE_EXPORT_METHOD
from .error_tracker import ImportErrorTracker
from .hash_utils import (
    canonical_hash_from_tree,
    canonical_hash_from_json_path,
    canonical_hash_from_json_data,
    canonical_hash_from_json_group,
    canonicalize_node_tree_data,
    get_json_mtime,
)
from .importer import import_node_tree_recursive
from .serializer import serialize_node_tree
from .socket_utils import get_tree_dependencies
from .sync_metadata import (
    generate_uuid,
    load_sync_metadata,
    save_sync_metadata,
    resolve_json_path,
    make_json_path_relative,
    store_uuid_on_tree,
    get_uuid_from_tree,
    find_uuid_for_tree,
    find_tree_by_uuid,
    get_tracked_group,
    get_tracked_group_by_name,
    add_tracked_group,
    update_tracked_group,
    set_ignored,
    is_ignored,
    remove_tracked_group,
    validate_metadata,
    _empty_metadata,
)
from .geometry_validator import (
    validate_node_tree,
    validate_all_tracked_trees,
    get_mesh_from_node_tree,
    format_issue_summary,
    ValidationIssue,
)


class SyncStatus(Enum):
    SYNCED = "synced"
    BLEND_MODIFIED = "blend_modified"
    JSON_MODIFIED = "json_modified"
    CONFLICT = "conflict"
    ORPHAN = "orphan"
    UNTRACKED = "untracked"
    JSON_MISSING = "json_missing"


class JsonLock:
    """Simple lock file to prevent concurrent JSON writes.

    Uses PID-based staleness detection to avoid blocking on orphaned
    locks from crashed Blender sessions.
    """

    def __init__(self, json_path: str):
        self.lock_path = json_path + ".lock"

    def _is_pid_alive(self, pid: int) -> bool:
        """Check if a process with the given PID is still running."""
        try:
            if os.name == "nt":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                PROCESS_QUERY_INFORMATION = 0x0400
                handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, 0, pid)
                if handle == 0:
                    return False
                kernel32.CloseHandle(handle)
                return True
            else:
                os.kill(pid, 0)
                return True
        except (OSError, AttributeError, Exception):
            return False

    def acquire(self, timeout: float = LOCK_TIMEOUT_SECONDS) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if not os.path.exists(self.lock_path):
                try:
                    with open(self.lock_path, 'w', encoding='utf-8') as f:
                        f.write(f"{os.getpid()}\n{time.time()}")
                    return True
                except OSError:
                    pass

            # Lock exists — check if it's stale
            try:
                with open(self.lock_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip().split('\n')
                if len(content) >= 2:
                    lock_pid = int(content[0])
                    lock_time = float(content[1])
                    age = time.time() - lock_time

                    # Stale if: PID dead OR age > 15s
                    if not self._is_pid_alive(lock_pid) or age > LOCK_TIMEOUT_SECONDS * 3:
                        try:
                            os.remove(self.lock_path)
                            continue
                        except OSError:
                            pass
                else:
                    # Malformed lock file — remove it
                    try:
                        os.remove(self.lock_path)
                        continue
                    except OSError:
                        pass
            except (OSError, ValueError):
                # Corrupt lock file — remove it
                try:
                    os.remove(self.lock_path)
                    continue
                except OSError:
                    pass

            time.sleep(0.05)
        return False

    def release(self) -> None:
        try:
            if os.path.exists(self.lock_path):
                os.remove(self.lock_path)
        except OSError:
            pass

    def is_locked(self) -> bool:
        if not os.path.exists(self.lock_path):
            return False
        try:
            with open(self.lock_path, 'r', encoding='utf-8') as f:
                content = f.read().strip().split('\n')
            if len(content) >= 2:
                lock_pid = int(content[0])
                lock_time = float(content[1])
                age = time.time() - lock_time
                if not self._is_pid_alive(lock_pid) or age > LOCK_TIMEOUT_SECONDS * 3:
                    return False
                return True
            # Malformed lock file — treat as not locked
            return False
        except (OSError, ValueError):
            return False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def read_json_tolerant(json_path: str, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Read a JSON file, tolerating concurrent writes from other sessions.

    Waits while the lock file is present (another session is mid-write)
    and retries briefly when the file fails to parse (caught halfway
    through a write). Returns the parsed data, or None after giving up.
    """
    lock = JsonLock(json_path)
    start = time.time()
    while time.time() - start < timeout:
        if lock.is_locked():
            time.sleep(0.05)
            continue
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            time.sleep(0.05)
        except OSError:
            return None
    return None


class SyncManager:
    """Orchestrates DNA/RNA synchronization state and actions."""

    def __init__(self):
        self.metadata: dict = _empty_metadata()
        self._status_cache: dict[str, SyncStatus] = {}
        self._dirty: bool = False
        self._geometry_issues_cache: dict[str, list] = {}

    # --- Load / save -------------------------------------------------------

    def load(self) -> None:
        self.metadata = load_sync_metadata()
        self._status_cache.clear()
        self._geometry_issues_cache.clear()
        self._dirty = False

    def save(self) -> bool:
        if not self._dirty:
            return True
        ok = save_sync_metadata(self.metadata)
        if ok:
            self._dirty = False
        return ok

    # --- Status detection --------------------------------------------------

    def check_status(self, sync_uuid: str) -> SyncStatus:
        """Determine the sync status of a tracked group.

        Uses per-group hashing within unified JSON packages so that
        only groups that actually changed show as modified.

        Checks the per-UUID cache first to avoid recomputation on
        every UI redraw.
        """
        cached = self._status_cache.get(sync_uuid)
        if cached is not None:
            return cached
        info = get_tracked_group(self.metadata, sync_uuid)
        if info is None:
            return SyncStatus.UNTRACKED

        blend_name = info.get("blend_name", "")
        tree = find_tree_by_uuid(blend_name, sync_uuid)
        if tree is None:
            return SyncStatus.ORPHAN

        json_path = resolve_json_path(info.get("json_path", ""), self._blend_dir())

        if not json_path or not os.path.isfile(json_path):
            return SyncStatus.JSON_MISSING

        # Layer 1: mtime fast check — if mtime hasn't changed, 
        # only .blend could have changed
        current_mtime = get_json_mtime(json_path)
        last_mtime = info.get("last_json_mtime", 0.0)

        if current_mtime is not None and current_mtime <= last_mtime:
            blend_hash_current = canonical_hash_from_tree(tree)
            last_blend_hash = info.get("last_blend_hash", "")
            if blend_hash_current == last_blend_hash:
                return SyncStatus.SYNCED
            else:
                return SyncStatus.BLEND_MODIFIED

        # Layer 2: compute per-group JSON hash
        json_hash_current = canonical_hash_from_json_group(json_path, blend_name)
        # Fallback: if per-group hash fails (e.g. standalone file), try whole-file
        if json_hash_current is None:
            json_hash_current = canonical_hash_from_json_path(json_path)

        # Layer 3: compute .blend hash
        blend_hash_current = canonical_hash_from_tree(tree)
        last_blend_hash = info.get("last_blend_hash", "")
        last_json_hash = info.get("last_json_hash", "")

        if json_hash_current is None:
            return SyncStatus.JSON_MISSING

        json_changed = (json_hash_current != last_json_hash) if last_json_hash else True
        blend_changed = (blend_hash_current != last_blend_hash) if last_blend_hash else True

        if not json_changed and not blend_changed:
            return SyncStatus.SYNCED
        elif json_changed and not blend_changed:
            return SyncStatus.JSON_MODIFIED
        elif not json_changed and blend_changed:
            return SyncStatus.BLEND_MODIFIED
        else:
            return SyncStatus.CONFLICT

    def check_all_statuses(self) -> dict[str, SyncStatus]:
        """Check all tracked groups, optimised for batch operation.

        Reads each JSON file only once and caches per-group hashes
        from the in-memory data, avoiding 439 separate disk reads.
        """
        tracked = self.metadata.get("tracked_groups", {})
        if not tracked:
            self._status_cache = {}
            return self._status_cache

        # Group by JSON path to batch-read files
        json_data_cache: dict[str, tuple] = {}  # path -> (data_dict, mtime)
        json_hash_cache: dict[str, dict[str, str]] = {}  # path -> {group_name: hash}

        # First pass: read each unique JSON file once
        unique_paths = set()
        for uid, info in tracked.items():
            jp = info.get("json_path", "")
            if jp:
                unique_paths.add(jp)

        for jp in unique_paths:
            json_path = resolve_json_path(jp, self._blend_dir())
            if json_path and os.path.isfile(json_path):
                data = read_json_tolerant(json_path)
                if data is not None:
                    mtime = os.path.getmtime(json_path)
                    json_data_cache[jp] = (data, mtime)
                    # Pre-compute per-group hashes from in-memory data
                    if isinstance(data, dict) and data.get("type") == "GN_UNIFIED_PACKAGE":
                        groups = data.get("node_groups", {})
                        group_hashes = {}
                        for gname, gdata in groups.items():
                            group_hashes[gname] = canonical_hash_from_json_data(gdata)
                        json_hash_cache[jp] = group_hashes

        # Second pass: compute status for each tracked group
        result = {}
        for uid, info in tracked.items():
            cached_status = self._status_cache.get(uid)
            if cached_status is not None:
                result[uid] = cached_status
                continue

            blend_name = info.get("blend_name", "")
            tree = find_tree_by_uuid(blend_name, uid)
            if tree is None:
                result[uid] = SyncStatus.ORPHAN
                continue

            jp = info.get("json_path", "")
            cached = json_data_cache.get(jp)
            if cached is None:
                result[uid] = SyncStatus.JSON_MISSING
                continue

            data, current_mtime = cached
            last_mtime = info.get("last_json_mtime", 0.0)

            # Fast path: mtime unchanged
            if current_mtime <= last_mtime:
                blend_hash_current = canonical_hash_from_tree(tree)
                last_blend_hash = info.get("last_blend_hash", "")
                if blend_hash_current == last_blend_hash:
                    result[uid] = SyncStatus.SYNCED
                else:
                    result[uid] = SyncStatus.BLEND_MODIFIED
                continue

            # Full check: JSON changed, use cached per-group hashes
            group_hashes = json_hash_cache.get(jp, {})
            json_hash_current = group_hashes.get(blend_name)
            if json_hash_current is None:
                # Fallback for standalone files
                if isinstance(data, dict) and "nodes" in data:
                    json_hash_current = canonical_hash_from_json_data(data)
                else:
                    result[uid] = SyncStatus.JSON_MISSING
                    continue

            blend_hash_current = canonical_hash_from_tree(tree)
            last_blend_hash = info.get("last_blend_hash", "")
            last_json_hash = info.get("last_json_hash", "")

            json_changed = (json_hash_current != last_json_hash) if last_json_hash else True
            blend_changed = (blend_hash_current != last_blend_hash) if last_blend_hash else True

            if not json_changed and not blend_changed:
                result[uid] = SyncStatus.SYNCED
            elif json_changed and not blend_changed:
                result[uid] = SyncStatus.JSON_MODIFIED
            elif not json_changed and blend_changed:
                result[uid] = SyncStatus.BLEND_MODIFIED
            else:
                result[uid] = SyncStatus.CONFLICT

        self._status_cache = result
        return result

    # --- Linking / unlinking -----------------------------------------------

    def link_group(self, tree, json_path: str) -> str:
        """Link a node tree to a JSON file for DNA/RNA tracking.

        Serializes the tree (without full dependency chain), computes
        per-group hashes, and registers the main group plus any untracked
        dependencies in metadata.

        If the JSON file already exists, it reads it and updates the
        entry in-place to preserve other groups in the unified package.
        """
        sync_uuid = generate_uuid()
        store_uuid_on_tree(tree, sync_uuid)

        abs_path = json_path
        if not os.path.isabs(abs_path):
            abs_path = os.path.join(self._blend_dir(), abs_path)

        # Read existing JSON or create new
        if os.path.isfile(abs_path):
            master_data = read_json_tolerant(abs_path)
            if master_data is None:
                master_data = {
                    "version": ADDON_VERSION,
                    "type": "GN_UNIFIED_PACKAGE",
                    "export_method": PACKAGE_EXPORT_METHOD,
                    "node_groups": {},
                    "modifiers": [],
                }
        else:
            master_data = {
                "version": ADDON_VERSION,
                "type": "GN_UNIFIED_PACKAGE",
                "export_method": PACKAGE_EXPORT_METHOD,
                "node_groups": {},
                "modifiers": [],
            }

        # Serialize ONLY the primary tree (no full dependency chain)
        master_data["node_groups"][tree.name] = serialize_node_tree(tree)

        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, 'w', encoding='utf-8') as f:
            json.dump(master_data, f, indent=4, ensure_ascii=False)

        # Per-group hash for the primary group
        blend_hash = canonical_hash_from_tree(tree)
        json_hash = canonical_hash_from_json_group(abs_path, tree.name)
        if json_hash is None:
            json_hash = canonical_hash_from_json_path(abs_path)
        json_mtime = os.path.getmtime(abs_path)

        # Build dependency UUID list (scan without serializing)
        dep_uuids = []
        for node in tree.nodes:
            if node.bl_idname == "GeometryNodeGroup" and getattr(node, "node_tree", None):
                dep_name = node.node_tree.name
                if dep_name == tree.name:
                    continue
                dep_tree = bpy.data.node_groups.get(dep_name)
                if dep_tree is not None:
                    dep_uid = get_uuid_from_tree(dep_tree)
                    if dep_uid and dep_uid != sync_uuid:
                        dep_uuids.append(dep_uid)

        stored_path = make_json_path_relative(abs_path, self._blend_dir())

        add_tracked_group(
            self.metadata, sync_uuid, tree.name, stored_path,
            blend_hash, json_hash, json_mtime,
            depends_on=dep_uuids,
        )

        # Auto-link dependency groups that share the same JSON file
        self._auto_link_dependencies(tree, abs_path, stored_path, sync_uuid)

        self._dirty = True
        return sync_uuid

    def _auto_link_dependencies(self, tree, abs_path: str,
                                  stored_path: str, parent_uuid: str) -> list[str]:
        """Auto-link dependency groups that don't already have tracking.

        Scans the tree's nodes for Group node references and creates
        tracking entries for each untracked dependency. All dependencies
        share the same JSON file as the parent.
        """
        new_uuids = []
        visited = set()
        
        def scan_deps(t):
            if t.name in visited:
                return
            visited.add(t.name)
            for node in t.nodes:
                if node.bl_idname == "GeometryNodeGroup" and getattr(node, "node_tree", None):
                    dep_tree = node.node_tree
                    if dep_tree.name == t.name:
                        continue
                    
                    # Check if already tracked
                    existing_uid = get_uuid_from_tree(dep_tree)
                    if existing_uid:
                        continue
                    uid = find_uuid_for_tree(dep_tree, self.metadata)
                    if uid:
                        continue
                    
                    dep_uuid = generate_uuid()
                    store_uuid_on_tree(dep_tree, dep_uuid)
                    
                    dep_blend_hash = canonical_hash_from_tree(dep_tree)
                    dep_json_hash = canonical_hash_from_json_group(abs_path, dep_tree.name)
                    if dep_json_hash is None:
                        dep_json_hash = canonical_hash_from_json_path(abs_path)
                    dep_mtime = os.path.getmtime(abs_path)
                    
                    add_tracked_group(
                        self.metadata, dep_uuid, dep_tree.name, stored_path,
                        dep_blend_hash, dep_json_hash, dep_mtime,
                        depends_on=[],
                    )
                    new_uuids.append(dep_uuid)
                    
                    # Recursively scan this dependency's deps
                    scan_deps(dep_tree)
        
        scan_deps(tree)
        return new_uuids

    def unlink_group(self, sync_uuid: str) -> None:
        info = get_tracked_group(self.metadata, sync_uuid)
        if info is None:
            return
        remove_tracked_group(self.metadata, sync_uuid)
        blend_name = info.get("blend_name", "")
        tree = bpy.data.node_groups.get(blend_name)
        if tree is not None and "gnt_sync_id" in tree:
            del tree["gnt_sync_id"]
        self._dirty = True

    # --- External connection preservation -----------------------------------

    @staticmethod
    def _save_external_connections(group_name: str) -> list[dict]:
        """Save all links in OTHER node groups that connect to the given group.

        When a group is reimported, its interface sockets change identifiers,
        which breaks any links from parent groups.  This function snapshots
        those links so they can be restored by name after the import.

        Returns a list of dicts, each with:
            parent_name, node_name, socket_name, socket_direction,
            from_socket_name, from_node_name, from_direction,
            to_socket_name, to_node_name, to_direction
        """
        target_tree = bpy.data.node_groups.get(group_name)
        saved = []
        for ng in bpy.data.node_groups:
            if ng is target_tree or ng.type != 'GEOMETRY':
                continue
            for node in ng.nodes:
                if getattr(node, 'node_tree', None) is not target_tree:
                    continue
                for link in ng.links:
                    from_node = link.from_node
                    to_node = link.to_node
                    from_sock = link.from_socket
                    to_sock = link.to_socket
                    if from_node == node or to_node == node:
                        direction = "output" if from_node == node else "input"
                        group_sock_name = from_sock.name if from_node == node else to_sock.name
                        entry = {
                            "parent_name": ng.name,
                            "group_node_name": node.name,
                            "group_socket_name": group_sock_name,
                            "group_socket_direction": direction,
                            "other_node_name": to_node.name if from_node == node else from_node.name,
                            "other_socket_name": to_sock.name if from_node == node else from_sock.name,
                            "other_socket_direction": "input" if from_node == node else "output",
                            "link_direction": "from_group" if from_node == node else "to_group",
                        }
                        saved.append(entry)
        return saved

    @staticmethod
    def _restore_external_connections(group_name: str, saved: list[dict]) -> None:
        """Restore external connections saved by _save_external_connections.

        After the group's interface has been rebuilt with potentially new
        socket identifiers, this function reconnects the parent groups by
        matching socket *names* (which are stable) rather than identifiers.
        """
        target_tree = bpy.data.node_groups.get(group_name)
        if target_tree is None:
            return
        for entry in saved:
            ng = bpy.data.node_groups.get(entry["parent_name"])
            if ng is None:
                continue
            group_node = ng.nodes.get(entry["group_node_name"])
            if group_node is None:
                continue
            other_node = ng.nodes.get(entry["other_node_name"])
            if other_node is None:
                continue

            group_sock_name = entry["group_socket_name"]
            other_sock_name = entry["other_socket_name"]

            group_sock = None
            if entry["group_socket_direction"] == "output":
                for s in group_node.outputs:
                    if s.name == group_sock_name:
                        group_sock = s
                        break
            else:
                for s in group_node.inputs:
                    if s.name == group_sock_name:
                        group_sock = s
                        break

            other_sock = None
            if entry["other_socket_direction"] == "output":
                for s in other_node.outputs:
                    if s.name == other_sock_name:
                        other_sock = s
                        break
            else:
                for s in other_node.inputs:
                    if s.name == other_sock_name:
                        other_sock = s
                        break

            if group_sock is None or other_sock is None:
                continue

            if entry["link_direction"] == "from_group":
                ng.links.new(group_sock, other_sock)
            else:
                ng.links.new(other_sock, group_sock)

    # --- Synchronization actions -------------------------------------------

    def _import_from_json_data(self, sync_uuid: str, tree_data: dict,
                                json_cache: dict, blend_name: str,
                                context=None) -> ImportErrorTracker:
        """Internal: import a node group from pre-loaded JSON data.

        Used by import_all_modified to avoid re-reading the JSON file
        from disk for each group.
        """
        tracker = ImportErrorTracker()
        group_interface_maps = {}

        # --- Save external connections for the target group only ---
        saved_connections = {}
        conns = self._save_external_connections(blend_name)
        if conns:
            saved_connections[blend_name] = conns

        ng = import_node_tree_recursive(
            tree_data, json_cache, group_interface_maps, context, tracker
        )

        if ng is not None:
            # Restore UUID
            store_uuid_on_tree(ng, sync_uuid)
            update_tracked_group(self.metadata, sync_uuid, blend_name=ng.name)

            # --- Restore external connections after importing ---
            for gname, conns in saved_connections.items():
                if conns:
                    self._restore_external_connections(gname, conns)

        return tracker

    def import_from_json(self, sync_uuid: str, context=None) -> ImportErrorTracker:
        """Import a node group from JSON, overwriting the .blend version.

        Also updates the hashes of all other tracked groups that share
        the same JSON file (cascade update).
        """
        info = get_tracked_group(self.metadata, sync_uuid)
        if info is None:
            tracker = ImportErrorTracker()
            tracker.record(f"UUID {sync_uuid} not found in metadata")
            return tracker

        json_path = resolve_json_path(info.get("json_path", ""), self._blend_dir())
        if not os.path.isfile(json_path):
            tracker = ImportErrorTracker()
            tracker.record(f"JSON file not found: {json_path}")
            return tracker

        json_data = read_json_tolerant(json_path)
        if json_data is None:
            tracker = ImportErrorTracker()
            tracker.record(f"Could not read JSON file (unreadable or concurrent write): {json_path}")
            return tracker

        blend_name = info.get("blend_name", "")

        if isinstance(json_data, dict) and json_data.get("type") == "GN_UNIFIED_PACKAGE":
            groups = json_data.get("node_groups", {})
            if not groups:
                tracker = ImportErrorTracker()
                tracker.record("No node groups found in JSON package")
                return tracker
            tree_data = groups.get(blend_name)
            if tree_data is None:
                tree_data = next(iter(groups.values()))
            json_cache = groups
        elif isinstance(json_data, dict) and "nodes" in json_data:
            tree_data = json_data
            json_cache = {blend_name: json_data}
        else:
            tracker = ImportErrorTracker()
            tracker.record(f"Unrecognized JSON format in {json_path}")
            return tracker

        tracker = self._import_from_json_data(sync_uuid, tree_data, json_cache, blend_name, context)

        ng = bpy.data.node_groups.get(blend_name)
        if ng is not None:
            # Update primary group hashes (per-group)
            new_blend_hash = canonical_hash_from_tree(ng)
            new_json_hash = canonical_hash_from_json_group(json_path, ng.name)
            if new_json_hash is None:
                new_json_hash = canonical_hash_from_json_path(json_path)
            new_mtime = os.path.getmtime(json_path)
            update_tracked_group(
                self.metadata, sync_uuid,
                last_blend_hash=new_blend_hash,
                last_json_hash=new_json_hash,
                last_json_mtime=new_mtime,
            )

            # Cascade: update hashes for all other tracked groups
            # that share the same JSON file and were also imported
            self._cascade_update_hashes(json_path, new_mtime, exclude_uuid=sync_uuid)

            # Auto-link any newly imported groups that aren't tracked
            self._auto_link_imported_groups(json_path, json_cache, json_path_stored=info.get("json_path", ""))

            # Invalidate status cache for all groups sharing this JSON file
            json_path_stored = info.get("json_path", "")
            for uid, ig in self.metadata.get("tracked_groups", {}).items():
                if ig.get("json_path", "") == json_path_stored:
                    self._status_cache.pop(uid, None)

            self._dirty = True

        return tracker

    def _cascade_update_hashes(self, json_path: str, new_mtime: float,
                                exclude_uuid: str | None = None,
                                serialized_data: dict | None = None) -> None:
        """After an export, update hashes for all groups sharing the same JSON file.

        Reads the JSON file ONCE and computes all hashes from in-memory data,
        avoiding repeated disk reads. Blend hashes are computed from .blend
        trees only for groups that were actually serialized.

        For groups that share the JSON but were NOT serialized, only the
        JSON hash and mtime are updated (blend hash stays as-is).
        """
        serialized_names = set(serialized_data.keys()) if serialized_data else set()

        # Read JSON file ONCE
        json_data = read_json_tolerant(json_path)
        if json_data is None:
            return

        # Extract group data from JSON
        if isinstance(json_data, dict) and json_data.get("type") == "GN_UNIFIED_PACKAGE":
            json_groups = json_data.get("node_groups", {})
        elif isinstance(json_data, dict) and "nodes" in json_data:
            json_groups = {json_data.get("name", ""): json_data}
        else:
            json_groups = {}

        # Pre-compute JSON hashes from in-memory data
        json_hashes = {}
        for gname, gdata in json_groups.items():
            json_hashes[gname] = canonical_hash_from_json_data(gdata)

        for uid, info in self.metadata.get("tracked_groups", {}).items():
            if uid == exclude_uuid:
                continue
            stored_path = info.get("json_path", "")
            abs_stored = resolve_json_path(stored_path, self._blend_dir())
            if abs_stored == json_path:
                blend_name = info.get("blend_name", "")
                tree = find_tree_by_uuid(blend_name, uid)
                if tree is not None:
                    # JSON hash from pre-computed cache
                    dep_json_hash = json_hashes.get(blend_name, "")

                    # Blend hash: only re-compute if this tree was serialized
                    if blend_name in serialized_names:
                        dep_blend_hash = canonical_hash_from_tree(tree)
                    else:
                        dep_blend_hash = info.get("last_blend_hash", "")

                    update_tracked_group(
                        self.metadata, uid,
                        last_blend_hash=dep_blend_hash,
                        last_json_hash=dep_json_hash,
                        last_json_mtime=new_mtime,
                    )

    def _auto_link_imported_groups(self, json_path_abs: str, json_cache: dict,
                                    json_path_stored: str) -> None:
        """Auto-link groups that were imported as dependencies but aren't tracked."""
        for group_name in json_cache:
            tree = bpy.data.node_groups.get(group_name)
            if tree is None:
                continue
            # Already tracked?
            existing_uid = get_uuid_from_tree(tree)
            if existing_uid:
                continue
            uid = find_uuid_for_tree(tree, self.metadata)
            if uid:
                continue

            # Create tracking entry
            dep_uuid = generate_uuid()
            store_uuid_on_tree(tree, dep_uuid)

            dep_blend_hash = canonical_hash_from_tree(tree)
            dep_json_hash = canonical_hash_from_json_group(json_path_abs, group_name)
            if dep_json_hash is None:
                dep_json_hash = canonical_hash_from_json_path(json_path_abs)
            dep_mtime = os.path.getmtime(json_path_abs)

            # Detect dependencies of this group
            dep_deps = get_tree_dependencies(tree)
            dep_dep_uuids = []
            for dd_name in dep_deps:
                if dd_name == group_name:
                    continue
                dd_tree = bpy.data.node_groups.get(dd_name)
                if dd_tree is not None:
                    dd_uid = get_uuid_from_tree(dd_tree)
                    if dd_uid:
                        dep_dep_uuids.append(dd_uid)

            add_tracked_group(
                self.metadata, dep_uuid, group_name, json_path_stored,
                dep_blend_hash, dep_json_hash, dep_mtime,
                depends_on=dep_dep_uuids,
            )
            self._dirty = True

    def link_dependencies(self, sync_uuid: str) -> list[str]:
        """Manually link all untracked groups that share the same JSON file.

        Scans the JSON file for groups that exist in .blend but aren't
        tracked, and creates tracking entries for them.

        Returns a list of newly created UUIDs.
        """
        info = get_tracked_group(self.metadata, sync_uuid)
        if info is None:
            return []

        json_path = resolve_json_path(info.get("json_path", ""), self._blend_dir())
        if not os.path.isfile(json_path):
            return []

        json_data = read_json_tolerant(json_path)
        if json_data is None:
            return []

        if isinstance(json_data, dict) and json_data.get("type") == "GN_UNIFIED_PACKAGE":
            group_names = list(json_data.get("node_groups", {}).keys())
        elif isinstance(json_data, dict) and "name" in json_data:
            group_names = [json_data["name"]]
        else:
            return []

        new_uuids = []
        for group_name in group_names:
            tree = bpy.data.node_groups.get(group_name)
            if tree is None:
                continue
            existing_uid = get_uuid_from_tree(tree)
            if existing_uid:
                continue
            uid = find_uuid_for_tree(tree, self.metadata)
            if uid:
                continue

            dep_uuid = generate_uuid()
            store_uuid_on_tree(tree, dep_uuid)

            dep_blend_hash = canonical_hash_from_tree(tree)
            dep_json_hash = canonical_hash_from_json_group(json_path, group_name)
            if dep_json_hash is None:
                dep_json_hash = canonical_hash_from_json_path(json_path)
            dep_mtime = os.path.getmtime(json_path)

            # Detect dependencies
            dep_deps = get_tree_dependencies(tree)
            dep_dep_uuids = []
            for dd_name in dep_deps:
                if dd_name == group_name:
                    continue
                dd_tree = bpy.data.node_groups.get(dd_name)
                if dd_tree is not None:
                    dd_uid = get_uuid_from_tree(dd_tree)
                    if dd_uid:
                        dep_dep_uuids.append(dd_uid)

            stored_path = info.get("json_path", "")
            add_tracked_group(
                self.metadata, dep_uuid, group_name, stored_path,
                dep_blend_hash, dep_json_hash, dep_mtime,
                depends_on=dep_dep_uuids,
            )
            new_uuids.append(dep_uuid)
            self._dirty = True

        return new_uuids

    def export_to_json(self, sync_uuid: str, force: bool = False) -> bool:
        """Export .blend group to JSON.

        Safety check prevents overwrite if JSON was externally modified,
        unless *force* is True.

        Uses surgical update: reads existing JSON, serializes ONLY the
        modified group, updates its entry in-place, and writes back.
        This preserves all other groups in the unified package and
        avoids expensive dependency-chain serialization.

        After export, updates hashes for ALL tracked groups sharing
        the same JSON file (cascade update).
        """
        info = get_tracked_group(self.metadata, sync_uuid)
        if info is None:
            return False

        json_path = resolve_json_path(info.get("json_path", ""), self._blend_dir())
        if not json_path:
            return False

        # Per-group safety check: only block if THIS group's hash changed
        if not force and os.path.isfile(json_path):
            blend_name = info.get("blend_name", "")
            current_json_hash = canonical_hash_from_json_group(json_path, blend_name)
            if current_json_hash is None:
                current_json_hash = canonical_hash_from_json_path(json_path)
            last_known_hash = info.get("last_json_hash", "")
            if current_json_hash != last_known_hash and last_known_hash:
                return False

        blend_name = info.get("blend_name", "")
        tree = find_tree_by_uuid(blend_name, sync_uuid)
        if tree is None:
            return False

        lock = JsonLock(json_path)
        if not lock.acquire():
            return False

        try:
            # Surgical update: read existing JSON, update only this group
            if os.path.isfile(json_path):
                master_data = read_json_tolerant(json_path)
                if master_data is None:
                    master_data = {
                        "version": ADDON_VERSION,
                        "type": "GN_UNIFIED_PACKAGE",
                        "export_method": PACKAGE_EXPORT_METHOD,
                        "node_groups": {},
                        "modifiers": [],
                    }
            else:
                master_data = {
                    "version": ADDON_VERSION,
                    "type": "GN_UNIFIED_PACKAGE",
                    "export_method": PACKAGE_EXPORT_METHOD,
                    "node_groups": {},
                    "modifiers": [],
                }

            # Serialize ONLY the modified group (no dependency chain)
            master_data["node_groups"][blend_name] = serialize_node_tree(tree)
            serialized_dict = {blend_name: master_data["node_groups"][blend_name]}

            os.makedirs(os.path.dirname(json_path), exist_ok=True)
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(master_data, f, indent=4, ensure_ascii=False)

            # Update primary group hashes
            new_blend_hash = canonical_hash_from_tree(tree)
            new_json_hash = canonical_hash_from_json_group(json_path, tree.name)
            if new_json_hash is None:
                new_json_hash = canonical_hash_from_json_path(json_path)
            new_mtime = os.path.getmtime(json_path)
            update_tracked_group(
                self.metadata, sync_uuid,
                last_blend_hash=new_blend_hash,
                last_json_hash=new_json_hash,
                last_json_mtime=new_mtime,
                blend_name=tree.name,
            )

            # Cascade: update per-group hashes for all groups in this JSON
            self._cascade_update_hashes(json_path, new_mtime, exclude_uuid=sync_uuid,
                                        serialized_data=serialized_dict)
            self._dirty = True

            # Invalidate status cache for all groups sharing this JSON file
            # so the UI immediately reflects the resolved state
            for uid, ig in self.metadata.get("tracked_groups", {}).items():
                if ig.get("json_path", "") == info.get("json_path", ""):
                    self._status_cache.pop(uid, None)

            return True
        finally:
            lock.release()

    def ignore_changes(self, sync_uuid: str) -> None:
        """Mark a tracked group as ignored.

        The real sync status is always computed — the ignored flag only
        affects UI visibility.  Does NOT modify any hashes so the actual
        state can be recovered at any time.
        """
        info = get_tracked_group(self.metadata, sync_uuid)
        if info is None:
            return

        set_ignored(self.metadata, sync_uuid, True)
        self._status_cache.pop(sync_uuid, None)
        self._dirty = True

    def unignore_changes(self, sync_uuid: str) -> None:
        """Remove the ignored flag from a tracked group."""
        info = get_tracked_group(self.metadata, sync_uuid)
        if info is None:
            return

        set_ignored(self.metadata, sync_uuid, False)
        self._status_cache.pop(sync_uuid, None)
        self._dirty = True

    def resolve_conflict(self, sync_uuid: str, keep: str) -> None:
        if keep == "blend":
            self.export_to_json(sync_uuid, force=True)
        elif keep == "json":
            self.import_from_json(sync_uuid)

    # --- Cache invalidation ------------------------------------------------

    def invalidate_cache(self, sync_uuid: str | None = None) -> None:
        if sync_uuid:
            self._status_cache.pop(sync_uuid, None)
            self._geometry_issues_cache.pop(sync_uuid, None)
        else:
            self._status_cache.clear()
            self._geometry_issues_cache.clear()

    def mark_dirty(self) -> None:
        self._dirty = True

    # --- Dependency helpers -------------------------------------------------

    def get_dependents(self, sync_uuid: str) -> list[str]:
        result = []
        for uid, info in self.metadata.get("tracked_groups", {}).items():
            if sync_uuid in info.get("depends_on", []):
                result.append(uid)
        return result

    def get_dependencies(self, sync_uuid: str) -> list[str]:
        info = get_tracked_group(self.metadata, sync_uuid)
        if info is None:
            return []
        return info.get("depends_on", [])

    # --- Utilities ---------------------------------------------------------

    def get_tracked_group(self, sync_uuid: str) -> dict | None:
        """Return the info dict for a tracked group, or None."""
        return get_tracked_group(self.metadata, sync_uuid)

    def get_blend_name(self, sync_uuid: str) -> str | None:
        info = get_tracked_group(self.metadata, sync_uuid)
        if info is None:
            return None
        return info.get("blend_name")

    def get_json_path(self, sync_uuid: str) -> str | None:
        info = get_tracked_group(self.metadata, sync_uuid)
        if info is None:
            return None
        return resolve_json_path(info.get("json_path", ""), self._blend_dir())

    def get_all_tracked_uuids(self) -> list[str]:
        return list(self.metadata.get("tracked_groups", {}).keys())

    # --- Batch operations ---------------------------------------------------

    def link_all_groups(self, json_path: str, context=None) -> dict:
        """Link all Geometry Node groups in the current .blend to a single
        master JSON file.

        Serializes every group, creates per-group tracking entries with
        per-group hashes, and auto-links all dependencies.

        Uses surgical update like export_all: the existing JSON is read and
        only the entries matching .blend groups are replaced, so groups that
        exist solely in the JSON (and any modifiers) are preserved.

        Returns a dict with counts:
            {"linked": int, "skipped": int, "errors": int}
        """
        # Collect all geometry node groups
        all_groups = {ng.name: ng for ng in bpy.data.node_groups
                      if ng.type == 'GEOMETRY'}
        if not all_groups:
            return {"linked": 0, "skipped": 0, "errors": 0}

        total = len(all_groups)
        print(f"[Link All] serializing {total} groups...")

        abs_path = json_path
        if not os.path.isabs(abs_path):
            abs_path = os.path.join(self._blend_dir(), abs_path)

        # Step 1: build the unified package surgically — start from the
        # existing JSON (preserving entries without a .blend counterpart),
        # or from a fresh skeleton when the file does not exist.
        if os.path.isfile(abs_path):
            master_data = read_json_tolerant(abs_path)
            if (not isinstance(master_data, dict)
                    or not isinstance(master_data.get("node_groups"), dict)):
                master_data = {
                    "version": ADDON_VERSION,
                    "type": "GN_UNIFIED_PACKAGE",
                    "export_method": PACKAGE_EXPORT_METHOD,
                    "node_groups": {},
                    "modifiers": [],
                }
        else:
            master_data = {
                "version": ADDON_VERSION,
                "type": "GN_UNIFIED_PACKAGE",
                "export_method": PACKAGE_EXPORT_METHOD,
                "node_groups": {},
                "modifiers": [],
            }
        done = 0
        for name, tree in all_groups.items():
            master_data["node_groups"][name] = serialize_node_tree(tree)
            done += 1
            if done % 50 == 0 or done == total:
                print(f"[Link All] serialized {done}/{total} groups")
                if context and hasattr(context, 'workspace') and context.workspace:
                    context.workspace.status_text_set(f"Link All: serializing {done}/{total}...")
                import bpy as _bpy
                _bpy.app.timers.register(lambda: None, first_interval=0.0)

        print(f"[Link All] writing JSON to {abs_path}...")
        if context and hasattr(context, 'workspace') and context.workspace:
            context.workspace.status_text_set("Link All: writing JSON...")

        # Write the master JSON
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, 'w', encoding='utf-8') as f:
            json.dump(master_data, f, indent=4, ensure_ascii=False)

        print(f"[Link All] JSON written, reading back from disk to compute hashes...")

        json_mtime = os.path.getmtime(abs_path)
        stored_path = make_json_path_relative(abs_path, self._blend_dir())

        # Read JSON back from disk to ensure hashes match exactly what
        # check_status and export_to_json will compute later.
        with open(abs_path, 'r', encoding='utf-8') as f:
            disk_data = json.load(f)

        disk_groups = disk_data.get("node_groups", {}) if isinstance(disk_data, dict) else {}

        # Pre-compute all JSON hashes from the on-disk data
        group_hashes = {}
        for gname, gdata in disk_groups.items():
            group_hashes[gname] = {"json": canonical_hash_from_json_data(gdata)}

        # Pre-compute all blend hashes directly from Blender trees
        # (ensures consistency with check_status which uses canonical_hash_from_tree)
        for name, tree in all_groups.items():
            group_hashes[name]["blend"] = canonical_hash_from_tree(tree)

        # Step 2: create tracking entries for each group
        linked = 0
        skipped = 0
        errors = 0

        # First pass: generate all UUIDs so depends_on can reference them
        name_to_uuid = {}
        for name, tree in all_groups.items():
            existing_uid = get_uuid_from_tree(tree)
            if existing_uid:
                name_to_uuid[name] = existing_uid
                skipped += 1
                continue
            uid = find_uuid_for_tree(tree, self.metadata)
            if uid:
                name_to_uuid[name] = uid
                skipped += 1
                continue
            new_uuid = generate_uuid()
            store_uuid_on_tree(tree, new_uuid)
            name_to_uuid[name] = new_uuid
            linked += 1

        print(f"[Link All] UUIDs assigned: {linked} new, {skipped} existing")

        # Second pass: create metadata entries using pre-computed hashes
        done = 0
        for name, tree in all_groups.items():
            done += 1
            sync_uuid = name_to_uuid.get(name)
            if sync_uuid is None:
                errors += 1
                continue

            # Use pre-computed hashes instead of re-serializing and re-reading
            blend_hash = group_hashes[name]["blend"]
            json_hash = group_hashes[name]["json"]

            # Skip if already tracked (avoid duplicates)
            existing_info = get_tracked_group(self.metadata, sync_uuid)
            if existing_info is not None:
                update_tracked_group(
                    self.metadata, sync_uuid,
                    last_blend_hash=blend_hash,
                    last_json_hash=json_hash,
                    last_json_mtime=json_mtime,
                    json_path=stored_path,
                )
                continue

            # Resolve dependency UUIDs
            deps = get_tree_dependencies(tree)
            dep_uuids = []
            for dep_name in deps:
                if dep_name == name:
                    continue
                dep_uid = name_to_uuid.get(dep_name)
                if dep_uid:
                    dep_uuids.append(dep_uid)

            add_tracked_group(
                self.metadata, sync_uuid, name, stored_path,
                blend_hash, json_hash, json_mtime,
                depends_on=dep_uuids,
            )

            if done % 50 == 0 or done == total:
                print(f"[Link All] tracking {done}/{total} groups")
                if context and hasattr(context, 'workspace') and context.workspace:
                    context.workspace.status_text_set(f"Link All: tracking {done}/{total}...")

        self._dirty = True
        # All groups are SYNCED after a fresh link — populate cache directly
        # without recomputing hashes (we just wrote them).
        for uid, info in self.metadata.get("tracked_groups", {}).items():
            self._status_cache[uid] = SyncStatus.SYNCED
        print(f"[Link All] done: {linked} linked, {skipped} skipped, {errors} errors")
        if context and hasattr(context, 'workspace') and context.workspace:
            context.workspace.status_text_set(f"Link All: done ({linked} linked, {skipped} skipped)")
        return {"linked": linked, "skipped": skipped, "errors": errors}

    def export_all(self, force: bool = False, context=None) -> dict:
        """Export all tracked groups to their respective JSON files.

        Groups sharing the same JSON file are exported together in a
        single write operation.

        Uses surgical update: reads existing JSON, serializes each
        tracked group ONCE (no dependency chains), updates entries
        in-place, and writes back. This preserves all groups in the
        unified package and avoids redundant serialization.

        Returns a dict with counts:
            {"exported": int, "skipped": int, "errors": int}
        """
        tracked = self.metadata.get("tracked_groups", {})
        if not tracked:
            return {"exported": 0, "skipped": 0, "errors": 0}

        total = len(tracked)
        print(f"[Export All] exporting {total} tracked groups...")

        # Group tracked entries by JSON path
        json_groups: dict[str, list[str]] = {}
        for uid, info in tracked.items():
            jp = info.get("json_path", "")
            if jp not in json_groups:
                json_groups[jp] = []
            json_groups[jp].append(uid)

        exported = 0
        skipped = 0
        errors = 0
        done = 0

        for jp, uids in json_groups.items():
            json_path = resolve_json_path(jp, self._blend_dir())
            if not json_path:
                errors += len(uids)
                continue

            # Collect all tracked groups for this JSON (serialize each ONCE)
            groups_to_export: dict[str, object] = {}
            any_tree_found = False
            abs_path = json_path

            for uid in uids:
                info = get_tracked_group(self.metadata, uid)
                if info is None:
                    continue
                blend_name = info.get("blend_name", "")
                tree = find_tree_by_uuid(blend_name, uid)
                if tree is None:
                    continue

                # Serialize each tracked group directly (no dependency chain)
                groups_to_export[blend_name] = tree
                any_tree_found = True

            if not any_tree_found:
                skipped += len(uids)
                continue

            print(f"[Export All] serializing {len(groups_to_export)} groups to {os.path.basename(json_path)}...")
            if context and hasattr(context, 'workspace') and context.workspace:
                context.workspace.status_text_set(f"Export All: writing {len(groups_to_export)} groups...")

            # Acquire lock
            lock = JsonLock(json_path)
            if not lock.acquire():
                errors += len(uids)
                continue

            try:
                # Read existing JSON to preserve non-tracked groups
                if os.path.isfile(json_path):
                    master_data = read_json_tolerant(json_path)
                    if master_data is None:
                        master_data = {
                            "version": ADDON_VERSION,
                            "type": "GN_UNIFIED_PACKAGE",
                            "export_method": PACKAGE_EXPORT_METHOD,
                            "node_groups": {},
                            "modifiers": [],
                        }
                else:
                    master_data = {
                        "version": ADDON_VERSION,
                        "type": "GN_UNIFIED_PACKAGE",
                        "export_method": PACKAGE_EXPORT_METHOD,
                        "node_groups": {},
                        "modifiers": [],
                    }

                # Update only the tracked groups in-place
                serialized_dict = {}
                for g_name, g_tree in groups_to_export.items():
                    master_data["node_groups"][g_name] = serialize_node_tree(g_tree)
                    serialized_dict[g_name] = master_data["node_groups"][g_name]

                os.makedirs(os.path.dirname(json_path), exist_ok=True)
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(master_data, f, indent=4, ensure_ascii=False)

                new_mtime = os.path.getmtime(json_path)

                # Update all tracked groups that share this JSON
                self._cascade_update_hashes(json_path, new_mtime, serialized_data=serialized_dict)
                exported += len(uids)
                done += len(uids)
                self._dirty = True
                print(f"[Export All] {exported}/{total} groups exported")

            except Exception as e:
                print(f"[Export All] error: {e}")
                errors += len(uids)
            finally:
                lock.release()

        print(f"[Export All] done: {exported} exported, {skipped} skipped, {errors} errors")
        if context and hasattr(context, 'workspace') and context.workspace:
            context.workspace.status_text_set(f"Export All: done ({exported} exported, {skipped} skipped)")
        return {"exported": exported, "skipped": skipped, "errors": errors}

    def export_all_modified(self, force: bool = False, context=None) -> dict:
        """Export only groups that are BLEND_MODIFIED or CONFLICT.

        Detects changes by comparing blend hashes against stored hashes.
        Groups that are SYNCED, JSON_MODIFIED, or ORPHAN are left untouched.

        Returns a dict with counts:
            {"exported": int, "skipped": int, "errors": int}
        """
        tracked = self.metadata.get("tracked_groups", {})
        if not tracked:
            return {"exported": 0, "skipped": 0, "errors": 0}

        print("[Export Modified] scanning for changes...")

        # Detect which groups are blend_modified or conflict
        groups_to_export: list[str] = []
        skipped = 0

        for uid, info in tracked.items():
            blend_name = info.get("blend_name", "")
            tree = find_tree_by_uuid(blend_name, uid)
            if tree is None:
                skipped += 1
                continue

            current_blend_hash = canonical_hash_from_tree(tree)
            stored_blend_hash = info.get("last_blend_hash", "")

            if stored_blend_hash and current_blend_hash != stored_blend_hash:
                groups_to_export.append(uid)
            else:
                skipped += 1

        total = len(tracked)
        to_export_count = len(groups_to_export)
        print(f"[Export Modified] {to_export_count} of {total} groups need exporting, {skipped} up-to-date")

        if not groups_to_export:
            return {"exported": 0, "skipped": skipped, "errors": 0}

        # Group by JSON path for batch export
        json_groups: dict[str, list[str]] = {}
        for uid in groups_to_export:
            info = tracked[uid]
            jp = info.get("json_path", "")
            if jp not in json_groups:
                json_groups[jp] = []
            json_groups[jp].append(uid)

        exported = 0
        errors = 0

        for jp, uids in json_groups.items():
            json_path = resolve_json_path(jp, self._blend_dir())
            if not json_path or not os.path.isfile(json_path):
                errors += len(uids)
                continue

            # Collect trees to serialize
            groups_to_serialize: dict[str, object] = {}
            for uid in uids:
                info = tracked[uid]
                blend_name = info.get("blend_name", "")
                tree = find_tree_by_uuid(blend_name, uid)
                if tree is not None:
                    groups_to_serialize[blend_name] = tree

            if not groups_to_serialize:
                errors += len(uids)
                continue

            print(f"[Export Modified] serializing {len(groups_to_serialize)} groups to {os.path.basename(json_path)}...")
            if context and hasattr(context, 'workspace') and context.workspace:
                context.workspace.status_text_set(f"Export Modified: writing {len(groups_to_serialize)} groups...")

            # Acquire lock
            lock = JsonLock(json_path)
            if not lock.acquire():
                errors += len(uids)
                continue

            try:
                # Read existing JSON
                master_data = read_json_tolerant(json_path)
                if master_data is None:
                    errors += len(uids)
                    continue

                # Serialize and update only the modified groups
                serialized_dict = {}
                for g_name, g_tree in groups_to_serialize.items():
                    master_data["node_groups"][g_name] = serialize_node_tree(g_tree)
                    serialized_dict[g_name] = master_data["node_groups"][g_name]

                os.makedirs(os.path.dirname(json_path), exist_ok=True)
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(master_data, f, indent=4, ensure_ascii=False)

                # Update hashes for all groups sharing this JSON
                new_mtime = os.path.getmtime(json_path)
                self._cascade_update_hashes(json_path, new_mtime, serialized_data=serialized_dict)
                exported += len(uids)

                # Invalidate status cache for affected groups
                for uid in uids:
                    self._status_cache.pop(uid, None)
                # Also invalidate other groups sharing the same JSON
                for uid, ig in self.metadata.get("tracked_groups", {}).items():
                    if ig.get("json_path", "") == jp:
                        self._status_cache.pop(uid, None)

            finally:
                lock.release()

        self._dirty = True
        print(f"[Export Modified] done: {exported} exported, {skipped} skipped, {errors} errors")
        if context and hasattr(context, 'workspace') and context.workspace:
            context.workspace.status_text_set(f"Export Modified: done ({exported} exported, {errors} errors)")
        return {"exported": exported, "skipped": skipped, "errors": errors}

    def import_all_modified(self, context=None) -> dict:
        """Import all groups that are JSON_MODIFIED, BLEND_MODIFIED, or CONFLICT.

        For JSON_MODIFIED: JSON has external changes, import to Blender.
        For BLEND_MODIFIED: Blender has changes, but user chose to overwrite with JSON.
        For CONFLICT: Both sides changed, import JSON to resolve.

        Uses fast JSON-hash comparison (no .blend serialization) to determine
        which groups need importing.  Groups that are SYNCED are left untouched.

        Returns a dict with counts:
            {"imported": int, "skipped": int, "errors": int,
             "auto_linked": int}
        """
        tracked = self.metadata.get("tracked_groups", {})
        if not tracked:
            return {"imported": 0, "skipped": 0, "errors": 0, "auto_linked": 0}

        print("[Import Modified] scanning for changes...")

        # --- Fast detection: compare json hashes from disk vs stored ---
        # Read each JSON file once and compute per-group hashes in memory.
        # No .blend serialization needed.
        json_data_cache: dict[str, dict] = {}
        json_hash_cache: dict[str, dict[str, str]] = {}

        unique_paths = set()
        for uid, info in tracked.items():
            jp = info.get("json_path", "")
            if jp:
                unique_paths.add(jp)

        for jp in unique_paths:
            json_path = resolve_json_path(jp, self._blend_dir())
            if json_path and os.path.isfile(json_path):
                data = read_json_tolerant(json_path)
                if data is not None:
                    json_data_cache[jp] = data
                    if isinstance(data, dict) and data.get("type") == "GN_UNIFIED_PACKAGE":
                        group_hashes = {}
                        for gname, gdata in data.get("node_groups", {}).items():
                            group_hashes[gname] = canonical_hash_from_json_data(gdata)
                        json_hash_cache[jp] = group_hashes

        # Determine which groups need importing
        # Include: json_modified, blend_modified, and conflict
        groups_to_import: list[tuple[str, dict, str]] = []
        skipped = 0

        for uid, info in tracked.items():
            blend_name = info.get("blend_name", "")
            jp = info.get("json_path", "")
            last_json_hash = info.get("last_json_hash", "")
            last_blend_hash = info.get("last_blend_hash", "")

            # Fast: compare stored json hash to current json hash
            group_hashes = json_hash_cache.get(jp, {})
            current_json_hash = group_hashes.get(blend_name)

            if current_json_hash is None:
                # Try standalone file
                data = json_data_cache.get(jp)
                if isinstance(data, dict) and "nodes" in data:
                    current_json_hash = canonical_hash_from_json_data(data)

            if current_json_hash is None:
                # Can't determine — skip
                skipped += 1
                continue

            json_changed = (not last_json_hash) or (current_json_hash != last_json_hash)

            # Also check if blend was modified (user wants to overwrite with JSON)
            blend_changed = False
            if last_blend_hash:
                tree = find_tree_by_uuid(blend_name, uid)
                if tree is not None:
                    current_blend_hash = canonical_hash_from_tree(tree)
                    blend_changed = current_blend_hash != last_blend_hash

            if json_changed or blend_changed:
                # Needs import (JSON changed or blend changed and user wants to overwrite)
                groups_to_import.append((uid, info, jp))
            else:
                skipped += 1

        total = len(tracked)
        to_import_count = len(groups_to_import)
        print(f"[Import Modified] {to_import_count} of {total} groups need importing, {skipped} up-to-date")

        if not groups_to_import:
            return {"imported": 0, "skipped": skipped, "errors": 0, "auto_linked": 0}

        # --- Import each modified group using cached data ---
        imported = 0
        errors = 0
        auto_linked = 0
        done = 0

        # Track which JSON paths were modified for batch post-processing
        modified_json_paths: set[str] = set()

        for uid, info, jp in groups_to_import:
            done += 1
            blend_name = info.get("blend_name", "")
            json_path = resolve_json_path(jp, self._blend_dir())
            modified_json_paths.add(jp)

            print(f"[Import Modified] importing '{blend_name}' ({done}/{to_import_count})...")
            if context and hasattr(context, 'workspace') and context.workspace:
                context.workspace.status_text_set(f"Import Modified: {done}/{to_import_count} — {blend_name}")

            # Use cached JSON data instead of re-reading from disk
            data = json_data_cache.get(jp)
            if data is None:
                errors += 1
                continue

            if isinstance(data, dict) and data.get("type") == "GN_UNIFIED_PACKAGE":
                json_cache = data.get("node_groups", {})
                tree_data = json_cache.get(blend_name)
                if tree_data is None:
                    tree_data = next(iter(json_cache.values()))
            elif isinstance(data, dict) and "nodes" in data:
                json_cache = {blend_name: data}
                tree_data = data
            else:
                errors += 1
                continue

            if tree_data is None:
                errors += 1
                continue

            # Use the fast internal import method (no disk reads, no cascade)
            tracker = self._import_from_json_data(uid, tree_data, json_cache, blend_name, context)
            if tracker.has_errors:
                errors += 1
            else:
                imported += 1

        # --- Post-processing: update hashes, cascade, auto-link ---
        # Do this once for all imported groups instead of per-group
        print(f"[Import Modified] updating hashes for {len(modified_json_paths)} JSON file(s)...")

        for jp in modified_json_paths:
            json_path = resolve_json_path(jp, self._blend_dir())
            if not json_path or not os.path.isfile(json_path):
                continue

            new_mtime = os.path.getmtime(json_path)

            # Cascade update: compute JSON hashes from cached data,
            # blend hashes from actual trees
            data = json_data_cache.get(jp)
            if data is None:
                continue

            if isinstance(data, dict) and data.get("type") == "GN_UNIFIED_PACKAGE":
                json_groups = data.get("node_groups", {})
            elif isinstance(data, dict) and "nodes" in data:
                json_groups = {data.get("name", ""): data}
            else:
                continue

            for uid, ig in self.metadata.get("tracked_groups", {}).items():
                if ig.get("json_path", "") != jp:
                    continue
                blend_name = ig.get("blend_name", "")
                tree = find_tree_by_uuid(blend_name, uid)
                if tree is None:
                    continue

                # JSON hash from cached data
                dep_json_hash = canonical_hash_from_json_data(json_groups.get(blend_name, {}))
                # Blend hash from actual tree
                dep_blend_hash = canonical_hash_from_tree(tree)

                update_tracked_group(
                    self.metadata, uid,
                    last_blend_hash=dep_blend_hash,
                    last_json_hash=dep_json_hash,
                    last_json_mtime=new_mtime,
                )

            # Auto-link new groups for this JSON
            if isinstance(data, dict) and data.get("type") == "GN_UNIFIED_PACKAGE":
                for group_name in data.get("node_groups", {}):
                    tree = bpy.data.node_groups.get(group_name)
                    if tree and not get_uuid_from_tree(tree):
                        existing = find_uuid_for_tree(tree, self.metadata)
                        if not existing:
                            auto_linked += 1

        # Invalidate status cache for all affected groups
        for jp in modified_json_paths:
            for uid, ig in self.metadata.get("tracked_groups", {}).items():
                if ig.get("json_path", "") == jp:
                    self._status_cache.pop(uid, None)

        self._dirty = True
        print(f"[Import Modified] done: {imported} imported, {skipped} skipped, {errors} errors, {auto_linked} auto-linked")
        if context and hasattr(context, 'workspace') and context.workspace:
            context.workspace.status_text_set(f"Import Modified: done ({imported} imported, {errors} errors)")
        return {"imported": imported, "skipped": skipped, "errors": errors, "auto_linked": auto_linked}

    def get_status_summary(self) -> dict[str, int]:
        """Return a summary count of each sync status for all tracked groups.

        Uses the status cache.  Returns all zeros if the cache is empty
        (i.e. no Refresh Status has been run yet), so the UI stays responsive.
        Call invalidate_cache() + check_all_statuses() to populate.
        """
        if not self._status_cache:
            return {s.value: 0 for s in SyncStatus} | {"ignored": 0}
        counts = {s.value: 0 for s in SyncStatus}
        counts["ignored"] = 0
        for uid, status in self._status_cache.items():
            counts[status.value] += 1
            if is_ignored(self.metadata, uid):
                counts["ignored"] += 1
        return counts

    def _blend_dir(self) -> str:
        filepath = bpy.data.filepath
        if filepath:
            return os.path.dirname(os.path.abspath(filepath))
        return os.path.abspath(".")

    def count_local_changes(self) -> int:
        """Count tracked groups with local (.blend) changes.

        Uses the status cache when populated so that repeated calls
        (e.g. per click of the Import Modified button) do not serialize
        every node group. When the cache is empty, computes statuses
        once (same cost as Refresh Status) and stores them.

        Returns the number of groups with BLEND_MODIFIED or CONFLICT
        status.
        """
        if not self._status_cache:
            self.check_all_statuses()
        return sum(
            1 for s in self._status_cache.values()
            if s in (SyncStatus.BLEND_MODIFIED, SyncStatus.CONFLICT)
        )

    # --- Geometry validation ------------------------------------------------

    def validate_geometry(self) -> dict[str, list]:
        """Validate all tracked node trees for geometry issues.

        Runs generic validators (attribute references, group references,
        unlinked inputs, invalid outputs) on every tracked group.

        Never blocks — only populates the issues cache for UI display.

        Returns:
            Dict mapping UUID to list of ValidationIssue objects.
        """
        self._geometry_issues_cache = validate_all_tracked_trees()
        return self._geometry_issues_cache

    def get_geometry_issues(self, sync_uuid: str = None) -> list:
        """Get geometry issues for a specific UUID or all UUIDs.

        Args:
            sync_uuid: If provided, returns issues for this UUID only.
                      If None, returns all issues across all UUIDs.

        Returns:
            List of ValidationIssue objects.
        """
        if sync_uuid:
            return self._geometry_issues_cache.get(sync_uuid, [])

        all_issues = []
        for issues in self._geometry_issues_cache.values():
            all_issues.extend(issues)
        return all_issues

    def get_geometry_issue_count(self) -> int:
        """Total number of geometry issues across all tracked groups."""
        return sum(len(issues) for issues in self._geometry_issues_cache.values())

    def get_geometry_issue_summary(self) -> str:
        """Human-readable summary of geometry issues."""
        all_issues = self.get_geometry_issues()
        return format_issue_summary(all_issues)

    def has_geometry_issues(self) -> bool:
        """Check if any tracked group has geometry issues."""
        return bool(self._geometry_issues_cache)

    def invalidate_geometry_cache(self) -> None:
        """Clear the geometry issues cache so it will be recomputed."""
        self._geometry_issues_cache.clear()


sync_manager = SyncManager()