# -*- coding: utf-8 -*-
"""Semantic 3-way merge of GNToolkit group JSON data (pure Python, no bpy).

Implements the merge semantics (specs 2026-10-16 and 2026-10-19):

* VALUES ONLY: socket defaults, node labels/properties, custom properties
  (node and tree), tree properties and the curated owner settings of
  embedded trees (materials/worlds), decided per item with a diff3 table
  over base/ours/theirs.  ABSENT is a value of its own.
* NODE GROUPS: Geometry Nodes, Shader and Compositor trees (spec
  2026-10-19, compositor 2026-10-20); any
  other tree type reports ``unsupported`` (a notice, not a conflict) so
  a mixed repository does not fail the preview.
* EMBEDDED ENTRIES: ``materials`` and ``worlds`` merge the tree and the
  owner settings per item (spec 2026-10-19, second extension); adds and
  removals follow the same one-side rules as node groups.
* Anything STRUCTURAL (nodes, links, interface presence, zones, menus,
  inner trees, texts, derived groups) is never fused: it is adopted
  whole from the side that changed it, or reported as a conflict when
  both sides changed it differently.
* Single source of truth: decisions compare the canonical forms produced
  by ``hash_utils.canonicalize_node_tree_data``, so every fold, exclusion
  and normalization of the hash algorithm applies here too.  Nothing is
  re-implemented.
* Never lose work silently: an edit that cannot be placed on the adopted
  structure becomes an explicit conflict, never a silent drop.
* MANUAL RESOLUTION (editor, spec 2026-10-17): an optional ``overrides``
  argument resolves conflicted items per value key (ours/theirs/base or a
  custom scalar) and a conflicted structure as one whole side.  Without
  it the output is byte-identical to the automatic merge.

Preview only: nothing here writes to disk.  ``merge_group_entries``
decides one entry, ``merge_group_maps`` decides a whole name -> entry map
(including groups added/removed on either side).
"""

from __future__ import annotations

from copy import deepcopy
import json

from .audit import content_hash_from_json_data
from .hash_utils import canonical_hash_from_json_data, canonicalize_node_tree_data

MERGE_VERSION = 4

_ABSENT = object()

_DROP = object()

_DEFAULT_TREE_TYPE = "GeometryNodeTree"

_MERGE_TREE_TYPES = frozenset(
    ("GeometryNodeTree", "ShaderNodeTree", "CompositorNodeTree"))


# ---------------------------------------------------------------------------
# Small helpers (deterministic, report-oriented)
# ---------------------------------------------------------------------------

def _tree_type(entry: dict) -> str:
    value = entry.get("tree_type", _DEFAULT_TREE_TYPE)
    return value if isinstance(value, str) else str(value)


def _short(value) -> str:
    """Compact deterministic rendering of a value for reports."""
    if value is _ABSENT:
        return "(absent)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "%.6g" % value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return "'%s'" % value
    if isinstance(value, list):
        return "(" + ", ".join(_short(v) for v in value) + ")"
    if isinstance(value, dict):
        if "name" in value and len(value) <= 2:
            return "'%s'" % value.get("name")
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return text if len(text) <= 60 else text[:57] + "..."
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else text[:57] + "..."


def _iter_occurrences(items, fields):
    """Yield ``(occ, item)``; *occ* counts repeats of the field-tuple.

    The occurrence index is the positional rule shared with the importer
    and the modifier remap: items are matched by their identity fields
    plus their order among same-identity items.
    """
    seen = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        prefix = tuple(str(item.get(f, "")) for f in fields)
        occ = seen.get(prefix, 0)
        seen[prefix] = occ + 1
        yield occ, item


def _find_raw(items, fields, prefix, occ):
    """Find the raw item whose (fields, occurrence) matches *prefix*."""
    seen = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        p = tuple(str(item.get(f, "")) for f in fields)
        n = seen.get(p, 0)
        seen[p] = n + 1
        if p == prefix and n == occ:
            return item
    return None


def _find_node(raw_entry: dict, node_name: str):
    for node in raw_entry.get("nodes", []):
        if isinstance(node, dict) and node.get("name") == node_name:
            return node
    return None


def _item_label(key) -> str:
    kind = key[0]
    if kind == "label":
        return "node '%s' label" % key[1]
    if kind == "nodeprop":
        return "node '%s' property '%s'" % (key[1], key[2])
    if kind == "nodecustom":
        return "node '%s' custom property '%s'" % (key[1], key[2])
    if kind == "sockdef":
        occ_note = " [#%d]" % (key[5] + 1) if key[5] else ""
        direction = "input" if key[2] == "inputs" else "output"
        return "node '%s' %s '%s'%s default" % (key[1], direction, key[3],
                                                occ_note)
    if kind == "iface":
        occ_note = " [#%d]" % (key[4] + 1) if key[4] else ""
        direction = key[3].lower() or key[1].lower()
        return "interface %s '%s'%s property '%s'" % (direction, key[2],
                                                      occ_note, key[5])
    if kind == "legacy_iface":
        occ_note = " [#%d]" % (key[4] + 1) if key[4] else ""
        return "interface %s '%s'%s '%s'" % (key[1], key[2], occ_note, key[5])
    if kind == "treeprop":
        return "tree property '%s'" % key[1]
    if kind == "treecustom":
        return "tree custom property '%s'" % key[1]
    if kind == "ownerprop":
        return "owner setting '%s'" % key[1]
    if kind == "ownercustom":
        return "owner custom property '%s'" % key[1]
    return repr(key)


def value_key_id(key) -> str:
    """Deterministic id for a value item key (the ``key`` report field)."""
    return json.dumps(list(key), ensure_ascii=True)


def _value_type(*values) -> str:
    """Common scalar type of a conflict's raw values (for custom editing)."""
    kinds = set()
    for value in values:
        if value is _ABSENT:
            continue
        if isinstance(value, bool):
            kinds.add("bool")
        elif isinstance(value, int):
            kinds.add("int")
        elif isinstance(value, float):
            kinds.add("float")
        elif isinstance(value, str):
            kinds.add("string")
        else:
            return "other"
    if len(kinds) == 1:
        return kinds.pop()
    return "other"


def _add_value_conflict(conflicts, key, v_adopted, b, o, t) -> None:
    msg = _item_label(key)
    if key not in v_adopted:
        msg += " - item not present in the adopted structure"
    value_type = _value_type(b, o, t)
    if key[0] == "ownerprop" and value_type == "string":
        # Owner enums/identifiers are not free-form: side choice only.
        value_type = "other"
    conflicts.append({"kind": "value", "item": msg,
                      "key": value_key_id(key),
                      "value_type": value_type,
                      "base": _short(b), "ours": _short(o),
                      "theirs": _short(t)})


def _apply_override_choice(decision, b, o, t):
    """Map one override decision to (resolved, winner, winner_side)."""
    if decision == "ours":
        return True, o, "ours"
    if decision == "theirs":
        return True, t, "theirs"
    if decision == "base":
        return True, b, "base"
    if isinstance(decision, dict) and "custom" in decision:
        return True, decision["custom"], "custom"
    return False, None, None


# ---------------------------------------------------------------------------
# Canonical views: value map + structure projection
# ---------------------------------------------------------------------------

def _extract_values(canonical: dict) -> dict:
    """Sparse {value item key: value} map of a canonical entry."""
    values = {}
    items = canonical.get("interface_items")
    if isinstance(items, list):
        for occ, item in _iter_occurrences(items, ("name", "in_out",
                                                   "item_type")):
            props = item.get("properties")
            if not isinstance(props, dict):
                continue
            for prop, val in props.items():
                values[("iface", str(item.get("item_type", "")),
                        str(item.get("name", "")), str(item.get("in_out", "")),
                        occ, str(prop))] = val
    else:
        for direction in ("inputs", "outputs"):
            for occ, item in _iter_occurrences(canonical.get(direction) or [],
                                               ("name", "type")):
                name = str(item.get("name", ""))
                stype = str(item.get("type", ""))
                if "default_value" in item:
                    values[("legacy_iface", direction, name, stype, occ,
                            "default_value")] = item["default_value"]
                props = item.get("properties")
                if isinstance(props, dict):
                    for prop, val in props.items():
                        values[("legacy_iface", direction, name, stype, occ,
                                str(prop))] = val
    for node in canonical.get("nodes", []):
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", ""))
        if "label" in node:
            values[("label", node_name)] = node["label"]
        props = node.get("properties")
        if isinstance(props, dict):
            for prop, val in props.items():
                values[("nodeprop", node_name, str(prop))] = val
        custom = node.get("custom_properties")
        if isinstance(custom, dict):
            for key, val in custom.items():
                values[("nodecustom", node_name, str(key))] = val
        for direction in ("inputs", "outputs"):
            for occ, sock in _iter_occurrences(node.get(direction) or [],
                                               ("name", "type")):
                if "default_value" in sock:
                    values[("sockdef", node_name, direction,
                            str(sock.get("name", "")),
                            str(sock.get("type", "")), occ)] = \
                        sock["default_value"]
    tree_props = canonical.get("tree_properties")
    if isinstance(tree_props, dict):
        for key, val in tree_props.items():
            values[("treeprop", str(key))] = val
    tree_custom = canonical.get("custom_properties")
    if isinstance(tree_custom, dict):
        for key, val in tree_custom.items():
            values[("treecustom", str(key))] = val
    settings = canonical.get("owner_settings")
    if isinstance(settings, dict):
        props = settings.get("properties")
        if isinstance(props, dict):
            for prop, val in props.items():
                values[("ownerprop", str(prop))] = val
        custom = settings.get("custom_properties")
        if isinstance(custom, dict):
            for key, val in custom.items():
                values[("ownercustom", str(key))] = val
    return values


def _extract_structure(canonical: dict) -> dict:
    """Canonical entry with every value field removed."""
    structure = {k: v for k, v in canonical.items()
                 if k not in ("tree_properties", "custom_properties",
                              "owner_settings")}
    items = structure.get("interface_items")
    if isinstance(items, list):
        structure["interface_items"] = [
            {k: v for k, v in item.items() if k != "properties"}
            if isinstance(item, dict) else item
            for item in items]
    else:
        for direction in ("inputs", "outputs"):
            lst = structure.get(direction)
            if isinstance(lst, list):
                structure[direction] = [
                    {k: v for k, v in item.items()
                     if k not in ("default_value", "properties")}
                    if isinstance(item, dict) else item
                    for item in lst]
    nodes = structure.get("nodes")
    if isinstance(nodes, list):
        new_nodes = []
        for node in nodes:
            if isinstance(node, dict):
                node = {k: v for k, v in node.items()
                        if k not in ("label", "properties",
                                     "custom_properties")}
                for direction in ("inputs", "outputs"):
                    lst = node.get(direction)
                    if isinstance(lst, list):
                        node[direction] = [
                            {k: v for k, v in item.items()
                             if k != "default_value"}
                            if isinstance(item, dict) else item
                            for item in lst]
            new_nodes.append(node)
        structure["nodes"] = new_nodes
    return structure


