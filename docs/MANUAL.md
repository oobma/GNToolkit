# GNToolkit user guide

GNToolkit versions node groups as deterministic JSON. The JSON is the
source of truth; the `.blend` is your working file. This guide covers the
daily loop, conflicts, history, the graph views, sharing and the declared
limits. For the CLI and CI, see [AUTOMATION.md](AUTOMATION.md). For Git,
see [GIT_COLLAB.md](GIT_COLLAB.md).

This guide follows the current development branch; check the release
notes for the exact feature set of your installed version. The UI labels
quoted here are the current ones. If your Blender shows an older layout,
update the add-on and restart Blender.

## The model

| Term | Meaning |
|---|---|
| **JSON** | The exported node-group data. The source of truth - readable, diffable, versionable with Git. |
| **.blend** | The working cache where you edit and preview. GNToolkit never writes it on its own. |
| **Sidecar** | The `<project>.blend.gntsync` file next to the `.blend`. Records which entries are tracked, their UUIDs and their last known hashes. |
| **Canonical hash** | A SHA-256 fingerprint of an entry's content, ignoring node positions and creation order. |

Each entry has one status:

| Status | Meaning | Typical action |
|---|---|---|
| **Synced** | Both sides match. | - |
| **Edited locally** | Only the `.blend` changed. | Save to JSON |
| **Changed in JSON** | Only the JSON changed. | Pull from JSON |
| **Conflict** | Both sides changed. | Merge, Keep JSON or Keep Blend |
| **Missing in blend** | The entry no longer exists in the `.blend`. | Restore from JSON or stop tracking |
| **Untracked** | Not tracked against any JSON. | Track |
| **JSON missing** | The JSON file is gone. | Re-create JSON or stop tracking |

## Install and open

Install GNToolkit as an extension (platform or release zip) and enable
it. Open the Node Editor and find the **GNToolkit** tab in the sidebar
(`N`). The tab has five panels: **Sync**, **Track**, **Explore**, **Git**
and **Advanced** (with **Project Health** and **Geometry Nodes
Validation**).

Save the `.blend` before tracking so JSON paths are stored relative to
it. Tracking an unsaved file works, but the sidecar and relative paths
settle on the first save.

## Start tracking

There are three ways to start; use the one that matches what you already
have.

### From the .blend: Track all groups...

**Track > Track all groups...** writes one master JSON from the current
`.blend` and tracks every trackable node group (Geometry Nodes, Shader
and Compositor).

- Each group gets a UUID stored on its node tree.
- The JSON is updated surgically: entries with no counterpart in the
  `.blend` (for example groups removed locally) are preserved.
- Library-linked groups are skipped; make them local first if you want
  to track and pull them.
- When more than one group will be tracked, the operator shows a
  confirmation with the group count, the already-tracked count and any
  library-linked groups that will be skipped.

Materials and worlds are tracked separately (see below).

### From a folder export: Track from folder...

**Track > Track from folder...** asks for any file inside a folder export
(`NodeGroups/`, plus `Materials/` and `Worlds/` when present) and tracks
every group, material and world found against its own file.

- Dependency edges are recorded.
- Already-tracked entries are skipped.
- This is the right action for a folder export. **Track from Existing
  JSON** accepts a single `.json` file, not a folder.

### From an existing JSON: Track from Existing JSON

**Track > Track from Existing JSON** starts tracking from an existing
unified package or single-group export file. The JSON is read only and
is **not** modified.

- If the JSON already differs from the `.blend`, those groups appear as
  **Changed in JSON** so you can decide when to pull.
- Use **Track from folder...** instead when the source is a folder with
  one file per group.
- Use this after importing a package when the JSON is already
  authoritative.

### Materials, worlds and the scene compositor

- Open a material or world in the Shader Editor and use **Track
  Material** / **Track World** in the **Track** panel. **Track all
  materials...** / **Track all worlds...** do it in batch.
- In Blender 5.x, with a scene compositor set up, use **Track scene
  compositor...** in the **Track** panel (or **Track Group** from the
  Compositor editor, or **Track all groups...**).
- Embedded entries (materials/worlds) appear with their own icon and
  have the same statuses, save, pull, conflict and merge behavior as
  node groups.

