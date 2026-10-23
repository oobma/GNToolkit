# GNToolkit

**Version your node groups like code - inside Blender.**

GNToolkit turns node groups into deterministic JSON: the reviewable
source of truth, while the `.blend` stays your working file. See what
changed, save or pull per entry, browse history and resolve conflicts
without leaving Blender. Geometry Nodes, Shader and Compositor groups,
plus the Shader Editor's materials and worlds, are supported.

![Blender](https://img.shields.io/badge/Blender-4.2%E2%80%935.2-orange)
![License](https://img.shields.io/badge/License-GPL--3.0--or--later-blue)
![pure tests](https://github.com/oobma/GNToolkit/actions/workflows/pure-tests.yml/badge.svg)

<sub>The latest release is
[v0.4.3](https://github.com/oobma/GNToolkit/releases).</sub>

![The GNToolkit sidebar in Blender's Node Editor - Sync, Track, Explore,
Git and Advanced](docs/images/sync-panel.png)

## Who it's for

- **Solo artists and TAs** with a Geometry Nodes- or shader-heavy project
  who want history and reviewable diffs for their node setups.
- **Teams** that share node-group projects through Git - JSON diffs are
  reviewable, the `.blend` is never merged.
- **Anyone building a reusable library** of node groups that needs to be
  tracked, documented and shared as plain files.

If you build pipelines, jump to [Automation and CI](#automation-and-ci);
the technical reference lives in [docs/AUTOMATION.md](docs/AUTOMATION.md).

## Install

Every install is an extension (Blender 4.2+):

1. **From the platform (recommended):** in Blender, **Edit >
   Preferences > Get Extensions**, search for **GNToolkit** and press
   **Install**. It is enabled automatically.
2. **Offline / manual:** download the **`GNToolkit-<version>-github.zip`**
   asset from the [Releases page](https://github.com/oobma/GNToolkit/releases),
   then **Edit > Preferences > Extensions > ⌄ > Install from Disk...**.

Open the Node Editor and find the **GNToolkit** tab in the sidebar (`N`).

The platform box bundles the collaboration engine. The GitHub box is the
same extension plus an optional system-Git engine. Upgrading from a
pre-0.4.0 manual install? Remove the old `scripts/addons/GNToolkit`
folder - the extension and the old layout cannot coexist.

## Your first project in 3 steps

1. **Track.** Save the `.blend`, then **Track > Track all groups...** and
   choose where the master JSON will live. Every trackable node group
   gets a UUID and is written into that JSON.
   - Already have a folder export? Use **Track from folder...**.
   - Already have a package? Use **Track from Existing JSON** (read
     only).
   - Materials, worlds and the scene compositor have their own **Track**
     buttons in the **Track** panel.
2. **Work normally.** Edit in the `.blend`, then press **Refresh
   Status**. The **Changes** list shows what diverged, with a 50-row
   default cap and **Show all**.
3. **Save or pull.** Use **Save selected (N)** / **Save changed (N)** to
   write the `.blend` edits into the JSON, or **Pull from JSON** to bring
   JSON changes into the `.blend`. Conflicts offer **Keep JSON**, **Keep
   Blend** or **Merge**.

The sidecar `<project>.blend.gntsync` records tracking state; it is
saved automatically with the project.

### What the statuses mean

| Status | Meaning | Typical action |
|---|---|---|
| **Synced** | Both sides match. | - |
| **Edited locally** | Only the `.blend` changed. | Save to JSON |
| **Changed in JSON** | Only the JSON changed. | Pull from JSON |
| **Conflict** | Both sides changed. | Merge, Keep JSON or Keep Blend |
| **Missing in blend** | The entry no longer exists in the `.blend`. | Restore or stop tracking |
| **Untracked** | Not tracked against any JSON. | Track |
| **JSON missing** | The JSON file is gone. | Re-create it or stop tracking |

## What you get

- **Change detection that ignores cosmetic noise.** Canonical SHA-256
  hashes ignore node positions and creation order; moving a node is not
  a change, editing a value is.
- **One Changes list, VS Code style.** Mark entries, save exactly the
  marked subset, filter by status, discard local edits, pull, open
  history or ignore. Untracked dependencies get their own **Track** row.
- **Conflict resolution per item.** Keep one side, or merge values with
  an Ours / Base / JSON choice. With node identities (current exports),
  disjoint structural edits also merge per node/link, and a real
  collision is resolved per entity (Ours / Base / JSON / Delete).
  Entries from older exports keep the whole-side rule. **Apply Chosen
  Merge** writes and rebuilds once every item is decided.
- **History (time machine).** Browse an entry's JSON revisions, preview
  the diff and restore a revision. Only the JSON is touched; Pull
  rebuilds the `.blend` when you decide.
- **Explore and Project Health.** Draw the project graph, a cluster
  view or a neighborhood as a disposable node tree; export an SVG poster;
  run audits (duplicates, JSON health, impact). The board warns when it
  is stale and offers **Rebuild**.
- **Import and share.** Single packages or one file per entry, category
  filters, dependency-first rebuild, **Track from folder...**, selective
  import with a plan preview (existing entries are never overwritten), and
  NODES modifier setups attached to objects by name.
- **Materials, worlds and the scene compositor.** Owner-aware entries
  with per-entry status, save, pull, conflict and merge; curated owner
  settings and custom properties travel with the tree. Object material
  slots are not versioned.
- **Git collaboration inside Blender.** Commit only the tracked JSONs,
  sync with the built-in Git panel, and let a diverged branch merge the
  tracked files per item into a real two-parent merge commit. Credentials
  and engine details: [GIT_COLLAB.md](docs/GIT_COLLAB.md).
- **Automation-ready.** The JSON side runs without Blender:
  `gnt_check.py` checks status, diffs and merge previews in CI or a hook.
  See [Automation and CI](#automation-and-ci).

### Not just another exporter

| | Snapshot exporters | GNToolkit |
|---|---|---|
| Question they answer | "How do I move a node tree in or out of Blender?" | "Is my `.blend` in sync with the project's JSON - and what changed?" |
| Change detection | None | Canonical hash per entry, cosmetic moves ignored |
| Dependencies | Captured at export | Tracked graph: transitive closure, dependency-first rebuild |
| State | None | Per-entry statuses and a Changes list |
| Environment | Blender | Blender **and** plain Python for CI/hooks |
| Git | Share files | Review, save/pull, per-item merge, bundled transport |

## Requirements and compatibility

- **Blender 4.2 - 5.2 LTS** (tested on 4.2.1, 5.1.1 and 5.2.0).
- No external programs required: the collaboration engine is bundled and
  the JSON side is pure Python.
- Version boundaries, hash rules, compositor limits and historical port
  notes: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).
- Format and limits: [docs/JSON_FORMAT.md](docs/JSON_FORMAT.md).

## Documentation

| If you want to... | Read |
|---|---|
| Learn every workflow | [User guide](docs/MANUAL.md) |
| Collaborate with Git | [Git collaboration](docs/GIT_COLLAB.md) |
| Run checks in CI or automate | [Automation and CI](docs/AUTOMATION.md) |
| Copy a CI recipe | [CI template](docs/CI_TEMPLATE.md) |
| Understand the JSON | [JSON format](docs/JSON_FORMAT.md) |
| Check Blender/hash compatibility | [Compatibility](docs/COMPATIBILITY.md) |
| Build, test or contribute | [CONTRIBUTING.md](CONTRIBUTING.md) |
| See a real project diff | [Case study](docs/case-studies/subd-to-compound/README.md) |
| Browse all docs | [docs/](docs/README.md) |

## Automation and CI

The JSON side is pure Python (no `bpy`), so the canonical hashes can be
checked anywhere Python 3.10+ exists:

```bash
# Check a folder export against a project's sidecar
python gnt_check.py NodeGroups/ --baseline project.blend.gntsync

# Validate that every file parses and hashes (no baseline needed)
python gnt_check.py NodeGroups/ --strict
```

`gnt_check.py` and `scripts/` are repository tools, not part of the
extension package; fetch them at the same tag as the add-on. The full
command reference (audits, cross-project, cross-version, diff, merge
preview, render, headless export/import, build and the public test
suite) is in [docs/AUTOMATION.md](docs/AUTOMATION.md). The copyable CI
recipe is in [docs/CI_TEMPLATE.md](docs/CI_TEMPLATE.md).

## Contributing

The public, Blender-free suite runs anywhere Python 3.10+ exists:

```bash
python pure-tests/run_all.py
```

Build, test, code style, project layout and documentation rules:
[CONTRIBUTING.md](CONTRIBUTING.md).

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