# ---------------------------------------------------------------------------
# Value transplant (adopted raw entry <- winning value)
# ---------------------------------------------------------------------------

def _apply_value(raw_entry: dict, key, value) -> bool:
    """Set/remove one value in the adopted raw entry.

    Returns False when the target item cannot be located (the caller turns
    that into an explicit conflict; work is never dropped silently).
    """
    kind = key[0]
    if kind == "label":
        node = _find_node(raw_entry, key[1])
        if node is None:
            return False
        if value is _ABSENT:
            node.pop("label", None)
        else:
            node["label"] = value
        return True
    if kind == "nodeprop":
        node = _find_node(raw_entry, key[1])
        if node is None:
            return False
        props = node.get("properties")
        if not isinstance(props, dict):
            props = {}
            node["properties"] = props
        if value is _ABSENT:
            props.pop(key[2], None)
        else:
            props[key[2]] = value
        return True
    if kind == "nodecustom":
        node = _find_node(raw_entry, key[1])
        if node is None:
            return False
        custom = node.get("custom_properties")
        if value is _ABSENT:
            if isinstance(custom, dict):
                custom.pop(key[2], None)
                if not custom:
                    node.pop("custom_properties", None)
            return True
        if not isinstance(custom, dict):
            custom = {}
            node["custom_properties"] = custom
        custom[key[2]] = value
        return True
    if kind == "sockdef":
        node = _find_node(raw_entry, key[1])
        if node is None:
            return False
        sock = _find_raw(node.get(key[2]) or [], ("name", "type"),
                         (key[3], key[4]), key[5])
        if sock is None:
            return False
        if value is _ABSENT:
            sock.pop("default_value", None)
        else:
            sock["default_value"] = value
        return True
    if kind == "iface":
        item = _find_raw(raw_entry.get("interface_items") or [],
                         ("name", "in_out", "item_type"),
                         (key[2], key[3], key[1]), key[4])
        if item is None:
            return False
        props = item.get("properties")
        if value is _ABSENT:
            if isinstance(props, dict):
                props.pop(key[5], None)
            return True
        if not isinstance(props, dict):
            props = {}
            item["properties"] = props
        props[key[5]] = value
        return True
    if kind == "legacy_iface":
        item = _find_raw(raw_entry.get(key[1]) or [], ("name", "type"),
                         (key[2], key[3]), key[4])
        if item is None:
            return False
        if value is _ABSENT:
            item.pop(key[5], None)
        else:
            item[key[5]] = value
        return True
    if kind == "treeprop":
        props = raw_entry.get("tree_properties")
        if value is _ABSENT:
            if isinstance(props, dict):
                props.pop(key[1], None)
            return True
        if not isinstance(props, dict):
            props = {}
            raw_entry["tree_properties"] = props
        props[key[1]] = value
        return True
    if kind == "treecustom":
        custom = raw_entry.get("custom_properties")
        if value is _ABSENT:
            if isinstance(custom, dict):
                custom.pop(key[1], None)
                if not custom:
                    raw_entry.pop("custom_properties", None)
            return True
        if not isinstance(custom, dict):
            custom = {}
            raw_entry["custom_properties"] = custom
        custom[key[1]] = value
        return True
    if kind == "ownerprop":
        settings = raw_entry.get("owner_settings")
        if not isinstance(settings, dict):
            settings = {}
            raw_entry["owner_settings"] = settings
        props = settings.get("properties")
        if not isinstance(props, dict):
            props = {}
            settings["properties"] = props
        if value is _ABSENT:
            props.pop(key[1], None)
        else:
            props[key[1]] = value
        return True
    if kind == "ownercustom":
        settings = raw_entry.get("owner_settings")
        if not isinstance(settings, dict):
            settings = {}
            raw_entry["owner_settings"] = settings
        custom = settings.get("custom_properties")
        if value is _ABSENT:
            if isinstance(custom, dict):
                custom.pop(key[1], None)
                if not custom:
                    settings.pop("custom_properties", None)
            return True
        if not isinstance(custom, dict):
            custom = {}
            settings["custom_properties"] = custom
        custom[key[1]] = value
        return True
    return False


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def _result(name, status, *, adopted_structure=None, merged=None,
            merged_hash=None, applied=None, conflicts=None, notices=None):
    return {
        "name": name,
        "status": status,
        "adopted_structure": adopted_structure,
        "merged": merged,
        "merged_hash": merged_hash,
        "applied": applied or [],
        "conflicts": conflicts or [],
        "notices": notices or [],
    }


def _merge_node_uids(base: dict, ours: dict, theirs: dict,
                     merged: dict) -> None:
    """Carry node identities independently of the adopted structure.

    Preference: ours, then theirs, then base.  Only names present in the
    merged tree survive.  Identity is bookkeeping (outside the canonical
    hash), so preserving it never changes the merge result.
    """
    names = {n.get("name", "") for n in merged.get("nodes", [])}
    maps = (ours.get("node_uids") or {}, theirs.get("node_uids") or {},
            base.get("node_uids") or {})
    out = {}
    for name in sorted(names):
        for mapping in maps:
            uid = mapping.get(name)
            if isinstance(uid, str) and uid:
                out[name] = uid
                break
    if out:
        merged["node_uids"] = out
    else:
        merged.pop("node_uids", None)


# ---------------------------------------------------------------------------
# Graph identity, matching and per-entity diff (pure, read-only)
# ---------------------------------------------------------------------------

_UI_NODE_PROPS = frozenset((
    "bl_width_default", "bl_width_min", "bl_width_max",
    "bl_height_default", "bl_height_min", "bl_height_max",
    "vector_dimensions",
))


def _entry_uids(entry: dict) -> dict:
    value = entry.get("node_uids")
    if not isinstance(value, dict):
        return {}
    return {name: uid for name, uid in value.items()
            if isinstance(name, str) and isinstance(uid, str) and uid}


def _entity_id(entry: dict, name: str) -> str:
    return _entry_uids(entry).get(name) or "name:" + name


def _node_signature(node: dict) -> tuple:
    props = {k: v for k, v in (node.get("properties") or {}).items()
             if not str(k).startswith("bl_") and k not in _UI_NODE_PROPS}
    ins = tuple(sorted((s.get("name", ""), s.get("type", ""))
                       for s in node.get("inputs", [])))
    outs = tuple(sorted((s.get("name", ""), s.get("type", ""))
                        for s in node.get("outputs", [])))
    try:
        prop_text = json.dumps(props, sort_keys=True, default=str)
    except (TypeError, ValueError):
        prop_text = repr(sorted(props))
    return (node.get("type", ""), ins, outs, prop_text)


def _neighbor_names(entry: dict, name: str) -> set:
    out = set()
    for link in entry.get("links", []):
        if link.get("from_node") == name:
            out.add(link.get("to_node", ""))
        if link.get("to_node") == name:
            out.add(link.get("from_node", ""))
    out.discard("")
    return out


def _normalize_link(link: dict) -> tuple:
    return (link.get("from_node", ""),
            link.get("from_socket_name", link.get("from_socket_id", "")),
            link.get("to_node", ""),
            link.get("to_socket_name", link.get("to_socket_id", "")))


def _connected_inputs(entry: dict) -> set:
    return {(link.get("to_node", ""),
             link.get("to_socket_name", link.get("to_socket_id", "")))
            for link in entry.get("links", [])}


def match_entry_nodes(base: dict, side: dict, use_uids: bool = True) -> dict:
    """Match base nodes to side nodes: UID, name+type, signature, topology.

    Returns ``{"matches": {base_name: side_name},
    "ambiguous_base": [...], "ambiguous_side": [...]}``.  Ambiguity is
    never guessed: the caller must treat it as a conflict.
    """
    b_nodes = {n.get("name", ""): n for n in base.get("nodes", [])}
    s_nodes = {n.get("name", ""): n for n in side.get("nodes", [])}
    matches = {}
    ambiguous_b = set()
    ambiguous_s = set()
    used_side = set()

    if use_uids:
        b_by_uid = {}
        s_by_uid = {}
        for name, uid in _entry_uids(base).items():
            if name in b_nodes:
                b_by_uid.setdefault(uid, []).append(name)
        for name, uid in _entry_uids(side).items():
            if name in s_nodes:
                s_by_uid.setdefault(uid, []).append(name)
        for uid in sorted(b_by_uid):
            bnames = sorted(b_by_uid[uid])
            snames = sorted(s_by_uid.get(uid, []))
            if len(bnames) == 1 and len(snames) == 1:
                matches[bnames[0]] = snames[0]
                used_side.add(snames[0])
            elif bnames and snames:
                for bname in bnames:
                    if bname in snames and bname not in used_side:
                        matches[bname] = bname
                        used_side.add(bname)
                for bname in bnames:
                    if bname not in matches:
                        ambiguous_b.add(bname)
                for sname in snames:
                    if sname not in used_side:
                        ambiguous_s.add(sname)

    for bname in sorted(b_nodes):
        if bname in matches or bname in ambiguous_b:
            continue
        snode = s_nodes.get(bname)
        if snode is not None and snode.get("type") == b_nodes[bname].get("type") \
                and bname not in used_side:
            matches[bname] = bname
            used_side.add(bname)

    buckets = {}
    for sname in sorted(s_nodes):
        if sname in used_side or sname in ambiguous_s:
            continue
        buckets.setdefault(_node_signature(s_nodes[sname]), []).append(sname)
    for bname in sorted(b_nodes):
        if bname in matches or bname in ambiguous_b:
            continue
        candidates = [n for n in buckets.get(_node_signature(b_nodes[bname]), [])
                      if n not in used_side and n not in ambiguous_s]
        if len(candidates) == 1:
            matches[bname] = candidates[0]
            used_side.add(candidates[0])

    remaining_b = [n for n in sorted(b_nodes)
                   if n not in matches and n not in ambiguous_b]
    remaining_s = [n for n in sorted(s_nodes)
                   if n not in used_side and n not in ambiguous_s]
    for bname in remaining_b:
        bnode = b_nodes[bname]
        b_neigh = _neighbor_names(base, bname)
        scored = []
        for sname in remaining_s:
            if sname in used_side:
                continue
            snode = s_nodes[sname]
            if snode.get("type") != bnode.get("type"):
                continue
            s_neigh = _neighbor_names(side, sname)
            union = b_neigh | s_neigh
            score = len(b_neigh & s_neigh) / len(union) if union else 0.0
            scored.append((score, sname))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if not scored:
            continue
        best_score = scored[0][0]
        best = [name for score, name in scored if score == best_score]
        if best_score >= 0.5 and len(best) == 1:
            matches[bname] = best[0]
            used_side.add(best[0])
        elif best_score >= 0.5:
            ambiguous_b.add(bname)
            ambiguous_s.update(best)

    return {"matches": matches,
            "ambiguous_base": sorted(ambiguous_b),
            "ambiguous_side": sorted(ambiguous_s)}


