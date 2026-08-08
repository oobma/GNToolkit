# GN DNA/RNA Toolkit

A Blender add-on for **Geometry Nodes JSON batch export/import** and
**DNA/RNA synchronization**: JSON is the DNA (source of truth), the .blend
file is the RNA (working cache).

![Blender](https://img.shields.io/badge/Blender-4.0%E2%80%935.1-orange)
![License](https://img.shields.io/badge/License-GPL--3.0-blue)

## Features

- **JSON package snapshots**: export/import of all Geometry Nodes groups
  and NODES modifier setups to/from a single JSON package, with in-place
  update of existing groups on import.
- **Export active group** with its full dependency chain.
- **DNA/RNA sync**: track any node group against a JSON file and detect
  changes on both sides with canonical hashing.
- **Status detection** per group: Synced, Edited Locally, Changed in JSON,
  Conflict, Missing in Blend, Untracked, JSON File Missing.
- **Sync operations**: Track (start), Commit Modified, Pull from JSON,
  Commit All, Refresh Status.
- **JSON remotes visibility**: the Sync panel always shows where each group
  is tracked — the active group's JSON (`Active: name → file.json`) and a
  list of all tracked JSON files with group counts, copy-path and
  Reveal-in-Explorer buttons, and an error icon when a file is missing.
- **Conflict resolution**: keep the .blend or the JSON version
  with a single click.
- **External connection preservation**: links from parent groups survive
  reimports.
- **Geometry validation**: generic checks (missing attributes, unlinked
  inputs, type mismatches) — warnings only, never blocks.
- **Concurrency-safe**: lock files prevent two Blender sessions from
  corrupting the same JSON.

## Requirements

- Blender 4.0 – 5.1.x (tested up to 5.1.1)
- No external dependencies

## Compatibility

A port to **Blender 5.2 LTS** is planned. Known 5.2 API changes that affect
this addon:

- Geometry Nodes modifier inputs/outputs moved from custom properties
  (`modifier["identifier"]`) to real RNA properties
  (`modifier.properties.inputs/outputs.<id>`), which will require a
  version-gated rewrite of the modifier export/import path.
- The socket identifiers of the **Compare** and **Random Value** nodes
  changed; the importer's name+type fallback already tolerates this.

## Installation

1. Download or clone this repository and zip the folder.
2. In Blender: **Edit → Preferences → Add-ons → Install...**, select the
   `.zip` file.
3. Enable **"GN DNA/RNA Toolkit"** in the list.
4. Open the Node Editor and find the **GN Tools** tab in the sidebar (N).

## Quick Start

### JSON package snapshots (no tracking)

1. In the Node Editor sidebar → **GN Tools**, use:
   - **Export Package** — writes every Geometry Nodes group and modifier
     setup to a single JSON package (or a folder structure).
   - **Export Active Group** — exports the active group plus its
     dependencies.
   - **Import Package** — reconstructs all groups (and modifiers) from
     JSON. With **Update existing groups** checked, groups that already
     exist in the file are rebuilt in place from the JSON (modifiers
     referencing them and external links are preserved); unchecked
     (default), existing groups are left untouched.

### Sync (git-style vocabulary)

The sync layer tracks groups between the JSON (source of truth) and the
.blend, using git-style actions: **Track** (start), **Commit**
(.blend → JSON), **Pull** (JSON → .blend).

1. Start tracking:
   - **Track All** — every Geometry Nodes group is tracked against one
     master JSON **written from the current .blend** (first commit). Each
     group gets a UUID stored on the node tree. The JSON is updated
     surgically: entries that have no counterpart in the .blend
     (e.g. groups removed locally) are preserved.
   - **Track from Existing JSON** — start tracking using an existing JSON
     as the source of truth; the JSON is read only and **NOT modified**.
     Use this after an **Import Package** when the JSON is already
     authoritative. (Equivalent to `git clone`.)
2. **Refresh Status** — computes the sync state of every tracked group.
3. Resolve issues with the per-group buttons:
   - **Commit** (.blend → JSON): save your .blend changes into the JSON.
   - **Pull** (JSON → .blend): overwrite the .blend version with the JSON.
   - **Keep JSON / Keep Blend**: resolve a conflict by choosing a side.
   - **Ignore**: hide the issue without changing any data.
   - **Stop Tracking**: remove tracking; the JSON file and the node tree
     are kept.

The Sync panel keeps the linked files visible: the top line shows the
active group's JSON (**Active: name → file.json**), and a **JSON remotes**
list below the actions shows every tracked JSON file (full resolved path,
group count, copy-path and Reveal-in-Explorer buttons; an error icon marks
files missing from disk).

Sync metadata is stored in a sidecar file next to the .blend
(`<project>.blend.gntsync`) and saved automatically on file save.

## How it works

| Term | Meaning |
|---|---|
| **DNA** | The JSON file. The source of truth — readable, diffable, versionable with git. |
| **RNA** | The .blend working cache where you edit and preview. |
| **UUID** | A unique ID stored as a custom property on each tracked node tree. |
| **Canonical hash** | A SHA-256 fingerprint of a group's content, ignoring cosmetic differences (node positions, creation order), so only real changes trigger a status change. |
| **Sidecar** | The `.gntsync` file that records which groups are tracked, their UUIDs, and their last known hashes. |

Each status is computed by comparing the JSON hash against the stored hash
and the .blend hash against the stored hash:

| Status | Meaning | Typical action |
|---|---|---|
| Synced | Both sides match | — |
| Edited Locally | Only the .blend changed | Commit |
| Changed in JSON | Only the JSON changed | Pull |
| Conflict | Both sides changed | Keep JSON or Keep Blend |
| Missing in Blend | The node tree no longer exists in the .blend | Restore from JSON or Stop Tracking |
| Untracked | Not tracked against any JSON | Track |
| JSON File Missing | The JSON file is gone | Re-create JSON or Stop Tracking |

## JSON format

Exports are **unified packages**:

```json
{
  "version": "0.2.0",
  "type": "GN_UNIFIED_PACKAGE",
  "export_method": "GN_TOOLKIT",
  "node_groups": {
    "group_name": { "name": "...", "nodes": [...], "links": [...], ... }
  },
  "modifiers": []
}
```

Each node group entry contains its interface, nodes (with socket defaults),
links and tree properties — enough to fully reconstruct the group.

## Project structure

| File | Role |
|---|---|
| `serializer.py` | Export: node trees → JSON-safe dictionaries |
| `importer.py` | Import: JSON → node trees (recursive reconstruction) |
| `codec.py` | Value conversion between Blender and JSON |
| `hash_utils.py` | Canonical SHA-256 hashing |
| `sync_manager.py` | State detection, batch operations, lock handling |
| `sync_metadata.py` | Sidecar file and UUID tracking |
| `sync_operators.py` | Sync operators (track, commit, pull, resolve, ...) |
| `sync_ui.py` | Sidebar panels |
| `geometry_validator.py` | Generic geometry issue detection |
| `operators.py` | JSON package export/import operators and main panel |

## Contributing

1. Fork the repository and clone your fork.
2. Create a branch for your change.
3. Commit with clear messages (Conventional Commits style:
   `fix:`, `feat:`, `refactor:`, `perf:`, `docs:`).
4. Open a pull request — the base branch is `main`.

## License

[GPL-3.0](LICENSE)
