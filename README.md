# GNToolkit

**Version control with semantics for Blender node trees.**

Node groups are serialized into deterministic JSON files — the DNA, or
source of truth — while the .blend stays your working cache (RNA).
GNToolkit watches both sides: canonical change detection, per-group sync
statuses, dependency-aware import and a commit/pull/conflict loop that
plugs into Git, all from inside Blender. Geometry Nodes is the first
supported tree type.

The status layer also runs **headless**: the JSON side is pure Python, so
the same canonical hashes the addon uses in Blender can be checked without
opening it — `python gnt_check.py <folder> --baseline <project>.gntsync`
reports every synced/changed/missing group in seconds. Use it in a git
hook, a CI pipeline or a release gate.

![Blender](https://img.shields.io/badge/Blender-4.2%E2%80%935.2-orange)
![License](https://img.shields.io/badge/License-GPL--3.0--or--later-blue)

<!-- Screenshot placeholder: docs/images/sync-panel.png — the GN Tools tab
     with the Sync panel (per-group statuses) and the Collaboration panel. -->

## Why this exists

- `.blend` files are binary: no diffs, no review, no merge. Node groups
  change invisibly between saves, and "which one is the latest?" is a
  guess.
- Snapshot exporters answer *"how do I move a node tree out of / into
  Blender?"* — but they stop there. Nothing tells you whether the JSON
  still matches the .blend, what depends on what, or whose version wins.
- GNToolkit adds the missing layer: a git-style working model for node
  groups, inside Blender.

## What makes it different

1. **Change detection that ignores cosmetic noise.** Canonical SHA-256
   hashes ignore node positions and creation order — moving a node is
   not a change, editing a value is. That is what makes the per-group
   statuses trustworthy: **Synced · Edited Locally · Changed in JSON ·
   Conflict · Missing in Blend · Untracked · JSON File Missing**.
2. **It tracks a project graph, not a snapshot.** Dependencies are
   resolved transitively on export and import (rebuilt dependency-first),
   and the tracked layer covers node groups, modifiers and their use on
   objects — not just isolated trees.
3. **A collaboration loop inside Blender.** Track, commit, pull and
   resolve conflicts per group, with a bundled Git transport that stages
   only the tracked JSONs and never auto-merges.

## Not just another exporter

| | JSON export/import tools | GNToolkit |
|---|---|---|
| Question they answer | "How do I move a node tree out of / into Blender?" | "Is my .blend in sync with the project's JSON — and what changed?" |
| Change detection | None (a snapshot is a snapshot) | Canonical hash per group — cosmetic moves produce no false positives |
| Dependencies | Captured at export time | Tracked graph: transitive closure, dependency-first rebuild, reverse edges |
| Project layer | Node trees | Node trees + modifiers + object usage |
| State | None | Synced / Edited Locally / Changed in JSON / Conflict / Missing / ... |
| Environment | Blender | Blender **and** plain Python — headless checks in CI/hooks |
| Collaboration | Share files | Commit, pull, resolve conflicts, bundled Git transport, per-group review |

Snapshot tools are great at moving and sharing groups; GNToolkit answers
the question that comes after: *is the project versioned, and in sync?*

## Who it's for

- Solo artists and developers with a Geometry Nodes-heavy project who
  want undo, history and reviewable diffs for their node setups.
- Teams that share node-group projects through a git repository —
  JSON diffs are reviewable, the .blend is never merged.
- Anyone building a reusable library of node groups that needs to be
  tracked, documented and shared as plain files.

## Headless checks — no Blender required

The JSON side of GNToolkit is pure Python (no `bpy`), so status checks can
run anywhere a Python interpreter exists:

```bash
# Check a folder export against a project's .gntsync sidecar
python gnt_check.py NodeGroups/ --baseline project.blend.gntsync

# Machine-readable output for CI
python gnt_check.py NodeGroups/ --baseline project.blend.gntsync --json

# Validate that every file parses and hashes (no baseline needed)
python gnt_check.py NodeGroups/ --strict

# Audit the project: dependency impact, duplicate logic, JSON health
python gnt_check.py --impact "My Group" --baseline project.blend.gntsync
python gnt_check.py NodeGroups/ --duplicates
python gnt_check.py NodeGroups/ --baseline project.blend.gntsync --health

# Compare two or more projects by content: shared logic, diverging forks
python gnt_check.py --cross projectA/NodeGroups projectB/NodeGroups
```

Audits answer the project questions that come before a change:
**impact** (reverse dependency graph + transitive closure, plus the objects
using it as a modifier — *what breaks if I touch this group?*), **duplicates** (content fingerprints that
ignore group identity — *is this logic copied under two names?*),
**health** (unreadable or conflicted JSONs, missing files, references to
untracked groups) and **cross** (compare two or more projects — *is this
same group shipped in another project, under any name, and which shared
groups have diverged?*; `--strict` exits non-zero when forks diverge). All
support `--json`.

The same audits are available inside Blender: the **Sync Issues** panel
has an **Audit** section (Run / Clear) that reports duplicates, JSON
health and the dependency impact of the group selected in the Node
Editor.

The checker is the standalone `gnt_check.py` script from this repository
— fetch it at the same tag as your addon (the canonical hasher must
match) and run it with any Python 3.10+ interpreter; no Blender needed.

Exit codes: `0` = all synced, `1` = changes or missing groups, `2` =
errors. The canonical hashes are the same ones the addon computes inside
Blender, so a hook and a `Refresh Status` always agree. The full status
check of a 582-group project runs in ~3 seconds.

### Headless export — no UI

The whole .blend can be exported to a folder export (one JSON per group,
plus `Modifiers/`) without opening Blender's UI:

```bash
blender --background --factory-startup project.blend --python scripts\export_all_json.py -- --out D:\NodeGroups
```

The output is the canonical JSON (the source of truth) — ready for
`gnt_check.py`, git, and `Track from Existing JSON` / `Track Folder…`
inside Blender:

```bash
python gnt_check.py D:\NodeGroups\NodeGroups --strict
```

`--minify` produces compact JSON. `--make-local` first turns every
library-linked tree that participates in the export (geometry groups,
their transitive group-node references and geometry-nodes modifiers)
into a local copy — needed when the groups come from an addon's
`assets.blend`: library-linked groups can be exported but never pulled
back. The conversion is only written to the .blend when `--save` is also
given; without it the source file is left untouched.
`--exclude-library FRAG` (repeatable) keeps linked trees whose library
path contains FRAG out of the export and out of the conversion — use it
for Blender's bundled assets (`datafiles\assets`) or third-party toolsets.

A library spread over several .blend files (each linking the previous
ones) ports to a single versioned project through the JSON: export every
source read-only, then rebuild one master .blend where all groups are
local:

```bash
blender --background --factory-startup "base.blend"    --python scripts\export_all_json.py -- --out D:\lib
blender --background --factory-startup "derived.blend" --python scripts\export_all_json.py -- --out D:\lib
blender --background --factory-startup --python scripts\import_all_json.py -- --in D:\lib --out master.blend
```

Open `master.blend` and use `Track from Existing JSON` / `Track All` to
start versioning it. The JSON folder stays one file per group — the
versioned source of truth; the master .blend is only the working file
GNToolkit syncs against. Neither source file is modified.

The scripts are `scripts/export_all_json.py` and
`scripts/import_all_json.py` from this repository — fetch them at the
same tag as your addon (they are not part of the extension zip) and they
enable the addon automatically when missing (legacy install, extension
module or the repo copy next to the script).

### Reviewing node changes in CI

`docs/CI_TEMPLATE.md` has a copyable CI recipe (pinned to the `v0.2.9` tag;
until that tag exists, pin `v0.2.8` — the template's commands are unchanged
between tags) that runs the status check and the audits on every proposed
change touching `NodeGroups/` or the `.gntsync` sidecar — in the CI service
of your choice. An accidental overwrite, a missing file or an unresolved
merge conflict fails the change before anyone merges. It is part of this
repository — never part of the extension zip.

## Features

### Sync — the state layer

- **DNA/RNA sync**: track any node group against a JSON file; per-group
  statuses (Synced, Edited Locally, Changed in JSON, Conflict, Missing
  in Blend, Untracked, JSON File Missing) tell you exactly what
  diverged.
- **Canonical hashing**: SHA-256 fingerprints that ignore node positions
  and creation order — only real content changes flip a status.
- **Deterministic JSON serialization**: nodes are emitted in name order,
  so renaming/reordering produces minimal, stable git diffs, and
  re-exporting an unchanged group is byte-identical.
- **Sync operations**: Track (start), Commit Modified, Pull from JSON,
  Commit All, Refresh Status.
- **Check on open**: after loading a .blend, a background JSON-side
  check shows a non-intrusive notice when JSON files changed outside
  Blender (toggleable).
- **Commit with Review**: decide per group (Keep JSON / Keep Blend /
  Skip) before committing locally edited groups.
- **Conflict resolution**: keep the .blend or the JSON version with a
  single click.
- **JSON files visibility**: the Sync panel always shows where each
  group is tracked — the active group's JSON (`Active: name →
  file.json`) and a collapsible list of all tracked JSON files with
  group counts, copy-path and Reveal-in-Explorer buttons, and an error
  icon when a file is missing.
- **Concurrency-safe**: lock files prevent two Blender sessions from
  corrupting the same JSON.

### Collaboration with Git

- **Self-contained transport** (no server, no reimplementation — the
  repository stays the authority): the Collaboration panel detects the
  repository behind the tracked JSONs and shows its state. The engine is
  bundled with the add-on; nothing extra needs to be installed.
- **Git Commit…** stages only the tracked JSONs; **Git Sync** is a
  fast-forward pull + push — diverged versions are refused, never
  auto-merged.
- Merge-conflict markers in a JSON are detected and reported; per-group
  and repository history is available. The JSONs are plain files, so the
  repository also works with any external Git tooling — see
  `docs/GIT_COLLAB.md`.

### Import & share

- **JSON package snapshots**: export/import of all Geometry Nodes groups
  and NODES modifier setups to/from a single JSON package, with in-place
  update of existing groups on import.
- **Export active group** with its full dependency chain.
- **Folder workflow**: the per-group folder export (`NodeGroups/` +
  `Modifiers/`) round-trips end to end — track every file of a folder in
  one click (**Track Folder…**), commit changes back to per-group files,
  and recreate a whole folder export with **Import Package/Folder**
  (modifiers applied by default to existing objects with matching names).
- **Selective group import**: pick one group from a JSON package and
  import it plus its missing dependency closure — searchable native
  picker with a plan preview (in-blend / to-import / divergent /
  external refs); existing groups are never overwritten; track-to-JSON
  built in.

### Robustness

- **Robust JSON reading**: UTF-8 with or without BOM; a file saved in a
  different encoding (e.g. ANSI) is reported with a clear "re-save as
  UTF-8" message instead of crashing.
- **External connection preservation**: links from parent groups survive
  reimports.
- **Geometry validation**: generic checks (missing attributes, unlinked
  inputs, type mismatches) — warnings only, never blocks.

## Quick Start

> For a step-by-step walkthrough with the expected behavior at every
> step, see the [Usage Manual](docs/MANUAL.md).

### Version your project in 3 steps

1. **Start tracking** (like `git init` + first commit):
   - **Track All** — every Geometry Nodes group is tracked against one
     master JSON **written from the current .blend** (first commit).
     Each group gets a UUID stored on the node tree. The JSON is
     updated surgically: entries that have no counterpart in the .blend
     (e.g. groups removed locally) are preserved.
   - **Track Folder…** — track every group found in a folder export
     (one file per group); see the folder workflow below.
   - **Track from Existing JSON** — start tracking using an existing
     JSON as the source of truth; the JSON is read only and **NOT
     modified**. Accepts a unified package or a single-group export
     file. Use this after an **Import Package/Folder** when the JSON is
     already authoritative. (Equivalent to `git clone`.)
2. **Work normally** — **Refresh Status** computes the sync state of
   every tracked group; the Sync panel shows what changed.
3. **Commit or Pull** per group:
   - **Commit** (.blend → JSON): save your .blend changes into the JSON.
   - **Pull** (JSON → .blend): overwrite the .blend version with the
     JSON.
   - **Keep JSON / Keep Blend**: resolve a conflict by choosing a side.
   - **Ignore**: hide the issue without changing any data.
   - **Stop Tracking**: remove tracking; the JSON file and the node tree
     are kept.

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

### Folder workflow (per-group files as sync participants)

A project exported with **Use Folder Structure** is a folder of
one-file-per-group JSONs that the sync layer treats like a repository:

1. **Export Package** with folder structure → `NodeGroups/*.json` +
   `Modifiers/*.json`.
2. **Track Folder…** (Sync panel): pick any file inside the export —
   every group found in the folder is tracked against its own file
   (dependency edges recorded; already-tracked groups are skipped).
3. Work normally: **Commit** writes each edited group back to its own
   file (a per-group file becomes a one-group package on the first
   commit — all readers accept both shapes), **Pull** restores them.
4. Recreate the project elsewhere with **Import Package/Folder** (pick
   any file in the folder) — or use **Track from Existing JSON**, which
   also accepts a single-group export file.

### Collaboration with Git

The deterministic JSONs are plain files; the add-on bundles its own
transport for them:

1. Put the exported folder in a Git repository (any Git host, a network
   share, a local bare repo, …).
2. The **Collaboration** panel (GN Tools tab) shows the repository
   state, commits with **Git Commit…** (only the tracked JSONs are
   staged) and synchronizes with **Git Sync** (fast-forward only —
   diverged versions are refused, never auto-merged).
3. Conflict markers in a JSON are detected and reported; resolve them
   with your Git tooling.
4. Full guide:
   [docs/GIT_COLLAB.md](https://github.com/oobma/GNToolkit/blob/main/docs/GIT_COLLAB.md).

### JSON package snapshots (no tracking)

1. In the Node Editor sidebar → **GN Tools**, use:
   - **Export Package** — writes every Geometry Nodes group and modifier
     setup to a single JSON package, or one file per group when **Use
     Folder Structure** is enabled (`NodeGroups/` + `Modifiers/` folders).
   - **Export Active Group** — exports the active group plus its
     dependencies.
   - **Import Package/Folder** — reconstructs all groups (and modifiers)
     from JSON. Pick the package file, or **any file inside a folder
     export** to recreate the whole folder (dependencies resolved
     automatically, dependency-first). With **Update existing groups**
     checked, groups that already exist in the file are rebuilt in place
     from the JSON (modifiers referencing them and external links are
     preserved); unchecked (default), existing groups are left untouched.
     **Apply Modifiers** (on by default) attaches the stored modifiers to
     existing objects with matching names.

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

## Requirements

- Blender 4.2 – 5.2 LTS (tested on 4.2.1, 5.1.1 and 5.2.0)
- No external programs required: the collaboration engine is bundled with
  the add-on, and the JSON side (sync checks, package export/import) is
  pure Python.

## Compatibility

Blender **4.2 – 5.2 LTS** (tested on 4.2.1, 5.1.1 and 5.2.0; the maintained
suites are `tests/smoke_test_5.1.py` — 160 checks —,
`tests/test_52_new_nodes_e2e.py` — 47 checks, 5.2-only — and
`tests/test_42_smoke.py` — 21 checks, run under 4.2). The 5.2 port
details are recorded in [docs/port-5.2.md](docs/port-5.2.md). What the
port required:

- Geometry Nodes modifier inputs/outputs: version-gated between the
  legacy custom properties (`modifier["identifier"]`) and the 5.2 RNA
  path (`modifier.properties.inputs/outputs.<id>`).
- Data-type-driven node sockets (Compare, Random Value, Boolean Math
  NOT, Capture Attribute, Value to String, Subdivision Surface): the
  importer and the canonical hash share an *active-socket* rule so 5.1
  and 5.2 produce identical fingerprints (the rule shipped as
  HASH_VERSION 5; the algorithm is versioned and currently at 8) and projects
  track across versions without noise.

## Installation

**Option 1 — Release zip (recommended):**

1. Go to the [Releases page](https://github.com/oobma/GNToolkit/releases)
   and download the addon zip of the latest release (e.g.
   `GNToolkit-v0.2.9.zip`).
2. In Blender: **Edit → Preferences → Add-ons → Install...**, select the
   downloaded zip (the zip contains the `GNToolkit/` addon folder).
3. Enable **"GNToolkit"** in the list.
4. Open the Node Editor and find the **GN Tools** tab in the sidebar (N).

**Option 2 — from the repository:**

1. Download or clone this repository and zip the `GNToolkit` folder
   (the repository root is the addon folder itself; a source zip
   extracts to a version-suffixed folder such as `GNToolkit-main`,
   which is not a valid add-on module name — use the release asset).
2. In Blender: **Edit → Preferences → Add-ons → Install...**, select the
   `.zip` file.
3. Enable **"GNToolkit"** in the list.
4. Open the Node Editor and find the **GN Tools** tab in the sidebar (N).

## JSON format

Exports are **unified packages**:

```json
{
  "version": "0.2.9",
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

A package is **node logic, not scene data**. It stores node groups and
their NODES modifier setups — which object uses them, where in the stack,
visibility flags and input values — but **not** objects, meshes,
materials or transforms. On import, stored modifiers attach to the
objects that already exist with the same name; geometry never travels
through the package.

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

## Bundled third-party components

The add-on bundles these pure-Python wheels (`wheels/`), installed by
Blender when the add-on is enabled as an extension. Each wheel ships with
its own license file.

| Wheel | License |
|---|---|
| [dulwich](https://www.dulwich.io/) | Apache-2.0 or GPL-2.0-or-later |
| [urllib3](https://urllib3.readthedocs.io/) | MIT |
| [keyring](https://github.com/jaraco/keyring) | MIT |
| [pywin32-ctypes](https://github.com/enthought/pywin32-ctypes) | BSD-3-Clause |
| [jaraco.classes](https://github.com/jaraco/jaraco.classes), [jaraco.context](https://github.com/jaraco/jaraco.context), [jaraco.functools](https://github.com/jaraco/jaraco.functools) | MIT |
| [more-itertools](https://github.com/more-itertools/more-itertools) | MIT |
| [typing_extensions](https://github.com/python/typing_extensions) | PSF-2.0 |
| [backports.tarfile](https://github.com/jaraco/backports.tarfile) | MIT |
| [importlib_metadata](https://github.com/python/importlib_metadata) | Apache-2.0 |
| [zipp](https://github.com/jaraco/zipp) | MIT |

## License

[GPL-3.0-or-later](LICENSE)

Releases up to and including v0.2.4 were published under GPL-3.0-only.