def _norm_node(node: dict, connected: set) -> dict:
    props = {k: v for k, v in (node.get("properties") or {}).items()
             if not str(k).startswith("bl_") and k not in _UI_NODE_PROPS}
    inputs = []
    for sock in node.get("inputs", []):
        item = {"name": sock.get("name", ""),
                "type": sock.get("type", ""),
                "bl_idname": sock.get("bl_idname", "")}
        if (node.get("name", ""), sock.get("name", "")) not in connected:
            item["default"] = sock.get("default_value")
        inputs.append(item)
    outputs = []
    for sock in node.get("outputs", []):
        outputs.append({"name": sock.get("name", ""),
                        "type": sock.get("type", ""),
                        "bl_idname": sock.get("bl_idname", ""),
                        "default": sock.get("default_value")})
    custom = {k: v for k, v in (node.get("custom_properties") or {}).items()
              if not str(k).startswith("gnt_")}
    return {
        "type": node.get("type", ""),
        "label": node.get("label", ""),
        "properties": props,
        "inputs": sorted(inputs, key=lambda x: (x["name"], x["type"])),
        "outputs": sorted(outputs, key=lambda x: (x["name"], x["type"])),
        "custom_properties": custom,
        "zone_items": node.get("zone_items") or [],
        "menu_items_data": node.get("menu_items_data") or [],
        "inner_tree": node.get("inner_tree"),
    }


def _socket_map(items) -> dict:
    seen = {}
    out = {}
    for item in items or []:
        prefix = (item.get("name"), item.get("type"))
        occ = seen.get(prefix, 0)
        seen[prefix] = occ + 1
        out[(prefix[0], prefix[1], occ)] = item
    return out


def _changed_fields(a: dict, b: dict) -> set:
    out = set()
    for key in ("type", "label", "properties", "inputs", "outputs",
                "custom_properties", "zone_items", "menu_items_data",
                "inner_tree"):
        if a.get(key) == b.get(key):
            continue
        if key == "properties":
            av = a.get(key) or {}
            bv = b.get(key) or {}
            for prop in sorted(set(av) | set(bv)):
                if av.get(prop) != bv.get(prop):
                    out.add("prop:" + prop)
        elif key in ("inputs", "outputs"):
            av = _socket_map(a.get(key))
            bv = _socket_map(b.get(key))
            prefix = "input:" if key == "inputs" else "output:"
            for item in sorted(set(av) | set(bv)):
                if av.get(item) != bv.get(item):
                    suffix = "[#%d]" % item[2] if item[2] else ""
                    out.add(prefix + item[0] + suffix)
        elif key == "custom_properties":
            av = a.get(key) or {}
            bv = b.get(key) or {}
            for prop in sorted(set(av) | set(bv)):
                if av.get(prop) != bv.get(prop):
                    out.add("custom:" + prop)
        else:
            out.add(key)
    return out


def diff_entries(base: dict, side: dict, use_uids: bool = True) -> dict:
    """Per-entity difference base -> side (pure, deterministic).

    ``added``/``removed`` map entity ids to node names; ``renamed`` maps
    base name to side name; ``changed`` maps entity ids to field keys;
    ``touched`` contains entity ids and link keys; ``field_keys`` is the
    fine-grained map used by the 3-way decision.
    """
    matched = match_entry_nodes(base, side, use_uids=use_uids)
    matches = matched["matches"]
    amb_b = set(matched["ambiguous_base"])
    amb_s = set(matched["ambiguous_side"])
    b_nodes = {n.get("name", ""): n for n in base.get("nodes", [])}
    s_nodes = {n.get("name", ""): n for n in side.get("nodes", [])}
    b_conn = _connected_inputs(base)
    s_conn = _connected_inputs(side)

    added = {}
    removed = {}
    renamed = {}
    changed = {}
    field_keys = {}
    touched = set()

    def _touch(entity, field):
        touched.add(entity)
        field_keys.setdefault(entity, set()).add(field)

    for bname in sorted(b_nodes):
        if bname in amb_b:
            _touch(_entity_id(base, bname), "ambiguous")
            continue
        if bname not in matches:
            entity = _entity_id(base, bname)
            removed[entity] = bname
            _touch(entity, "removed")
            continue
        sname = matches[bname]
        snode = s_nodes[sname]
        entity = _entity_id(base, bname) or _entity_id(side, sname)
        if sname != bname:
            renamed[bname] = sname
            _touch(entity, "name")
        fields = _changed_fields(_norm_node(b_nodes[bname], b_conn),
                                 _norm_node(snode, s_conn))
        if fields:
            changed[entity] = sorted(fields)
            for field in sorted(fields):
                _touch(entity, field)

    matched_side = set(matches.values())
    for sname in sorted(s_nodes):
        if sname in amb_s:
            _touch(_entity_id(side, sname), "ambiguous")
            continue
        if sname not in matched_side:
            entity = _entity_id(side, sname)
            added[entity] = sname
            _touch(entity, "added")

    def link_key(entry, link):
        f_node, f_sock, t_node, t_sock = _normalize_link(link)
        return (_entity_id(entry, f_node), f_sock,
                _entity_id(entry, t_node), t_sock)

    base_links = {link_key(base, link) for link in base.get("links", [])}
    side_links = {link_key(side, link) for link in side.get("links", [])}
    links_added = side_links - base_links
    links_removed = base_links - side_links
    added_targets = {}
    for key in sorted(links_added):
        entity = "link:" + repr(key)
        _touch(entity, "added")
        added_targets.setdefault((key[2], key[3]), set()).add(key)
    for key in sorted(links_removed):
        _touch("link:" + repr(key), "removed")

    return {
        "matches": matches,
        "ambiguous_base": sorted(amb_b),
        "ambiguous_side": sorted(amb_s),
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "changed": changed,
        "field_keys": field_keys,
        "touched": touched,
        "links_added": links_added,
        "links_removed": links_removed,
        "added_targets": added_targets,
    }


def _structure_conflicts(s_base: dict, s_ours: dict, s_theirs: dict,
                         limit: int = 50) -> list:
    """Human-readable per-element report of two divergent structures.

    Only used when BOTH sides changed the structure differently; the two
    sides are compared directly (base is informational).  Deterministic
    ordering; capped so a large restructure cannot flood the report.
    """
    items = []

    def add(msg, b="", o="", t=""):
        items.append({"kind": "structure", "item": msg,
                      "base": b, "ours": o, "theirs": t})

    def presence(present):
        return "present" if present else "absent"

    nb = {n.get("name"): n for n in s_base.get("nodes", [])
          if isinstance(n, dict)}
    no = {n.get("name"): n for n in s_ours.get("nodes", [])
          if isinstance(n, dict)}
    nt = {n.get("name"): n for n in s_theirs.get("nodes", [])
          if isinstance(n, dict)}
    for name in sorted(set(no) | set(nt) | set(nb), key=str):
        in_o, in_t, in_b = name in no, name in nt, name in nb
        if in_o and in_t:
            if no[name] != nt[name]:
                changed = sorted(
                    str(k) for k in set(no[name]) | set(nt[name])
                    if no[name].get(k) != nt[name].get(k))
                add("node '%s' differs (%s)" % (name, ", ".join(changed)),
                    presence(in_b), "present", "present")
        elif in_o != in_t:
            add("node '%s' only in %s" % (name, "ours" if in_o else "theirs"),
                presence(in_b), presence(in_o), presence(in_t))

    def link_keys(structure):
        return {json.dumps(link, sort_keys=True)
                for link in structure.get("links", [])
                if isinstance(link, dict)}

    lo, lt = link_keys(s_ours), link_keys(s_theirs)
    if lo != lt:
        add("links differ (+%d only in ours, +%d only in theirs; "
            "multi-input order included)" % (len(lo - lt), len(lt - lo)))

    def iface_index(structure):
        out = {}
        items_list = structure.get("interface_items")
        if isinstance(items_list, list):
            for occ, item in _iter_occurrences(items_list,
                                               ("name", "in_out",
                                                "item_type")):
                key = (str(item.get("name", "")), str(item.get("in_out", "")),
                       str(item.get("item_type", "")), occ)
                out[key] = item
            return out
        for direction in ("inputs", "outputs"):
            for occ, item in _iter_occurrences(structure.get(direction) or [],
                                               ("name", "type")):
                key = (str(item.get("name", "")), direction,
                       str(item.get("type", "")), occ)
                out[key] = item
        return out

    io, it = iface_index(s_ours), iface_index(s_theirs)
    for key in sorted(set(io) | set(it), key=repr):
        if key in io and key in it:
            if io[key] != it[key]:
                add("interface item '%s' differs" % key[0])
        else:
            add("interface item '%s' only in %s"
                % (key[0], "ours" if key in io else "theirs"))

    handled = {"nodes", "links", "interface_items", "inputs", "outputs",
               "name", "tree_type"}
    for key in sorted(set(s_ours) | set(s_theirs)):
        if key in handled:
            continue
        if s_ours.get(key) != s_theirs.get(key):
            add("tree field '%s' differs" % key)

    if len(items) > limit:
        extra = len(items) - limit
        items = items[:limit]
        items.append({"kind": "structure",
                      "item": "... (%d more differences)" % extra,
                      "base": "", "ours": "", "theirs": ""})
    return items


# ---------------------------------------------------------------------------
# Core: graph merge (S3) - nodes/links per UID, values remapped by identity
# ---------------------------------------------------------------------------

def _graph_identity_ready(entry: dict) -> bool:
    nodes = entry.get("nodes") or []
    if not nodes:
        return False
    uids = _entry_uids(entry)
    names = {n.get("name", "") for n in nodes}
    return names <= set(uids) and len(set(uids.values())) == len(uids)


def _by_uid(entry: dict) -> dict:
    nodes = {n.get("name", ""): n for n in entry.get("nodes", [])}
    out = {}
    for name, uid in _entry_uids(entry).items():
        node = nodes.get(name)
        if node is not None:
            out[uid] = node
    return out


def _node_structure(node: dict) -> dict:
    out = {}
    for key, value in node.items():
        if key in ("name", "label", "location", "parent", "hide",
                   "properties", "custom_properties"):
            continue
        if key in ("inputs", "outputs"):
            sockets = []
            for sock in value or []:
                sockets.append({k: v for k, v in sock.items()
                                if k not in ("default_value", "identifier",
                                             "hide")})
            out[key] = sorted(sockets, key=lambda s: (
                s.get("name", ""), s.get("type", ""),
                s.get("bl_idname", "")))
        else:
            out[key] = value
    return out


