# GNToolkit — Collaborating with Git

GNToolkit tracks node groups (Geometry Nodes and Shader) as deterministic
JSON files, which makes every Git client a collaboration backend for
them. Two ways to work:

1. **Manual flow** — any Git client (CLI, GitHub Desktop, VS Code, …);
   the addon only writes/reads the JSON files. Zero addon features
   required.
2. **Collaboration panel** — the addon's thin Git transport: repository
   state, commit, sync and history from inside the Sync area of the
   GN Tools tab.

Git remains the authority for history and merging — the addon never
reimplements version control and never runs its own server. With a
system `git`, credentials are whatever you already configured for it
(credential manager, SSH keys, …); the bundled engine resolves per-host
credentials itself — entered from the panel, kept in the OS keyring.

## Local repo or web repo — same thing

The addon never talks to a server: **Sync Changes** and the manual flow
just run `git pull` / `git push` against whatever remote the repository
is configured with. That remote can be:

- a local folder or bare repo (`C:\backups\gntoolkit.git`),
- a network share,
- a web host — GitHub, GitLab, Bitbucket, Gitea, …

Nothing changes in the addon. The only difference is how git itself
reaches and authenticates against the remote. If `git push` works from
your terminal, it works from the panel.

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
3. The first push opens the **Git Credential Manager** window (browser):
   sign in to your host once, authorize, and Windows stores the token —
   every later sync works without asking.

From then on, the Collaboration panel's **Sync Changes** pushes and pulls
from the remote exactly as it did from the local one, and your
colleagues just run:

```
git clone https://github.com/<user>/<repo>.git
```

### HTTPS vs SSH

- **HTTPS** (recommended): zero setup beyond the one-time credential
  manager sign-in above.
- **SSH**: add your key on your host and use its SSH URL
  (`git@github.com:<user>/<repo>.git` on GitHub). Git handles the key —
  the addon never sees any credentials.

### Two gotchas before sharing a repo

- **Save the .blend before tracking** — JSON paths are stored relative
  to the .blend, so the file must exist on disk first.
- **Keep the .blend out of the repo** (add it to `.gitignore`): JSONs
  are the shared source of truth; the .blend is personal working state.
  The addon never stages or pushes the .blend, but your own git
  commands might.

## Manual flow (works with any Git client)

1. **Export** the project with **Export Package → Use Folder Structure**
   (one JSON per group under `NodeGroups/`, modifiers under
   `Modifiers/`) — per-group files give the cleanest diffs and the
   fewest conflicts.
2. **Publish**: `git init`, `git add NodeGroups Modifiers`,
   `git commit -m "initial"`, add a remote, `git push`.
3. **On the other machine**: clone the repository, open (or create) the
   .blend, run **Import Package/Folder** (pick any JSON inside the
   folder) and **Track Folder…** to start tracking every group against
   its own file.
4. **Daily loop**:
   - Edit groups in the .blend → **Commit** (addon writes the JSONs) →
     `git commit` / `git push` (your client).
   - A colleague pushed changes → `git pull` (your client) → the addon's
     **check on open** notices the JSON files changed ("N group(s) out
     of sync with JSON") → **Pull from JSON** applies them to the .blend.
5. **Conflicts**: if two people edited the *same* group, `git pull`
   leaves conflict markers in that JSON. The addon reports the file
   ("has merge conflicts — resolve them with your git client"); resolve
   the file with your client (or `git checkout --theirs/--ours`) and
   then **Pull from JSON** again. The addon never merges JSON text.
   (Editing the *values* of the same group on both sides is a different
   case: when the addon itself reports the group as **Conflict** — local
   .blend edits against changed JSON — the **Merge** action resolves it
   per item; see “Merging conflicted groups” below.)

## Collaboration panel (thin git transport)

Appears in the **GN Tools** tab when groups are tracked. Nothing extra
needs to be installed — the transport engine is bundled with the add-on
(pure Python); the GitHub build additionally uses a system `git`
automatically when one is present.

- **One row per repository** detected from the tracked JSONs (a project
  can point at several repos), showing: the repository name, the state
  — `clean`, `N to commit`, `ahead N`, `behind N` — and the buttons:
  - **Git Commit…** — opens a dialog for the commit message; stages
    **only the tracked JSON files that changed** (never `git add -A`,
    never the .blend) and commits locally.
  - **Sync Changes** — pull, then push. When the local
    branch is simply behind, the remote changes arrive; when local and
    remote **diverged**, the tracked JSON files are merged per item
    (values, modifier inputs, Geometry Nodes and Shader groups,
    materials/worlds per entry, and exact-content renames per entry or
    per renamed file — `ours` = your HEAD,
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
- **History** (collapsible):
  - *Repository history* — the latest commits of the repository (shown
    when exactly one repository is detected).
  - *Active: <group> — history* — the latest commits of the active
    group's JSON file.
- `ahead`/`behind` reflect the last fetch (like `git status -b`) — run
  **Sync Changes** (or a fetch in your client) to see the latest remote
  state.
- Git operations never freeze Blender: the row buttons are disabled
  while a job is queued/active and the final result (committed files,
  refusal reason, …) is reported in the **status bar** — there is no
  popup, because the operator already finished.

### Network access and the on-open fetch

- **Fetch remotes on open** (Collaboration panel, **on by default**):
  opening a .blend fetches every tracked repository's remote in the
  background so the row's `behind N` reflects the shared repository
  without touching **Sync Changes** first. Offline setups and repositories
  without a remote are a silent no-op.
- The on-open fetch and **Sync Changes** (pull/push) require Blender's
  *Allow Online Access* setting. Without it the operations report that
  online access is disabled instead of failing halfway.
- The local `git status` refresh, **Git Commit…** and the whole manual
  flow with your own git client work **offline**.

### Notes and limits

- The panel is a transport, not a replacement for a Git client: staging
  non-addon files, branches, rebases, PRs and conflict resolution happen
  in your client.
- The .blend is never committed or pushed by the addon.
- `Git Commit…` commits to the **local** repository; sharing happens
  with **Sync Changes** (push) or your client.

## Merging conflicted groups (Sync panel)

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

Merge covers Geometry Nodes and Shader node groups; material/world
entries (embedded trees) are not covered and report why. Merges are
values only — socket defaults, node labels/properties, custom
properties, tree properties; the structure is adopted whole from the
side that changed it and never guessed. Exact-content renames (same
logic under a new name) are paired and merged under the new name for
Geometry Nodes and Shader groups; a rename that also edited values on
the same side still reads as delete + add, and animated groups are
flagged but not merged specially.
