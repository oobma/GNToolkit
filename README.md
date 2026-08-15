# GNToolkit

A Blender add-on for **Geometry Nodes JSON batch export/import** and
**DNA/RNA synchronization**: JSON is the DNA (source of truth), the .blend
file is the RNA (working cache).

![Blender](https://img.shields.io/badge/Blender-4.0%E2%80%935.2-orange)
![License](https://img.shields.io/badge/License-GPL--3.0-blue)

## Features

- **JSON package snapshots**: export/import of all Geometry Nodes groups
  and NODES modifier setups to/from a single JSON package, with in-place
  update of existing groups on import.
- **Export active group** with its full dependency chain.
- **Deterministic JSON serialization**: nodes are emitted in name order,
  so renaming/reordering produces minimal, stable git diffs, and
  re-exporting an unchanged group is byte-identical.
- **DNA/RNA sync**: track any node group against a JSON file and detect
  changes on both sides with canonical hashing.
- **Check on open**: after loading a .blend, a background JSON-side check
  shows a non-intrusive notice when JSON files changed outside Blender
  (toggleable).
- **Status detection** per group: Synced, Edited Locally, Changed in JSON,
  Conflict, Missing in Blend, Untracked, JSON File Missing.
- **Sync operations**: Track (start), Commit Modified, Pull from JSON,
  Commit All, Refresh Status.
- **Selective group import**: pick one group from a JSON package and
  import it plus its missing dependency closure — searchable native
  picker with a plan preview (in-blend / to-import / divergent /
  external refs); existing groups are never overwritten; track-to-JSON
  built in.
- **Commit with Review**: decide per group (Keep JSON / Keep Blend /
  Skip) before committing locally edited groups.
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

- Blender 4.0 – 5.2 LTS (tested on 5.1.1 and 5.2.0)
- No external dependencies

## Compatibility

Blender **4.0 – 5.2 LTS** (tested on 5.1.1 and 5.2.0; the maintained
suites are `tests/smoke_test_5.1.py` — 99 checks — and
`tests/test_52_new_nodes_e2e.py` — 40 checks, 5.2-only). The 5.2 port
details are recorded in [docs/port-5.2.md](docs/port-5.2.md). What the
port required:

- Geometry Nodes modifier inputs/outputs: version-gated between the
  legacy custom properties (`modifier["identifier"]`) and the 5.2 RNA
  path (`modifier.properties.inputs/outputs.<id>`).
- Data-type-driven node sockets (Compare, Random Value, Boolean Math
  NOT, Capture Attribute, Value to String, Subdivision Surface): the
  importer and the canonical hash share an *active-socket* rule so 5.1
  and 5.2 produce identical fingerprints (HASH_VERSION 5) and projects
  track across versions without noise.

## Installation

**Option 1 — GitHub Releases (recommended):**

1. Go to the [Releases page](https://github.com/oobma/GNToolkit/releases)
   and download the addon zip of the latest release (e.g.
   `GNToolkit-v0.2.3.zip`).
2. In Blender: **Edit → Preferences → Add-ons → Install...**, select the
   downloaded zip (the zip contains the `ADNRNAGNTOOLKIT/` addon folder).
3. Enable **"GNToolkit"** in the list.
4. Open the Node Editor and find the **GN Tools** tab in the sidebar (N).

**Option 2 — from the repository:**

1. Download or clone this repository and zip the `ADNRNAGNTOOLKIT` folder
   (the repository root is the addon folder itself; the GitHub source zip
   also installs, but it creates a version-named folder that leaves
   duplicates when upgrading — the release asset avoids this).
2. In Blender: **Edit → Preferences → Add-ons → Install...**, select the
   `.zip` file.
3. Enable **"GNToolkit"** in the list.
4. Open the Node Editor and find the **GN Tools** tab in the sidebar (N).

## Quick Start

> For a step-by-step walkthrough with the expected behavior at every
> step, see the [Usage Manual](docs/MANUAL.md).

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

### Selective import (one group + its dependencies)

**Import Group from JSON…** picks a package and opens a searchable picker
in the Sync panel:

1. Use the search field (native Blender search widget) to find a group;
   the plan preview under the field shows what will happen:
   - **CHECKMARK** — already in the .blend (never touched),
   - **NODETREE** — will be imported (missing dependencies included),
   - **ERROR** — exists in the .blend but differs from the package
     (align it with Track + Pull / Keep JSON),
   - **STATUS_WARNING** — referenced but not in this package.
2. **Import** imports the group plus its missing dependency closure.
   Existing groups are never overwritten; the picker stays open to
   import several groups in a row.
3. **Track to JSON…** writes the selected group (and its untracked
   dependencies) into a package of your choice and starts tracking it —
   no active-tree dependency.
4. **Close Picker** closes it. A live refresh redraws the picker every
   0.5s, so JSON edits on disk show up within a second.

### Commit with Review

**Commit with Review…** lists every locally edited group (conflicts
default to **Skip**) and applies your per-group decision:

- **Keep Blend** — commit the .blend version into the JSON,
- **Keep JSON** — pull the JSON version into the .blend,
- **Skip** — leave the group untouched.

### Check on open

With **check_on_load** enabled (plug icon next to Refresh Status), after
opening a .blend the addon compares the JSON hashes in the background
and shows a non-intrusive notice when files changed outside Blender —
status bar message, a warning row in the Sync panel and entries in the
Sync Issues list. Use **Pull** on those groups to apply the JSON changes.

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
  "version": "0.2.2",
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
| `constants.py` | Versions, shared constants and skip-lists |
| `serializer.py` | Export: node trees → JSON-safe dictionaries |
| `importer.py` | Import: JSON → node trees (recursive reconstruction) |
| `codec.py` | Value conversion between Blender and JSON |
| `hash_utils.py` | Canonical SHA-256 hashing |
| `error_tracker.py` | Import error/warning accounting |
| `socket_utils.py` | Robust socket lookup and dependency graph |
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