def _nodes_equal(entry_a: dict, node_a: dict, entry_b: dict,
                 node_b: dict) -> bool:
    if node_a.get("name") != node_b.get("name"):
        return False
    return _norm_node(node_a, _connected_inputs(entry_a)) == \
        _norm_node(node_b, _connected_inputs(entry_b))


def _merged_node_name(base: dict, ours: dict, theirs: dict):
    bn = base.get("name", "")
    on = ours.get("name", "")
    tn = theirs.get("name", "")
    if on == tn:
        return on, ""
    if on == bn:
        return tn, ""
    if tn == bn:
        return on, ""
    return None, "node/rename-rename"


def _opaque_structure(canonical: dict) -> dict:
    out = {}
    items = canonical.get("interface_items")
    if items is not None:
        out["interface_items"] = [
            {k: v for k, v in item.items() if k != "properties"}
            for item in items if isinstance(item, dict)]
    for key in ("derived_node_groups", "texts", "compositor_scenes"):
        if key in canonical:
            out[key] = canonical[key]
    return out


def _entity_override(overrides, key: str):
    if not isinstance(overrides, dict):
        return None
    entities = overrides.get("entities")
    if not isinstance(entities, dict):
        return None
    decision = entities.get(key)
    if decision in ("ours", "theirs", "base", "delete"):
        return decision
    return None


def _resolve_entity(overrides, key, candidates):
    decision = _entity_override(overrides, key)
    if decision is None:
        return False, None, ""
    chosen = candidates.get(decision)
    if decision == "delete" or chosen is None:
        return True, None, ""
    return True, deepcopy(chosen), chosen.get("name", "")


def _graph_conflict(kind: str, item: str, key: str, *, uid: str = "",
                    base="", ours="", theirs="") -> dict:
    conflict = {"kind": kind, "item": item, "key": key,
                "base": base, "ours": ours, "theirs": theirs}
    if uid:
        conflict["uid"] = uid
    return conflict


def _link_map(entry: dict) -> dict:
    out = {}
    for link in entry.get("links", []):
        from_node, from_sock, to_node, to_sock = _normalize_link(link)
        key = (_entity_id(entry, from_node), from_sock,
               _entity_id(entry, to_node), to_sock)
        out[key] = link
    return out


def _remap_value_key(key, mapping):
    if key[0] in ("label", "nodeprop", "nodecustom", "sockdef"):
        name = mapping.get(key[1])
        if not name:
            return None
        return (key[0], name) + key[2:]
    return key


def _merge_graph_entries(base: dict, ours: dict, theirs: dict,
                         group_name: str, canonical: tuple,
                         overrides: dict | None = None) -> dict | None:
    """Per-entity 3-way merge; requires complete, unique UID maps."""
    if not (_graph_identity_ready(base) and _graph_identity_ready(ours)
            and _graph_identity_ready(theirs)):
        return None

    canon_b, canon_o, canon_t = canonical
    b_by_uid = _by_uid(base)
    o_by_uid = _by_uid(ours)
    t_by_uid = _by_uid(theirs)
    merged_by_uid = {}
    merged_name = {}
    applied = []
    conflicts = []
    notices = []
    failed_uids = set()
    resolved_uids = set()
    target_decisions = {}

    for uid in sorted(set(b_by_uid) | set(o_by_uid) | set(t_by_uid)):
        b = b_by_uid.get(uid)
        o = o_by_uid.get(uid)
        t = t_by_uid.get(uid)
        node = None
        name = ""

        if b is not None and o is not None and t is not None:
            b_struct = _node_structure(b)
            o_struct = _node_structure(o)
            t_struct = _node_structure(t)
            o_changed = not _nodes_equal(base, b, ours, o)
            t_changed = not _nodes_equal(base, b, theirs, t)
            if not o_changed and not t_changed:
                node, name = b, b.get("name", "")
            elif o_changed and not t_changed:
                node, name = o, o.get("name", "")
            elif t_changed and not o_changed:
                node, name = t, t.get("name", "")
            else:
                chosen_name, rename_error = _merged_node_name(b, o, t)
                if rename_error:
                    resolved, chosen_node, chosen_name = _resolve_entity(
                        overrides, "node:" + uid,
                        {"ours": o, "theirs": t, "base": b})
                    if not resolved:
                        conflicts.append(_graph_conflict(
                            rename_error,
                            "node '%s' renamed differently on both sides" %
                            b.get("name", ""),
                            "node:" + uid, uid=uid,
                            base=b.get("name", ""), ours=o.get("name", ""),
                            theirs=t.get("name", "")))
                        failed_uids.add(uid)
                        continue
                    resolved_uids.add(uid)
                    if chosen_node is None:
                        applied.append({"item": "node '%s' removed" %
                                        b.get("name", ""), "from": "chosen",
                                        "value": "(removed)"})
                        failed_uids.add(uid)
                        continue
                    node, name = chosen_node, chosen_name
                else:
                    if o_struct != b_struct and t_struct != b_struct \
                            and o_struct != t_struct:
                        resolved, chosen_node, chosen_name = _resolve_entity(
                            overrides, "field:" + uid + ":structure",
                            {"ours": o, "theirs": t, "base": b})
                        if not resolved:
                            conflicts.append(_graph_conflict(
                                "node/field",
                                "node '%s' changed structurally differently "
                                "on both sides" % b.get("name", ""),
                                "field:" + uid + ":structure", uid=uid,
                                base="structure", ours="structure",
                                theirs="structure"))
                            failed_uids.add(uid)
                            continue
                        resolved_uids.add(uid)
                        if chosen_node is None:
                            applied.append({"item": "node '%s' removed" %
                                            b.get("name", ""),
                                            "from": "chosen",
                                            "value": "(removed)"})
                            failed_uids.add(uid)
                            continue
                        node, name = chosen_node, chosen_name
                    else:
                        if o_struct != b_struct:
                            source = o
                        elif t_struct != b_struct:
                            source = t
                        else:
                            source = o
                        node = deepcopy(source)
                        name = chosen_name
        elif b is not None and o is not None:
            if _nodes_equal(base, b, ours, o):
                applied.append({"item": "node '%s' removed" % b.get("name", ""),
                                "from": "theirs", "value": "(removed)"})
                continue
            resolved, chosen_node, chosen_name = _resolve_entity(
                overrides, "node:" + uid,
                {"ours": o, "theirs": t, "base": b})
            if resolved:
                resolved_uids.add(uid)
                if chosen_node is None:
                    applied.append({"item": "node '%s' removed" %
                                    b.get("name", ""), "from": "chosen",
                                    "value": "(removed)"})
                    failed_uids.add(uid)
                    continue
                node, name = chosen_node, chosen_name
            else:
                conflicts.append(_graph_conflict(
                    "node/delete-edit",
                    "node '%s' deleted on theirs but edited on ours" %
                    b.get("name", ""), "node:" + uid, uid=uid,
                    base=b.get("name", ""), ours=o.get("name", ""),
                    theirs="(removed)"))
                failed_uids.add(uid)
                continue
        elif b is not None and t is not None:
            if _nodes_equal(base, b, theirs, t):
                applied.append({"item": "node '%s' removed" % b.get("name", ""),
                                "from": "ours", "value": "(removed)"})
                continue
            resolved, chosen_node, chosen_name = _resolve_entity(
                overrides, "node:" + uid,
                {"ours": o, "theirs": t, "base": b})
            if resolved:
                resolved_uids.add(uid)
                if chosen_node is None:
                    applied.append({"item": "node '%s' removed" %
                                    b.get("name", ""), "from": "chosen",
                                    "value": "(removed)"})
                    failed_uids.add(uid)
                    continue
                node, name = chosen_node, chosen_name
            else:
                conflicts.append(_graph_conflict(
                    "node/delete-edit",
                    "node '%s' deleted on ours but edited on theirs" %
                    b.get("name", ""), "node:" + uid, uid=uid,
                    base=b.get("name", ""), ours="(removed)",
                    theirs=t.get("name", "")))
                failed_uids.add(uid)
                continue
        elif b is not None:
            applied.append({"item": "node '%s' removed" % b.get("name", ""),
                            "from": "both", "value": "(removed)"})
            continue
        elif o is not None and t is not None:
            if _nodes_equal(ours, o, theirs, t):
                node, name = deepcopy(o), o.get("name", "")
            else:
                resolved, chosen_node, chosen_name = _resolve_entity(
                    overrides, "node:" + uid,
                    {"ours": o, "theirs": t, "base": None})
                if resolved:
                    resolved_uids.add(uid)
                    if chosen_node is None:
                        applied.append({"item": "node '%s' removed" %
                                        o.get("name", ""), "from": "chosen",
                                        "value": "(removed)"})
                        failed_uids.add(uid)
                        continue
                    node, name = chosen_node, chosen_name
                else:
                    conflicts.append(_graph_conflict(
                        "node/field",
                        "node '%s' added on both sides with different content"
                        % o.get("name", ""), "node:" + uid, uid=uid,
                        base="(absent)", ours=o.get("name", ""),
                        theirs=t.get("name", "")))
                    failed_uids.add(uid)
                    continue
        elif o is not None:
            node, name = deepcopy(o), o.get("name", "")
        elif t is not None:
            node, name = deepcopy(t), t.get("name", "")

        if node is None:
            continue
        node = deepcopy(node)
        node["name"] = name
        merged_by_uid[uid] = node
        merged_name[uid] = name

    names = {}
    for uid, name in merged_name.items():
        if name in names:
            if _entity_override(overrides, "node:" + uid) == "delete":
                merged_by_uid.pop(uid, None)
                merged_name.pop(uid, None)
                applied.append({"item": "node '%s' removed" % name,
                                "from": "chosen", "value": "(removed)"})
                failed_uids.add(uid)
                continue
            conflicts.append(_graph_conflict(
                "identity/name",
                "two merged nodes would be named '%s'" % name,
                "node:" + uid, uid=uid,
                base="", ours=name, theirs=""))
            failed_uids.add(uid)
            continue
        names[name] = uid

    merged_nodes = [merged_by_uid[uid] for uid in sorted(
        merged_by_uid, key=lambda u: (merged_name[u], u))]
    node_uids = {merged_name[uid]: uid for uid in sorted(
        merged_by_uid, key=lambda u: (merged_name[u], u))}

    base_links = _link_map(base)
    ours_links = _link_map(ours)
    theirs_links = _link_map(theirs)
    all_links = set(base_links) | set(ours_links) | set(theirs_links)
    kept = {}
    added_o = {k for k in ours_links if k not in base_links}
    added_t = {k for k in theirs_links if k not in base_links}
    removed_o = {k for k in base_links if k not in ours_links}
    removed_t = {k for k in base_links if k not in theirs_links}

    def _targets(keys):
        out = {}
        for key in keys:
            out.setdefault((key[2], key[3]), set()).add(key)
        return out

    added_o_by_target = _targets(added_o)
    added_t_by_target = _targets(added_t)
    removed_o_by_target = _targets(removed_o)
    removed_t_by_target = _targets(removed_t)
    for target in sorted(set(added_o_by_target) | set(added_t_by_target),
                         key=repr):
        decision = _entity_override(overrides, "link:" + repr(target))
        if decision:
            target_decisions[target] = decision
            continue
        o_keys = added_o_by_target.get(target, set())
        t_keys = added_t_by_target.get(target, set())
        if o_keys and t_keys and o_keys != t_keys:
            conflicts.append(_graph_conflict(
                "link/contention",
                "two different links added to the same socket", "link:" +
                repr(target), base="", ours=repr(sorted(o_keys)),
                theirs=repr(sorted(t_keys))))
        if target in removed_o_by_target and target in added_t_by_target:
            conflicts.append(_graph_conflict(
                "link/contention",
                "one side removed the link to a socket the other side "
                "rewired", "link:" + repr(target), base="",
                ours="removed", theirs="added"))
        if target in removed_t_by_target and target in added_o_by_target:
            conflicts.append(_graph_conflict(
                "link/contention",
                "one side removed the link to a socket the other side "
                "rewired", "link:" + repr(target), base="",
                ours="added", theirs="removed"))

    for key in sorted(all_links, key=repr):
        in_b = key in base_links
        in_o = key in ours_links
        in_t = key in theirs_links
        if in_o and in_t:
            kept[key] = ours_links[key]
        elif not in_b and (in_o or in_t):
            kept[key] = ours_links[key] if in_o else theirs_links[key]

    for target, decision in target_decisions.items():
        for key in list(kept):
            if (key[2], key[3]) == target:
                del kept[key]
        source = {"ours": ours_links, "theirs": theirs_links,
                  "base": base_links}.get(decision)
        if source is not None:
            for key, raw in source.items():
                if (key[2], key[3]) == target:
                    kept[key] = raw
        applied.append({"item": "links to '%s.%s'" % (
            merged_name.get(target[0], target[0]), target[1]),
            "from": decision, "value": "(chosen)"})

    for target in sorted({(key[2], key[3]) for key in kept}, key=repr):
        if target in target_decisions:
            continue
        if len([k for k in kept if (k[2], k[3]) == target]) > 1:
            base_count = len([k for k in base_links if (k[2], k[3]) == target])
            if base_count <= 1:
                conflicts.append(_graph_conflict(
                    "link/contention",
                    "more than one link would feed the same socket",
                    "link:" + repr(target)))

    link_order = []
    for key in base_links:
        if key in kept and key not in link_order:
            link_order.append(key)
    for key in ours_links:
        if key in kept and key not in link_order:
            link_order.append(key)
    for key in theirs_links:
        if key in kept and key not in link_order:
            link_order.append(key)
    merged_links = []
    for key in link_order:
        raw = kept[key]
        if key[0] in failed_uids or key[2] in failed_uids:
            continue
        from_name = merged_name.get(key[0])
        to_name = merged_name.get(key[2])
        if not from_name or not to_name:
            conflicts.append(_graph_conflict(
                "link/contention",
                "a link points to a node removed by the merge",
                "link:" + repr(key)))
            continue
        link = deepcopy(raw)
        link["from_node"] = from_name
        link["to_node"] = to_name
        merged_links.append(link)

    opaque_b = _opaque_structure(canon_b)
    opaque_o = _opaque_structure(canon_o)
    opaque_t = _opaque_structure(canon_t)
    opaque_source = None
    if opaque_o != opaque_b and opaque_t != opaque_b:
        if opaque_o != opaque_t:
            conflicts.append(_graph_conflict(
                "structure",
                "interface/derived/texts changed differently on both sides",
                "structure:opaque", base="changed", ours="changed",
                theirs="changed"))
        else:
            opaque_source = ours
    elif opaque_o != opaque_b:
        opaque_source = ours
    elif opaque_t != opaque_b:
        opaque_source = theirs

    merged = deepcopy(base)
    merged["name"] = group_name or base.get("name", "")
    merged["nodes"] = merged_nodes
    merged["links"] = merged_links
    merged["node_uids"] = node_uids
    if opaque_source is not None:
        for key in ("interface_items", "derived_node_groups", "texts",
                    "compositor_scenes"):
            if key in opaque_source:
                merged[key] = deepcopy(opaque_source[key])
            else:
                merged.pop(key, None)

    base_map = {name: merged_name[uid] for name, uid in
                _entry_uids(base).items() if uid in merged_name}
    ours_map = {name: merged_name[uid] for name, uid in
                _entry_uids(ours).items() if uid in merged_name}
    theirs_map = {name: merged_name[uid] for name, uid in
                  _entry_uids(theirs).items() if uid in merged_name}
    merged_name_to_uid = {name: uid for uid, name in merged_name.items()}

    def _values(canonical_entry, mapping):
        out = {}
        for key, value in _extract_values(canonical_entry).items():
            remapped = _remap_value_key(key, mapping)
            if remapped is None:
                continue
            if remapped[0] in ("label", "nodeprop", "nodecustom", "sockdef"):
                uid = merged_name_to_uid.get(remapped[1])
                if uid in resolved_uids:
                    continue
            out[remapped] = value
        return out

    v_b = _values(canon_b, base_map)
    v_o = _values(canon_o, ours_map)
    v_t = _values(canon_t, theirs_map)
    value_overrides = {}
    if isinstance(overrides, dict) and isinstance(overrides.get("values"), dict):
        value_overrides = overrides["values"]

    for key in sorted(set(v_b) | set(v_o) | set(v_t), key=value_key_id):
        b = v_b.get(key, _ABSENT)
        o = v_o.get(key, _ABSENT)
        t = v_t.get(key, _ABSENT)
        decision = value_overrides.get(value_key_id(key))
        manual = decision is not None
        if not manual:
            if o == t:
                if b == o:
                    continue
                winner, winner_side = o, "ours"
            elif b == o:
                winner, winner_side = t, "theirs"
            elif b == t:
                winner, winner_side = o, "ours"
            else:
                _add_value_conflict(conflicts, key, v_t, b, o, t)
                continue
        else:
            resolved, winner, winner_side = _apply_override_choice(
                decision, b, o, t)
            if not resolved:
                _add_value_conflict(conflicts, key, v_t, b, o, t)
                continue
        if winner is _ABSENT:
            if _apply_value(merged, key, _ABSENT):
                applied.append({"item": _item_label(key),
                                "from": winner_side, "value": "(removed)"})
            else:
                _add_value_conflict(conflicts, key, v_t, b, o, t)
        elif _apply_value(merged, key, deepcopy(winner)):
            applied.append({"item": _item_label(key),
                            "from": winner_side, "value": _short(winner)})
        else:
            _add_value_conflict(conflicts, key, v_t, b, o, t)

    if conflicts:
        return _result(group_name, "conflicted", adopted_structure="graph",
                       applied=applied, conflicts=conflicts, notices=notices)
    return _result(group_name, "merged", adopted_structure="graph",
                   merged=merged,
                   merged_hash=canonical_hash_from_json_data(merged),
                   applied=applied, notices=notices)


