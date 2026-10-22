# Compatibility and version boundaries

## Blender

GNToolkit requires **Blender 4.2 - 5.2 LTS** and is tested on 4.2.1,
5.1.1 and 5.2.0. Everything installs as an extension (Blender 4.2+).

No external programs are required: the collaboration engine is bundled
with the add-on and the JSON side is pure Python. The GitHub build
additionally includes an optional system-Git engine; the platform build
keeps the bundled engine.

## Tree kinds

- Geometry Nodes and Shader node groups.
- Compositor node groups on Blender 5.0+. In 4.2 the scene compositor is
  `Scene.node_tree`, not a node group, so only reusable compositor groups
  are covered there.
- Shader Editor materials and worlds (embedded trees plus curated owner
  settings).
- Texture node trees are never versioned.

## Hash algorithm

The canonical hash ignores cosmetic differences (node positions and
creation order) and is versioned. The current algorithm is
`HASH_VERSION = 23`. The `.gntsync` sidecar records the version that
wrote its baselines; `gnt_check.py` refuses to compare across versions
(exit `3`) instead of reporting false changes. Update the checker and the
add-on together.

## Version boundaries

Representational differences between Blender versions (socket classes
and subtypes, version-gated properties, class-level tree text) are
normalized, so the same group hashes identically when tracked from 4.2
and from 5.x. A node model Blender itself changed is a **real**
difference and is reported:

- the Principled BSDF gained sockets in 5.2;
- some enum values were renamed between versions (for example a shader
  `falloff` value).

Groups without such model changes track noise-free between 5.1 and 5.2,
and a project tracked from 4.2 is recognized as synced when opened in
5.x. Blender itself cannot open 5.x-saved `.blend` files in 4.2; the
useful direction is 4.2 to 5.x.

Measured limits with real corpora:

- 5.1 to 5.2 shader corpus: 20/24 hashes identical; the four differences
  are Blender model changes, not serialization noise.
- 4.2 to 5.x: the real workflow (same `.blend` opened in 5.x, same JSON)
  is recognized as synced. Independent fixtures exported separately can
  differ because Blender reinterprets versioned properties when opening
  older files; that is not a GNToolkit regression.

## Compositor limits

Compositor groups are covered from Blender 5.0. Trees that use **Render
Layers/Cryptomatte** depend on the referenced scene's view layers and
passes; those are not versioned (the scene-owner boundary). A clone into
a file without that scene cannot recreate those pass sockets/links.
Pulling into the project file, where the scene exists, keeps them.
Library-linked compositor groups are skipped by **Track all groups...**;
make them local first if you want to track and pull them.

## Historical engineering notes

The Blender 5.2 port research is kept as a historical engineering note:
[docs/history/port-5.2.md](history/port-5.2.md). It records how the port
was verified and what not to re-investigate; it is not current user
documentation.

The 2026-09 JSON layout design report is also historical:
[docs/history/JSON_LAYOUT_TREE_VS_DAG.md](history/JSON_LAYOUT_TREE_VS_DAG.md).
