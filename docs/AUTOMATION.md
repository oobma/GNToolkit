# Automation, headless checks and CI

The JSON side of GNToolkit is pure Python (no `bpy`), so the same
canonical hashes the add-on computes inside Blender can be checked
without opening it. This page covers the repository tools: `gnt_check.py`,
the headless export/import scripts, the release build and the public test
suite.

`gnt_check.py` and `scripts/` are GitHub repository tools - they are not
part of the extension package. Fetch them at the same tag as your add-on:
the canonical hasher must match the one that wrote the `.gntsync` sidecar.

## gnt_check.py

Run it with any Python 3.10+ interpreter:

```bash
# Check a folder export against a project's .gntsync sidecar
python gnt_check.py NodeGroups/ --baseline project.blend.gntsync

# Machine-readable output for CI
python gnt_check.py NodeGroups/ --baseline project.blend.gntsync --json

# Validate that every file parses and hashes (no baseline needed)
python gnt_check.py NodeGroups/ --strict
```

Exit codes:

- `0` - every tracked group matches the sidecar.
- `1` - changes or missing groups.
- `2` - unreadable files, unresolved conflict markers or another hard
  input error.
- `3` - the `.gntsync` baseline was written by a different
  hash-algorithm version. The checker refuses to report false changes:
  use `gnt_check.py` from the add-on's release, or open the project once
  with the current add-on to re-stamp the sidecar.

The canonical hashes are the same ones the add-on computes inside
Blender, so a hook and **Refresh Status** always agree.

### Audits

```bash
# Dependency impact: what breaks if I touch this group?
python gnt_check.py --impact "My Group" --baseline project.blend.gntsync

# Content fingerprints that ignore group identity
python gnt_check.py NodeGroups/ --duplicates

# Missing/unreadable/conflicted JSONs and references to untracked groups
python gnt_check.py NodeGroups/ --baseline project.blend.gntsync --health
```

All audits support `--json`. The duplicate audit is informational; the
health audit fails under `--strict` only on hard problems (unreadable
files, conflict markers).

### Compare projects and Blender versions

```bash
# Shared logic and diverging forks across projects
python gnt_check.py --cross projectA/NodeGroups projectB/NodeGroups

# Same project exported from two Blender versions: sockets added by the
# newer version are additive; removed sockets, changes on shared sockets
# and structural changes exit 1
python gnt_check.py --cross-version export_52/NodeGroups export_53/NodeGroups
```

`--cross` answers "is this group shipped in another project under any
name, and which shared groups have diverged?"; `--strict` exits non-zero
when forks diverge.

`--cross-version` compares JSON exports only - it never launches Blender,
so export the same `.blend` once per version first. Exit code `0` = no
destructive change, `1` = destructive (or any change with `--strict`), `2`
= unreadable input; `--json` emits the full report.

### Semantic diff and merge preview

```bash
# Readable summary between two JSON versions (folders, files or revisions)
python gnt_check.py --diff HEAD~1 HEAD --repo /path/to/project --subdir motor/NodeGroups

# Preview a semantic 3-way merge (values; graph per entity when identities exist; never writes files)
python gnt_check.py --merge base/NodeGroups ours/NodeGroups theirs/NodeGroups
python gnt_check.py --merge HEAD~1 HEAD theirs --repo . --subdir NodeGroups
```

`--diff` summarizes interface items, nodes added/removed (grouped by name
prefix), value/property changes and re-linked connections. Layout-only
properties (position, sizes, selection) are ignored.

`--merge` previews the semantic merge of the three sides without writing
anything: the **values** (socket defaults, node labels/properties, custom
properties, tree properties, curated owner settings of materials/worlds)
merge per item with a base/ours/theirs decision. When all three sides
carry complete node identities (current exports), **nodes and links**
also merge per entity: disjoint additions/removals/renames/rewires
combine, while delete/edit, same-node structural divergence,
rename/rename and link contention are per-element conflicts. Entries
without identities keep the whole-side structure adoption (never
guessed). Exact-content renames (same logic under a new name) are matched
and merged under the new name for node groups. Sides are folders, JSON
files or git revisions (`--repo`/`--subdir`; BASE is typically
`git merge-base`); `--json` prints a machine-readable report. Conflicts
exit `1`; any unreadable JSON in any side exits `2`. Group-tree merge
covers Geometry Nodes, Shader and Compositor groups on the current
development branch; other tree types are reported as notices
(`--strict` turns them into a failure). The v0.4.3 checker covers
Geometry Nodes, Shader and Compositor groups plus materials and worlds,
including graph merge and `merge_version` 4. Older tags may cover less.
Check the release notes for the tag you fetch. The decision core is
`merge.py` (pure Python, the same canonical rules as the hasher).