## Daily loop

1. Edit in the `.blend`.
2. Press **Refresh Status**. On large projects it runs in the background
   (`Checking x/y...`) with a **Cancel** button; the UI stays responsive.
3. Open **Changes** (in the Sync panel). It lists every out-of-sync
   entry, with status filters and a 50-row default cap (**Show all**
   reveals the rest).
4. Save or pull the entries you want.

### Changes and marks

Nothing is marked by default, like VS Code's Source Control.

- **Mark** (`+`) selects an entry for the next save; **Unmark** (`-`)
  removes it. The header has mark-all/unmark-all.
- Marked rows regroup at the top under **Marked (N)**.
- The primary button is **Save selected (N)** when rows are marked and
  **Save changed (N)** otherwise.
- **Save all tracked groups...** lives in the secondary menu and asks for
  confirmation.
- `...` opens the per-row menu: **Go to item**, **History**, **Save to
  JSON**, **Pull from JSON**, **Merge with JSON**, **Keep JSON**, **Keep
  Blend**, **Discard local changes**, **Ignore** and **Stop tracking**
  (the exact entries depend on the status).

A marked conflict is saved with the `.blend` version - an explicit
choice. Unmarked entries are left untouched.

### Save to JSON

**Save to JSON** writes the `.blend` version of the entry into its JSON.
The add-on writes atomically and keeps backups for mass operations (last
five snapshots under `backups/` next to the `.blend`).

### Pull from JSON

**Pull from JSON (N)** brings the JSON into the `.blend`. The pull
rebuilds the whole connected component of the changed groups
(dependencies and parents) so that no group is left with broken links.
On large projects this can take minutes; progress is shown in the status
bar, and the report says how many groups were pulled, skipped, and how
many still differ.

A group whose rebuild cannot reproduce the JSON stays visible as
**Changed in JSON**; it is never silently hidden.

### Check on open

With **Check JSON on open** enabled in the add-on preferences, opening a
`.blend` compares the JSON hashes in the background and shows a
non-intrusive notice when files changed outside Blender - a status-bar
message and a warning above the Changes list. Use **Pull** on those
entries to apply the JSON changes.

## Import and share

### Export Package

**Advanced > Package > Export Package** writes the node groups, NODES
modifier setups, materials and worlds.

- **Use Folder Structure** (on by default) writes one file per entry
  under `NodeGroups/`, `Modifiers/`, `Materials/` and `Worlds/`.
- Unticked, it writes one unified JSON package.
- The **Include:** switches filter Geometry Nodes groups, Shader groups,
  Compositor groups, Modifiers, Materials and Worlds. All are on by
  default; NODES modifiers are exported only together with Geometry
  Nodes groups.

### Import Package/Folder

**Import Package/Folder** reconstructs entries from JSON:

- Pick a package file, or **any file inside a folder export** to
  recreate the whole folder.
- Dependencies are resolved first.
- **Update existing groups** (off by default) rebuilds entries that
  already exist in the file; unchecked, existing entries are left
  untouched.
- **Apply Modifiers** (on by default) attaches stored modifier setups to
  existing objects with matching names.

A package stores node logic, not scene data. On a fresh file the groups
are recreated but there are no objects for the stored modifiers to
attach to.

### Selective import

**Import Group from JSON...** opens a searchable picker in the Sync
panel:

1. Pick a package, then search for an entry (group, material or world).
2. The plan preview shows what will happen:
   - **CHECKMARK** - already in the `.blend` (never touched).
   - **NODETREE** - will be imported, with missing dependencies.
   - **ERROR** - exists in the `.blend` but differs from the package
     (align it with Track + Pull / Keep JSON).
   - **STATUS_WARNING** - referenced but not in this package.
3. **Import** imports the entry plus its missing dependency closure.
   Existing entries are never overwritten; the picker stays open for
   several imports.
4. **Track to JSON...** (node groups) writes the selected group and its
   untracked dependencies into a package of your choice and starts
   tracking it.
5. **Close Picker** closes it. The preview refreshes live while the
   picker is open.

### Folder round-trip

A folder export is a full sync participant:

1. **Export Package** with **Use Folder Structure**.
2. **Track from folder...** and pick any file inside the export.
3. Work normally: **Save selected** / **Save changed** writes each entry
   back to its own file. A per-group file becomes a one-group package on
   the first save; all readers accept both shapes.
4. Recreate elsewhere with **Import Package/Folder**, or start tracking
   from the folder on another machine.

## Dependencies

References between groups are stored **by name**; a package never embeds
the referenced group's content. **Save to JSON** writes only the selected
entry; **Track to JSON...** and **Track all groups...** include the
group's untracked dependencies. If a tracked group references a group
that is not in the package, a clone creates the group node without an
assigned tree and the import picker reports the unconnected reference;
add and track the missing group.

## Conflicts

A **Conflict** means the entry changed in the `.blend` and in the JSON
since the last sync. You have three exits:

- **Keep JSON** - discard the local edits and take the JSON version.
- **Keep Blend** - keep the `.blend` version and overwrite the JSON
  (mark the row and save, or use the menu).
- **Merge with JSON** - decide per item.

### Merge per item

Merge keeps your local edits and applies the JSON changes on top. The
common base is recovered from the JSON file's Git history (the newest
revision whose entry hash matches the recorded baseline - no new storage).

- If the merge is clean, the merged entry is written and the group is
  rebuilt; it ends **Synced**.
- If items changed differently on both sides, **nothing is written**.
  The report lists each item with its Ours / Base / JSON values and an
  **Ours / Base / JSON** choice, plus a custom value for simple scalars
  (float, int, bool, string).
- If the structure changed differently on both sides, pick one whole
  side.
- **Apply Chosen Merge** writes the merged package and rebuilds the
  entry once every conflict has a decision.

The editor works on the sides captured when Merge was pressed. If the
entry changed after that, Apply asks for a new Merge. Merge requires the
JSON file to be committed in a Git repository; otherwise it reports why.

Merge covers Geometry Nodes, Shader and Compositor groups and
material/world embedded trees, tree plus owner settings. It merges
values; the structure is adopted whole from the side that changed it.
Exact-content renames (same logic under a new name) are matched and
merged under the new name for node groups.

The conflict report produced by **Git sync** is informative: resolve
those items with each entry's Merge or with your Git client.

## History (time machine)

The **History** button next to the active entry (and in the Changes row
menu) opens the entry's JSON revisions:

- Date, author and commit message, newest first.
- A readable diff of the selected revision against the current entry.
- **Restore This Revision** writes that entry back into the JSON.

Only the JSON is touched: the entry becomes **Changed in JSON** and a
Pull rebuilds it when you decide. The `.blend` and the rest of the
package stay as they are.

On the **GNT Graph** board, use the bar's History button or enable
**Follow board selection** in Explore. Selecting a revision marks the
groups of the same file that differ from today and wraps the board in a
labelled **HISTORY MODE** frame; closing the box clears the marks. The
box says so when there is no repository or the file was never committed.

## Explore: graph views

### Graph view

**Explore > Graph view...** draws the dependency graph as a disposable
node tree:

- the whole project,
- a **cluster view** (one bar per name prefix, aggregated status and
  edges),
- the neighborhood of the selected group (± hops), optionally including
  untracked groups.

Groups appear as compact bars sized to their names, coloured by sync
status; cables that jump columns travel through routing nodes. Nothing
is saved. **Clear graph** removes the board.

The board is a snapshot. A completed **Refresh Status** recolors the
existing bars, but after a status change the board also shows a stale
notice with a **Rebuild** button. Rebuild uses the original scope and
refreshes the structure.

### Graph poster

**Graph poster...** exports the same model as a standalone SVG (no
Graphviz needed) with the status legend and per-status counts. The
Layout option can use the compact automatic layout or **As arranged on
the board** to reproduce the current node-editor arrangement.

### Project Health

**Advanced > Project Health** has an **Audit** section (Run / Clear). It
runs in the background with progress and Cancel and reports:

- **duplicates** - groups sharing a content fingerprint under different
  names,
- **JSON health** - missing, unreadable or conflicted files and
  references to untracked groups,
- **impact** - what depends on the group selected in the Node Editor,
  following the dependency graph transitively.

