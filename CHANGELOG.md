# Changelog

All notable changes to this project are documented in this file.

## [0.2.4] - 2026-09-09

### Added

- **Collaboration panel (thin Git transport).** A new "Collaboration"
  panel (between Sync and Sync Issues) turns any git repository holding
  tracked JSONs into a collaboration backend — Git stays the authority
  for history, merge and credentials, the addon only drives the CLI
  (`git_integration.py`):
  - One row per detected repository (walking up from the tracked JSONs
    to the nearest `.git`): repo name, state (`clean` / `N to commit` /
    `ahead N` / `behind N` from `git status --porcelain -b`), and
    **Git Commit…** / **Git Sync** / **Reveal** buttons.
  - **Git Commit…** (dialog with a message field) stages **only the
    tracked JSON files that changed** — never `git add -A`, never the
    .blend.
  - **Git Sync** = `git pull --ff-only` + `git push`: remote changes
    arrive only when the branch is simply behind (no automatic merge,
    ever); diverged versions are refused with a clear message. After a
    clean sync the tracked groups of the files the pull changed are
    re-checked automatically ("Changed in JSON" appears without a
    manual Refresh Status); `git_sync(..., report_files=True)` returns
    the repo-relative paths the pull modified.
  - **Merge-conflict detection**: `json_read_failure_reason` gained a
    `'conflict'` reason; tracking/importing a conflicted JSON reports
    "has merge conflicts — resolve them with your git client" and the
    panel lists conflicted files with a Reveal button.
  - **History** (collapsible): the repository's latest commits and the
    active group's file history (10 entries each).
  - State refreshes on load, on Refresh Status and after every Git
    operation (cached — no subprocess per redraw); "Git not found" and
    "not a git repository" states are reported with guidance.
  - **Fetch on load**: opening a .blend now silently fetches every
    tracked repo's remote before the state refresh
    (`refresh_git_state(fetch=True)` in the load handler), so `behind`
    reflects the shared repository as-is without touching Git Sync
    first. Offline/repository-less setups are a no-op (10s timeout,
    errors swallowed — `git status` semantics otherwise unchanged).
    The network part is gated by the new **Fetch remotes on open**
    preference (Collaboration panel, on by default) and by Blender's
    *Allow Online Access* setting; the local `git status` refresh always
    runs.
- **Folder workflow (per-group JSON files as full sync participants).**
  The "Use Folder Structure" export (`NodeGroups/` + `Modifiers/`) is no
  longer a one-shot snapshot — it round-trips through the sync layer:
  - **Track Folder…** (`gn.sync_link_folder`): picks any file inside an
    exported folder (the `NodeGroups/` subfolder is detected) and tracks
    every group found in the JSONs against its own file — read-only,
    per-group hashes, `depends_on` edges recorded in a second pass,
    `linked/skipped/errors` report. Accepts standalone and one-group
    package files (mixed folders work).
  - **Track from Existing JSON** accepts single-group exports: a file
    with `nodes` + `name` tracks that one group (same normalization the
    F3 picker already had). Modifier exports and unrecognizable JSONs
    get clear error messages naming the file and the expected shapes.
  - **Import Package/Folder** (`gn.import_batch_json`): picking any file
    inside a folder export recreates the whole folder — groups
    (dependency-first via the shared interface maps) and modifiers
    (`Apply Modifiers` is now **on by default**; modifiers attach to
    existing objects with matching names, as before). The loader
    (`load_package_sources`) handles package files, standalone files and
    folders, including one-group packages produced by a commit.
  - **Write-back to standalone exports**: `export_to_json`/`export_all`/
    `export_all_modified`/`link_group` normalize a standalone single-group
    file into a one-group `GN_UNIFIED_PACKAGE` on the first commit
    (previously a `KeyError` — committing to a per-group export failed).
