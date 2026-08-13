# GNToolkit — Blender 5.2 LTS port research

Status: **PORTED — published in GNToolkit 0.2.2.** The same code runs
the full headless suite on Blender 5.1.1 (**187/187 checks**) and
5.2.0 LTS (**227/227 checks**, including the new-node E2E for the 25
new instantiable 5.2 node classes and every GN socket type). This
document records the research and the changes the port required so
future maintenance does not repeat the investigation.

Produced: 2026-08-11. Research phase ran the full headless suite
(identical code) on 5.1.1 and 5.2.0 LTS and contrasted every failure
with the official documentation; the implementation phase added the
mapping rules and the modifier RNA path described below.

Updated: 2026-08-13 (0.2.3) — the suite now also covers the **new 5.2
node set** (test #10, `test_52_new_nodes_e2e.py`); three roundtrip
fidelity bugs found by that test were fixed (see §9).

---

## 1. Method — the fidelity suite

All tests import the addon from the repository root (not the installed
copy), so both engines ran the **exact same code**. Order matters for
shared artifacts (`master_sync_test.json` must be pristine before the
e2e; reload reads the e2e artifacts).

| # | Test | 5.1.1 | 5.2.0 LTS |
|---|---|---|---|
| 1 | `smoke_test_5.1.py` | 41 pass, 1 warn | 41 pass, 1 warn |
| 2 | `test_sync_e2e.py` | 74 pass | 74 pass |
| 3 | `test_sync_reload.py` (persist / fresh) | 8 / 5 pass | 8 / 5 pass |
| 4 | `test_hash_migration.py` | 9 pass | 9 pass |
| 5 | `test_pull_fidelity.py` | 14 pass | 14 pass |
| 6 | `test_real_blend.py` | 7 pass, 1 warn | 7 pass, 1 warn |
| 7 | `test_manual_flow.py` | 12 pass | 12 pass |
| 8 | `test_stress_pulls.py` | 11 pass | 11 pass |
| 9 | `test_save_relativize.py` | 6 pass | 6 pass |
| 10 | `test_52_new_nodes_e2e.py` | n/a (skips < 5.2) | **40 pass** |

Pre-port (2026-08-11) the 5.2 failures were: smoke T11 (modifier RNA),
e2e 6, reload 1, pull-fidelity 1, manual-flow 4, stress 6 — all sharing
one root cause (data-type-driven socket layouts) except the modifier RNA.

**Current state (2026-08-13):** 187/187 checks on 5.1.1 and 227/227 on
5.2.0 LTS (test #10 runs on 5.2 only), same code on both engines.

## What the port changed (implemented 2026-08-11)

- **HASH_VERSION → 4** (`constants.py`). The canonical hash now applies
  a version-independent *active-socket* rule to nodes whose socket
  layout follows the data type/mode/operation, drops engine-dependent
  UI properties, and collapses duplicated link tuples:
  - `node_socket_is_active()` (`hash_utils.py`, shared with the
    importer): Compare (A/B of the active data type; C for
    mode=DOT_PRODUCT; Angle for mode=DIRECTION; Epsilon only for
    FLOAT/VECTOR + EQUAL/NOT_EQUAL), Random Value (Min/Max of the
    active type + ID/Seed), Boolean Math (NOT → single input),
    Value to String (Base/Padding are 5.2-only), Capture Attribute
    (Selection is 5.2-only), Subdivision Surface (Quality is
    5.2-only).
  - Node properties: `bl_*` UI-template limits excluded wholesale
    (5.1: 30.0 vs 5.2: FLT_MAX for bl_height_max) plus
    `vector_dimensions` (5.2-only Vector input node) and `height`
    (Frame layout, auto-resized by 5.2 rebuilds).
  - `use_fake_user` excluded from tree properties (same class as
    `use_extra_user`).
  - Canonical links deduplicated by (from_node, from_socket_name,
    to_node, to_socket_name): 5.1 exports carry the same link twice
    under type-variant sockets (Compare A and A_INT both named "A").
- **Importer** (`importer.py`): the defaults pass skips inactive socket
  records (eliminates the ~310 C/Angle/Epsilon WARN noise);
  `find_robust_socket` (`socket_utils.py`) resolves Compare/Random
  Value sockets by name+type first, which also supports the reverse
  flow (5.2-exported JSON imported on 5.1).
- **Modifier RNA** (`operators.py`): version-aware helpers
  `_serialize_modifier_inputs` / `_apply_modifier_inputs` —
  `modifier["identifier"]` custom properties on < 5.2,
  `modifier.properties.inputs/outputs["Socket_N"]["value"/"type"/
  "attribute_name"]` (IDPropertyGroup wrappers) on 5.2+ (see §4.2).

## 2. What already works on 5.2 (verified)

- Addon import/registration, bl_info, all module imports (smoke T1–T3).
- Serialization + canonical hashing of plain groups (smoke T4–T10).
- Geometry node group import/export, interface rebuilds, group-node
  link wiring (the fix machinery of 0.2.1: pre-unlink, zone session,
  depsgraph flush, dangling-link guard) — the pull converges, is
  idempotent, and reports residual divergence honestly.
- **Zone pairing** (`bpy.ops.node.add_zone`, `paired_output`): no change
  in 5.2, including headless background mode (virtual screens still
  provide Node Editor areas). All zone checks pass.
- Hash migration, sidecar, text-block fallback, save-relativize.
- 3.5.x-style importer tolerances (name+type socket fallback).

## 3. Incompatibility A — Compare node dynamic sockets (the big one)

### 3.1 What the tests observed

The e2e F2 pull reports ~310 warnings of the form:

```
[WARN] Node 'Compare': input socket 'Angle' (id=Angle) not found, default_value not restored
[WARN] Node 'Compare': input socket 'C' (id=C) not found, default_value not restored
[WARN] Node 'Compare': input socket 'A_INT' (id=A_INT) not found, default_value not restored
```

The pull-fidelity run logs 2040 such warnings. Because almost every
group contains Compare nodes, the missing defaults change the canonical
hashes of ~315 of 439 groups → they stay divergent → the sync correctly
reports them (never hidden), but the project can never converge on 5.2
as-is. The manual-flow run also loses **3 links** (25,436 vs 25,439 —
links into sockets that do not exist in 5.2).

### 3.2 Empirical socket layouts (probes, exact)

Blender 5.1.1, `FunctionNodeCompare`, `data_type=FLOAT` (all inputs,
name + identifier):

```
A, A_INT, A_VEC3, A_COL, A_STR, B, B_INT, B_VEC3, B_COL, B_STR,
C, Angle, Epsilon
```
(the A_INT/A_VEC3/A_COL/A_STR variants are the same "A" input under
the other data types; in 5.1 they always exist as socket objects).

Blender 5.2.0, same node:

- default: `A, B`
- `mode = DOT_PRODUCT`: `A, B, C`
- `mode = DIRECTION`: `A, B, Angle`
- `operation = EQUAL/NOT_EQUAL` (Float/Vector): `A, B, Epsilon`

Enums — **identical in both versions**:
- `mode`: `ELEMENT, LENGTH, AVERAGE, DOT_PRODUCT, DIRECTION`
- `operation`: `LESS_THAN, LESS_EQUAL, GREATER_THAN, GREATER_EQUAL,
  EQUAL, NOT_EQUAL, BRIGHTER, DARKER`

So in 5.2 the node only creates the sockets for the **active** data
type and mode; the type-variant sockets (A_INT, …) and the mode-gated
sockets (C, Angle) do not exist unless configured.

### 3.3 Quantified impact on the reference project

From `tests/master_manual_test.json` (439 groups):

- **715 Compare nodes.**
- `mode`: 714 ELEMENT + **1 DOT_PRODUCT**.
- `operation`: only the six comparisons (EQUAL 387, GREATER_THAN 132,
  LESS_THAN 92, GREATER_EQUAL 42, NOT_EQUAL 38, LESS_EQUAL 24) — all
  still valid in 5.2.
- `data_type`: INT 526, FLOAT 153, VECTOR 36.
- **Linked input sockets** (the only ones that matter for wiring):
  `A_INT` 526, `B_INT` 215, `A` 154, `B` 53, `A_VEC3` 36, `B_VEC3` 6,
  `Epsilon` 4. **C and Angle are never linked** (the single DOT_PRODUCT
  node has its C available once `mode` is set).

Random Value node (`FunctionNodeRandomValue`): 5.1 exposes `Min, Max,
Min_001, Max_001, Min_002, Max_002, Probability, ID, Seed`; 5.2 only
`Min, Max, ID, Seed`. In this project only `ID` is linked (1 link) —
the variant sockets are unlinked noise.

### 3.4 Official documentation

- Manual (5.2), Compare node:
  https://docs.blender.org/manual/en/5.2/modeling/geometry_nodes/utilities/math/compare.html
  — inputs **A, B** plus *"C — Compared against the dot product of two
  input vectors when the Mode property is set to Dot Product"* and the
  *"Angle"* input referenced by the **Direction** mode; operations
  listed exactly as the probe found them.
- Python API changelog (5.2):
  https://developer.blender.org/docs/release_notes/5.2/python_api/
  — *"Socket identifiers for the Compare and Random Value node changed
  (3a5cd7862b)."* (This is the dynamic-socket layout change; the
  before/after identifier tables are the probes above.)

### 3.5 What the port must do (Compare)

1. **Socket identifier mapping** in the importer's socket resolution
   (`socket_utils.find_robust_socket` name fallback and the defaults
   pass in `importer._apply_default_values_gen`): when the serialized
   identifier is a type-variant of an existing base socket, resolve to
   the base socket:
   - Compare: `A_INT/A_VEC3/A_COL/A_STR → A`, `B_* → B`
     (valid only when the serialized `data_type` matches the variant's
     type — the JSON always carries `data_type`).
   - Random Value: `Min_001/Min_002 → Min`, `Max_001/Max_002 → Max`
     (map by index: the nth variant maps to the base; the variant's
     own data type is the discriminator).
2. **Mode-gated sockets**: set `mode` from the JSON before wiring
   (`importer` node-property pass already applies `properties`; the
   1 DOT_PRODUCT node then gets its C socket). C/Angle lookups should
   be skipped (WARN) when the mode does not create them — the current
   warnings are harmless but noisy.
3. **Epsilon is conditional**: it only exists for EQUAL/NOT_EQUAL.
   Skip its default/link when the operation is a plain comparison
   (the 4 linked Epsilon nodes are all EQUAL-family, so links survive;
   the defaults WARNs for the other 128 operations are noise).
4. **Hash normalization (required for convergence)**: the canonical
   form (`hash_utils.canonicalize_node_tree_data`) must map variant
   socket identifiers to their base (`A_INT→A`, `Min_001→Min`) so the
   JSON side and the 5.2 tree side produce the same socket set.
   Without this, even a perfect import stays divergent because the
   JSON serializes 13 sockets and the tree has 3. Bump `HASH_VERSION`
   (v4) so the auto-rebaseline re-stamps old baselines.

## 4. Incompatibility B — NODES modifier inputs/outputs RNA (smoke T11)

### 4.1 What the test observed

```
[FAIL] T11: modifier export path ->
TypeError('bpy_struct[key] = val: id properties not supported for this type')
```

The 0.2.1 modifier export/import path reads/writes
`modifier["identifier"]` custom properties (5.0/5.1 behavior).

### 4.2 Official documentation (exact quote)

Python API changelog (5.2):
https://developer.blender.org/docs/release_notes/5.2/python_api/

> "The API for accessing Geometry Nodes modifier properties has
> changed. The modifier now has proper RNA properties rather than using
> custom properties for inputs and output attribute names
> (1561c1ea4a)."

```
# Before
modifier["identifier"] = 5.0
modifier["identifier_use_attribute"] = True
modifier["identifier_attribute_name"] = "some_input_attribute"
modifier["identifier_attribute_name"] = "some_output_attribute"

# After
modifier.properties.inputs.identifier.value = 5.0
modifier.properties.inputs.identifier.type = "ATTRIBUTE"
modifier.properties.inputs.identifier.attribute_name = "some_input_attribute"
modifier.properties.outputs.identifier.attribute_name = "some_output_attribute"
```

### 4.3 What the port must do (modifier)

Version-gate the modifier export/import path in `operators.py`
(and the serialization helpers it uses):
- `bpy.app.version >= (5, 2)` → use `modifier.properties.inputs/
  outputs.<identifier>.value/.type/.attribute_name` (RNA).
- else → keep the `modifier["identifier"]` custom-property path.
The `type` enum values (VALUE/ATTRIBUTE/…) may differ; verify against
the 5.2 RNA (`modifier.properties.inputs[0].type`).

## 5. Porting order (recommended)

1. **Compare/Random Value identifier mapping** in the importer
   (`socket_utils.py`, `importer.py` defaults + wiring passes). ✅ done
2. **Hash normalization v4** for variant sockets (`hash_utils.py`,
   `constants.py` HASH_VERSION). ✅ done
3. Re-run suites 1–5, 7–8 on 5.2 → expect the Compare-related failures
   to disappear and the projects to converge (0 still differ). ✅ done
4. **Modifier RNA path** (`operators.py`) — smoke T11. ✅ done
5. Full suite green on both 5.1.1 and 5.2.0 (180/180 each). ✅ done
6. Update README Compatibility + this document. ✅ done

## 6. Known residual edge (not a port blocker)

On the *manual_test.blend* file (a file with local edits), the batch
pull can reset the `input_type` (GEOMETRY → FLOAT) of one Switch node
in `SP - NURBS Patch Viewers` — the isolated rebuild and all fresh-file
flows (e2e, manual-flow, fidelity) are correct; the stress test
tolerates this single group. If it reappears on other files, compare
the batch context (shared interface maps / pre-unlink / zone session)
against the isolated rebuild to isolate the trigger.

## 6. Repro scripts (kept for reference)

- Compare/Random Value socket layout probe (both engines):
  create `FunctionNodeCompare`/`FunctionNodeRandomValue` in a temp
  tree, set `data_type`, `mode`, `operation`, print `inputs`
  name/identifier and the `bl_rna.properties["mode"]/["operation"]`
  enum keys. (Exact outputs in §3.2.)
- Impact quantification: parse `tests/master_manual_test.json`,
  count `FunctionNodeCompare` nodes, group by mode/operation/
  data_type, and count links by `to_socket_id` (exact numbers in §3.3).

## 7. Things NOT to re-investigate

- Zone pairing (`add_zone`/`paired_output`): unchanged in 5.2 — see
  CHANGELOG "Technical note: zone node pairing" (verified on 5.1.1;
  the 5.2 suite runs all zone checks green, including headless).
- Background-mode virtual screens: still present in 5.2.
- `mode`/`operation` enums of Compare: identical in 5.1 and 5.2.
- The sync machinery itself is 5.2-faithful (measured): the only
  content-level gaps are the two node layouts and the modifier RNA.

## 8. Key URLs

- 5.2 release notes: https://www.blender.org/download/releases/5-2/
- 5.2 Python API changelog:
  https://developer.blender.org/docs/release_notes/5.2/python_api/
- 5.2 Compare node manual:
  https://docs.blender.org/manual/en/5.2/modeling/geometry_nodes/utilities/math/compare.html
- Commit changing the socket identifiers: `3a5cd7862b`
- Commit changing the modifier API: `1561c1ea4a`

## 9. New-node set verification (0.2.3, 2026-08-13)

Test #10 (`tests/test_52_new_nodes_e2e.py`, fixture
`tests/gn52_all_nodes.blend` built by `tests/prepare_52_fixture.py`)
covers every node class that is new in 5.2 plus every GN-valid socket
type. Verified facts worth keeping (do NOT re-investigate):