def merge_group_entries(base: dict, ours: dict, theirs: dict,
                        name: str | None = None,
                        overrides: dict | None = None) -> dict:
    """Decide the 3-way merge of one group entry.

    Returns a result dict with ``status`` one of ``unchanged``, ``merged``,
    ``conflicted``, ``unsupported``; ``merged`` (the new entry) only when
    the merge is clean; ``conflicts``/``applied``/``notices`` for reports.

    ``overrides`` (manual editor, spec 2026-10-17) resolves conflicted
    items: ``{"structure": "ours"|"theirs", "values": {key: choice}}``
    with ``choice`` one of ``"ours"``, ``"theirs"``, ``"base"`` or
    ``{"custom": value}``.  Only keys that were in conflict are honoured;
    anything that still cannot be placed stays an explicit conflict.
    """
    group_name = name or ours.get("name") or base.get("name") \
        or theirs.get("name") or ""

    types = (_tree_type(base), _tree_type(ours), _tree_type(theirs))
    if len(set(types)) > 1:
        return _result(group_name, "conflicted", conflicts=[{
            "kind": "identity",
            "item": "tree type mismatch - identity conflict, never fused",
            "base": _short(types[0]), "ours": _short(types[1]),
            "theirs": _short(types[2])}])

    hash_b = canonical_hash_from_json_data(base)
    hash_o = canonical_hash_from_json_data(ours)
    hash_t = canonical_hash_from_json_data(theirs)
    if hash_b == hash_o == hash_t:
        return _result(group_name, "unchanged", merged=deepcopy(ours),
                       merged_hash=hash_o)

    tree_type = types[0]
    if tree_type not in _MERGE_TREE_TYPES:
        return _result(group_name, "unsupported", notices=[
            "tree type '%s' is not covered by merge "
            "(Geometry Nodes, Shader and Compositor trees only)" % tree_type])

    canon = [canonicalize_node_tree_data(entry)
             for entry in (base, ours, theirs)]
    s_b, s_o, s_t = (_extract_structure(c) for c in canon)
    v_b, v_o, v_t = (_extract_values(c) for c in canon)

    notices = []
    if s_o != s_b or s_t != s_b:
        ready_b = _graph_identity_ready(base)
        ready_o = _graph_identity_ready(ours)
        ready_t = _graph_identity_ready(theirs)
        if ready_o and ready_t and not ready_b:
            notices.append(
                "merge base has no node identities - re-export or commit "
                "the base JSON with this version to enable graph merge")
        elif not (ready_o and ready_t):
            notices.append(
                "one or both JSON sides have no node identities - graph "
                "merge is unavailable; export both sides with this version")

    structure_override = (overrides.get("structure")
                          if isinstance(overrides, dict) else None)
    if structure_override not in ("ours", "theirs") \
            and (s_o != s_b or s_t != s_b):
        graph = _merge_graph_entries(base, ours, theirs, group_name,
                                     tuple(canon), overrides)
        if graph is not None:
            return graph

    manual_structure = False
    if s_o == s_t:
        adopted_side, adopted = "ours", ours
        if s_o != s_b:
            notices.append("structure changed identically on both sides; "
                           "taken from ours")
    elif s_t == s_b:
        adopted_side, adopted = "ours", ours
    elif s_o == s_b:
        adopted_side, adopted = "theirs", theirs
    else:
        structure_choice = (overrides.get("structure")
                            if isinstance(overrides, dict) else None)
        if structure_choice in ("ours", "theirs"):
            adopted_side = structure_choice
            adopted = ours if adopted_side == "ours" else theirs
            manual_structure = True
            notices.append("conflicting structure resolved manually: "
                           "taken from %s" % adopted_side)
        else:
            return _result(group_name, "conflicted",
                           conflicts=_structure_conflicts(s_b, s_o, s_t),
                           notices=notices)

    value_overrides = {}
    if isinstance(overrides, dict):
        raw_values = overrides.get("values")
        if isinstance(raw_values, dict):
            value_overrides = raw_values

    merged = deepcopy(adopted)
    applied = []
    conflicts = []
    dropped = 0
    v_adopted = v_o if adopted_side == "ours" else v_t

    for key in sorted(set(v_o) | set(v_t)):
        b = v_b.get(key, _ABSENT)
        o = v_o.get(key, _ABSENT)
        t = v_t.get(key, _ABSENT)
        if o == t:
            continue
        if manual_structure and key not in v_adopted:
            # the user explicitly kept the other side's structure: values
            # that only lived in the discarded structure go with it
            dropped += 1
            continue
        decision = value_overrides.get(value_key_id(key))
        manual = decision is not None
        if not manual:
            if b == o:
                winner, winner_side = t, "theirs"
            elif b == t:
                winner, winner_side = o, "ours"
            else:
                _add_value_conflict(conflicts, key, v_adopted, b, o, t)
                continue
        else:
            resolved, winner, winner_side = _apply_override_choice(
                decision, b, o, t)
            if not resolved:
                _add_value_conflict(conflicts, key, v_adopted, b, o, t)
                continue
        if not manual and winner_side == adopted_side:
            continue
        if winner is _ABSENT:
            _apply_value(merged, key, _ABSENT)
            applied.append({"item": _item_label(key), "from": winner_side,
                            "value": "(removed)"})
        elif _apply_value(merged, key, deepcopy(winner)):
            applied.append({"item": _item_label(key), "from": winner_side,
                            "value": _short(winner)})
        else:
            _add_value_conflict(conflicts, key, v_adopted, b, o, t)

    if dropped:
        notices.append("%d value(s) of the discarded structure were dropped"
                       % dropped)
    _merge_node_uids(base, ours, theirs, merged)
    if conflicts:
        return _result(group_name, "conflicted", conflicts=conflicts,
                       applied=applied, notices=notices)
    return _result(group_name, "merged", adopted_structure=adopted_side,
                   merged=merged,
                   merged_hash=canonical_hash_from_json_data(merged),
                   applied=applied, notices=notices)


