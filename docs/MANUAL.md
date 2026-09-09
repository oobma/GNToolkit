# GNToolkit Usage Manual

**JSON → .blend import and sync verification workflow.**

This manual walks through the complete workflow step by step — from
importing a JSON package into a fresh file to verifying that the sync
layer converges cleanly. Each step describes **what to do** and **what
should happen**; do not move to the next step until the current one
completes as described.

Applies to GNToolkit 0.2.4 on Blender 4.0 – 5.2 LTS (tested on 5.1.1 and
5.2.0; the same code and the same workflow run on both).

---

## Terms used

| Term | Meaning |
|---|---|
| **Baseline JSON** | The exported package you start from (the source of truth). |
| **Authoritative JSON** | The JSON you track against after the import — normally the same package, possibly with deliberate changes you want to verify. |
| **Sidecar** | The `<project>.blend.gntsync` file next to the .blend that records tracking state (UUIDs + last known hashes). Saved automatically. |
| **Divergent** | A group whose .blend content differs from its JSON entry. |

---

## Step 1 — Restart Blender after updating the addon

**What to do**

1. Close Blender completely.
2. Open Blender again.
3. Open the GNToolkit panel: Node Editor → sidebar (**N**) → **GN Tools** tab.
4. Check the addon version shown in the panel.

**What should happen**

- The panel shows **GNToolkit 0.2.4**.
- No addon import errors appear in the System Console
  (Window → Toggle System Console).

---

## Step 2 — New file + Import Package/Folder (baseline)

**What to do**

1. File → New → General (clean file).
2. In the GN Tools tab, use **Import Package/Folder** (the full-package
   batch import) and select the **baseline JSON**.
3. Wait for the import to finish (a couple of minutes on large
   projects; progress is shown in the status bar).

**What should happen**

