# Jianying Protocol Service Intake

This document is the intake checklist for selectively absorbing capabilities from
an external JianYing protocol-service sample into this repository.

Scope:

- keep the current `JyProject + asset_index + jy_wrapper.py` surface as the primary entrypoint
- absorb protocol-writing capabilities only
- do not merge the external repo's FastAPI runtime, task manager, or OSS flow into the core path

Non-goals:

- no whole-repo merge
- no replacement of the current patch-mode reopen/edit flow
- no direct adoption of the external repo's platform-specific defaults

## Intake Principles

1. Preserve the current agent-first surface:
   - `search-assets`
   - `resolve-asset`
   - query-driven write flags
2. Land new protocol capabilities behind `JyProject` methods first, then CLI.
3. Every new capability must work in both:
   - normal in-memory project mode
   - saved-draft patch mode via `draft_content.json`
4. Treat the external repo as a sample and schema source, not a runtime dependency.
5. Do not copy large code paths wholesale until licensing is clarified. At time of review, no
   `LICENSE` file was found in the cloned external repository.

## Sample Intake Map

These are the most useful sample sources in the external repo and how they should be reused.

| Capability | External sample source | What to extract | Proposed local landing |
| --- | --- | --- | --- |
| Complex text template payload | `tmp/external/jianying-protocol-service/test/test.py` -> `complex_text_material_info()` | `text_segment`, `materials.texts`, `materials.effects`, `materials.material_animations` structure | Proposed curated sample JSON under `references/protocol_samples/complex_text/` |
| Complex text builder behavior | `tmp/external/jianying-protocol-service/src/utils/complex_text.py` | how extra material refs and text material ids are rewritten | implement equivalent logic in `scripts/core/protocol_ops.py` and `scripts/utils/draft_patch.py` |
| Filter segment payload | `tmp/external/jianying-protocol-service/test/test.py` -> `filter_material_info()` | `effects` material shape for filter track segments | Proposed curated sample JSON under `references/protocol_samples/filter/` |
| Video effect segment payload | `tmp/external/jianying-protocol-service/test/test.py` -> `video_effect_material_info()` | `video_effects` material shape and adjustable params | Proposed curated sample JSON under `references/protocol_samples/effect/` |
| Transition / animation attachment payload | `tmp/external/jianying-protocol-service/test/test.py` -> `transition_effect_info()`, `animation_effect_info()` | `transitions` and `material_animations` shapes that are attached to existing segments | Proposed curated sample JSON under `references/protocol_samples/internal_material/` |
| Adjustment model | `tmp/external/jianying-protocol-service/src/utils/models.py` -> `AdjustInfo` | parameter names and expected value ranges | local typed wrapper in `scripts/core/protocol_ops.py` later, not first batch |
| Segment attachment behavior | `tmp/external/jianying-protocol-service/src/utils/protocol_utils.py` -> `add_internal_material_to_segment()`, `update_segment_adjust_info()` | where refs live and how materials are added/updated | patch-mode helpers in `scripts/utils/draft_patch.py` |

Important note:

- first normalize external samples into local reference JSON files before wiring runtime code
- do not point production code directly at the cloned external repo under `tmp/external/`

## Our Repo Landing Map

This is the recommended module split for the absorbed capability.

### 1. New protocol-level mixin

Add:

- `scripts/core/protocol_ops.py`

Responsibilities:

- `add_complex_text(...)`
- `add_filter_segment(...)`
- `add_effect_segment(...)`
- `attach_internal_material(...)`
- `add_adjustments_to_segment(...)`

Why:

- these operations are closer to raw draft JSON protocol writes than to the current `pyJianYingDraft`
  high-level wrapper style
- keeping them isolated avoids overloading `text_ops.py` and `vfx_ops.py`

### 2. Patch-mode primitives

Extend:

- `scripts/utils/draft_patch.py`

Add helpers for:

- appending a complex text segment with rewritten material ids
- appending filter track segments
- appending effect track segments
- attaching transition / animation refs to an existing segment
- upserting adjust materials and removing/overwriting prior adjust refs

Why:

- all new protocol features must work on saved drafts, not only new in-memory projects

### 3. Asset search / resolve extension

Extend:

- `scripts/utils/asset_index.py`

Add or formalize categories for:

- `text_effect`
- `text_shape`
- `material_animation`
- `transition`
- existing `filter`
- existing `video_effect`

Expected write mappings:

- `filter` -> `filter_query` / `filter_id`
- `video_effect` -> `effect_query` / `effect_id`
- `transition` -> `query` when `kind=transition`
- `material_animation` -> `query` when `kind=animation`
- `text_effect` -> `text_effect_query`
- `text_shape` -> `bubble_query`

### 4. CLI and Python wrapper

Extend:

- `scripts/jy_wrapper.py`

Add new `JyProject` methods and CLI subcommands.

### 5. Tests

Start in:

- `tests/test_wrapper.py`

Later split out if needed:

- `tests/test_protocol_ops.py`

### 6. Example

No protocol-operations demo is bundled yet. Add one only after the first batch
lands and its new-draft and patch-mode tests pass. The future demo should show
one draft containing:

- a complex text segment
- a filter track segment
- one attached transition or animation

## First 3 CLI Commands

These are the highest-value first batch commands to land.

### Command 1: `add-complex-text`

Why first:

- this is the biggest current feature gap versus the external repo
- it unlocks flower text, bubble text, and templated animated text rather than only rich text styling

Suggested CLI shape:

```bash
python scripts/jy_wrapper.py add-complex-text \
  --name "DraftName" \
  --text "大标题" \
  --bubble-query "标题58" \
  --flower-query "彩色手绘线条花字" \
  --anim-in-query "向上弹入" \
  --start-time 0s \
  --duration 3s \
  --json
```

Suggested first implementation surface:

- Python: `JyProject.add_complex_text(...)`
- CLI: `add-complex-text`

Primary landing files:

- `scripts/core/protocol_ops.py`
- `scripts/utils/draft_patch.py`
- `scripts/jy_wrapper.py`
- `scripts/utils/asset_index.py`

### Command 2: `add-filter`

Why second:

- filter track segments are a clean protocol capability
- the current repo already has search coverage for `filter`, so it fits naturally into `search/resolve/write`

Suggested CLI shape:

```bash
python scripts/jy_wrapper.py add-filter \
  --name "DraftName" \
  --filter-query "1980" \
  --start-time 0s \
  --duration 5s \
  --json
```

Suggested first implementation surface:

- Python: `JyProject.add_filter_segment(...)`
- CLI: `add-filter`

Primary landing files:

- `scripts/core/protocol_ops.py`
- `scripts/utils/draft_patch.py`
- `scripts/jy_wrapper.py`

### Command 3: `attach-material`

Why third:

- this is the protocol-level primitive that makes transitions and internal animations reusable
- it is more foundational than a one-off transition command because it creates the generic
  segment ref attachment path

Suggested CLI shape:

```bash
python scripts/jy_wrapper.py attach-material \
  --name "DraftName" \
  --segment-id "<segment-id>" \
  --kind transition \
  --query "推近 II" \
  --json

python scripts/jy_wrapper.py attach-material \
  --name "DraftName" \
  --segment-id "<segment-id>" \
  --kind animation \
  --query "展开" \
  --json
```

Suggested first implementation surface:

- Python: `JyProject.attach_internal_material(...)`
- CLI: `attach-material`

Primary landing files:

- `scripts/core/protocol_ops.py`
- `scripts/utils/draft_patch.py`
- `scripts/jy_wrapper.py`
- `scripts/utils/asset_index.py`

## Second Batch After the First 3

These should come after the first batch because they reuse the same infrastructure.

1. `add-effect`
2. `add-adjustments`

Why deferred:

- `add-effect` shares much of the same protocol scaffolding as `add-filter`
- `add-adjustments` is important, but it is narrower than complex text and requires more careful
  overwrite semantics on existing effect refs

## Test Matrix

Each command should land with CLI and patch-mode coverage.

### Tests for `add-complex-text`

Add at least:

1. create a new draft and append one complex text segment
2. reopen an existing saved draft in patch mode and append a complex text segment
3. verify `materials.texts` has a rewritten text material id
4. verify `materials.effects` contains the resolved bubble / flower effect refs
5. verify `materials.material_animations` contains the resolved intro animation when provided
6. verify the segment `extra_material_refs` point to newly inserted local material ids, not stale sample ids
7. verify CLI `--json` returns the resolved assets used
8. verify `--text` overrides the sample text payload correctly

### Tests for `add-filter`

Add at least:

1. create a filter track segment in a new draft
2. add a filter segment to a reopened saved draft in patch mode
3. verify the target track type is `filter`
4. verify `materials.effects` contains the filter material payload
5. verify query-driven resolution returns the expected `write_field` / `write_value`
6. verify CLI `--json` returns the resolved filter asset

### Tests for `attach-material`

Add at least:

1. create a video segment, attach a transition, and save
2. create a video segment, attach a material animation, and save
3. reopen an existing saved draft in patch mode and attach a transition
4. verify the target segment `extra_material_refs` includes the new material id
5. verify the inserted material lands under the correct materials bucket:
   - `transitions`
   - `material_animations`
6. verify `kind=transition` and `kind=animation` reject mismatched sample payloads cleanly
7. verify CLI `--json` returns the resolved asset and attached material id

## Acceptance Criteria for the Batch

The batch is only complete when all of the following are true:

1. new Python APIs exist under `JyProject`
2. the 3 CLI commands above return stable `--json` payloads
3. query-driven resolution uses the existing `asset_index` layer
4. each command works on:
   - a new draft
   - a reopened saved draft through patch mode
5. `tests/test_wrapper.py` covers the new commands
6. docs and `SKILL.md` describe the new agent-callable surface

## Recommended Execution Order

1. curate local sample JSON payloads from the external repo
2. create `scripts/core/protocol_ops.py`
3. extend `scripts/utils/draft_patch.py`
4. wire `JyProject` and `jy_wrapper.py`
5. add tests for `add-complex-text`
6. add tests for `add-filter`
7. add tests for `attach-material`
8. update docs and examples

## Summary

Do not merge `jianying-protocol-service` as a service runtime.

Do absorb:

- complex text protocol samples
- filter / effect material samples
- transition / animation attachment samples
- adjustment parameter schema later

Land them into this repository as:

- `JyProject` methods
- patch-mode helpers
- `search/resolve/write` compatible CLI commands

This keeps the current repository as the primary agent-facing surface while upgrading its
protocol depth.