# ---------------------------------------------------------------------------
# Core: a whole {name: entry} map (adds/removals included)
# ---------------------------------------------------------------------------

def _simple(name, status, entry=None, notices=None, conflicts=None):
    return _result(name, status, merged=deepcopy(entry) if entry else None,
                   merged_hash=(canonical_hash_from_json_data(entry)
                                if entry else None),
                   notices=notices, conflicts=conflicts)


# ---------------------------------------------------------------------------
# Exact-content renames (v2): one side removed a group and added it under
# a new name with an identical content fingerprint.  Values decide as
# usual; the pair is only recognized when the match is 1-to-1.
# ---------------------------------------------------------------------------

def _content_fingerprint(entry):
    """Identity-free content hash (the group name is not part of it).

    ``None`` when the entry cannot be canonicalized; callers skip it.
    """
    if not isinstance(entry, dict):
        return None
    try:
        return content_hash_from_json_data(entry)
    except Exception:
        return None


def _rename_pairings(base_groups: dict, ours_groups: dict,
                     theirs_groups: dict) -> dict:
    """``{old_name: [(side, new_name), ...]}`` rename candidates.

    A side *removed* a name when it is present in base and absent there,
    *added* one when the name is absent from base, absent from the other
    side and present there.  A removed and an added candidate pair up
    only when their fingerprints match exactly and the match is 1-to-1 -
    ambiguity never guesses.  Mergeable tree types only (Geometry Nodes,
    Shader and Compositor), mirroring the values-merge scope.  Deterministic:
    per-old entries sort by side.
    """
    pairs = {}
    for side, own, other in (("ours", ours_groups, theirs_groups),
                             ("theirs", theirs_groups, ours_groups)):
        removed = {}
        for name, entry in base_groups.items():
            if name in own or _tree_type(entry) not in _MERGE_TREE_TYPES:
                continue
            fingerprint = _content_fingerprint(entry)
            if fingerprint:
                removed.setdefault(fingerprint, []).append(name)
        added = {}
        for name, entry in own.items():
            if name in base_groups or name in other:
                continue
            if _tree_type(entry) not in _MERGE_TREE_TYPES:
                continue
            fingerprint = _content_fingerprint(entry)
            if fingerprint:
                added.setdefault(fingerprint, []).append(name)
        for fingerprint, olds in removed.items():
            news = added.get(fingerprint) or []
            if len(olds) == 1 and len(news) == 1:
                pairs.setdefault(olds[0], []).append((side, news[0]))
    for old in pairs:
        pairs[old].sort(key=lambda item: item[0])
    return pairs


def _merge_rename_pair(old, side, new, base_groups, ours_groups,
                       theirs_groups):
    """Decide the entry of a rename pair; the result lives under *new*.

    *side* renamed *old* to *new* with identical content; the other side
    modified *old* (values merge normally), left it alone, or removed it
    (the kept rename is reported with a notice).
    """
    ours_entry = ours_groups.get(new if side == "ours" else old)
    theirs_entry = theirs_groups.get(new if side == "theirs" else old)
    if ours_entry is None or theirs_entry is None:
        kept = ours_entry if ours_entry is not None else theirs_entry
        return _result(
            new, "renamed", merged=deepcopy(kept),
            merged_hash=canonical_hash_from_json_data(kept),
            notices=["renamed from '%s' on %s - the other side removed "
                     "the old name; the rename was kept" % (old, side)])
    inner = merge_group_entries(base_groups.get(old), ours_entry,
                                theirs_entry, name=new)
    notices = ["renamed from '%s' on %s" % (old, side)]
    notices.extend(inner.get("notices") or [])
    if inner["status"] == "conflicted":
        return _result(new, "conflicted", applied=inner.get("applied"),
                       conflicts=inner.get("conflicts"), notices=notices)
    return _result(new, "renamed",
                   adopted_structure=inner.get("adopted_structure"),
                   merged=inner.get("merged"),
                   merged_hash=inner.get("merged_hash"),
                   applied=inner.get("applied"), notices=notices)


def pair_renamed_files(deletions: dict, additions: dict) -> dict:
    """``{old_rel: new_rel}`` exact-content renames between two file sets.

    *deletions* / *additions* map repo-relative paths to their parsed
    JSON data (a deletion uses its base content, an addition its new
    content; ``None`` entries are skipped).  A pair forms when both sides
    parse as packages, every ``node_groups`` entry is a mergeable tree
    (Geometry Nodes, Shader or Compositor), the entry-set fingerprints match exactly
    and the match is 1-to-1 on both sides - ambiguity never guesses.
    """
    def fingerprint(data):
        if not isinstance(data, dict):
            return None
        pkg = as_package_dict(data)
        groups = pkg.get("node_groups") if isinstance(pkg, dict) else None
        if not isinstance(groups, dict) or not groups:
            return None
        hashes = []
        for entry in groups.values():
            if _tree_type(entry) not in _MERGE_TREE_TYPES:
                return None
            value = _content_fingerprint(entry)
            if value is None:
                return None
            hashes.append(value)
        return tuple(sorted(hashes))

    olds, news = {}, {}
    for rel, data in deletions.items():
        value = fingerprint(data)
        if value:
            olds.setdefault(value, []).append(rel)
    for rel, data in additions.items():
        value = fingerprint(data)
        if value:
            news.setdefault(value, []).append(rel)
    pairs = {}
    for value, old_rels in olds.items():
        new_rels = news.get(value) or []
        if len(old_rels) == 1 and len(new_rels) == 1:
            pairs[old_rels[0]] = new_rels[0]
    return pairs


def merge_group_maps(base_groups: dict, ours_groups: dict,
                     theirs_groups: dict) -> dict:
    """Decide the 3-way merge of three ``{name: entry}`` maps.

    Exact-content renames (v2) are decided first: a group removed from
    one side and added under a new name on that same side with an
    identical content fingerprint is a rename - the old name reports as
    removed and the new one carries the merged entry with a ``renamed``
    status.  The same group renamed to different names on both sides is a
    ``rename/rename`` conflict.

    Returns ``{"merge_version", "groups", "summary", "ok", "notices"}``;
    ``ok`` is True when no group is conflicted (``unsupported`` groups are
    notices here; the CLI decides whether ``--strict`` turns them into a
    failure).  Nothing is written to disk.
    """
    groups = {}
    pairs = _rename_pairings(base_groups, ours_groups, theirs_groups)
    paired_new = {new for entries in pairs.values()
                  for _side, new in entries}
    for name in sorted(set(base_groups) | set(ours_groups) | set(theirs_groups)):
        if name in paired_new:
            continue
        pairing = pairs.get(name)
        if pairing and len(pairing) > 1:
            digest = _entry_hash(base_groups.get(name)) or ""
            groups[name] = _result(name, "conflicted", conflicts=[{
                "kind": "rename/rename",
                "item": "group renamed to '%s' on ours and to '%s' on "
                        "theirs" % (pairing[0][1], pairing[1][1]),
                "base": digest[:12], "ours": "-> '%s'" % pairing[0][1],
                "theirs": "-> '%s'" % pairing[1][1]}])
            continue
        if pairing:
            side, new = pairing[0]
            groups[name] = _result(
                name, "removed",
                notices=["renamed to '%s' on %s" % (new, side)])
            groups[new] = _merge_rename_pair(
                name, side, new, base_groups, ours_groups, theirs_groups)
            continue

        b = base_groups.get(name)
        o = ours_groups.get(name)
        t = theirs_groups.get(name)

        if b is not None and o is not None and t is not None:
            result = merge_group_entries(b, o, t, name=name)
        elif o is not None and t is not None:
            hash_o = canonical_hash_from_json_data(o)
            hash_t = canonical_hash_from_json_data(t)
            if hash_o == hash_t:
                result = _simple(name, "added", t, notices=[
                    "group added on both sides with identical content"])
            elif _tree_type(o) != _tree_type(t):
                result = _result(name, "conflicted", conflicts=[{
                    "kind": "identity",
                    "item": "tree type mismatch - identity conflict, "
                            "never fused",
                    "base": "(absent)", "ours": _short(_tree_type(o)),
                    "theirs": _short(_tree_type(t))}])
            else:
                result = _result(name, "conflicted", conflicts=[{
                    "kind": "both-added",
                    "item": "group added on both sides with different "
                            "content (no base to attribute changes)",
                    "base": "(absent)", "ours": hash_o[:12],
                    "theirs": hash_t[:12]}])
        elif b is not None and o is not None:
            hash_b = canonical_hash_from_json_data(b)
            if canonical_hash_from_json_data(o) == hash_b:
                result = _simple(name, "removed")
            else:
                result = _result(name, "conflicted", conflicts=[{
                    "kind": "delete/modify",
                    "item": "group removed on theirs while modified on "
                            "ours",
                    "base": hash_b[:12],
                    "ours": canonical_hash_from_json_data(o)[:12],
                    "theirs": "deleted"}])
        elif b is not None and t is not None:
            hash_b = canonical_hash_from_json_data(b)
            if canonical_hash_from_json_data(t) == hash_b:
                result = _simple(name, "removed")
            else:
                result = _result(name, "conflicted", conflicts=[{
                    "kind": "delete/modify",
                    "item": "group removed on ours while modified on "
                            "theirs",
                    "base": hash_b[:12],
                    "ours": "deleted",
                    "theirs": canonical_hash_from_json_data(t)[:12]}])
        elif o is not None:
            result = _simple(name, "added", o)
        elif t is not None:
            result = _simple(name, "added", t)
        else:
            result = _simple(name, "removed")
        groups[name] = result

    summary = {status: 0 for status in
               ("unchanged", "merged", "added", "removed", "renamed",
                "conflicted", "unsupported")}
    for result in groups.values():
        summary[result["status"]] += 1

    report_notices = []
    if summary["added"] and any(
            result["status"] == "removed" and name not in pairs
            for name, result in groups.items()):
        report_notices.append(
            "removed and added groups in the same merge - possible "
            "renames or double materialization (no exact-content match)")

    return {
        "merge_version": MERGE_VERSION,
        "groups": groups,
        "summary": summary,
        "ok": summary["conflicted"] == 0,
        "notices": report_notices,
    }


