# JSON Layout Architecture: Tree vs DAG

Technical report on the feasibility of a dependency-based folder layout for
the GNToolkit JSON storage, compared with the current unified-package model.

## 1. Current state

**JSON data model:** a single unified package file (`node_groups` keyed by
group name plus `modifiers`), where cross-group references are stored **by
name** (`node_tree_reference`, serializer.py:241). A "Use Folder Structure"
export already exists (operators.py:131), but it is a **flat** layout
(`NodeGroups/` + `Modifiers/`) and a one-shot snapshot without sync.

**Sync model (`.gntsync`):** each tracked group has a UUID, a `json_path`
(relative `//`), canonical hashes for the blend and JSON sides, and an mtime
fast-path. The dependency graph is already resolved in both directions:

- `get_tree_dependencies()` — transitive closure of a tree
  (socket_utils.py:184).
- `all_graph` (group → dependencies) and `rev_graph` (dependency → parents),
  built during the pull (sync_manager.py:1994-2005).
- **Topological order** (Kahn) to rebuild dependencies before parents
  (sync_manager.py:2072-2087), with cycle tolerance.

**Key point:** change detection (per-group canonical hashing) is
**independent of where a file lives**. The graph and hash infrastructure
already exists; what is missing is the "one file per group in a hierarchy"
layer.

## 2. Tree vs DAG — fundamental differences

| | Tree | DAG (Directed Acyclic Graph) |
|---|---|---|
| In-degree | ≤ 1 (exactly one parent) | Arbitrary (multiple parents) |
| Structure | Hierarchical / containment | Partial order |
| Mental image | Nested folders, XML, file systems | Git commits, makefiles, dataflow diagrams |
| Folder mapping | 1:1 without duplication — every node lives in exactly one place | 1:1 impossible — shared nodes must be duplicated or hoisted to a shared folder |

**What is the Geometry Nodes graph?** A **DAG** (and sometimes not even a
DAG: Blender tolerates cycles with a warning). A "Utils" group can be used
by "Rig", "Hair" and "Terrain" at once — three parents.

**Direct consequence:** "dependency folders" **cannot** be a real tree. Only
two escapes exist: **duplicate** the shared groups (drift: two copies that
diverge) or **hoist** the shared groups to a common folder (`_shared/`,
npm/include style) — which is no longer "dependency-based" but
"convention-based".

## 3. What the implementation would really represent

A dependency-based folder system is a **tree projection of the DAG** — a
layout decision, not the real structure. Concretely:

- **Folder per root** (`rootA/` contains rootA plus its transitive
  dependencies): the folders are **visibility scopes**, not a hierarchy.
  Shared groups are duplicated (with one copy chosen canonical) or hoisted
  to `_shared/`.
- **One file per group** (groups are already serialized individually):
  each `.json` is one group; the folder hierarchy is only navigation and
  convention.

Technically it would require: (1) multi-file reads in the pull (today the
`json_data_cache` reads one package per path; the folder import already
merges several files into one cache — operators.py:244-258 — but **sync**
does not), (2) surgical per-file commit (already surgical per group, but
inside a package), (3) **rename handling**: renaming a group means moving
its file, updating `node_tree_reference` in **all** parent files and
re-stamping statuses, and (4) a policy for **orphans** (groups with no
parents: where does their file live?).

## 4. Aspect and difficulty table

| Aspect | Status | Difficulty |
|---|---|---|
| Per-group canonical hash (`hash_utils.py`) | Already exists | Trivial |
| Per-file mtime fast-path | Already exists | Trivial |
| Forward dependency graph (`get_tree_dependencies`, `all_graph`) | Already exists | Trivial |
| Reverse graph `rev_graph` (dependency → parents) | Already exists | Trivial |
| Topological order / dependency-first rebuild | Already exists | Low |
| Individual group serialization (`serialize_node_tree`) | Already exists | Trivial |
| Per-file lock (`JsonLock`) | Already exists (reusable) | Trivial |
| Cycle tolerance in pull | Already exists | Low |
| Multi-file import (merge folder into one cache) | Exists only in snapshot (operators.py:244) | Medium |
| **Multi-file sync** (parent pull reads N dependency files) | **Missing** — today 1 package per path | Medium |
| **Surgical per-file commit** (today surgical per group inside the package) | **Missing** (extension) | Medium |
| **DAG layout policy** (duplicate vs `_shared/`) | **Missing** (design decision) | Medium |
| **Renames**: move file + cascade `node_tree_reference` in all parents + re-stamp | **Missing** | High |
| Orphans (group with no parents: where does its file live) | **Missing** | Low |
| Metadata/package migration → folders | **Missing** | Medium |
| UI: layout selector, file indicators | **Missing** | Low |
| Change detection (the part that seemed hard) | Already exists | Trivial |

## 5. Pros and cons vs the current system

### Pros

- **Human navigation**: see the full scope of a root at a glance; the
  unified package is unreadable without tooling.
- **Git granularity**: per-group diffs, cleaner merges, reviewable PRs.
- **Selective sharing**: share `root/` with a colleague = share its whole
  closure (a reusable "package").
- **Incremental reads/writes**: per-file locks, per-file mtime, no
  rewriting hundreds of groups to touch one.
- Change detection does not change — hashes are already per-group.

### Cons

- **Duplication/drift** from the shared DAG (the central problem; either
  duplicate or add `_shared/` conventions).
- **Multi-file sync adds state**: a parent pull must open N files; the
  proven "component rebuild" logic (dependencies + parents in one pass, one
  package) gets more complicated.
- **Expensive renames**: a cascade of updates between files that does not
  exist today (the unified package resolves everything by name in one file).
- **Orphans and cycles**: more edge cases of "where does this go".
- **Operational complexity**: the current model (1 package = 1 project) is
  battle-tested for pull fidelity; fragmenting adds more friction than it
  solves for a single user.
- The existing folder export is a one-shot snapshot — turning the folder
  into a *sync participant* is where almost all the real work lies.

## 6. Conclusion

The folder system is **viable**, and change detection is **not a problem**
(already solved and layout-independent). The implementation would be a tree
projection of a DAG, with duplication or shared folders as the price. The
benefit is mainly **readability and selective sharing**, not technical. The
real cost is: layout policy, multi-file sync and renames. For a personal
project, the current unified package plus per-group hashes is more robust;
for a git-driven team, "one file per group" in a folder with a `_shared/`
convention is the variant worth pursuing.