- **25 of the 26 new classes are usable**; `GeometryNodeApplySimulatedData`
  is registered in `bpy.types` but `nodes.new` raises on 5.2.0 final —
  a leftover of the experimental physics system. Excluded from the fixture.
- **`node_groups.new("...", "GeometryNodeTree")` has no settable
  `geometry_type`** on 5.2.0 (the property is gone) and there is no
  PHYSICS tree type to create physics-only nodes in.
- **Interface `new_socket` accepts ONLY the 18 base types** on 5.2:
  Float, Int, Bool, Vector, Color, Rotation, Matrix, String, Menu,
  Geometry, Object, Collection, Image, Material, Font, Sound, Bundle,
  Closure. All subtypes (FloatAngle, VectorEuler, StringFilePath, …)
  must be created as the base type and then `item.subtype = ...`
  (same mechanism the importer already used — see the 0.2.3 subtype
  fix). `Vector2D` = Vector + `dimensions = 2`. `FloatUnsigned` /
  `IntUnsigned` / `IntVector3D` cannot be created via the interface.
- **`item.subtype` enum_items reports only `["DEFAULT"]`** even though
  the full enum is accepted by the setter — never validate subtypes
  against `enum_items` (this cost the 0.2.3 subtype bug).
- **`default_input`** (new on interface items) supports
  `SCENE_FRAME` and `SELF_OBJECT` among its enum values.