# ---------------------------------------------------------------------------
# Core: embedded entries (materials/worlds) - tree + owner settings
# ---------------------------------------------------------------------------

def _embedded_merged(kind, name, status, entry, notices=None):
    result = _simple(name, status, entry, notices=notices)
    result["kind"] = kind
    return result


def _embedded_conflict(kind, name, conflict):
    result = _result(name, "conflicted", conflicts=[conflict])
    result["kind"] = kind
    return result


def _embedded_entry(kind, name, eb, eo, et):
    """One material/world entry: tree and owner settings through the core."""
    if eo is not None and et is not None and eb is not None:
        result = merge_group_entries(eb, eo, et, name=name)
        result["kind"] = kind
        if result["status"] != "unsupported":
            return result
        # Non-mergeable tree content: keep the whole-entry adoption rule.
        hb, ho, ht = _entry_hash(eb), _entry_hash(eo), _entry_hash(et)
        if ho == ht:
            return _embedded_merged(kind, name, "unchanged", eo,
                                    notices=result.get("notices"))
        if hb == ht:
            return _embedded_merged(kind, name, "merged", eo,
                                    notices=result.get("notices"))
        if hb == ho:
            return _embedded_merged(kind, name, "merged", et,
                                    notices=result.get("notices"))
        return _embedded_conflict(kind, name, {
            "kind": "value",
            "item": "%s '%s': tree type not covered by merge changed on "
                    "both sides differently" % (kind, name),
            "base": (hb or "(absent)")[:12],
            "ours": (ho or "(absent)")[:12],
            "theirs": (ht or "(absent)")[:12]})

    if eo is not None and et is not None:
        if _same(eo, et):
            return _embedded_merged(kind, name, "added", eo)
        return _embedded_conflict(kind, name, {
            "kind": "both-added",
            "item": "%s '%s' added on both sides with different content"
                    % (kind, name),
            "base": "(absent)", "ours": (_entry_hash(eo) or "")[:12],
            "theirs": (_entry_hash(et) or "")[:12]})

    if eb is not None and eo is None and et is None:
        return _embedded_merged(kind, name, "removed", None)
    if eb is not None and eo is None:
        if _entry_hash(et) == _entry_hash(eb):
            return _embedded_merged(kind, name, "removed", None)
        return _embedded_conflict(kind, name, {
            "kind": "delete/modify",
            "item": "%s '%s' removed on ours while modified on theirs"
                    % (kind, name),
            "base": (_entry_hash(eb) or "")[:12], "ours": "deleted",
            "theirs": (_entry_hash(et) or "")[:12]})
    if eb is not None and et is None:
        if _entry_hash(eo) == _entry_hash(eb):
            return _embedded_merged(kind, name, "removed", None)
        return _embedded_conflict(kind, name, {
            "kind": "delete/modify",
            "item": "%s '%s' removed on theirs while modified on ours"
                    % (kind, name),
            "base": (_entry_hash(eb) or "")[:12],
            "ours": (_entry_hash(eo) or "")[:12], "theirs": "deleted"})

    if eo is not None:
        return _embedded_merged(kind, name, "added", eo)
    if et is not None:
        return _embedded_merged(kind, name, "added", et)
    return _embedded_merged(kind, name, "removed", None)


def merge_embedded_maps(kind: str, base: dict, ours: dict, theirs: dict) -> dict:
    """Decide a ``{name: entry}`` map of materials or worlds, per item.

    ``kind`` is ``"material"`` or ``"world"`` (used in reports).  Each
    entry goes through ``merge_group_entries`` (tree values + structure)
    and ``owner_settings`` merge per property/custom property.  Adds,
    removals, both-added and delete/modify follow the same one-side rules
    as node groups.  Never mutates the inputs.
    """
    base = base if isinstance(base, dict) else {}
    ours = ours if isinstance(ours, dict) else {}
    theirs = theirs if isinstance(theirs, dict) else {}
    entries = {}
    merged = {}
    for name in sorted(set(base) | set(ours) | set(theirs), key=str):
        result = _embedded_entry(kind, name, base.get(name), ours.get(name),
                                 theirs.get(name))
        entries[name] = result
        if result["status"] in ("unchanged", "merged", "added", "renamed"):
            if result.get("merged") is not None:
                merged[name] = result["merged"]
    summary = {status: 0 for status in
               ("unchanged", "merged", "added", "removed", "renamed",
                "conflicted", "unsupported")}
    for result in entries.values():
        summary[result["status"]] += 1
    return {
        "merge_version": MERGE_VERSION,
        "kind": kind,
        "entries": entries,
        "merged": merged,
        "summary": summary,
        "ok": summary["conflicted"] == 0,
        "notices": [],
    }


# ---------------------------------------------------------------------------
# Whole package files (F4: the git diverged-branch flow)
# ---------------------------------------------------------------------------
#
# ``merge_package_dicts`` merges three package dicts the way the git flow
# needs it: node groups through ``merge_group_maps`` (values only), the
# ``modifiers`` task list per task with its ``inputs`` merged per key
# (local modifier overrides ride along), and every other section
# (materials, worlds, unknown fields) per entry with the one-side
# adoption rule.  Anything that cannot be decided is an explicit
# conflict: the caller aborts the whole operation and writes nothing.
#
# Inputs are normalized package dicts (see ``as_package_dict``); the
# legacy standalone shape (``{"nodes": ..., "name": ...}``) is wrapped
# automatically, mirroring the add-on's write-back policy.

def as_package_dict(data):
    """Normalize a JSON file dict to the unified package shape."""
    if (isinstance(data, dict) and "node_groups" not in data
            and "nodes" in data and data.get("name")):
        return {
            "type": "GN_UNIFIED_PACKAGE",
            "node_groups": {data["name"]: data},
            "modifiers": [],
        }
    return data


def _canon_value(value):
    """Float-stable comparison form (mirrors the serializer rounding)."""
    if isinstance(value, float):
        value = round(value, 6)
        return 0.0 if value == 0.0 else value
    if isinstance(value, list):
        return [_canon_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _canon_value(v) for k, v in value.items()}
    return value


def _same(a, b) -> bool:
    if a is _ABSENT or b is _ABSENT:
        return a is b
    try:
        return _canon_value(a) == _canon_value(b)
    except Exception:
        return a == b


def _entry_hash(obj):
    """Canonical hash of a JSON entry, with a tolerant fallback."""
    if obj is None:
        return None
    try:
        return canonical_hash_from_json_data(obj)
    except Exception:
        return json.dumps(_canon_value(obj), sort_keys=True)


def _task_key(task):
    return (str(task.get("object", "")), str(task.get("modifier_name", "")))


def _task_label(key):
    return "modifier '%s' on '%s'" % (key[1], key[0])


def _tasks_equal(a, b) -> bool:
    return _same(a, b)


def _task_structure(task):
    """Everything but ``inputs`` (the mergeable values)."""
    return {k: _canon_value(v) for k, v in task.items() if k != "inputs"}


def _merge_task_inputs(adopted, adopted_side, b, o, t, label, conflicts,
                       applied):
    """Diff3 per input key over the adopted task; values only."""
    v_b = (b or {}).get("inputs") or {}
    v_o = (o or {}).get("inputs") or {}
    v_t = (t or {}).get("inputs") or {}
    inputs = dict(adopted.get("inputs") or {})
    for key in sorted(set(v_o) | set(v_t), key=str):
        bv = v_b.get(key, _ABSENT)
        ov = v_o.get(key, _ABSENT)
        tv = v_t.get(key, _ABSENT)
        if _same(ov, tv):
            continue
        if _same(bv, ov):
            winner, side = tv, "theirs"
        elif _same(bv, tv):
            winner, side = ov, "ours"
        else:
            conflicts.append({
                "kind": "value",
                "item": "%s input '%s'" % (label, key),
                "base": _short(bv), "ours": _short(ov), "theirs": _short(tv)})
            continue
        if side == adopted_side:
            continue
        if winner is _ABSENT:
            inputs.pop(key, None)
            applied.append({"item": "%s input '%s'" % (label, key),
                            "from": side, "value": "(removed)"})
        else:
            inputs[key] = deepcopy(winner)
            applied.append({"item": "%s input '%s'" % (label, key),
                            "from": side, "value": _short(winner)})
    if inputs:
        adopted["inputs"] = inputs
    else:
        adopted.pop("inputs", None)


def _merge_task(b, o, t, label, conflicts, applied, notices):
    """Decide one modifier task present and changed on both sides."""
    s_b, s_o, s_t = (_task_structure(x) for x in (b, o, t))
    if s_o == s_t:
        adopted_side = "ours"
        if s_b != s_o:
            notices.append("%s stack settings changed identically on both "
                           "sides; taken from ours" % label)
    elif s_b == s_o:
        adopted_side = "theirs"
    elif s_b == s_t:
        adopted_side = "ours"
    else:
        conflicts.append({
            "kind": "structure",
            "item": "%s stack settings changed on both sides" % label,
            "base": _short(s_b), "ours": _short(s_o), "theirs": _short(s_t)})
        return None
    task = deepcopy(o if adopted_side == "ours" else t)
    _merge_task_inputs(task, adopted_side, b, o, t, label, conflicts, applied)
    return task


