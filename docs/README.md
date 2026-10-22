# GNToolkit documentation

Start with the path that matches what you want to do.

| I want to... | Read |
|---|---|
| Install it and version my first project | [README](../README.md) then [User guide](MANUAL.md) |
| Understand the daily save/pull/conflict loop | [User guide](MANUAL.md) |
| Collaborate with Git | [Git collaboration](GIT_COLLAB.md) |
| Run checks in CI or without Blender | [Automation and CI](AUTOMATION.md) and [CI template](CI_TEMPLATE.md) |
| Understand the JSON format | [JSON format](JSON_FORMAT.md) |
| Know which Blender versions and hash rules apply | [Compatibility](COMPATIBILITY.md) |
| Build, test or contribute | [CONTRIBUTING](../CONTRIBUTING.md) |
| See a real project diff | [Case study](case-studies/subd-to-compound/README.md) |

## The two ideas behind the add-on

- **JSON is the source of truth.** Node groups are serialized into
  deterministic JSON files that can be diffed, reviewed and versioned.
- **The .blend is the working cache.** GNToolkit compares both sides and
  tells you what changed; it never writes the `.blend` on its own.

The status names used everywhere are: **Synced**, **Edited locally**,
**Changed in JSON**, **Conflict**, **Missing in blend**, **Untracked**
and **JSON missing**.

## Documentation conventions

Public documentation lives in `README.md`, `CONTRIBUTING.md`,
`CHANGELOG.md` and `docs/`. Verification runbooks are not part of the
public documentation. Historical engineering notes live under
[docs/history](history/) and are not current user documentation.