- **Untracked dependency groups are detected and filed before a commit.**
  A tracked group that gains a new nested group locally used to commit a
  reference to a group that exists in no JSON — the collaborator (or a
  later folder import) got a dangling Group node with no sockets. The
  Sync Issues panel now lists every untracked dependency group found in
  the .blend (one row per child: name, parent and a **Track** button),
  and **Commit with Review…** shows one row per child (default: track)
  so they are filed and committed together with their parent. Tracking
  follows the layout recorded for the parent at tracking time (`layout`
  metadata, inferred from the file for older entries): folder exports
  get a per-group JSON next to the parent's file — reusing an existing
  file when it already holds the group, never rewriting it — while
  master packages get the group added to the same file (surgical
  update). Plain **Commit** / **Commit Modified** / **Commit All**
  buttons only warn ("N untracked dependency group(s) were NOT
  committed") — no surprise writes. The hidden `gn.sync_link_deps`
  operator (it scanned the parent's JSON, so a brand-new .blend group
  was invisible to it) is removed. Regression:
  `tests/test_untracked_deps.py` (20 checks on 5.1 and 5.2, in the
  release gate).
- **Encoding robustness**: JSON reads accept UTF-8 with or without BOM
  (`utf-8-sig`) and a non-UTF-8 file (e.g. saved as ANSI/Windows-1252 by
  an editor) no longer crashes with a raw traceback — the tolerant reader
  returns None and the operators report a clear, actionable message
  ("not valid UTF-8 — re-save it as UTF-8…") via the new
  `json_read_failure_reason()` classifier (encoding / json / io). The
  status-check hash reader got the same hardening.
- **Collapsible panel lists**: the Sync panel's JSON files list (one row
  per tracked file — hundreds after a folder workflow) and the Sync
  Issues list are collapsible via disclosure toggles; JSON files default
  collapsed.
- **Import diagnostics**: skipped links (endpoint not in the node map)
  are recorded as DEBUG records, and the wiring WARNs include the
  resolution flags.
- **Headless JSON-side status check (`gnt_check.py`).** The JSON side is
  now usable without Blender: `python gnt_check.py <folder-or-package>
  [--baseline project.blend.gntsync | flat.json]` hashes every group with
  the same canonical hasher the addon uses inside Blender and reports
  synced / changed / missing groups (exit 0/1/2, `--json` for machines,
  `--strict` to fail on unreadable files, `--selftest` to prove the
  hasher runs without bpy). Baseline accepts a `.gntsync` sidecar (paths
  resolved against its directory) or a flat `{group: hash}` JSON. The
  module loads `hash_utils`/`constants` through a synthetic package, so
  no addon module is touched and `bpy` is never imported. The release
  gate gained a `gnt_check` step (selftest + optional folder validation),
  and the README documents the CI/hook workflow. Reference run: 582
  groups in ~2.5 s. The description now centers the semantics — sync is
  one application of the change/conflict layer — while the supported
  scope stays explicit: Geometry Nodes groups today.

### Fixed

- **Package import was not undoable as a single step.** `GN_OT_ImportBatchJSON`
  ("Import Package/Folder") mutated `bpy.data` from its modal timer without
  declaring `bl_options = {'UNDO'}`, so the first Ctrl+Z after an import
  restored the last registered undo step — which could predate the import and
  silently roll back user work along with it. It now declares
  `{'REGISTER', 'UNDO'}`: Blender pushes the undo event when the modal returns
  `FINISHED`, making the whole import (groups + modifiers, including "Update
  existing groups") one undoable/redoable step; ESC/cancel does not push and
  headless/background runs are unaffected. The sync operators (Pull, batch
  pull, picker import) already had `UNDO`.
- **NODES modifier inputs were silently reset on every Pull/import.**
  Blender 5.2 stores modifier inputs keyed by the referenced tree's
  interface-socket identifier; rebuilding a group renumbers the
  interface, which orphaned the stored values and reset every configured
  input to its default (e.g. a meshing chain dropping from 18 to 8
  patches while the rebuilt graph itself was 100% faithful).  The
  importer now snapshots every modifier that references the tree being
  rebuilt — before the interface is cleared — and restores the values
  through an identifier remap built by `(name, in_out)` matching
  (`modifier_utils.py`); this covers Pull, batch pull, single-group
  import and package import in one place.  The package import path
  (`_apply_modifier_inputs`) also translates the serialized keys
  (export-time identifiers) through the rebuild's interface map instead
  of assuming they still match, and no longer depends on the tree being
  built in the same order.  Menu inputs are plain integer indices (they
  were already serialized) and are restored by the same pass.  The 5.1
  legacy ID-property path (where Blender itself renames the stored keys)
  is preserved as a harmless no-op.  Regression:
  `tests/test_modifier_inputs_pull.py` (15 checks on 5.2 / 14 on 5.1,
  in the release gate) — verified to fail (A5-A7) with the restore
  disabled.
- **Menu socket defaults on nodes were never serialized.** `MENU` was
  listed in `_NON_SCALAR_SOCKET_TYPES`, so `serialize_node` skipped
  `default_value` on every `NodeSocketMenu` input/output.  Per-node
  overrides on Group nodes were reset to the referenced interface
  default on import, silently changing geometry: a menu-switched meshing
  chain produced no meshed instances where the original produced the
  full patch set (the reconstructed object "disappeared").  Menu sockets
  now serialize their string enum identifier (empty identifiers are
  still omitted, matching the importer); node-level menu defaults are
  deferred to the importer's final menu pass (where `enum_items` already
  exist) instead of warning on the early attempt.  Regression: smoke T21
  (5.1), E2E T9 (5.2) and the name-agnostic folder diagnostic
  `tests/diag_menu_defaults.py` (every node-level menu default must
  reach the JSON and every rebuilt group must hash identically).
  Packages exported with the old serializer keep the lossy value and now
  show "Changed in JSON" until re-exported.