def _merge_modifier_tasks(b_list, o_list, t_list, conflicts, applied, notices):
    bd = {_task_key(task): task for task in b_list if isinstance(task, dict)}
    od = {_task_key(task): task for task in o_list if isinstance(task, dict)}
    td = {_task_key(task): task for task in t_list if isinstance(task, dict)}
    out = []
    for key in sorted(set(bd) | set(od) | set(td), key=repr):
        eb, eo, et = bd.get(key), od.get(key), td.get(key)
        label = _task_label(key)
        if eo is not None and et is not None:
            if _tasks_equal(eo, et):
                out.append(deepcopy(eo))
            elif eb is not None and _tasks_equal(eo, eb):
                out.append(deepcopy(et))
            elif eb is not None and _tasks_equal(et, eb):
                out.append(deepcopy(eo))
            else:
                task = _merge_task(eb, eo, et, label, conflicts, applied,
                                   notices)
                if task is not None:
                    out.append(task)
        elif eo is not None:
            if eb is None:
                out.append(deepcopy(eo))
            elif not _tasks_equal(eo, eb):
                conflicts.append({
                    "kind": "delete/modify",
                    "item": "%s modified on ours while removed on theirs"
                            % label,
                    "base": _short(_task_structure(eb)), "ours": "modified",
                    "theirs": "deleted"})
        elif et is not None:
            if eb is None:
                out.append(deepcopy(et))
            elif not _tasks_equal(et, eb):
                conflicts.append({
                    "kind": "delete/modify",
                    "item": "%s modified on theirs while removed on ours"
                            % label,
                    "base": _short(_task_structure(eb)), "ours": "deleted",
                    "theirs": "modified"})
        # both absent: both removed (or never existed) - nothing to do
    return out


def _merge_named_entries(key, b, o, t, conflicts):
    """Per-entry merge of an unknown ``{name: entry}`` section."""
    b = b if isinstance(b, dict) else {}
    o = o if isinstance(o, dict) else {}
    t = t if isinstance(t, dict) else {}
    merged = {}
    for name in sorted(set(b) | set(o) | set(t), key=str):
        eb, eo, et = b.get(name), o.get(name), t.get(name)
        hb, ho, ht = _entry_hash(eb), _entry_hash(eo), _entry_hash(et)
        if eo is not None and et is not None and _same(eo, et):
            merged[name] = deepcopy(eo)
        elif eb is not None and hb is not None and ht == hb:
            if eo is not None:
                merged[name] = deepcopy(eo)
        elif eb is not None and hb is not None and ho == hb:
            if et is not None:
                merged[name] = deepcopy(et)
        elif eb is None:
            if eo is None:
                merged[name] = deepcopy(et)
            elif et is None:
                merged[name] = deepcopy(eo)
            else:
                conflicts.append({
                    "kind": "both-added",
                    "item": "%s '%s' added on both sides with different "
                            "content" % (key, name),
                    "base": "(absent)", "ours": (ho or "")[:12],
                    "theirs": (ht or "")[:12]})
        elif eo is None:
            conflicts.append({
                "kind": "delete/modify",
                "item": "%s '%s' removed on ours while modified on theirs"
                        % (key, name),
                "base": (hb or "")[:12], "ours": "deleted",
                "theirs": (ht or "")[:12]})
        elif et is None:
            conflicts.append({
                "kind": "delete/modify",
                "item": "%s '%s' removed on theirs while modified on ours"
                        % (key, name),
                "base": (hb or "")[:12], "ours": (ho or "")[:12],
                "theirs": "deleted"})
        elif ho == ht:
            merged[name] = deepcopy(eo)
        else:
            conflicts.append({
                "kind": "value",
                "item": "%s '%s' changed on both sides differently"
                        % (key, name),
                "base": (hb or "")[:12], "ours": (ho or "")[:12],
                "theirs": (ht or "")[:12]})
    return merged


def _merge_top_value(key, b, o, t, conflicts):
    """One top-level field: dict sections per entry, scalars by diff3.

    Returns the merged value or ``_DROP`` when the field must disappear.
    """
    if isinstance(o, dict) and isinstance(t, dict):
        return _merge_named_entries(key, b, o, t, conflicts)
    bv = _ABSENT if b is None else b
    ov = _ABSENT if o is None else o
    tv = _ABSENT if t is None else t
    if _same(ov, tv):
        return deepcopy(o) if o is not None else _DROP
    if b is not None and _same(bv, ov):
        return deepcopy(t) if t is not None else _DROP
    if b is not None and _same(bv, tv):
        return deepcopy(o) if o is not None else _DROP
    conflicts.append({
        "kind": "value", "item": "field '%s' changed on both sides" % key,
        "base": _short(bv), "ours": _short(ov), "theirs": _short(tv)})
    return deepcopy(o) if o is not None else _DROP


def merge_package_dicts(base: dict, ours: dict, theirs: dict) -> dict:
    """Decide the 3-way merge of three whole package dicts.

    Returns ``{"ok", "merged", "groups", "conflicts", "applied",
    "notices", "summary", "embedded", "embedded_summary"}``.  ``merged``
    is the package dict to write only when ``ok`` is True; always
    deterministic and never mutating the inputs.  ``node_groups`` go
    through the per-group core (exact-content renames included),
    ``materials``/``worlds`` per item (tree + owner settings),
    ``modifiers`` per task with per-key input decisions, and every other
    section per entry with the one-side adoption rule; a field that
    cannot be decided is an explicit conflict.
    """
    base = as_package_dict(base) if isinstance(base, dict) else base
    ours = as_package_dict(ours) if isinstance(ours, dict) else ours
    theirs = as_package_dict(theirs) if isinstance(theirs, dict) else theirs
    base = base if isinstance(base, dict) else {}
    ours = ours if isinstance(ours, dict) else {}
    theirs = theirs if isinstance(theirs, dict) else {}

    conflicts = []
    applied = []
    notices = []

    ours_type = ours.get("type")
    theirs_type = theirs.get("type")
    if ours_type and theirs_type and ours_type != theirs_type:
        conflicts.append({
            "kind": "identity",
            "item": "file type mismatch - never fused",
            "base": _short(base.get("type")),
            "ours": _short(ours_type), "theirs": _short(theirs_type)})

    merged = deepcopy(ours)

    b_groups = base.get("node_groups") or {}
    o_groups = ours.get("node_groups") or {}
    t_groups = theirs.get("node_groups") or {}
    groups_report = {}
    if o_groups or t_groups or b_groups:
        report = merge_group_maps(b_groups, o_groups, t_groups)
        groups_report = report["groups"]
        out_groups = {}
        for name in sorted(groups_report, key=str):
            entry_result = groups_report[name]
            status = entry_result["status"]
            if status in ("unchanged", "merged", "added", "renamed"):
                if entry_result.get("merged") is not None:
                    out_groups[name] = entry_result["merged"]
            elif status == "removed":
                continue
            elif status == "unsupported":
                eo = o_groups.get(name)
                et = t_groups.get(name)
                eb = b_groups.get(name)
                hb, ho, ht = _entry_hash(eb), _entry_hash(eo), _entry_hash(et)
                if ho == ht:
                    out_groups[name] = deepcopy(eo)
                elif hb is not None and ht == hb:
                    out_groups[name] = deepcopy(eo)
                elif hb is not None and ho == hb:
                    out_groups[name] = deepcopy(et)
                else:
                    conflicts.append({
                        "kind": "value",
                        "item": "group '%s': tree type not covered by "
                                "merge changed on both sides differently"
                                % name,
                        "base": (hb or "(absent)")[:12],
                        "ours": (ho or "(absent)")[:12],
                        "theirs": (ht or "(absent)")[:12]})
                    notices.append("group '%s' is not a Geometry Nodes, "
                                   "Shader or Compositor tree and merge does "
                                   "not fuse it"
                                   % name)
            else:
                for conflict in entry_result.get("conflicts") or []:
                    conflicts.append(dict(
                        conflict,
                        item="group '%s': %s" % (name, conflict["item"])))
            for item in entry_result.get("applied") or []:
                applied.append(dict(
                    item, item="group '%s': %s" % (name, item["item"])))
        merged["node_groups"] = out_groups

    embedded = {}
    embedded_summary = {status: 0 for status in
                        ("unchanged", "merged", "added", "removed",
                         "renamed", "conflicted", "unsupported")}
    for section, kind in (("materials", "material"), ("worlds", "world")):
        if section not in base and section not in ours \
                and section not in theirs:
            continue
        result = merge_embedded_maps(kind, base.get(section) or {},
                                     ours.get(section) or {},
                                     theirs.get(section) or {})
        embedded[section] = result["entries"]
        merged[section] = result["merged"]
        for name in sorted(result["entries"], key=str):
            entry_result = result["entries"][name]
            for conflict in entry_result.get("conflicts") or []:
                conflicts.append(dict(
                    conflict,
                    item="%s '%s': %s"
                         % (section, name, conflict["item"])))
            for item in entry_result.get("applied") or []:
                applied.append(dict(
                    item, item="%s '%s': %s"
                                % (section, name, item["item"])))
            for note in entry_result.get("notices") or []:
                notices.append("%s '%s': %s" % (section, name, note))
        for status, count in result["summary"].items():
            embedded_summary[status] += count

    if "modifiers" in ours or "modifiers" in theirs or "modifiers" in base:
        merged["modifiers"] = _merge_modifier_tasks(
            base.get("modifiers") or [], ours.get("modifiers") or [],
            theirs.get("modifiers") or [], conflicts, applied, notices)

    for key in sorted(set(base) | set(ours) | set(theirs), key=str):
        if key in ("node_groups", "modifiers", "materials", "worlds", "type"):
            continue
        if key in ("version", "export_method"):
            if key in ours:
                merged[key] = deepcopy(ours[key])
            elif key in theirs:
                merged[key] = deepcopy(theirs[key])
            elif key in base:
                merged[key] = deepcopy(base[key])
            continue
        value = _merge_top_value(key, base.get(key), ours.get(key),
                                 theirs.get(key), conflicts)
        if value is _DROP:
            merged.pop(key, None)
        else:
            merged[key] = value

    summary = {status: 0 for status in
               ("unchanged", "merged", "added", "removed", "renamed",
                "conflicted", "unsupported")}
    for entry_result in groups_report.values():
        summary[entry_result["status"]] += 1

    return {
        "ok": not conflicts,
        "merged": merged if not conflicts else None,
        "groups": groups_report,
        "conflicts": conflicts,
        "applied": applied,
        "notices": notices,
        "summary": summary,
        "embedded": embedded,
        "embedded_summary": embedded_summary,
    }
