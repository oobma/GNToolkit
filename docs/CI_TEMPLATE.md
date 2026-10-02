# Reviewing node changes in CI

**Status: unreleased.** This template lives in the repository (it is never
shipped inside the extension zip). The workflow downloads `gnt_check.py` from
a **tagged release** of this repository, so the pinned tag matters: the
`v0.2.8` tag is not published yet — until it is, use `v0.2.5` in the download
URL below. The template's commands are unchanged between tags, so the checks
behave the same either way.

The JSON side of GNToolkit runs without Blender, so a project repository can
review node changes in CI: every proposed change touching the tracked JSONs or
the `.gntsync` sidecar gets its status checked and its health audited before
anyone merges — no Blender on the runner, seconds of CPU. Any CI service that
can check out a repository tag and run Python 3.11+ works: the commands below
are the whole integration. The example at the end is written for one hosted CI
service; only the wrapper changes between services.

## What it catches

- Groups whose tracked JSON no longer matches the last recorded agreement
  (`[CHANGED]`) — a group was edited outside the flow, or an older version
  was committed over a newer one.
- Groups that vanished from the folder (`[MISSING]`).
- JSONs that do not parse, including unresolved git conflict markers
  (`[UNREADABLE]` / `[CONFLICT]`; hard failure with `--strict`).
- Duplicate logic and project health issues (audits, informational).

It does not look at the `.blend`: accidents that live only in the working
file never reach git, and are the addon's job (status panel at pull time).

## The commands

Fetch `gnt_check.py` at the addon tag and run the checks:

```
python gnt_check.py NodeGroups --baseline project.blend.gntsync --strict
python gnt_check.py NodeGroups --duplicates
python gnt_check.py NodeGroups --baseline project.blend.gntsync --health --strict
```

`gnt_check.py` must come from the same tag as the addon: the canonical hashes
must match the ones the team computes inside Blender.

Optionally, `--impact "Group"` reports what depends on a group — directly
and transitively — plus the objects using it as a modifier (from the
`Modifiers/` exports): informational, handy for a review comment.

## Example — hosted CI workflow (GitHub Actions)

Adjust the two paths (your folder of per-group JSONs, here `NodeGroups`, and
your sidecar, here `project.blend.gntsync`) and the pinned tag, then commit.
No secrets, no runner setup.

```yaml
name: GNToolkit check

on:
  pull_request:
    paths:
      - "**/NodeGroups/**"
      - "**/*.gntsync"

jobs:
  gnt-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"

      - name: Fetch GNToolkit (pinned to the addon tag)
        run: |
          curl -fsSL -o gnt.zip https://github.com/oobma/GNToolkit/archive/refs/tags/v0.2.8.zip
          unzip -q gnt.zip
          mv GNToolkit-0.2.8 gnt

      - name: JSON-side status check
        run: python gnt/gnt_check.py NodeGroups --baseline project.blend.gntsync --strict

      - name: Audit (duplicates / health)
        run: |
          python gnt/gnt_check.py NodeGroups --duplicates
          python gnt/gnt_check.py NodeGroups --baseline project.blend.gntsync --health --strict
```

Optional extra step (add it and set your group name — informational):

```yaml
      - name: Impact of a group (optional, informational)
        run: python gnt/gnt_check.py --impact "My Group" --baseline project.blend.gntsync
```

## What the status step reports (exit codes)

- `0` — every tracked group matches the sidecar.
- `1` — changes or missing groups: the change must update the sidecar
  (Refresh Status inside Blender) or restore the group that was overwritten.
- `2` — unreadable files or unresolved conflict markers (with `--strict`):
  fix the merge before it can pass.

The duplicate audit is informational (always exits `0`); the health audit
fails under `--strict` only on hard problems (unreadable files, conflict
markers).

## Local pre-commit hook (optional)

The same check in about a second, before the commit leaves your machine:

```
python gnt_check.py NodeGroups --baseline project.blend.gntsync --strict
```

A non-zero exit blocks the commit.

## Machine-readable output

`--json` prints one dict for the status report (`files`, `groups`, `clean`,
`changed`, `missing`, `unparseable`, `dt_seconds`); the audits support it
too (`--duplicates`, `--health`, `--impact`). Use it to build richer change
summaries later without changing this template.

## Why it can be trusted

The checker is the same pure-Python code the addon uses inside Blender —
same canonical hasher, same sidecar format — so a hook and a *Refresh
Status* always agree. Reference run: a 582-group project checks in about
3 seconds with no Blender installed.