- **Nested interface panels were flattened on import.** `_rebuild_interface`
  created every PANEL with `new_panel()` but never attached it to its parent
  (the panel `parent` is read-only in the Python API; nesting requires
  `interface.move_to_parent()`), so a panel inside a panel came back at the
  root of the interface while its sockets kept pointing at it.  Panels now
  nest exactly as serialized.  The canonical hash keeps the interface item
  `parent` (empty parents excluded) so this class of divergence is visible
  in sync from now on: HASH_VERSION 8, stored baselines are re-stamped
  automatically.  Regression: smoke T22.
- **Interface socket presentation flags were lost.** `is_panel_toggle`
  and `structure_type` were skipped by the serializer and `optional_label`
  was never applied on Menu sockets, so a bool socket acting as its
  panel's toggle came back as a plain checkbox inside the panel and
  list/field structure types reset to AUTO (12 / 5 / 9 sockets in the
  reference project).  All three now roundtrip and are covered by the
  canonical hash (HASH_VERSION 8); Blender only accepts `is_panel_toggle`
  once the socket is nested in its panel, which the importer guarantees
  by parenting every item before applying properties.  Regression: smoke
  T22.
- **Blender 5.x interface sockets are now handled as a complete,
  self-verified matrix.** `parse_interface_socket_variant()` decomposes
  every `NodeSocket<Base><Subtype>[2D|3D|4D]` name (Vector's 30 classes,
  the Float/Int/String subtypes, and the integer-vector family) into base
  type + dimensions + subtype; the importer builds them in that order.
  The 12 GN-valid classes the Python API cannot build (Float/Int
  `Unsigned`, the 10 integer-vector variants — no UNSIGNED enum value, no
  `dimensions` on Int, and the `bl_socket_idname` recast crashes Blender
  with an access violation) now fall back to their base type and are
  **visible in the import report** (one WARN each: "cannot be recreated
  by this Blender version").  The tree remembers the requested names so a
  re-export never silently downgrades the JSON, and the canonical hash
  maps them to the fallback so sync shows no phantom "Changed in JSON"
  (HASH_VERSION 7).  New test `tests/verify_folder_recreation.py` sweeps
  every class discovered from `bpy.types` (never a hand list) and fails
  when a future Blender exposes one the parser does not know.
- **Blender 5.2 vector interface sockets with subtype + extra dimensions
  were dropped on import (`NodeSocketVectorFactor2D` and the whole
  family).** The 5.2 interface API accepts only the base socket types and
  the remap/subtype tables only covered the float/int/string/vector-3D
  subtypes plus the plain 2D vector variants, so `new_socket()` raised on
  e.g. `NodeSocketVectorFactor2D`.  A 582-group folder export with 10
  such sockets logged 10 `[ERROR] Failed to create interface item …`,
  lost the 14 links into/out of them (6 groups) and kept none of their
  saved defaults.  The base+dimensions+subtype recipe now recreates every
  creatable variant (including the 4D ones) exactly.
- **4-component vector defaults serialized as `mathutils.Quaternion`
  reprs.** `clean_value` handled `Vector`/`Color`/`Euler` but not
  `Quaternion`/`Matrix`, so a 4D vector subtype default (e.g.
  `NodeSocketVectorEuler4D`) became an unparseable string
  (`"<Quaternion …>"`).  Quaternions now serialize as `[w, x, y, z]`,
  matrices as rows, and `unclean_value` builds 4-component values for
  `…4D` socket types.
- **Spurious "Changed in JSON" for groups whose JSON carries `-0.0`.**
  `round(-1e-9, 6)` writes `-0.0`, which `json.dumps` prints differently
  from `0.0`; Blender stores float32 and flips the sign on re-export, so
  two identical trees hashed differently (2 groups on the real project).
  The canonical hash now normalises `-0.0` to `0.0` (stored baselines
  migrate automatically via the re-baseline mechanism).
- **"Missing in Blend" returned after every save for restored groups.**
  Blender does not persist zero-user node groups across save/reload; a
  group restored from JSON (or batch-imported) that nothing references
  disappeared from the .blend on the next open, so the sync layer
  reported it as orphan forever. The importer now gives every tree it
  creates a fake user (`use_fake_user`) so imported/restored groups
  survive saving (the canonical hash excludes that flag — sync
  detection is unaffected).
- **Links into group nodes with duplicated socket names were lost,
  silently and order-dependently.** When a dependency was not rebuilt in
  the same pass, wiring fell back to name-only socket lookup; interfaces
  can carry duplicated names (e.g. two "Switch Target End"), so both
  links of a pair landed on the FIRST socket and the second `links.new`
  silently replaced the first (a socket takes one incoming link). The
  importer now resolves such sockets **positionally**: the node's
  serialized socket order mirrors the referenced interface, so the index
  disambiguates the duplicates exactly (`_positional_group_socket`, with
  a name sanity check and fallback to the previous path). Recreation of
  the 439-group reference project from a folder export is now
  **439/439 byte-identical** (nodes, links and canonical hashes) on
  5.1.1 and 5.2.0 — previously 8 groups lost 1-4 links.
- **`ImportErrorTracker.has_errors`/`warn_count` semantics** are
  unchanged, but the recreation suite now proves the roundtrip: the
  previous "headless imports lose links" known issue is closed by the
  positional fix above.
- **Folder import skipped committed per-group files**: a standalone file
  that had been committed (converted to a one-group package) lost its
  top-level `name`, so the folder loader skipped it (4 of 5 groups).
  `load_package_sources` merges `node_groups` from package-shaped files
  in the folder.
- **Git transport (found by the two-user collaboration e2e)**:
  - `repos_for_tracked` resolved tracked paths without the blend dir
    (`resolve_json_path` requires it) — the Collaboration panel found
    no repositories.
  - `ahead`/`behind` parsing kept the `]` of `[ahead 1]` (`int("1]")`)
    and silently reported 0.
  - tracked-file matching compared backslash relpaths against git's
    forward-slash status lines — changed files were invisible; all
    paths are normalized to `/` before comparing.
  - untracked directories were collapsed (`?? dir/`) and paths with
    spaces arrived quoted (`?? "dir/a b.json"`); status now runs with
    `-uall -z` (NUL-separated, unquoted, individual files), and a
    "No commits yet on <branch>" head line parses as that branch.
- **Git jobs no longer run in a worker thread.** Every git call now
  happens on the main thread: a job is a small generator of stages
  executed by the existing timer pump — short local commands
  (`status`/`add`/`commit`/`rev-parse`) run inline (tens of
  milliseconds), fetch/pull/push are started as child processes and
  polled across ticks (output to temporary files, never pipes, so a
  verbose command cannot deadlock on a full buffer), and the Python
  batches (tracked-path resolution, conflict scans over hundreds of
  files) yield control between chunks. The job bodies receive the blend
  directory and the sync metadata in their payload, resolved on the main
  thread at submit time: they no longer reach into Blender data
  (`bpy.data.filepath`) off the main thread, which the previous
  worker did through the sync manager.   `shutdown_git_worker` is now
  `shutdown_git_jobs`. A repo whose `git status` fails shows the error
  in its panel row instead of failing the whole refresh.
- **JSON writes are atomic.** Packages, standalone exports and the
  `.gntsync` sidecar go through `file_utils.write_json_file`, which
  writes to a temporary file in the destination directory and swaps it
  into place with `os.replace` — previously `open(path, 'w')` truncated
  the destination first, so a crash, a full disk or a stalled write
  could leave a half-written source of truth and destroy the previous
  good copy. A failed write now leaves the old file untouched (and no
  `.tmp` files behind).
- **Folder exports can no longer silently overwrite each other.** File
  names use the same lossy cleaning rule as always (alphanumerics,
  spaces and underscores kept; `A/B` and `A:B` both become `A_B`), so
  the second file used to replace the first and the report counted node
  trees instead of files. The exporter now allocates
  unique stems (`A_B`, `A_B~<hash>` for the colliding one), falls back
  to `unnamed` for empty names and prefixes Windows reserved device
  names (`CON` → `_CON` — `CON.json` cannot be created), and reports
  the number of files actually written plus every adjusted name
  (`old → new`). The cleaning rule itself is unchanged, so existing
  folder-export paths keep working (verified against the 439 group
  names of the reference project: 0 differences). The active-group
  export dialog sanitizes its default file name too.
- **"Pull from JSON" raised `name 'conflicts' is not defined` after the
  import.** The batch pull's completion log still referenced a
  `conflicts` counter removed in an earlier refactor, so the operator
  reported `Pull failed` (the import itself had already been applied).
  Found by the new Blender 4.2 platform suite; the log now reports the
  counters that exist (`imported`, `skipped`, `errors`, `auto-linked`,
  `still differ`).
- **"Reveal" buttons were Windows-only.** Both *Reveal JSON in Explorer*
  and *Reveal Repository* called `os.startfile`, which does not exist on
  macOS/Linux (the operators only warned). They now use Blender's native
  `bpy.ops.wm.path_open`, so the folder opens in the system file browser
  on every platform.

### Tests

- Smoke suite (Blender 5.1): **143 checks** (+T8 encoding checks, T8b
  standalone write-back, T8c folder loader, T8d duplicated-socket
  regression, T8e git transport — 15 checks over a real temporary
  repository: status/commit/log/sync-fast-forward/divergence-refusal/
  conflict markers, with a bare local remote; skipped with a warning
  when git is not installed; T19 atomic-write checks; T20 export
  filename checks: reserved names, collision suffixes, one file per
  group under the real folder export). New-node E2E (Blender 5.2):
  40 checks (unchanged).
- Reproduction suite `tests/repro_folder_flow.py` (24 checks on 5.1 and 5.2):
  the full folder workflow — export by folders, per-group track, commit
  to a standalone file, folder batch track, master package track,
  folder recreation with modifiers, non-UTF-8 guard.
- Recreation suite `tests/recreate_from_folder_test.py` (out-of-battery,
  run manually): exports all 439 groups of the reference project by
  folders, resets to an empty file and reimports everything from the
  folder — verifying counts, canonical hashes and dependency closure
  (113-group chain resolved deps-first). Passes 9/9 on 5.1.1 and 5.2.0.
- Collaboration e2e `tests/git_collab_e2e.py` (out-of-battery, run
  manually): a full two-user simulation over a local bare remote —
  folder export into a repo, track, colleague clone/edit/push, sync
  (fast-forward), "Changed in JSON" + pull into the .blend, addon
  commit (standalone→package conversion visible to the colleague) and
  push. Passes 18/18 on 5.1.1 and 5.2.0.
- Git job pipeline diagnostic `tests/diag_git_worker.py` (out-of-battery,
  run manually; drives the pump by hand since background mode has no
  timers): submit → stages → results → handlers, the status/commit/sync
  jobs, the fetch-on-load path, the operator path, and that no git
  thread is created and nothing runs before the pump ticks. Passes 30/30
  on 5.1.1 and 5.2.0.
- Blender 4.2 platform suite `tests/test_42_smoke.py` (21 checks): the
  extension ZIP is installed from disk into an **isolated** extensions
  directory under Blender **4.2.1 LTS** (the declared
  `blender_version_min`), then the sync core runs end to end with
  4.2-compatible nodes — install/enable, folder export, Track Folder,
  local edit, Commit All (standalone → package write-back), external
  JSON edit, Pull from JSON, status transitions. Found the `conflicts`
  `NameError` fixed above. Passes 21/21 on 4.2.1.

## [0.2.3] - 2026-08-15

### Added

- **Deterministic JSON serialization (F1).** Node entries are emitted in
  name order (per-node item collections also sorted by name), so
  renaming/reordering produces minimal, stable git diffs and re-exporting
  an unchanged group is byte-identical (roundtrip verified by the smoke
  test). Links and interface items keep their creation order: link order
  is functional (some volatile 5.2 nodes change their socket layout while
  links are being made) and a NODES modifier requires the first interface
  output to be geometry.
- **Check on open (F2).** After loading a .blend, a deferred JSON-side
  check (pure Python, chunked via timers — no tree hashing at load)
  compares the package hashes against the stored baselines and shows a
  non-intrusive notice ("N group(s) out of sync with JSON", status bar +
  Sync panel + issues list). Toggleable via the `check_on_load` pref
  (plug icon next to Refresh Status).
- **Selective group import (F3).** "Import Group from JSON…" imports one
  group plus its missing dependency closure from any JSON package:
  - Panel picker with Blender's native search widget (`prop_search`),
    a plan preview under the field (CHECKMARK in-blend / NODETREE to
    import / ERROR divergent / STATUS_WARNING external refs) and a
    "Track to JSON…" button that tracks the selected group to a package
    of your choice (no active-tree dependency).
  - Existing groups are **never overwritten** (skip mode); aligning
    existing groups goes through Track + Pull / Keep JSON.
  - Divergence detection ignores the socket records of group nodes whose
    reference is not in the package (unverifiable noise).
  - Live refresh pump redraws the picker every 0.5s while open, so a JSON
    edit on disk shows up in under a second.
- **Commit with Review (F4).** "Commit with Review…" lists every locally
  edited group (conflicts default to Skip) and applies a per-group
  decision: Keep JSON / Keep Blend / Skip.
- Test packages (tests/, gitignored): `test_package_icons.json` (the four
  picker icon states) and `test_package_search.json` (40 groups for
  search testing).

### Fixed

- **`GN_SyncPrefs` properties were silently unregistered**: the assignment
  syntax (`prop = bpy.props.X()`) registers nothing on Blender 4.x/5.x —
  converted to the annotation syntax (regression guard added to the smoke
  test).
- **`UILayout.prop_search` argument shape**: `search_data` must be the
  owning object with the collection property name, not the collection.
- **EnumProperty items with icons require 5-tuples** (id, name, desc,
  icon, number) on Blender 5.x — the 4-tuple is rejected.
- **Icon enums**: `WARNING` was removed from the UI icon set in 5.2
  (now `STATUS_WARNING`); `WindowManager.invoke_confirm` keeps the small
  enum where `WARNING` is valid. The two must not be mixed.
- **Picker performance**: the in-blend map and the package parse are
  cached (mtime/group-count keyed) — drawing the picker over a 439-group
  package costs ~0.3 ms instead of ~600 ms per redraw.
- **Divergent status spurious for external references**: group nodes whose
  reference is not in the package carry socket records that depend on
  whatever tree is attached at import time; the picker's divergence
  check drops them.
- **Track Group report** now includes the tree name and the destination
  package; UI strings no longer leak internal jargon.
- **`bpy.app.timers.register` returns None in script/background
  contexts** — the refresh pump tracks its registration with a flag,
  never with the handle.
- **Zone pairs are created with `pair_with_output()`** — a public RNA
  method on the zone input nodes, no operator and no Node Editor area
  (works headless). The importer's `add_zone` machinery (`run_add_zone_operator`,
  `ensure_zone_area`/`restore_zone_area`, zone sessions) was removed
  (~300 lines); all four zone types roundtrip headless (T14b/T14c).
  See the Technical note below.

