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
  headless/background mode.** `importer.run_add_zone_operator` requires a
  Node Editor area (`bpy.ops.node.add_zone`); zone inputs are preserved when
  importing from a GUI session.

## [0.1.4] - earlier

### Added
- Batch JSON export/import for Geometry Nodes groups and modifier setups.
- Standalone group export, unified package format, folder-structure export.
- Socket identifier collision evasion for volatile nodes (zones, menus, captures).
