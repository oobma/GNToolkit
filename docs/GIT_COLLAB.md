# GNToolkit — Collaborating with Git

GNToolkit tracks node groups (Geometry Nodes, Shader and Compositor) and
the Shader Editor's materials and worlds as deterministic JSON files,
which makes every Git client a collaboration backend for them. Two ways
to work:

1. **Manual flow** — any Git client (CLI, GitHub Desktop, VS Code, …);
   the addon only writes/reads the JSON files. Zero addon features
   required.
2. **Git panel** — the addon's thin Git transport: repository
   state, commit, sync and history from inside the GNToolkit tab.

Git remains the authority for history and merging — the addon never
reimplements version control and never runs its own server. This guide
follows the current development branch; check the release notes for the
exact merge coverage of your installed version.

## Which engine do I have?

| Build | Engine | Remotes | Credentials |
|---|---|---|---|
| Platform extension | Bundled (pure Python) | HTTPS and local files | Add-on Preferences / OS keyring |
| GitHub release zip | System `git` when available; otherwise the bundled engine | HTTPS and local files; SSH only with system `git` | Your Git client (credential manager, SSH keys) or add-on preferences |

The engine is selected automatically; you can override it in Add-on
Preferences. The manual flow with your own Git client always works,
regardless of the add-on engine.

## Local repo or web repo — same thing

The addon does not run a server. **Git sync** and the manual flow use
whatever remote the repository is configured with. That remote can be:

- a local folder or bare repo (`C:\backups\gntoolkit.git`),
- a network share,
- a web host — GitHub, GitLab, Bitbucket, Gitea, …

For the bundled engine the remote must be **HTTPS or a local file
path**; SSH is rejected. The system-Git engine supports SSH. If `git
push` works from your terminal and you use the system-Git engine, the
panel uses the same credentials; with the bundled engine, save the host
credentials in the add-on preferences (or provide them through the OS
keyring).

### Going from local to a web remote (one-time setup)

GitHub is used as the example below; GitLab, Codeberg, Gitea, Bitbucket
and self-hosted hosts work the same way — only the repository URL
changes.

1. **Create an empty repository** on your host (no README, no license —
   or you will have to merge unrelated histories). Note the HTTPS URL:
   `https://github.com/<user>/<repo>.git`.
2. In the folder that already holds your exported JSONs and is already
   a git repository:
   ```
   git remote add origin https://github.com/<user>/<repo>.git
   git push -u origin main
   ```
3. With the **system-Git engine**, the first push opens the **Git
   Credential Manager** window (browser): sign in once, authorize, and
   the credentials are stored for later syncs. With the **bundled
   engine**, save the host credentials in the add-on preferences (or
   provide them through the OS keyring); the next sync uses them.

From then on, the Git panel's **Git sync** pushes and pulls
from the remote exactly as it did from the local one, and your
colleagues just run:

```
git clone https://github.com/<user>/<repo>.git
```

### HTTPS vs SSH

- **HTTPS** (recommended): works with both engines. With system Git, the
  one-time credential manager sign-in above; with the bundled engine,
  save the token in the add-on preferences.
- **SSH**: supported by the system-Git engine (GitHub build). The
  bundled engine rejects SSH remotes; use HTTPS or a local file remote
  instead.

### Two gotchas before sharing a repo

- **Save the .blend before tracking** — JSON paths are stored relative
  to the .blend, so save it first for a portable repo. Tracking an
  unsaved file works locally, with absolute paths until the first save.
- **Keep the .blend out of the repo** (add it to `.gitignore`): JSONs
  are the shared source of truth; the .blend is personal working state.
  The addon never stages or pushes the .blend, but your own git
  commands might.

## Manual flow (works with any Git client)

1. **Export** the project with **Export Package → Use Folder Structure**
   (one JSON per entry under `NodeGroups/`, `Modifiers/`, `Materials/`
   and `Worlds/`) — per-group files give the cleanest diffs and the
   fewest conflicts.
2. **Publish**: run `git init`, stage the folders that exist
   (`git add NodeGroups Modifiers Materials Worlds`), commit with
   `git commit -m "initial"`, add a remote and `git push`.
3. **On the other machine**: clone the repository, open (or create) the
   .blend, run **Advanced → Package → Import package/folder...** (pick any JSON inside the
   folder) and **Track from folder…** to start tracking every group against
   its own file.
4. **Daily loop**:
   - Edit groups in the .blend → **Save changed** (addon writes the JSONs) →
     `git commit` / `git push` (your client).
   - A colleague pushed changes → `git pull` (your client) → the addon's
     **check on open** notices the JSON files changed ("N group(s) out
     of sync with JSON") → **Pull from JSON** applies them to the .blend.
5. **Conflicts**: if two people edited the *same* group, `git pull`
   leaves conflict markers in that JSON. The addon reports the file
   ("has merge conflicts — resolve them with your git client"); resolve
   the file with your client (or `git checkout --theirs/--ours`) and
   then **Pull from JSON** again. In the manual flow the addon does not
   resolve Git's text conflict markers. (Editing the *values* of the
   same group on both sides is a different case: when the addon itself
   reports the group as **Conflict** — local .blend edits against changed
   JSON — the **Merge** action resolves it per item; see “Merging
   conflicted groups” below. The Git panel's **Git sync** also uses that
   semantic merge on diverged branches.)

## Git panel (thin git transport)