### Tests

- Smoke suite (Blender 5.1): 99 checks (deterministic roundtrip, minimal
  rename diff, JSON-side check, selective import, zone pairs via
  `add_zone`, commit review, picker states).
- New-node E2E (Blender 5.2): 40 checks (unchanged).

## Technical note: zone node pairing (Simulation/Repeat/Foreach/Closure)

**Zone pairs are created with `node.pair_with_output(output_node)` — a
public RNA method on the zone INPUT subclasses — verified on Blender
5.1.1 and 5.2.0 (2026-08-15). No operator, no Node Editor area; works
headless.**

The earlier investigation (2026-08-08) concluded that
`bpy.ops.node.add_zone` was the only way to establish the pairing, because
it only inspected *properties*: the input subclasses expose `paired_output`
as read-only, the base `Node` and the output subclasses expose nothing.
That conclusion was wrong — the pairing API is an **instance method**
(`pair_with_output`), which was found by checking the RNA function table
(`bl_rna.functions`) on all four zone input types in 5.1 and 5.2, and by
cross-checking how Tree Clipper (`Algebraic-UG/tree_clipper`) imports
zones in its 3-OS CI.

Current architecture (0.2.4):

- The importer creates **all** nodes via `nodes.new` (zone inputs and
  outputs included), populates the zone output's dynamic items
  (repeat_items/state_items/main+generation+input_items/input+output_items)
  during creation, then — after every node exists and before defaults and
  links — calls `input.pair_with_output(output)`. The input node's socket
  set is complete right after pairing. Items live on the OUTPUT node (only
  Repeat/Simulation mirror them onto the input). Legacy data without
  pairing info leaves the nodes unpaired (created as regular nodes).