- **`TransferAttributes` "Attribute Names" is a STRING LIST socket**:
  a plain string default is silently ignored (the node early-returns).
  Feed it a `Field to List` (STRING item) or `Split String` list. It
  maps by ID fields (index fields by default).
- **`GetGeometryComponent` defaults to `Type=MESH`, `Remove=True`**:
  the "Geometry" output is the input minus the component (empty), the
  extracted component is the "Component" output.
- **`MeshGrid` in 5.2 has no node-level size properties and no input
  geometry** — everything is socket inputs (Size X/Y, Vertices X/Y).
- **Custom menu items for `FunctionNodeInputMenu`/menu sockets cannot
  be created from Python on 5.2.0** (no `enum_items`/`menu_items` RNA;
  the items live in a C-only `bNodeSocketValueMenu`). MenuSwitch's
  `enum_items` still works and is the serializable path.
- **`bpy.data.sounds`/`fonts` have `.load()` (no `.new()`)**;
  `textures` keeps `.new()`. Node-socket datablock defaults of
  non-scalar type (COLLECTION/OBJECT/MATERIAL/…) are not serialized
  by design; the Sound socket default IS serialized and restored.
- **The list data type has no dedicated socket class**: list values
  ride on typed sockets (`NodeSocketVirtual` for Field/Closure to
  List outputs) and `list_items` collections on the nodes must be
  round-tripped (0.2.3 fix).
