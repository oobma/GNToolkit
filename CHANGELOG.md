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
- **Auto-rebaseline of tracking baselines when the hash algorithm
  version changes.** `HASH_VERSION` is recorded in the sync metadata; on
  load or Refresh Status, if the stored version differs, every baseline
  is silently re-stamped with the current algorithm using the same rule
  as Track from Existing JSON — matching groups become SYNCED, genuinely
  divergent groups stay visible as Changed in JSON. Without this, an
  addon update that refines the hash would report a spurious
  "everything changed" once (stored hashes use the old algorithm).
- New **Stop Tracking All** button in the Sync panel: unlinks every
  tracked group in one click (with a confirmation dialog). Removes the
  tracking metadata and the UUID custom properties; the JSON files and
  the node trees are kept. Backed by `sync_manager.unlink_all_groups()`.
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

- **Pulls crashed Blender with access violations (interactive sessions).**
  Three crash vectors were removed from the pull paths (batch and
  per-group):
  - The interface rebuild of a dependency clears and recreates its
    sockets while the parents' group nodes still hold **live links into
    them**; Blender's propagation of that churn can leave dangling link
    pointers. Both pulls now **remove the external links into a group
    before rebuilding it** (they are re-created from the external-
    connection snapshot afterwards) — the batch uses the already-known
    reverse dependency graph, so there is no extra full-project scan.
  - Zone pairs were created with a fresh `temp_override` + pin/unpin of
    `space.node_tree` per zone, plus `ensure_zone_area()` mutating the
    area type mid-pull. Zone creation now uses **one pinned Node Editor
    session per rebuilt tree** (created once, restored in `finally`),
    and both pull operators always restore the zone area, even on
    failure.
  - Pending interface propagation was resolved during the first UI
    redraw after the operator returned ("crash just after the pull").
    Both pulls now **flush the depsgraph** (`view_layer.update()`) and
    **validate every rebuilt tree's links** before returning.
- **Dangling-link detection and self-repair.** A crash mid-interface-
  rebuild can leave links whose sockets point at other nodes' (or other
  trees') sockets; reading them crashes Blender again ("crash almost
  immediately on the next pull"). The pull now checks every tree's links
  first (pointer equality — NOT Python identity, which produces false
  positives because Blender wraps the same C pointer differently per
  access path), strips the dead links (their content is rebuilt from the
  JSON anyway), and refuses with a clear message if any remain. The
  snapshot/restore steps skip links with missing endpoints instead of
  dereferencing them.
- **Per-group Pull (Pull from JSON) now rebuilds the target's whole
  connected component — dependencies AND parents — with the same
  verification and honest re-stamping as the batch.** It previously
  rebuilt only the target and its dependencies: renumbering a dependency
  interface broke the links its (unrebuilt) parents had into it, and the
  cascade hash update silently absorbed the damage (the project looked
  SYNCED while dozens of groups were broken). The per-group pull now
  goes through the same machinery as the batch (bidirectional closure,
  external-connection snapshot/restore, tree-vs-JSON verification, still
  divergent groups reported and kept visible), so a per-group pull can
  no longer break or hide anything.
- **Pull rebuilt the wrong-socket links and then hid the damage.** The
  pull's rebuild set covered only the divergent groups plus their
  *dependencies*; rebuilding a dependency renumbers its interface, which
  removes the links its *parents* have into it (Blender drops links into
  recreated interface sockets), so a pull could break more groups than it
  repaired — and the unconditional baseline re-stamping then made the
  damage invisible (a second pull found "nothing to do"). The pull now
  rebuilds the transitive closure in **both directions** (dependencies
  and parents), the rebuilt trees are **verified against the JSON**, and
  any group the rebuild cannot reproduce stays visible as "Changed in
  JSON" and is counted in the report ("N still differ after pull")
  instead of being silently absorbed. On the reference project a single
  pull now aligns all 439 groups byte-perfect (25,439/25,439 links, 0
  errors, 0 residual divergence) — previously it left ~108 groups
  divergent.
- **Links into/out of dependency group nodes landed on wrong sockets
  when the dependency was not rebuilt in the same pass.** The identifier
  remap (`group_interface_maps`) is only available for rebuilt
  dependencies; with the raw (stale) identifier, the socket lookup fell
  back to a bare-id match that could hit a *different* socket after the
  roundtrip — and `links.new` on an already-linked input silently
  replaced the correct link, so links were lost without any error. When
  the dependency has no registered map the lookup now matches by socket
  name only (with the type hint), never by the stale identifier.
- **Per-instance Group-node socket defaults lost on interfaces with
  duplicated socket names.** `_reapply_group_node_defaults` resolved
  sockets by name with a first-match, so with two same-named sockets
  (e.g. two "Switch Target End") the JSON override was always written to
  the first one; it now resolves through the dependency's identifier
  map (unique) and only falls back to the name.
- **Canonical hash normalization (HASH_VERSION 3).** `use_extra_user`
  (fake user — a file-management flag, not content) and
  `location_absolute` are excluded; the 2D vector socket subtypes
  `NodeSocketVectorTranslation2D`/`NodeSocketVector2D` hash as one type
  (the importer cannot create the Translation subtype via
  `interface.new_socket`); datablock-reference defaults (fonts, objects)
  and null defaults are dropped — they cannot round-trip (the .blend may
  not contain the referenced datablock, and re-created ones get
  different names). Existing baselines migrate automatically via the
  auto-rebaseline mechanism.
