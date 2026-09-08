# GNToolkit — Collaborating with Git

The deterministic JSON exports make every Git client a collaboration
backend for node groups. Two ways to work:

1. **Manual flow** — any Git client (CLI, GitHub Desktop, VS Code, …);
   the addon only writes/reads the JSON files. Zero addon features
   required.
2. **Collaboration panel** — the addon's thin Git transport: repository
   state, commit, sync and history from inside the Sync area of the
   GN Tools tab.

Git remains the authority for history, merging and credentials — the
addon never reimplements version control and never runs its own server.
Credentials are whatever the user already configured for git
(credential manager, SSH keys, …).

## Local repo or web repo — same thing

The addon never talks to a server: **Git Sync** and the manual flow
just run `git pull` / `git push` against whatever remote the repository
is configured with. That remote can be:

- a local folder or bare repo (`C:\backups\gntoolkit.git`),
- a network share,
- a web host — GitHub, GitLab, Bitbucket, Gitea, …

Nothing changes in the addon. The only difference is how git itself
reaches and authenticates against the remote. If `git push` works from
your terminal, it works from the panel.

### Going from local to GitHub (one-time setup)

1. **Create an empty repository** on GitHub (no README, no license —
   or you will have to merge unrelated histories). Note the HTTPS URL:
   `https://github.com/<user>/<repo>.git`.
2. In the folder that already holds your exported JSONs and is already
   a git repository:
   ```
   git remote add origin https://github.com/<user>/<repo>.git
   git push -u origin main
   ```
3. The first push opens the **Git Credential Manager** window (browser):
   sign in to GitHub once, authorize, and Windows stores the token —
   every later sync works without asking.

From then on, the Collaboration panel's **Git Sync** pushes and pulls
from GitHub exactly as it did from the local remote, and your
colleagues just run:

```
git clone https://github.com/<user>/<repo>.git
```

### HTTPS vs SSH

- **HTTPS** (recommended): zero setup beyond the one-time credential
  manager sign-in above.
- **SSH**: add your key on GitHub and use
  `git@github.com:<user>/<repo>.git`. Git handles the key — the addon
  never sees any credentials.

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

## Collaboration panel (thin git transport)

Appears in the **GN Tools** tab when groups are tracked. Requires `git`
on the PATH.

- **One row per repository** detected from the tracked JSONs (a project
  can point at several repos), showing: the repository name, the state
  — `clean`, `N to commit`, `ahead N`, `behind N` — and the buttons:
  - **Git Commit…** — opens a dialog for the commit message; stages
    **only the tracked JSON files that changed** (never `git add -A`,
    never the .blend) and commits locally.
  - **Git Sync** — `git pull --ff-only` then `git push`. Fast-forward
    only: remote changes arrive only when the local branch is simply
    behind (no merge is ever attempted). When local and remote
    versions diverged, the sync refuses and reports "Versions diverged
    — resolve with your git client". After a clean sync the sync status
    cache is refreshed so a changed JSON shows up as "Changed in JSON"
    immediately.
  - **Reveal** — open the repository folder in the file explorer.
- **Merge conflicts**: when a tracked JSON carries conflict markers the
  panel shows a warning row per file with a Reveal button.
- **History** (collapsible):
  - *Repository history* — the latest commits of the repository (shown
    when exactly one repository is detected).
  - *Active: <group> — history* — the latest commits of the active
    group's JSON file.
- `ahead`/`behind` reflect the last fetch (like `git status -b`) — run
  **Git Sync** (or a fetch in your client) to see the latest remote
  state.

### Notes and limits

- The panel is a transport, not a replacement for a Git client: staging
  non-addon files, branches, merges, rebases, PRs and conflict
  resolution happen in your client.
- The .blend is never committed or pushed by the addon.
- `Git Commit…` commits to the **local** repository; sharing happens
  with `Git Sync` (push) or your client.