### Diagrams

```bash
# Needs Graphviz; SVG by default
python gnt_check.py --render deps NodeGroups/ --focus "My Group" --hops 2
python gnt_check.py --render diff old.json --new new.json
```

`--render` draws the JSONs as diagrams: project graph with
added/removed/changed coloring, a focused neighborhood, a single group
optionally at its stored node positions, or a node-level diff. It needs
the Graphviz binaries and, unlike the rest of the checker, **its
placement is not canonical** - a different-looking render does not mean
the project changed. The graphs in
[docs/case-studies](case-studies/subd-to-compound/README.md) are produced
this way.

## CI

[docs/CI_TEMPLATE.md](CI_TEMPLATE.md) has a copyable CI recipe pinned to
a released tag. It runs the status check and the audits on every proposed
change touching `NodeGroups/` or the `.gntsync` sidecar, with no Blender
on the runner. Update the pinned tag whenever the team updates the
add-on: the checker and the add-on must share the hash-algorithm version.

## Headless export and import

Export every trackable node group (Geometry Nodes, Shader and
Compositor) of a `.blend` to a folder export (one JSON per group, plus
`Modifiers/`, `Materials/` and `Worlds/` when present) without opening
Blender's UI:

```bash
blender --background --factory-startup project.blend --python scripts\export_all_json.py -- --out D:\NodeGroups
```

The output is the canonical JSON (the source of truth) - ready for
`gnt_check.py`, git, and **Track from folder...** inside Blender:

```bash
python gnt_check.py D:\NodeGroups\NodeGroups --strict
```

`--minify` produces compact JSON. `--categories
gn,shader,compositor,modifiers,materials,worlds` exports only the listed
categories; NODES modifiers are exported only together with the Geometry
Nodes groups.

`--make-local` first turns every library-linked tree that participates in
the export (geometry, shader and compositor groups, their transitive
group-node references and geometry-nodes modifiers) into a local copy -
needed when the groups come from an add-on's `assets.blend`:
library-linked groups can be exported but never pulled back. The
conversion is only written to the `.blend` when `--save` is also given;
without it the source file is left untouched.

`--exclude-library FRAG` (repeatable) keeps linked trees whose library
path contains FRAG out of the export and out of the conversion - use it
for Blender's bundled assets (`datafiles\assets`) or third-party toolsets.

A library spread over several `.blend` files (each linking the previous
ones) ports to a single versioned project through the JSON: export every
source read-only, then rebuild one master `.blend` where all groups are
local:

```bash
blender --background --factory-startup "base.blend"    --python scripts\export_all_json.py -- --out D:\lib
blender --background --factory-startup "derived.blend" --python scripts\export_all_json.py -- --out D:\lib
blender --background --factory-startup --python scripts\import_all_json.py -- --in D:\lib --out master.blend
```

Open `master.blend` and use **Track from folder...** to start tracking
each group against its own JSON. The JSON folder stays one file per group
- the versioned source of truth; the master `.blend` is only the working
file GNToolkit syncs against. Neither source file is modified.

The scripts live in `scripts/` in this repository - fetch them at the
same tag as your add-on. They are not part of the extension zip.

## Build the release packages

Both extension packages are reproducible from this repository:

```bash
python build_platform.py                  # platform box (Extensions Platform)
python build_platform.py --include-git    # GitHub box (+ system-Git engine)
```

They write `dist/GNToolkit-<version>.zip` and
`dist/GNToolkit-<version>-github.zip` and **fail** on any compliance
violation (module-path manipulation, threading, OS-level calls,
system-Git use, non-pure wheels, missing imports, version drift). The
build is deterministic (same input, same bytes). Run Blender's
`--command extension validate` on the result if you have Blender
available.

## Public test suite

The JSON-side logic is covered by a public, Blender-free suite in
[`pure-tests/`](../pure-tests/): canonical hashes (including the 4.2 to
5.x normalization), merge values/package/overrides, the audit helpers,
the `gnt_check.py` modes (diff, merge, render in dot-only mode, version
guard) and the exclusion tables - 300+ checks. Every test builds its own
tiny fixtures in a temporary folder, so it runs anywhere Python 3.10+
exists:

```bash
python pure-tests/run_all.py
```

The suite runs on every push and pull request (Linux and Windows) via
GitHub Actions. The Blender-side suites (import fidelity, real-project
round trips, UI panels) use local fixtures and are not part of the
repository.
