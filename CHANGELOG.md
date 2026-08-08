# Changelog

All notable changes to this project are documented in this file.

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

## Known issues (0.2.0)

- **Importer roundtrip is not byte-identical.** Reimported node groups are
  functionally faithful (node/link counts, socket values and connections
  verified on a 439-group project), but interface socket identifiers may be
  assigned in a different order than the original (e.g. `Socket_0`/`Socket_1`
  swapped) and Reroute node widths reset to Blender's default. This can make
  a freshly imported group briefly report BLEND_MODIFIED against its JSON;
  the sync system self-heals by storing the new hash as the baseline. Found
  via the headless smoke test against the real project file.
- **Zone input nodes (Simulation/Repeat/Foreach/Closure) are not recreated in
  headless/background mode.** Zone pairs can only be created via
  `bpy.ops.node.add_zone`, which requires a screen with areas; background
  mode has none. In a GUI session the importer now guarantees a Node Editor
  area (converting an existing area temporarily when the current layout has
  none), so script-driven GUI imports create zones regardless of screen
  layout.

### Fixed (found via real-project import testing)

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
  Progress counts all tasks (groups + modifiers) instead of groups only.

Remaining known characteristic: very large node trees (hundreds of nodes)
still import slowly because of the per-mutation tree re-validation in
Blender 5.1's node system; this is inherent to the API and cannot be
avoided from Python.

## [0.1.4] - earlier

### Added
- Batch JSON export/import for Geometry Nodes groups and modifier setups.
- Standalone group export, unified package format, folder-structure export.
- Socket identifier collision evasion for volatile nodes (zones, menus, captures).