- `run_add_zone_operator`, `ensure_zone_area`/`restore_zone_area`,
  `begin_zone_session`/`end_zone_session`, the `_zone_session`/
  `_zone_area_state` state and the operators' `finally` cleanup blocks
  are **gone** — no Node Editor area is ever touched, which also removes
  the "imports from layouts without the Node Editor silently drop zone
  input nodes" failure mode.
- The serialized format is unchanged (`zone_paired_node` by name) — no
  migration, no hash change.

The operator remains negligible-cheap (0.4-0.7 ms per call, ~74 ms for all
106 zones of the reference project); the new path drops the context setup
(pin/unpin + temp_override) entirely. The smoke suite now roundtrips all
four zone types headless (T14b/T14c), including evaluation of a repeat
zone through a NODES modifier.

## [0.2.2] - 2026-08-12 (updated 2026-08-13: roundtrip-fidelity fixes and the 5.2 new-node E2E)

### Added

- **Blender 5.2 LTS support.** The full headless test suite
  (180/180 checks) passes on Blender 5.1.1 and 5.2.0 with the same
  code; see `docs/port-5.2.md` for the research and the changes.

### Changed

- **Canonical hash v5 — version-independent fingerprints.** A project
  tracked in 5.1 and opened in 5.2 (or vice versa) converges without
  noise:
  - `node_socket_is_active()` (shared with the importer) keeps only the
    sockets a node actually exposes for its active configuration:
    Compare (A/B of the active data type; C for `mode=DOT_PRODUCT`;
    Angle for `mode=DIRECTION`; Epsilon only for FLOAT/VECTOR with
    EQUAL/NOT_EQUAL), Random Value (active Min/Max + ID/Seed),
    Boolean Math (NOT has a single input), Capture Attribute (5.2-only
    `Selection`), Value to String (5.2-only `Base`/`Padding`),
    Subdivision Surface (5.2-only `Quality`).
  - Engine-dependent properties are excluded: all `bl_*` UI-template
    limits (e.g. `bl_height_max` differs between versions), the 5.2-only
    `vector_dimensions` (Vector input node), `height` (Frame layout,
    auto-resized by 5.2 rebuilds) and `use_fake_user` (file flag).
  - Duplicated canonical link tuples are collapsed: 5.1 exports carry
    the same link twice under type-variant sockets (e.g. Compare `A`
    and `A_INT`, both named "A").
  - Stored baselines migrate automatically via the auto-rebaseline
    mechanism (HASH_VERSION bump).