Appears in the **GNToolkit** tab when entries are tracked. The transport
engine is bundled with the add-on (pure Python); the GitHub build
additionally uses a system `git` automatically when one is present. See
[Which engine do I have?](#which-engine-do-i-have) for credentials and
remote support.

- **One row per repository** detected from the tracked JSONs (a project
  can point at several repos), showing: the repository name, the state
  — `clean`, `N changed`, `ahead N`, `behind N` — and the buttons:
  - **Git commit...** — opens a dialog for the commit message; stages
    **only the tracked JSON files that changed** (never `git add -A`,
    never the .blend) and commits locally.
  - **Git sync** — pull, then push. When the local
    branch is simply behind, the remote changes arrive; when local and
    remote **diverged**, the tracked JSON files are merged per item
     (values, modifier inputs, Geometry Nodes/Shader/Compositor groups,
     materials and worlds, and exact-content renames per entry or per
     renamed file
    — `ours` = your HEAD,
    `theirs` = the upstream branch, `base` = their merge-base), written,
    committed as a real two-parent merge commit and pushed. Any item
    that cannot be decided aborts the whole operation with a per-item
    report in the merge report box and **nothing is written**; files that
    are not tracked JSON changed on both sides also abort (they are never
    merged blindly). That sync report is informative: conflict items are
    resolved in the add-on (per-group Merge) or with your Git client, not
    from the report box itself. Tracked files must be committed first — the refusal
    message says so. After a clean sync the sync status cache is
    refreshed so a changed JSON shows up as "Changed in JSON"
    immediately. When the pull brought changes, the tracked groups of
    the updated files are re-checked automatically — no manual
    **Refresh Status** needed.
  - **Reveal** — open the repository folder in the file explorer.
- **Merge conflicts**: when a tracked JSON carries conflict markers the
  panel shows a warning row per file with a Reveal button.
- **Cosmetic-only discard**: when Git reports changed tracked JSONs while
  GNToolkit is synced, the panel explains that Git compares worktree vs
  HEAD. **Discard cosmetic-only changes** verifies semantic equality
  against HEAD and restores the files only when no node/value changed;
  otherwise it aborts and reports the file.
- **History** (collapsible):
  - *Repository history* — the latest commits of the repository (shown
    when exactly one repository is detected).
  - *JSON history* — the entry's revisions live in **Explore** (History);
    the Git panel no longer duplicates a per-file Git log.
- `ahead`/`behind` reflect the last fetch (like `git status -b`) — run
  **Git sync** (or a fetch in your client) to see the latest remote
  state.
- Git operations never freeze Blender: the row buttons are disabled
  while a job is queued/active and the final result (committed files,
  refusal reason, …) is reported in the **status bar** — there is no
  popup, because the operator already finished.

### Network access and the on-open fetch

- **Fetch remotes on open** (Add-on Preferences, **on by default**):
  opening a .blend fetches every tracked repository's remote in the
  background so the row's `behind N` reflects the shared repository
  without touching **Git sync** first. Offline setups and repositories
  without a remote are a silent no-op.
- The on-open fetch and **Git sync** (pull/push) require Blender's
  *Allow Online Access* setting. Without it the operations report that
  online access is disabled instead of failing halfway.
- The local `git status` refresh, **Git commit...** and the whole manual
  flow with your own git client work **offline**.

### Notes and limits

- The panel is a transport, not a replacement for a Git client: staging
  non-addon files, branches, rebases, PRs and conflict resolution happen
  in your client.
- The .blend is never committed or pushed by the addon.
- `Git commit...` commits to the **local** repository; sharing happens
  with **Git sync** (push) or your client.

## Merging conflicted groups (Changes list)

When a tracked group changed on **both sides** — you edited the group in
the .blend and the JSON changed (a pull, a checkout, another tool) — the
row shows as **Conflict** and offers three exits:

- **Merge** — the add-on decides the file per item: your local edits are
  kept and the JSON changes are applied on top; the common base is
  recovered from the JSON file's git history (the newest revision whose
  content hash matches the recorded baseline — nothing new is stored).
  A clean merge writes the merged entry into its package and rebuilds
  the group, ending Synced. If any item changed differently on both
  sides (or both restructured the group differently), **nothing is
  written** and the merge report box lists every item with its
  base/ours/theirs values and an **Ours / Base / JSON** choice (plus a
  custom value for simple scalars); a conflicting structure asks for one
  whole side. **Apply Chosen Merge** writes the merged package and
  rebuilds the group once every conflict has a decision. The decisions
  apply to the sides captured when Merge was pressed — if the group
  changed after that, apply asks for a new Merge.
- **Keep JSON** / **Keep Blend** — the one-sided resolutions, unchanged.

`git merge-base`/history is required: merging needs the JSON file to be
committed in a git repository. Without a repository (or without the
baseline revision in history) the Merge action reports why.

Merge covers Geometry Nodes/Shader/Compositor node groups and
material/world embedded trees, per item (tree plus owner settings).
Values merge per item - socket defaults, node labels/properties, custom
properties, tree properties, curated owner settings. When the three
sides carry node identities (current exports), nodes and links merge per
entity too: disjoint adds/removals/renames/rewires and edits on
different nodes combine; delete/edit, same-node structural divergence,
rename/rename and link contention are listed per node or link and
resolved with Ours / Base / JSON / Delete in the merge report. The order
of the links feeding a multi-input socket (Join Geometry, Mix, ...)
follows the side that reordered it; a divergent reorder on both sides is
a `link/order` conflict with the same Ours / Base / JSON choices. Entries
without identities are adopted whole from the side that changed them and
never guessed. Exact-content renames (same logic under a new name) are
paired and merged under the new name for Geometry Nodes, Shader and
Compositor groups; a rename that also edited values on the same side
still reads as delete + add, and animated groups are flagged but not
merged specially. A compositor entry's informational `compositor_scenes`
list is not part
of the hash and travels with the adopted side.
