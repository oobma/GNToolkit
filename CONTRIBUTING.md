# Contributing

Thanks for helping improve GNToolkit. This page covers the repository
layout, the public test suite, the release build and the documentation
rules.

## Repository layout

| Path | Role |
|---|---|
| `__init__.py` | Add-on registration, load/save handlers, logging |
| `constants.py` | Versions, shared constants and exclusion tables |
| `serializer.py` | Export: node trees to JSON-safe dictionaries |
| `importer.py` | Import: JSON to node trees (recursive reconstruction) |
| `codec.py` | Value conversion between Blender and JSON |
| `hash_utils.py` | Canonical SHA-256 hashing |
| `socket_utils.py` | Socket lookup, normalization and dependency graph |
| `sync_manager.py` | State detection, batch operations, pull/merge orchestration |
| `sync_metadata.py` | Sidecar file and UUID tracking |
| `sync_operators.py` | Sync operators (track, save, pull, resolve, history) |
| `sync_ui.py` | Sidebar panels and widgets |
| `merge.py` | Pure semantic merge core (values, package, embedded entries) |
| `semantic_diff.py` | Human-readable semantic diff between JSON versions |
| `audit.py` | Pure project audits (impact, duplicates, cross-project) |
| `graph_utils.py` | Pure graph model, layered layout and SVG poster rendering |
| `graph_view.py` | Board builder: the disposable graph node tree in the editor |
| `geometry_validator.py` | Generic geometry issue detection |
| `modifier_utils.py` | Modifier task serialization and input restore |
| `operators.py` | JSON package export/import operators and main panel |
| `file_utils.py` | Atomic writes, safe filenames, backup snapshots |
| `credentials.py` | Per-host Git credentials (keyring/preferences) |
| `git_integration.py` | Git facade and job pipeline |
| `git_backend_dulwich.py` | Bundled Git engine (platform build) |
| `git_backend_git.py` | Optional system-Git engine (GitHub build only) |
| `git_network_worker.py` | Network operations in a bounded subprocess |
| `gnt_check.py` | Headless JSON-side checker (repository tool, not shipped) |
| `cross_version.py` | Export comparison across Blender versions (repository tool) |
| `scripts/` | Headless export/import helpers (repository tools) |
| `pure-tests/` | Public Blender-free test suite |
| `docs/` | Public user and technical documentation |

## Tests

The public suite needs only Python 3.10+:

```bash
python pure-tests/run_all.py
```

It covers canonical hashes, merge values/package/overrides, the audits,
the `gnt_check.py` modes and the exclusion tables. Every test builds its
own fixtures in a temporary folder.

The Blender-side suites (import fidelity, real-project round trips, UI
panels, the release gate) use local fixtures and are not part of the
public repository. If you have them, run the smoke suite with the
source tree (background mode, `PYTHONPATH` pointing at the bundled
wheels when the test needs dulwich):

```bash
set PYTHONPATH=wheels\*.whl
blender --background --factory-startup --python-use-system-env --python-exit-code 1 --python tests\smoke_test_5.1.py
```

`--python-exit-code 1` must come before `--python`; Blender ignores it
otherwise.

Syntax check the add-on and repository tools before sending a change:

```bash
python -m py_compile __init__.py audit.py build_platform.py codec.py constants.py credentials.py cross_version.py error_tracker.py file_utils.py geometry_validator.py git_backend_dulwich.py git_backend_git.py git_integration.py git_network_worker.py gnt_check.py hash_utils.py importer.py merge.py modifier_utils.py operators.py semantic_diff.py serializer.py socket_utils.py sync_manager.py sync_metadata.py sync_operators.py sync_ui.py
```

## Build

The two extension packages are reproducible:

```bash
python build_platform.py                  # platform box
python build_platform.py --include-git    # GitHub box (+ system-Git engine)
```

They write `dist/GNToolkit-<version>.zip` and
`dist/GNToolkit-<version>-github.zip`. The build fails on compliance
violations, missing imports or version drift; do not bypass those checks.
If Blender is available, validate the result with
`--command extension validate`.

## Code style

- Follow the existing module boundaries: operators in `operators.py` /
  `sync_operators.py`, logic in `sync_manager.py`, serialization in
  `serializer.py`, reconstruction in `importer.py`.
- Keep the merge/hash core pure (no `bpy`) so it stays testable and
  usable by `gnt_check.py`.
- Do not add comments unless they explain something non-obvious.
- Commit messages in English, Conventional Commits style:
  `fix:`, `feat:`, `refactor:`, `perf:`, `docs:`.

## Documentation rules

- Public docs live in `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md` and
  `docs/`. Private maintainer notes must never be linked or copied into a
  public document.
- Use the current UI labels: **Save to JSON**, **Save selected**,
  **Save changed**, **Save all**, **Pull**, **Track from folder...**,
  **Project Health**, **Explore**. JSON is saved; Git is committed.
- Do not hardcode the add-on version in prose. Use "the current release"
  or a `<version>` placeholder; the JSON sample in
  [docs/JSON_FORMAT.md](docs/JSON_FORMAT.md) uses a placeholder on purpose.
  On release, update the README release banner, the
  [CI template](docs/CI_TEMPLATE.md) pin and the store notes.
- Keep one document as the owner of each topic:
  - Git: [docs/GIT_COLLAB.md](docs/GIT_COLLAB.md)
  - CLI/CI/headless: [docs/AUTOMATION.md](docs/AUTOMATION.md)
  - JSON format: [docs/JSON_FORMAT.md](docs/JSON_FORMAT.md)
  - compatibility: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)
- Verification runbooks are not part of the public docs; do not add them
  to `docs/`.

## Pull requests

1. Fork the repository and create a branch.
2. Keep the change focused; one logical change per commit.
3. Run the public suite and `python gnt_check.py --selftest`.
4. Describe what changed and how you verified it.
5. Open the pull request against `main`.