- **Modifier inputs/outputs are version-gated** (`operators.py`):
  legacy `modifier["identifier"]` custom properties below 5.2, the new
  RNA path (`modifier.properties.inputs/outputs["Socket_N"]
  ["value"/"type"/"attribute_name"]`) on 5.2+.

### Fixed

- **Frame parenting was lost on every import/rebuild**: the serializer
  never wrote the node `parent` (frame) relationship, so children were
  recreated at the right positions but unparented. Nodes now serialize
  their parent frame name, the importer re-parents children in a
  deferred pass (the frame may appear after its children in the package;
  Blender converts the absolute location to the frame's local space, so
  the visual position is preserved), and the canonical hash includes the
  relationship (HASH_VERSION 5 — older packages without the field hash
  the same as an explicit None, so old JSONs keep converging).
- **~310 "socket not found" warnings** (C/Angle/Epsilon) during pulls on
  5.2: the defaults pass now skips inactive socket records.
- **Importing 5.2-exported packages on 5.1**: Compare/Random Value
  sockets resolve by name+type first, so links land on the correct
  type-variant socket.

### Fixed (2026-08-13 — roundtrip fidelity, found by the new-node E2E)

- **`clean_value` serialized a Collection data-block as an empty dict.**
  The ID check ran after the container checks and a Collection's class
  name contains "collection", so it was treated as a `bpy_prop_collection`
  (which iterates its contents). Interface-socket Collection defaults
  now round-trip as `{"type": "Collection", "name": ...}` references
  (8dedd10).