- The import completes with a success message (e.g. "Package import
  finished successfully") and **0 errors**.
- Every group from the package exists in the file.
- The System Console shows no `[ERROR]` / `[CRITICAL ERROR]` lines
  (`[DEBUG]` / `[DEFAULT_VALUE]` lines are normal and harmless).

---

## Step 3 — Save the .blend

**What to do**

1. File → Save As and give the file a name (e.g. `project.blend`).

**What should happen**

- The file saves without errors.
- A **sidecar** file `project.blend.gntsync` appears next to it.
- Saving *before* tracking is **strongly recommended** so that relative
  JSON paths resolve against the file's directory.

**What happens if you track without saving first**

Tracking still works: the metadata is kept in a text block inside the
.blend, the JSON paths start absolute, and the sidecar does not exist
yet. On the **first save** everything settles automatically: the sidecar
is created, stored JSON paths are re-written to relative form when they
live under the project directory (paths elsewhere or on another drive
stay absolute by design), and the text block is saved with the file. The
track operators show an informational message when the .blend is
unsaved — the only real risk is closing Blender without saving, which
discards the session (Blender asks for confirmation).

---

## Step 4 — Track from Existing JSON (authoritative JSON)

**What to do**

1. In the panel, use **Track from Existing JSON**.
2. Select the **authoritative JSON** (the JSON that is the source of
   truth — typically the baseline, possibly with deliberate changes).
3. Wait for the tracking to finish (seconds).

> The operator accepts a **unified package** or a **single-group export**
> (one file from a folder export — the group is tracked against that
> file). For tracking a whole folder export in one click, use
> **Track Folder…** instead (see "Folder workflow" below).

**What should happen**

- A message reports the tracked groups, e.g.
  *"Tracking started: N groups, 0 skipped, M differ from the JSON
  (use Pull to apply)"*.
- **M is a small number** — the honest content differences between the
  imported file and the JSON. If you deliberately changed defaults or
  added nodes in the JSON, those groups **must** appear as divergent.
- The JSON file is **read only** — it is not modified.

> If the report says "0 differ from the JSON" while the JSON contains
> deliberate changes, stop and check the setup: tracking may have used
> the wrong JSON or the file was not the freshly imported baseline.

---

## Step 5 — Refresh Status

**What to do**

1. Click **Refresh Status** in the panel.

**What should happen**

- The panel shows a summary: **Total / Synced / To pull** (and other
  issue buckets).
- The **Sync Issues** panel shows one entry per divergent group with the
  state **Changed in JSON** (the list is collapsible via its header
  triangle; likewise the **JSON files** list in the Sync panel).
- Each issue row provides per-group actions: **Pull**, **Ignore**
  (and Commit / Keep JSON / Keep Blend for other states).

---

## Step 6 — Pull from JSON (batch) — the key step

**What to do**

1. Click **Pull from JSON** (the batch action, not the per-group one).
2. Wait for it to finish. **This is the longest step**: the pull
   rebuilds the whole connected component of the changed groups
   (dependencies *and* parents) so that no group is left with broken
   links. On large projects this can take several minutes; progress is
   shown in the status bar.

**What should happen**

- **Blender must not crash.**
- The report shows, e.g.:
  *"Pulled N, skipped M, errors 0, auto-tracked 0, 0 still differ after
  pull"* (numbers vary per project).
- **errors 0** and **0 still differ** are the important parts.
- The System Console shows `[OK] Reconstruction of node '...'
  completed.` lines without `[CRITICAL ERROR]`.

> If the pull crashes or reports errors, collect the exact message and
> stop — do not continue to the next step.

---

## Step 7 — Refresh Status + save checkpoint

**What to do**

1. Click **Refresh Status** again.
2. Save the .blend (File → Save) as a checkpoint.

**What should happen**

- The summary shows **all groups Synced, 0 issues**.
- The issues list is empty.
- The refresh itself does not modify the .blend; saving persists the
  aligned state. (Without saving, a later close/reopen would reload the
  pre-pull .blend and the statuses would no longer match — a false
  alarm, not a data problem.)

> **Note (debugging only):** the batch Pull only rebuilds groups that
> differ — after a clean pull, running it again finds nothing to do
> (report "Pulled 0, skipped N, errors 0") and changes nothing. Running
> it back-to-back is **not part of the normal workflow**; it is only a
> sanity check that the sync is idempotent (useful after updating the
> addon or when investigating odd behavior). If a second pull rebuilds
> hundreds of groups or introduces new divergence, report it — that is
> a regression.

---

## Step 8 — Verify the applied changes

**What to do**

1. Open a group that had a deliberate JSON change.
2. Inspect the values/nodes you know were changed (interface defaults,
   node counts, wiring).

**What should happen**

- The values match the authoritative JSON exactly (e.g. changed default
  parameters are applied, added nodes are present and wired).

---

## Step 9 — Per-group Pull

**What to do**

1. Note: the per-group **Pull** button only appears inside the issue
   entry of a group (for groups in **Changed in JSON** / **Missing in
   Blend** state). If everything is Synced, there is nothing to pull
   per group.
2. To exercise the per-group path on a fully synced project, invoke the
   per-group pull on any group (e.g. from the Python console via the
   operator, or by making a JSON change and re-tracking).

**What should happen**

- The per-group pull uses the **same component-wide rebuild** as the
   batch: it can take as long as the batch pull. This is by design —
   safety over speed.
- A group that already matches the JSON stays identical and the
   project remains all Synced (0 errors, 0 warnings).

---

## Interpreting the pull report

| Report line | Meaning |
|---|---|
| `Pulled N` | N groups were rebuilt from the JSON. |
| `skipped M` | M groups were up to date and left untouched. |
| `errors 0` | No rebuild raised errors. |
| `0 still differ after pull` | Every rebuilt group now matches the JSON. |
| `N still differ after pull` | N groups could not be reproduced by the rebuild. They **stay visible** as **Changed in JSON** (never silently hidden) — report the names; pulling again will not fix them. |

The sync never hides un-repaired divergence: groups whose rebuild could
not reproduce the JSON keep a divergent baseline and remain listed as
issues.

---

## Additional workflow 1 — Selective import (one group + dependencies)

**What to do**

1. In the Sync panel, click **Import Group from JSON…** and select a JSON
   package.
2. In the picker that appears in the panel, use the search field (native
   Blender search widget) to find a group.
3. Select the group and inspect the **plan preview** under the field:
   - CHECKMARK: already in the .blend (not touched),
   - NODETREE: will be imported (missing dependencies included),
   - ERROR: exists in the .blend but differs from the package,
   - STATUS_WARNING: referenced but not in this package.
4. Click **Import** to import the group plus its missing dependencies.
5. If you want the imported group tracked, click **Track to JSON…** and
   choose the destination package (the group is written into it and
   becomes sync-tracked — the other groups in the package are kept).
6. Click **Close Picker** when done.

**What should happen**

- Only the selected group and its missing dependency closure appear;
  isolated package groups are not imported.
- Groups already in the .blend are listed under "Already in the .blend
  (not touched)" — they are **never overwritten** by the picker.
- The report shows the imported groups and any warnings (divergent
  members, unconnected external refs).
- The picker refreshes live: editing the package JSON on disk while the
  picker is open updates the preview within a second.

---

## Additional workflow 2 — Commit with Review

**What to do**

1. Edit one or more groups locally in the .blend.
2. Click **Commit with Review…** in the Sync panel.
3. For each listed group choose: **Keep Blend** (commit the .blend
   version into the JSON), **Keep JSON** (pull the JSON version into the
   .blend) or **Skip** (leave it untouched). Conflicts default to Skip.
4. Click OK to apply.

**What should happen**

- Only locally edited groups appear in the dialog — unedited groups are
  ignored.
- The report shows the applied decisions
  ("Committed N, pulled M, skipped K").
- Groups with unedited content stay untouched and synced.

---

## Additional workflow 3 — Check on open

**What to do**

1. With the addon enabled, save the .blend and close it.
2. Edit a tracked JSON file outside Blender (e.g. change a default value)
   and save it.
3. Reopen the .blend.

**What should happen**

- Within a couple of seconds a non-intrusive notice appears:
  "GNToolkit: N group(s) out of sync with JSON" (status bar, a warning
  row in the Sync panel and entries in the Sync Issues list).
- Open the Sync Issues list: the affected groups appear as
  **Changed in JSON** — use **Pull** to apply the JSON changes.
- The plug icon next to **Refresh Status** toggles this check
  (`check_on_load` pref). When disabled, no notice is shown on open.

---

## Additional workflow 4 — Folder export round-trip (one file per group)

The folder export ("Export Package" with **Use Folder Structure**) is a
full sync participant: every group lives in its own `NodeGroups/*.json`
file and modifiers in `Modifiers/*.json`.

**What to do**

1. In the GN Tools tab, run **Export Package** with **Use Folder
   Structure** enabled and choose a directory.
2. In the Sync panel, click **Track Folder…** and select **any file
   inside the exported folder** (the `NodeGroups/` subfolder is detected
   automatically).
3. Work and commit normally: **Commit** (per-group or batch) writes each
   edited group back to **its own file** — a per-group file becomes a
   one-group package on the first commit; both shapes are interchangeable
   for every reader.
4. To recreate the project elsewhere: **Import Package/Folder** and pick
   any file inside the folder — every group is imported (dependency-first)
   and the stored modifiers are attached to existing objects with matching
   names (**Apply Modifiers** is on by default).

**What should happen**

- Track Folder reports the linked/skipped counts; every group shows
  **Synced** right after tracking.
- The Sync panel's **JSON files** list shows one row per file (collapsed
  by default — use the triangle to expand it).
