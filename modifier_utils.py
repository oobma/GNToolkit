# -*- coding: utf-8 -*-
"""gn_toolkit.modifier_utils — NODES modifier input helpers.

Blender 5.2 stores NODES modifier inputs keyed by the *identifier* of the
referenced tree's interface sockets (``mod.properties.inputs[identifier]``).
Rebuilding a tree renumbers those identifiers (cosmetic, see
``hash_utils``), which orphans the stored modifier values — they silently
fall back to their defaults.

These helpers snapshot the values before a rebuild and restore them through
an identifier remap built by ``(name, in_out)`` matching, so a Pull/import
no longer destroys the user's modifier settings.
"""

from __future__ import annotations

import bpy


def modifier_uses_rna_inputs(mod) -> bool:
    """True when the NODES modifier stores its inputs as RNA (Blender 5.2+)."""
    return hasattr(mod, "properties") and hasattr(mod.properties, "inputs")


def iter_modifier_sockets(collection):
    """Yield the socket subgroups of a 5.2 modifier inputs/outputs wrapper.

    ``dir()`` may list identifiers that the ``[]`` accessor refuses to
    resolve; every entry is resolved defensively and bad ones skipped.
    """
    for attr in dir(collection):
        if not attr.startswith("Socket"):
            continue
        try:
            item = collection[attr]
        except (KeyError, TypeError):
            continue
        if item is not None:
            yield attr, item


def modifier_input_is_attribute(raw_type) -> bool:
    """Interpret a 5.2 modifier input ``type`` value (string or enum index).

    A fresh input reads back the enum index of its default (VALUE); a
    written value round-trips as the string.
    """
    if isinstance(raw_type, str):
        return raw_type == "ATTRIBUTE"
    return raw_type != 1


def _tree_interface_sockets(tree):
    """Yield ``(identifier, name, in_out)`` for every socket of *tree*."""
    interface = getattr(tree, "interface", None)
    items = getattr(interface, "items_tree", None) if interface is not None else None
    if items is not None:
        for item in items:
            if getattr(item, "item_type", "") == "SOCKET":
                yield (getattr(item, "identifier", ""),
                       getattr(item, "name", ""),
                       getattr(item, "in_out", "INPUT"))
        return
    for in_out, collection in (("INPUT", getattr(tree, "inputs", ())),
                               ("OUTPUT", getattr(tree, "outputs", ()))):
        for sock in collection:
            ident = getattr(sock, "identifier", "") or getattr(sock, "name", "")
            yield (ident, getattr(sock, "name", ""), in_out)


def snapshot_modifier_inputs(tree):
    """Capture every NODES modifier that references *tree*.

    Must run BEFORE the tree's interface is cleared.  Returns
    ``(snapshots, old_items)`` where *snapshots* is a list of
    ``(modifier, entries)`` and each entry is
    ``(old_identifier, value, raw_type, attribute_name)`` — ``None`` for
    fields the socket does not expose.
    """
    old_items = list(_tree_interface_sockets(tree))
    snapshots = []
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if mod.type != 'NODES' or mod.node_group != tree:
                continue
            entries = []
            if modifier_uses_rna_inputs(mod):
                for attr, item in iter_modifier_sockets(mod.properties.inputs):
                    value = raw_type = attribute_name = None
                    try:
                        value = item["value"]
                    except (KeyError, TypeError):
                        pass
                    try:
                        raw_type = item["type"]
                    except (KeyError, TypeError):
                        pass
                    try:
                        attribute_name = item["attribute_name"]
                    except (KeyError, TypeError):
                        pass
                    if value is None and raw_type is None and attribute_name is None:
                        continue
                    entries.append((attr, value, raw_type, attribute_name))
            else:
                try:
                    legacy = dict(mod)
                except (TypeError, RuntimeError):
                    legacy = {}
                entries = [(key, val, None, None) for key, val in legacy.items()]
            if entries:
                snapshots.append((mod, entries))
    return snapshots, old_items


def build_identifier_remap(old_items, tree) -> dict:
    """Map the pre-rebuild interface identifiers to the rebuilt tree's.

    Sockets are matched by ``(name, in_out)``, pairing duplicates in
    order.  Sockets that no longer exist are absent from the map.
    """
    queues: dict[tuple[str, str], list[str]] = {}
    for ident, name, in_out in _tree_interface_sockets(tree):
        queues.setdefault((name, in_out), []).append(ident)
    remap = {}
    for ident, name, in_out in old_items:
        queue = queues.get((name, in_out))
        if queue:
            remap[ident] = queue.pop(0)
    return remap


def restore_modifier_inputs(snapshots, remap) -> int:
    """Re-apply snapshotted modifier inputs through *remap*.

    Returns the number of restored sockets.  Missing identifiers and
    rejected writes are skipped silently (defensive: a removed or
    type-changed socket must not abort the import).
    """
    if not snapshots or not remap:
        return 0
    restored = 0
    for mod, entries in snapshots:
        if modifier_uses_rna_inputs(mod):
            props = mod.properties
            input_ids = {attr for attr, _ in iter_modifier_sockets(props.inputs)}
            for old_id, value, raw_type, attribute_name in entries:
                new_id = remap.get(old_id)
                if not new_id or new_id not in input_ids:
                    continue
                try:
                    item = props.inputs[new_id]
                    if raw_type is not None:
                        item["type"] = raw_type
                    if attribute_name is not None:
                        item["attribute_name"] = attribute_name
                    if value is not None:
                        item["value"] = value
                    restored += 1
                except (TypeError, AttributeError, ValueError, RuntimeError, KeyError):
                    pass
        else:
            for old_id, value, _raw_type, _attribute_name in entries:
                new_id = remap.get(old_id)
                if not new_id:
                    continue
                try:
                    mod[new_id] = value
                    restored += 1
                except (TypeError, AttributeError, ValueError, RuntimeError):
                    pass
    return restored


def remap_input_keys(inputs: dict, identifier_map: dict | None) -> dict:
    """Translate serialized modifier-input keys through *identifier_map*.

    Keys may carry the ``_use_attribute`` / ``_attribute_name`` suffixes;
    the base identifier is translated and the suffix preserved.
    """
    if not identifier_map:
        return inputs
    out = {}
    for key, value in inputs.items():
        mapped = key
        for suffix in ("_use_attribute", "_attribute_name"):
            if key.endswith(suffix):
                base = key[:-len(suffix)]
                mapped = identifier_map.get(base, base) + suffix
                break
        else:
            mapped = identifier_map.get(key, key)
        out[mapped] = value
    return out