- **`list_items` of the 5.2 list nodes was non-deterministic garbage.**
  `GeometryNodeFieldToList` / `GeometryNodeClosureToList` keep their
  typed items in a `list_items` collection; the generic RNA loop
  serialized it as `bpy_struct` reprs containing memory addresses
  (the JSON changed on every export) and the importer never restored it.
  Items (name + socket type) are now serialized as `list_items_data` and
  recreated before wiring, since the nodes' output socket is named after
  the active item (c83e12e).
- **Interface socket subtypes silently degraded to base types on 5.2.**
  Blender 5.2 reports only `["DEFAULT"]` for the interface-socket
  `subtype` enum items, so the importer's pre-validation (and the
  serializer's subtype read) dropped every subtype: `FloatAngle`,
  `FloatDistance`, `StringFilePath`, ... were recreated as plain
  `Float`/`String`, changing hashes and losing the socket types. The
  enum pre-validation is gone (the `setattr` itself raises on genuinely
  invalid values) and the subtype/remap tables now cover the 5.2
  subtypes (`FloatFrequency`, `FloatMass`, `FloatPixel`,
  `FloatColorTemperature`, `FloatWavelength`, `IntPixel`,
  `StringFilePath`) (968a236).

### Verified (2026-08-13)

- **Default-value precision policy (unchanged, now documented):** socket
  default values are serialized with `round(6)` (six decimals — policy
  since the initial commit); Blender stores float32, so a value like
  `0.8` reads back as `0.800000011920929`, but the roundtrip is stable:
  re-serialization always yields `0.8`. Color defaults are 4-component
  (`bpy_prop_array`), so the RGBA alpha is preserved. Node `location` is
  serialized raw (full float32 precision) and is excluded from the
  canonical hash.