- Recreation reproduces the project exactly: node/link counts and
  canonical hashes are identical, dependencies are resolved first, and
  groups that already exist in the target file are skipped (unless
  **Update existing groups** is checked).

---

## Additional workflow 5 — Collaborate with Git

**What to do**

1. Export the project with **Export Package → Use Folder Structure**
   (one JSON per group), then `git init` + commit + push it with your
   git client (see `docs/GIT_COLLAB.md` for the full manual flow).
2. In the GN Tools tab, the **Collaboration** panel shows the detected
   repository, its state (`clean` / `N to commit` / `ahead` / `behind`)
   and the buttons:
   - **Git Commit…** — message dialog; stages only the tracked JSONs
     that changed and commits locally.
   - **Git Sync** — `pull --ff-only` + `push`. Remote changes arrive
     only when your branch is simply behind; if versions diverged, it
     refuses with a clear message.
3. Use **Pull from JSON** (Sync panel) after a sync to apply changed
   JSONs to the .blend.

**What should happen**

- Git Commit reports the number of committed files; the repository row
  returns to `clean`.
- Git Sync on a behind branch fast-forwards and pushes; on divergent
  branches it refuses without touching any file.
- A JSON with merge-conflict markers is listed in the panel (and
  reported when tracking/importing); after resolving it with your git
  client, **Pull from JSON** applies the resolved content.

