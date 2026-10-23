# JSON format

The JSON is the source of truth; the `.blend` is the working cache.
Exports are deterministic: nodes are emitted in name order and
re-exporting an unchanged group is byte-identical, so git diffs stay
minimal and reviewable.

## Unified package

A single-file export is a unified package:

```json
{
  "version": "<addon version>",
  "type": "GN_UNIFIED_PACKAGE",
  "export_method": "GN_TOOLKIT",
  "node_groups": {
    "group_name": {
      "name": "...",
      "nodes": [],
      "links": [],
      "...": "..."
    }
  },
  "modifiers": [],
  "materials": {},
  "worlds": {}
}
```

`version` is written from the add-on's current `ADDON_VERSION`. Treat it
as an informational marker, not as an input to the canonical hash.

A folder export uses the same entry shapes, one file per entry:

```
NodeGroups/<group>.json
Modifiers/<object>.json
Materials/<material>.json
Worlds/<world>.json
```

A per-group file is accepted everywhere a package is accepted. On the
first save, a per-group file becomes a one-group package; both shapes are
interchangeable for every reader.

## Node groups

Each `node_groups` entry carries the interface (items and default
values), nodes (with socket defaults), links and tree properties - enough
to fully reconstruct the group.

- Geometry Nodes entries omit the `tree_type` marker; absent means
  `GeometryNodeTree`, so existing exports are unchanged.
- Shader and Compositor entries carry `tree_type`.
- Compositor entries (Blender 5.0+) can also carry an informational
  `compositor_scenes` list (the scenes that use the group). It is not
  part of the canonical hash and drives non-destructive scene assignment
  on import.
- References between groups are stored by name (`node_tree_reference`);
  a package never embeds the referenced group's content.
- Entries may carry a `node_uids` map (node name -> stable id). It is
  additive, outside the canonical hash, and ignored by older readers;
  the id survives renames because it also lives on the node as the
  bookkeeping `gnt_node_uid` custom property. Nodes without a persisted
  id get a deterministic UUIDv5 at export, so exports stay byte-identical
  across processes.

## Materials and worlds

Tracked materials and worlds live in their own sections (`materials`,
`worlds`) because their node trees live inside the datablock, not as
named node groups.

Each entry carries the owner's name, its node tree, and an
`owner_settings` block with the curated datablock properties and custom
properties that GNToolkit versions. The rest of the datablock and object
material slots are not versioned.

## Modifiers

`modifiers` stores the NODES modifier setups that use the exported
groups:

- object name,
- position in the stack (`order`),
- viewport/render visibility flags,
- input values and per-object overrides.

Geometry never travels through the package. On import, stored modifiers
attach to existing objects with matching names.

## What is not stored

A package is **node logic, not scene data**. It does not store objects,
meshes, transforms or object material slots. On a fresh file, the groups
are recreated but there are no objects for the stored modifiers to attach
to.

Declared limits (see the manual for details):

- Texture node trees are never versioned.
- Animation (keyframes/drivers/NLA) is not saved; animated entries are
  flagged instead.
- Third-party custom group nodes keep their per-instance inner tree
  embedded with the node; the inner tree is not a standalone tracked
  entry.
- OSL Script nodes version their code in `texts`; `External` mode is
  reference-only.
- Compositor trees with Render Layers/Cryptomatte depend on the scene's
  view layers and passes, which are not versioned.

## Canonical hash

The canonical hash ignores node positions and creation order; moving a
node is not a change, editing a value is. The hash algorithm is versioned
(`HASH_VERSION`): the `.gntsync` sidecar records the version that wrote
its baselines, and `gnt_check.py` refuses to compare across versions
instead of reporting false changes.

See [COMPATIBILITY.md](COMPATIBILITY.md) for version boundaries and
[CI_TEMPLATE.md](CI_TEMPLATE.md) for checking the JSON side in CI.