The same audits run headless with `gnt_check.py` (see
[AUTOMATION.md](AUTOMATION.md)).

**Geometry Nodes Validation** is a separate panel: **Validate** runs
generic checks (missing attributes, unlinked inputs, type mismatches) in
the background, with **Validating x/y...** and Cancel. It reports
warnings only and never blocks; each issue offers **Go to node**.

## Git collaboration

The **Git** panel shows the repository behind the tracked JSONs, its
state, commit and sync actions, cosmetic-only discard and repository
history. **Git commit...** stages only the tracked JSONs; **Git sync**
pulls, pushes and, on a diverged branch, merges the tracked JSON files
per item.

Full guide, including the two engines, credentials and SSH limits:
[GIT_COLLAB.md](GIT_COLLAB.md).

## Large projects

- **Refresh Status** processes the project in small batches and keeps
  the UI responsive. The result appears in the status bar and in the
  Changes list.
- **Track all groups...** and **Save all tracked groups...** show a
  confirmation with counts; Track All skips it when a single group is
  affected.
- Before overwriting JSONs in a mass operation, the add-on copies them
  to `backups/<timestamp>/` next to the `.blend` (relative layout
  preserved). The last five snapshots are kept; older ones are removed.
- If a batch pull fails for some groups, the panel lists their names and
  reasons with **Retry Failed** and **Dismiss**.

## Declared limits

GNToolkit versions node logic, not scene data:

- Objects, meshes, transforms and object material slots are not stored.
- Texture node trees are never versioned.
- Animation (keyframes, drivers, NLA) is not saved; animated entries are
  flagged in the panel and by `gnt_check.py` instead of passing as
  silently synced.
- Third-party custom group nodes keep their per-instance inner tree
  embedded with the node; the node class itself must be installed for
  the node to work.
- OSL Script nodes version their code. `External` mode is reference-only:
  the `.osl` file lives outside, like images.
- Compositor trees with Render Layers/Cryptomatte depend on the scene's
  view layers and passes, which are not versioned. A clone into a file
  without that scene cannot recreate those pass sockets/links; pulling
  into the project file keeps them.
- In Blender 4.2 the scene compositor is not a node group; only reusable
  compositor groups are covered there.
- Library-linked entries can be exported but never pulled back; make them
  local first.

## Troubleshooting

**"Is not valid UTF-8 - re-save it as UTF-8"**

The JSON was saved by an external tool in another encoding (for example
Notepad ANSI/Windows-1252). Re-save it as UTF-8 and retry. The add-on
refuses to guess, because a wrong guess would corrupt the source of
truth.

**"Dangling links" / pull aborted**

After a Blender crash mid-rebuild, links can be inconsistent. The pull
strips dead links first and rebuilds their content from the JSON; if
anything remains, it aborts with a message instead of crashing. Save,
restart Blender and pull again.

**Blender crashes during or after a pull**

Collect `blender.exe.stacktrace.txt` / `crash.log` from Blender's config
directory. The pull removes external links before rebuilding an
interface, creates zone pairs directly, flushes the depsgraph and
validates the rebuilt trees for dangling links.

**Import on a fresh file recreates groups but no objects or modifiers**

Expected: the package stores node logic only. Run the import on the
working `.blend`, or recreate the objects first; with **Apply Modifiers**
checked, each stored modifier attaches to the object with the same name.

**The System Console shows nothing**

Interactive sessions may not flush console output. The panel summary,
the status-bar progress and the operator messages are authoritative.

**Per-group pull takes as long as the batch**

Expected: both paths rebuild the whole connected component
(dependencies and parents) so that renumbering an interface cannot break
the links of an unrebuilt parent group.

## If you already have a JSON export

1. Save the `.blend` first.
2. **Advanced > Package > Import Package/Folder**: pick the package, or
   any file inside a folder export.
3. If the JSON is authoritative, start tracking it:
   - a single package or single-group file: **Track from Existing
     JSON**;
   - a folder export: **Track from folder...**.
4. **Refresh Status**. Entries that differ from the imported `.blend`
   show as **Changed in JSON**.
5. **Pull from JSON** to apply the authoritative JSON.

The JSON is read only during tracking; the add-on does not modify it.