---

## References between groups and the package

Two principles drive everything below:

- References between groups are stored **by name**
  (`node_tree_reference`) — the package never embeds the referenced
  group's content.
- **Commit** (per-group) is **surgical**: it writes only the committed
  group, never its dependencies. **Track to JSON… / Track Group** writes
  the group **and its untracked dependencies** — the recommended path for
  a new group that depends on others.

| # | Situation (G = the group you commit, R = the referenced group) | On commit | On import elsewhere | What to do |
|---|---|---|---|---|
| 1 | R exists in the .blend but **not** in the package | Commit succeeds and shows **Synced**; the package is left with a **dangling reference** (no warning) | R's group node is created **without an assigned tree** → links into/out of it do not connect; the import picker warns "Unconnected refs" | Use **Track to JSON…** (includes R automatically) or add R to the package |
| 2 | R in the package, .blend and package **match** | Commit succeeds | Resolves correctly | — |
| 3 | R in the package, .blend and package **differ** | Commit succeeds (the package keeps the older version of R) | The reference resolves to the package's R; if the **interfaces** differ, socket warnings and links that do not connect are possible. R shows as divergent in Sync Issues | Resolve R's divergence (Pull / Keep JSON) when needed |
| 4 | **New** group with dependencies | — | — | Use **Track to JSON… / Track Group** (writes the group + dependencies); Commit alone is not enough |

Additional notes:

- **Groups in the .blend that no book knows**: not a conflict and not an
  error — they are simply invisible to the sync (untracked). "All synced N"
  only speaks about the tracked groups.
- The import picker always shows the plan preview before importing:
  references that are external to the package are flagged as
  STATUS_WARNING before anything is imported.

---

## Troubleshooting

**"…is not valid UTF-8 — re-save it as UTF-8…" when tracking/importing**

The JSON file was saved by an external tool in a different encoding
(e.g. Notepad "ANSI"/Windows-1252). The addon reads UTF-8 (with or
without BOM) and refuses to guess, because a wrong guess would silently
corrupt the source of truth. Fix: open the file in a text editor and
re-save it as UTF-8 (most editors: File → Save As → UTF-8), then retry.

**"Dangling links" message / pull aborted**

After a Blender crash mid-rebuild, links can be left in an inconsistent
state. The pull checks every tree first: dead links are stripped
automatically (their content is rebuilt from the JSON) and, if anything
remains, the pull aborts with a clear message instead of crashing.
Fix: save, restart Blender, pull again.

**Blender still crashes during or after a pull**

Collect `blender.exe.stacktrace.txt` / `crash.log` from Blender's config
directory. Known mitigations already in place: external links are
removed before a group's interface is rebuilt (and restored from a
snapshot), zone pairs are created directly via `pair_with_output` (no
Node Editor area involved), the depsgraph is flushed before the operator
returns, and every rebuilt tree is validated for dangling links.

**The System Console shows nothing**

Interactive sessions may not flush print output to the System Console;
the panel summary, the status-bar progress and the operator's report
messages are authoritative.

**The per-group pull takes as long as the batch**

Expected. Both pull paths rebuild the whole connected component
(dependencies and parents) so that renumbering an interface can never
break the links of an unrebuilt parent group.

---

## Notes

- The addon **never writes the .blend**; it only writes the sidecar and
  the JSON files you explicitly commit to.
- All state for the sync layer lives in the sidecar — keep it next to
  the .blend when moving or backing up projects.
