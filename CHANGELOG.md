# Changelog

All notable changes to this project are documented in this file.

## Technical note: zone node pairing (Simulation/Repeat/Foreach/Closure)

**Why `bpy.ops.node.add_zone` is the ONLY way to create zone pairs from Python
(verified on Blender 5.1.1, 2026-08-08 — do not re-investigate).**

The zone pairing (a zone Input node's `paired_output` pointer) cannot be
established from Python except via the operator:

- `bpy.types.Node` (base class) exposes **no** `paired_*` property at all.
- The zone Input subclasses (`GeometryNodeRepeatInput`, `...SimulationInput`,
  `...ForeachGeometryElementInput`, `NodeClosureInput`) expose a
  `paired_output` property that is **read-only** (`is_readonly == True`).
- The zone Output subclasses expose **no** pairing property.
- There is no RNA setter, no hidden attribute, and no other API surface
  that writes the pairing pointer.

The operator itself is **negligibly cheap**: measured 0.4-0.7 ms per call
(background mode, with a `temp_override` context + pinned node tree). All
106 zones of the reference project cost ~74 ms total. The importer's zone
creation is therefore NOT a performance bottleneck; the dominant import cost
is the O(tree size) re-validation that Blender 5.1 performs on every
socket/`links.new` mutation (see the Performance section below).

Consequences for the code architecture (keep these):

- `run_add_zone_operator()` must pin the target tree into a Node Editor
  space (`space.pin = True; space.node_tree = nt`) and call the operator
  inside `bpy.context.temp_override(...)` — the operator's poll() fails
  without it.
- `ensure_zone_area()` / `restore_zone_area()` exist because the operator
  requires a Node Editor area in the current screen; without them, scripted
  imports from a layout without the Node Editor silently drop all zone
  input nodes. Background mode has a virtual screen (Blender 5.1), so the
  fallback also works headless.
- The "Batch Import" button lives in the Geometry Nodes editor so
  interactive users naturally satisfy the context requirement.
- Creating the nodes via `nodes.new(...)` without the operator does NOT
  pair them (`paired_output` stays null) — zones created that way are not
  functional zones.

Blender 5.2 LTS and 5.3 (alpha) do NOT change this: neither release touches
zone creation or the socket pairing API (5.2 only changed Geometry Nodes
modifier properties and Compare/Random Value socket identifiers; 5.3 alpha
has no node/socket Python API changes at all).

## [0.2.1] - 2026-08-09

### Added
- Batch Import: new **Update existing groups** option (off by default).
  When enabled, groups that already exist in the file are rebuilt in place
  from the JSON — the datablock is kept, so modifiers referencing the group
  stay valid, and external links (from parent groups into the group's
  interface sockets) are snapshot and restored by name after the rebuild.
  This enables in-place refresh of a distributed .blend without the
  "import into a fresh file" ritual.
- Sync panel now shows **JSON remotes**: the list of distinct JSON files
  the tracked groups point at (resolved full paths), each with its group
  count, a copy-path button, a "Reveal in Explorer" button (Windows), and
  an error icon when the file is missing from disk. The panel header also
  shows the active group's JSON (**Active: name → file.json**) so the
  user always knows where a commit goes or a pull comes from. Two new
  operators: `gn.sync_copy_json_path` and `gn.sync_reveal_json_path`.

### Changed
- **Link All Groups** now updates the master JSON surgically, matching
  `export_all`: the existing JSON is read and only entries for groups
  present in the .blend are replaced, so JSON-only groups and modifiers
  are preserved instead of being wiped by an overwrite.
- **UI vocabulary moved to git-style terms** with clarifying tooltips on
  every operator; DNA/RNA jargon was removed from buttons (kept in
  tooltips and docs):
  - Track Group / Track All / Track from Existing JSON (was Link Group /
    Link All / Initialize Sync from JSON) — "Track All" writes the JSON
    from the .blend (first commit); "Track from Existing JSON" reads an
    existing JSON and never modifies it.
  - Stop Tracking (was Unlink).
  - Commit to JSON / Commit Modified to JSON / Commit All to JSON
    (was Export / Export Modified / Export All).
  - Pull from JSON / Restore from JSON (was Import / Import Modified /
    Re-import).
  - Keep JSON / Keep Blend (was Keep DNA / Keep RNA); status labels now
    read Edited Locally / Changed in JSON / Missing in Blend / JSON File
    Missing; summary shows "To commit" / "To pull".
  - Main panel renamed "JSON Package" with a "Snapshot" section
    (Export Package / Import Package); "Pipeline Tools" label removed.

### Fixed

- **"Track from Existing JSON" silently swallowed pre-existing divergence
  between the .blend and the JSON.** The operator stored both current
  hashes as the tracking baseline, so groups whose JSON content already
  differed from the .blend (e.g. after the JSON was updated externally)
  showed SYNCED and 0 issues — the user could not see that a Pull was
  needed. Divergent groups now store the .blend hash as the baseline
  (with the mtime fast-path disabled), so Refresh Status reports them as
  **Changed in JSON** and the operator report includes
  "N differ from the JSON (use Pull to apply)". Matching groups keep the
  previous behavior (SYNCED).
- **`JsonLock` deadlocked on its own lock file, silently clobbering the
  whole JSON package on every commit.** `export_to_json` and `export_all`
  acquire the lock and then call `read_json_tolerant`, which treated the
  process's OWN lock as foreign and waited until timeout, fell back to an
  empty skeleton and wrote back only the committed group — wiping every
  other group from the package (found by the new real-project e2e suite:
  committing one group left 438 of 439 groups deleted). The lock is now
  process-aware: `acquire` is re-entrant and `is_locked` ignores locks
  held by the same PID; foreign live locks still block as before.
- `bl_info` was not a literal dict (it used f-strings), which broke
  `addon_utils` parsing — the addon could not be listed or enabled from
  Preferences. It is now literal; the smoke test keeps its version in sync
  with `constants.py`.
- Zone output nodes could be duplicated when the serialized node order had an
  output entry before its paired input (the normal creation path ran first
  and collided with the pair created by `bpy.ops.node.add_zone`). The main
  import loop now skips zone outputs whose paired input exists in the data.
- Zone creation depended on the current screen having a Node Editor area;
  imports from any other layout silently dropped all zone input nodes
  (106 nodes on the reference project). `ensure_zone_area()` now converts an
  existing area temporarily (restored at the end of the import).

### Performance (found via per-group profiling of a 439-group project)

Blender 5.1+ re-validates the whole node tree on every RNA mutation
(socket `default_value`, `location`, `links.new`) — measured O(tree size)
per write. Import of a 795-node group costs tens of seconds of pure RNA
write time. Mitigations applied:

- `importer`: socket `default_value` writes are skipped for inputs that are
  connected in the serialized data (the link overrides the default at
  runtime) and for sockets whose current value already matches (reading is
  O(1) in Blender 5.1). Import of the reference project: 262s → 200s
  (~23%), pathological groups ~74s → ~52s.
- `serializer`: output-socket `default_value` is no longer serialized for
  GEOMETRY trees (outputs are computed, never stored). Old JSON files that
  still contain output defaults import unchanged (backward compatible).
- `operators` (Batch Import modal): the progress bar now paints from 0%
  immediately and uses a two-phase tick so the percentage is drawn before
  each task runs — previously the cursor did not show progress until the
  UI got control back from the heavy synchronous tasks (~19% in practice).
  Progress counts all tasks (groups + modifiers) instead of groups only,
  and the status bar shows the name of the group being imported (visible
  during heavy, UI-blocking groups).
- `importer`: the generic node-property loop now skips writes whose value
  already matches the current one (some property writes, e.g. `mute`, are
  O(tree size) like socket writes on Blender 5.1+). The 795-node reference
  group imports in ~35s vs ~74s originally (isolated measurement).
- **Chunked import (Batch Import modal)**: the importer core is now a
  generator (`_import_node_tree_gen`) that yields progress every ~25 nodes,
  ~100 sockets and ~50 links. The modal drives one chunk per timer tick, so
  the UI repaints during even the heaviest groups: the progress bar shows a
  monotonic overall percentage (never resets), the status bar shows
  `Group i/439: <name> — NN% · MM% total`, and the OS busy cursor no longer
  appears. ESC now cancels between chunks. `import_node_tree_recursive`
  remains a synchronous wrapper (unchanged behavior for all other callers;
  verified byte-identical node/link/zone totals against the pre-refactor
  code).
- **Modifiers are opt-in on Batch Import**: applying the modifiers stored
  in the JSON to existing objects with matching names was surprising (the
  default Cube would silently receive a stored modifier). The operator now
  has an "Apply Modifiers" checkbox (off by default); when off, modifiers
  are skipped entirely.

### Known issues

- **Importer roundtrip is not byte-identical.** Reimported node groups are
  functionally faithful (node/link counts, socket values and connections
  verified on a 439-group project), but interface socket identifiers may be
  assigned in a different order than the original (e.g. `Socket_0`/`Socket_1`
  swapped) and Reroute node widths reset to Blender's default. This can make
  a freshly imported group briefly report BLEND_MODIFIED against its JSON;
  the sync system self-heals by storing the new hash as the baseline. Found
  via the headless smoke test against the real project file.
- **Zone input nodes are only recreated when the serialized data carries
  pairing info.** Data exported by this addon does; older JSON files and
  hand-built trees (e.g. the smoke test's tiny zone nodes, created via
  `nodes.new`) do not, so those zones are dropped. Background mode has a
  virtual screen, so screen availability is never the blocker — see the
  "Technical note: zone node pairing" at the top of this file.
- **Batch imports run from a headless/background session lose some links in
  groups that reference other node groups** (pre-existing; identical in the
  pre-refactor code). GUI batch imports (the normal workflow) reproduce all
  25,437 links of the reference project exactly.

Remaining known characteristic: very large node trees (hundreds of nodes)
still import slowly because of the per-mutation tree re-validation in
Blender 5.1's node system; this is inherent to the API and cannot be
avoided from Python.

### Findings from the real-project e2e suite (439 groups, real deps)

- **Link fidelity on roundtrip**: links into/out of group-reference nodes
  can land on swapped sockets when a dependency's interface identifiers
  are reordered during import (counts preserved; 8 of 85 links on
  `SP - NURBS Patch Meshing`). A pull that rebuilds such a group can
  legitimately mark its parent groups BLEND_MODIFIED (their link
  identifiers change), which Commit Modified then publishes.
- **Blender drops zero-user node groups on save** (verified with pure
  Blender): a package imported into a fresh file and saved without
  references keeps only groups referenced by other groups; leaf groups
  referenced solely by object modifiers are lost unless fake users are
  set. Distribution .blend files must reference the groups (modifiers or
  `use_fake_user`).

## [0.2.0] - 2026-08-08

### Added
- DNA/RNA synchronization subsystem: JSON (DNA) is the source of truth, .blend (RNA) is the working cache.
- `sync_manager.py`: state detection (SYNCED, BLEND_MODIFIED, JSON_MODIFIED, CONFLICT, ORPHAN, JSON_MISSING), per-group canonical hashing within unified JSON packages, batch operations (link all, export all, export modified, import modified), cascade hash updates, PID-based JSON lock file, external connection preservation on reimport.
- `sync_metadata.py`: `.gntsync` sidecar file + text block cache, UUID tracking per node group.
- `sync_operators.py`: 15 operators (link, unlink, import, export, ignore, unignore, resolve, check, link dependencies, link all, export all, export modified, import modified, initialize).
- `sync_ui.py`: N-panel with batch operations, filterable issues list, geometry validation panel, conflict resolution.
- `hash_utils.py`: deterministic canonical SHA-256 hashing (sorted structures, volatile properties excluded).
- `geometry_validator.py`: generic issue detection (missing attributes, degenerate geometry, invalid outputs, missing groups, unlinked inputs, type mismatches).
- Load/save/undo handlers in `__init__.py` for persistent synchronization.

### Changed
- All 8 modules from 0.1.4 evolved in place (`__init__.py`, `constants.py`, `importer.py`, `serializer.py`).

## [0.1.4] - earlier

### Added
- Batch JSON export/import for Geometry Nodes groups and modifier setups.
- Standalone group export, unified package format, folder-structure export.
- Socket identifier collision evasion for volatile nodes (zones, menus, captures).
