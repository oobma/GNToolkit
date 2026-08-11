# GNToolkit Usage Manual

**JSON → .blend import and sync verification workflow.**

This manual walks through the complete workflow step by step — from
importing a JSON package into a fresh file to verifying that the sync
layer converges cleanly. Each step describes **what to do** and **what
should happen**; do not move to the next step until the current one
completes as described.

Applies to GNToolkit 0.2.1 on Blender 4.0 – 5.1.x.

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

- The panel shows **GNToolkit 0.2.1**.
- No addon import errors appear in the System Console
  (Window → Toggle System Console).

---

## Step 2 — New file + Import Package (baseline)

**What to do**

1. File → New → General (clean file).
2. In the GN Tools tab, use **Import Package** (the full-package batch
   import) and select the **baseline JSON**.
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
- Saving *before* tracking is required so that relative JSON paths
  resolve against the file's directory.

---

## Step 4 — Track from Existing JSON (authoritative JSON)

**What to do**

1. In the panel, use **Track from Existing JSON**.
2. Select the **authoritative JSON** (the JSON that is the source of
   truth — typically the baseline, possibly with deliberate changes).
3. Wait for the tracking to finish (seconds).

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
- The **Sync Issues** list shows one entry per divergent group with the
  state **Changed in JSON**.
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

## Troubleshooting

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
snapshot), zone pairs are created through a single pinned Node Editor
session per tree, the depsgraph is flushed before the operator returns,
and every rebuilt tree is validated for dangling links.

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
