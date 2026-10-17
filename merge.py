# -*- coding: utf-8 -*-
"""Semantic 3-way merge of GNToolkit group JSON data (pure Python, no bpy).

Implements the v1 merge semantics (spec 2026-10-16):

* VALUES ONLY: socket defaults, node labels/properties, custom properties
  (node and tree) and tree properties, decided per item with a diff3 table
  over base/ours/theirs.  ABSENT is a value of its own.
* GEOMETRY NODES ONLY: other tree types report ``unsupported`` (a notice,
  not a conflict) so a mixed repository does not fail the preview.
* Anything STRUCTURAL (nodes, links, interface presence, zones, menus,
  inner trees, texts, derived groups, owner settings) is never fused: it
  is adopted whole from the side that changed it, or reported as a
  conflict when both sides changed it differently.
* Single source of truth: decisions compare the canonical forms produced
  by ``hash_utils.canonicalize_node_tree_data``, so every fold, exclusion
  and normalization of the hash algorithm applies here too.  Nothing is
  re-implemented.
* Never lose work silently: an edit that cannot be placed on the adopted
  structure becomes an explicit conflict, never a silent drop.

Preview only: nothing here writes to disk.  ``merge_group_entries``
decides one entry, ``merge_group_maps`` decides a whole name -> entry map
(including groups added/removed on either side).
"""

from __future__ import annotations

from copy import deepcopy
import json

from .audit import content_hash_from_json_data
from .hash_utils import canonical_hash_from_json_data, canonicalize_node_tree_data

MERGE_VERSION = 2

_ABSENT = object()

_DROP = object()

_DEFAULT_TREE_TYPE = "GeometryNodeTree"


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
    return repr(key)


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
    return values


def _extract_structure(canonical: dict) -> dict:
    """Canonical entry with every value field removed."""
    structure = {k: v for k, v in canonical.items()
                 if k not in ("tree_properties", "custom_properties")}
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
# Core: one entry
# ---------------------------------------------------------------------------

def merge_group_entries(base: dict, ours: dict, theirs: dict,
                        name: str | None = None) -> dict:
    """Decide the 3-way merge of one group entry.

    Returns a result dict with ``status`` one of ``unchanged``, ``merged``,
    ``conflicted``, ``unsupported``; ``merged`` (the new entry) only when
    the merge is clean; ``conflicts``/``applied``/``notices`` for reports.
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
    if tree_type != _DEFAULT_TREE_TYPE:
        return _result(group_name, "unsupported", notices=[
            "tree type '%s' is not covered by merge v1 "
            "(Geometry Nodes only)" % tree_type])

    canon = [canonicalize_node_tree_data(entry)
             for entry in (base, ours, theirs)]
    s_b, s_o, s_t = (_extract_structure(c) for c in canon)
    v_b, v_o, v_t = (_extract_values(c) for c in canon)

    notices = []
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
        return _result(group_name, "conflicted",
                       conflicts=_structure_conflicts(s_b, s_o, s_t))

    merged = deepcopy(adopted)
    applied = []
    conflicts = []
    v_adopted = v_o if adopted_side == "ours" else v_t

    for key in sorted(set(v_o) | set(v_t)):
        b = v_b.get(key, _ABSENT)
        o = v_o.get(key, _ABSENT)
        t = v_t.get(key, _ABSENT)
        if o == t:
            continue
        if b == o:
            winner, winner_side = t, "theirs"
        elif b == t:
            winner, winner_side = o, "ours"
        else:
            msg = _item_label(key)
            if key not in v_adopted:
                msg += " - item not present in the adopted structure"
            conflicts.append({"kind": "value", "item": msg,
                              "base": _short(b), "ours": _short(o),
                              "theirs": _short(t)})
            continue
        if winner_side == adopted_side:
            continue
        if winner is _ABSENT:
            _apply_value(merged, key, _ABSENT)
            applied.append({"item": _item_label(key), "from": winner_side,
                            "value": "(removed)"})
        elif _apply_value(merged, key, deepcopy(winner)):
            applied.append({"item": _item_label(key), "from": winner_side,
                            "value": _short(winner)})
        else:
            conflicts.append({
                "kind": "value",
                "item": _item_label(key)
                + " - item not present in the adopted structure",
                "base": _short(b), "ours": _short(o), "theirs": _short(t)})

    if conflicts:
        return _result(group_name, "conflicted", conflicts=conflicts,
                       notices=notices)
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
    ambiguity never guesses.  Geometry Nodes trees only, mirroring the
    values-merge scope.  Deterministic: per-old entries sort by side.
    """
    pairs = {}
    for side, own, other in (("ours", ours_groups, theirs_groups),
                             ("theirs", theirs_groups, ours_groups)):
        removed = {}
        for name, entry in base_groups.items():
            if name in own or _tree_type(entry) != _DEFAULT_TREE_TYPE:
                continue
            fingerprint = _content_fingerprint(entry)
            if fingerprint:
                removed.setdefault(fingerprint, []).append(name)
        added = {}
        for name, entry in own.items():
            if name in base_groups or name in other:
                continue
            if _tree_type(entry) != _DEFAULT_TREE_TYPE:
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
    parse as packages, every ``node_groups`` entry is a Geometry Nodes
    tree, the entry-set fingerprints match exactly and the match is
    1-to-1 on both sides - ambiguity never guesses.
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
            if _tree_type(entry) != _DEFAULT_TREE_TYPE:
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
    """Per-entry merge of a {name: entry} section (materials, worlds)."""
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

    Returns ``{"ok", "merged", "groups", "groups_summary", "conflicts",
    "applied", "notices", "summary"}``.  ``merged`` is the package dict to
    write only when ``ok`` is True; always deterministic and never
    mutating the inputs.      ``node_groups`` go through the per-group core (exact-content renames
    included), ``modifiers`` per task with per-key input decisions, every
    other section per entry with the one-side adoption rule; a field that
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
                        "item": "group '%s': non-Geometry-Node tree changed "
                                "on both sides differently" % name,
                        "base": (hb or "(absent)")[:12],
                        "ours": (ho or "(absent)")[:12],
                        "theirs": (ht or "(absent)")[:12]})
                    notices.append("group '%s' is not a Geometry Nodes tree "
                                   "and merge v1 does not fuse it" % name)
            else:
                for conflict in entry_result.get("conflicts") or []:
                    conflicts.append(dict(
                        conflict,
                        item="group '%s': %s" % (name, conflict["item"])))
            for item in entry_result.get("applied") or []:
                applied.append(dict(
                    item, item="group '%s': %s" % (name, item["item"])))
        merged["node_groups"] = out_groups

    if "modifiers" in ours or "modifiers" in theirs or "modifiers" in base:
        merged["modifiers"] = _merge_modifier_tasks(
            base.get("modifiers") or [], ours.get("modifiers") or [],
            theirs.get("modifiers") or [], conflicts, applied, notices)

    for key in sorted(set(base) | set(ours) | set(theirs), key=str):
        if key in ("node_groups", "modifiers", "type"):
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
    }