- **New headless E2E for the 5.2 node set** (test #10 of the regression
  suite, see `docs/port-5.2.md`): `tests/test_52_new_nodes_e2e.py` plus
  the generated fixture `tests/gn52_all_nodes.blend` (builder:
  `tests/prepare_52_fixture.py`). The fixture is one object with a NODES
  modifier whose tree covers all 25 instantiable 5.2 node classes and
  every GN-valid socket type (including Menu/Sound/Font/Bundle/Closure/
  IntVector and the `Scene Frame`/`Self Object` interface default-input
  modes). The test verifies: serialization determinism, canonical-hash
  agreement, and that the modifier output geometry (vertex counts,
  attribute values, attribute names) is byte-identical after an import
  roundtrip. 40/40 checks.

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
  dependencies have **duplicated interface socket names** (e.g. two
  identically-named "Tension" sockets in one interface) — name-based
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
  swapped) and Reroute node widths reset to Blender's default. *(Resolved in
  0.2.4: the recreation suite proves canonical-hash-identical roundtrips;
  identifier reordering remains cosmetic and hash-excluded.)*
- **Zone input nodes are only recreated when the serialized data carries
  pairing info.** Data exported by this addon does; older JSON files and
  hand-built trees (e.g. the smoke test's tiny zone nodes, created via
  `nodes.new`) do not, so those zones are dropped. Background mode has a
  virtual screen, so screen availability is never the blocker — see the
  "Technical note: zone node pairing" at the top of this file.
- **Batch imports run from a headless/background session lose some links in
  groups that reference other node groups** *(Resolved in 0.2.4 by the
  positional group-socket resolution — see the 0.2.4 section above:
  439/439 groups recreate identically headless.)*

Remaining known characteristic: very large node trees (hundreds of nodes)
still import slowly because of the per-mutation tree re-validation in
Blender 5.1's node system; this is inherent to the API and cannot be
avoided from Python.

### Findings from the real-project e2e suite (439 groups, real deps)

- **Link fidelity on roundtrip**: links into/out of group-reference nodes
  could land on swapped sockets when a dependency's interface identifiers
  are reordered during import (counts preserved; 8 of 85 links on one
  deep meshing chain). *(Resolved in 0.2.4 — the positional
  group-socket resolution recreates every link exactly; 439/439 groups
  hash-identical from a folder export.)*
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