- **Font references by name now reuse the existing datablock**
  (`vectorfont` resolves against `bpy.data.fonts` — previously the
  collection lookup used a nonexistent `vectorfonts` name and the
  assignment was skipped).
- **Batch pull (Import Modified) and per-group Pull now rebuild the
  transitive dependency closure.** A rebuilt group wires its links
  against the freshly rebuilt interfaces of its dependencies (shared
  interface maps); skipping a "matching" dependency leaves its reordered
  identifiers in place and breaks the parent's wiring (215 links lost
  without the closure). The external-connection snapshot covers the full
  rebuild set, and the affected non-rebuilt parents are re-baselined
  after the restore (both in the batch and the per-group pull), so the
  result is byte-perfect except for a known edge: groups whose
  dependencies have **duplicated interface socket names** (e.g. the two
  "Tension" sockets of `SP - Blend Curve [Intern]`) — name-based
  restoration is ambiguous there and a handful of links cannot be
  reconnected (5 of 25,439 on the reference project, verified).
- **Canonical hash is now name-based and roundtrip-robust.** The hash
  previously included interface/socket *identifiers*, which the import
  roundtrip reorders (cosmetic), so every reimported group looked
  changed: tracking an imported project against its own JSON reported
  hundreds of false divergences and a full Pull equaled a fresh Import
  Package. The canonical form now keys interface items and links by
  socket **names** (identifiers dropped), drops volatile fields
  (`width`, `select`, `socket_idname`, socket `bl_idname` subtypes,
  `hide`, enum/menu `description`, interface panel `parent`,
  `optional_label`/`menu_expanded`), and ignores `default_value` of
  connected sockets (matched by unique identifier — socket names are
  frequently shared, e.g. the three "Value" inputs of a Math node).
  Real changes (defaults, nodes, links to different-named sockets) are
  still detected; the remaining importer fidelity noise is small (on the
  reference project a fresh track reports only the genuinely affected
  groups) and one Pull repairs it.
- **Batch Import modal: restored the two-phase progress tick and finer
  chunks.** The modal set the new group's "0% · name" status and
  immediately entered the blocking first chunk, so the status bar kept
  showing the previous group at "100% · N% total" during the first
  (possibly long) chunk of a huge group. The tick now returns before
  running the chunk (paints the new group's name at 0%), and the chunk
  granularity is finer (10 nodes / 40 sockets / 20 links) so the
  displayed progress updates more often during heavy groups.
- **Node `width` excluded from serialization and the canonical hash.**
  Reroute/box widths are cosmetic and not restorable after a roundtrip
  (Blender resets them), so they poisoned the hash and made every group
  with a non-default width report as changed. `width` is now skipped in
  `NODE_PROPS_TO_SKIP` (new exports no longer carry it; old JSONs still
  import) and excluded in `HASH_EXCLUDE_NODE_PROPS` (the hash ignores it
  on both sides, so the noise disappears even for existing JSON files).
- **Pull (per-group and batch) lost and swapped links into dependency
  group nodes on real dependency webs.** The pull path rebuilt each group
  with a fresh empty interface map and without dependency ordering: when
  a parent was rebuilt before (or while) its dependency changed interface
  socket identifiers, links into the dependency's group node were dropped
  or landed on the wrong sockets (the manual test on the real project
  lost 12 of Meshing's 85 links; `import_all_modified` reported 21
  groups with wiring errors). Fixes:
  - `group_interface_maps` is now **shared** across the pulls of a batch
    (and per-group pull), so links into freshly rebuilt dependencies are
    remapped correctly.
  - Pull order is **dependency-first**: `import_all_modified` topologically
    sorts its candidates over the JSON reference graph; `import_from_json`
    recursively pulls modified dependencies before the target (using
    json-changed semantics, matching the batch, so roundtrip fidelity
    noise does not trigger unnecessary dependency rebuilds).
  - Verified on the real project: batch pull now imports all 430
    divergent groups with **0 wiring errors**, Meshing keeps all 85 links
    with zero missing or extra signatures, and the real JSON changes
    (Resolution 32 / Trim Contour off, Torus +2 nodes) apply correctly.
- **Batch pull (Import Modified) spent O(N²) on external-connection
  scans.** Each imported group snapshotted links from ALL other trees
  (`_save_external_connections` scans every node group), making a
  400+ group pull slower than a fresh package import. The snapshot is
  now taken ONCE for all candidates (`_save_external_connections_batch`,
  one pass) and restored afterwards for non-rebuilt parents only —
  rebuilt groups already get their links from the JSON (dependency-first
  order), so restoring into them would clobber JSON-wired links. The
  post-pull cascade hash update is also limited to the rebuilt groups
  (the pull never writes the JSON, so other baselines cannot change).
  With the batch pull now verified to reproduce **all 25,439 links of
  the reference project byte-for-byte** (global link-total invariant
  matches the JSON exactly), the O(N²) scans are gone and the remaining
  pull cost is the rebuild work itself plus one JSON hash pass.
- **Import summary counted DEBUG records as warnings/errors.**
  `ImportErrorTracker` now distinguishes informational records (DEBUG,
  DEFAULT_VALUE) from real issues (WARN+); `warn_count`/`has_errors`
  reflect only the latter. The Batch Import summary and the Pull report
  use `warn_count`, so a clean import no longer reports "19 warnings".
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
