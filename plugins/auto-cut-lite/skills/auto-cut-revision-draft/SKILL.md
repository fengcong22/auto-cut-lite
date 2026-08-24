---
name: auto-cut-revision-draft
description: Use when revising a JianYing or CapCut desktop draft from review comments and the result must preserve visible source media, separated audio, replacement audio, timeline cut points, visual-aware pause decisions, and per-source-item review markers.
---

# Jianying Revision Draft

## Packaged Runtime

Before executing this skill from the distributed plugin, read the [portable runtime contract](../auto-cut-lite/references/portable-runtime.md). Use the installed isolated runtime and `workflow_mode=lite`; do not fall back to a source checkout or another Auto-Cut installation.


## Overview

Use this skill for revision-driven JianYing draft work where the output must remain audit-friendly inside the draft itself. It is for requests like "continue revising the same draft", "preserve edit traces", "keep visible cut points", "show both video and audio materials in the media bin", or "leave a draft ready for second-stage manual editing".

This skill is not a generic "make a fresh video" workflow. It is for modifying an existing draft or generating a reviewable replacement draft while preserving source visibility, edit history shape, and review labels.

This rule set is repository-scoped. Keep it with this repository under `skills/auto-cut-revision-draft/`. It should travel with the repo and be reviewed as part of the repo, not installed as a machine-wide default rule.

## Iron Law

For this repository, a revision draft must remain a real editing project.

- do not hand back a baked final export disguised as a draft
- do not replace the timeline with one full-length preview video plus one full-length preview audio track
- do not hide the edit process that the user needs for second-stage manual editing
- if a special effect must be pre-rendered, confine that baking to the smallest local window that requires it

## When To Use

Use this skill when the user cares about one or more of these outcomes:

- video source material must still appear in the media bin
- imported audio, separated audio, and replacement audio must all appear in the media bin
- timeline must keep the video clip segments and the separated audio clip segments
- clip boundaries must remain visible after deletes, trims, and replacements
- review changes must be labeled individually instead of merged into one generic note
- the output draft is intended for second-stage manual editing in JianYing
- the user asks to continue modifying the same draft instead of producing many new drafts

Do not use this skill for a simple one-shot export where the user does not need a reusable draft.

## Workflow

1. Inspect the current draft and source assets first.
2. Translate review comments into a complete `review_items` ledger with one stable source item ID per issue before building edits. A later `doc_items` read is canonical.
3. When review comments include spoken-word deletion or audio repair, load and follow `auto-cut-review-audio-precision` before cutting.
4. When review comments include pointers, highlights, or animation timing, load the matching visual skill before writing overlays so target, duration, release, and collision evidence are planned first.
5. Apply edits while preserving timeline segmentation.
6. Sync draft metadata so video/audio materials appear in the media bin.
7. Load `auto-cut-final-acceptance` and validate draft structure, source item coverage, audio evidence, visual evidence, pause timing, and item-level acceptance before claiming success.
8. If overwriting an existing draft, refuse unsafe overwrite states instead of forcing JianYing UI navigation.
9. If same-draft continuation is unsafe because JianYing is busy with another editing/export session, automatically create one fallback revision draft instead of stopping with no output.
10. Reject the result if the saved draft collapses into a baked preview-style project instead of an editable revision project.
11. Reduce draft clutter without weakening validation: defer retention cleanup until after validation and final acceptance, clean failed fallback drafts, and keep at most one accepted fallback per project family.

## Source-Document Acceptance Gate

For Feishu/Lark review documents, the edit request must carry a source-of-truth ledger:

- every source review item must appear in `review_items`, or in a separate `doc_items.json` passed to validation
- the visible marker for each source item must equal its canonical `source_text` code-point-for-code-point; never add a summary, prefix, ID, translation, or truncation
- one stable source item ID produces one marker even when multiple internal edits implement it; duplicate IDs fail, while identical text under different IDs stays separate
- missing internal source text triggers automatic document re-read/recovery; if the source is unavailable, keep the most complete unmodified candidate with `verbatim_status=unverified_source_unavailable`, warn, and continue editing and draft generation
- `acceptance.expected_review_item_count` and `acceptance.expected_review_item_ids` must come from the document read, not from the generated draft
- `修改...` items must be executed as timeline edits or overlay operations
- `校对...` items may remain marker-only only when they are intentionally review-only; if they ask to delete, add, retime, point, underline, replace, or remove a visual, they require execution evidence
- a marker segment is traceability evidence, not completion evidence
- if the user names a clean source baseline, such as `PPT定稿+翻录`, rebuild or patch from that source and do not inherit unrelated markers or overlays from previous attempts

Before delivery, run the strict gate:

```powershell
python scripts/jy_wrapper.py revision-validate --request-json "<request.json>" --doc-items-json "<doc_items.json>" --strict --json
```

If the strict gate fails, do not report the draft as complete. Source-text recovery itself is nonblocking and must not prevent a draft from being generated; fix remaining final-acceptance failures before delivery.

## Resumable Source-document Jobs

For natural-language source-document work, start with `review-job-compile`, use its canonical `doc_items.json` and `revision_request.json`, and resume from atomic `job_state.json` checkpoints. The ordered phases and cache identities live in [the packaged revision input spec](../auto-cut-lite/references/revision-input-spec.md); do not duplicate or bypass that source of truth.

Run only bounded, independent read-only preparation concurrently. JianYing writes and saved-draft inspection are serialized, and inspection waits for the corresponding save; ordered Feishu writes are serialized globally; overlapping timeline repairs are prohibited. No two writers may target the same draft. A corrupt phase reruns in isolation, and missing source-text recovery never cancels unrelated phases or draft generation.

Keep `job_timing.json`, local evidence plans, cache entries, and processed validation media under the job's ignored `tmp/` directory. Existing direct revision scripts remain compatible only when they preserve the same editable structure, marker receipts, checkpoints, and final evidence.

## Preservation Contract

These rules are mandatory for revision work under this skill.

### 1. Material Visibility

The draft must keep all relevant materials visible in the imported-material area:

- original video material
- original separated audio material
- replacement audio material
- any newly added supporting media that is required by the revision

If a track uses a material, that material must still be represented in draft metadata, not only in raw timeline JSON.

### 2. Timeline Presence

The timeline must preserve the editable structure:

- video edits stay on video tracks
- separated source audio stays on audio tracks
- replacement narration or replacement audio stays on its own audio track unless the user explicitly asks to merge behavior
- do not duplicate the same video material on stacked tracks unless the edit actually requires layered video
- do not replace a multi-edit revision timeline with one full-length rendered clip unless the user explicitly requested a baked export
- if the user asks to see the editing process, keep explicit cut-trace lanes, review labels, or equivalent in-draft evidence

### 3. Cut Preservation

All user-visible edit actions must remain inspectable:

- head deletions should still leave the resulting cut boundary visible
- trims and split points should remain as separate segments where the edit occurred
- do not collapse adjacent segments back into one long segment if that hides the edit history
- when replacing only part of an audio region, preserve surrounding audio segment boundaries

Short version: never optimize away the proof that an edit happened.

Hard rejection rule:

- if `keep_cut_points` is true for a multi-edit revision job, a saved draft with one full-length main-video segment is invalid

### 4. Review Labels

Each revision note must remain independently traceable:

- create separate review markers or labels for separate issues
- do not merge unrelated fixes into one umbrella label
- source-ledger marker wording is the exact canonical `source_text`; item IDs and execution summaries stay in receipts/evidence rather than decorating visible text
- place every dynamic `Review Marker N` label in one horizontal top safe band on centerline `y=0.8`; concurrent markers divide the width horizontally and never create a second row in the middle of the picture
- `workflow_mode=lite` may use the explicit `auto-cut-lite` grouped exception:
  `Review Marker Delete/Visual/Animation N` families are separate, each saved label remains in
  the top safe band, and the lite validator additionally enforces left alignment, 4–5 font size,
  forced safe-width wrapping for long text, stable red/green/amber family colors, and stage-edge
  bounds.
- caller-supplied `x_positions` cannot override automatic or explicitly named dynamic tracks; explicit positioning is reserved for explicitly non-dynamic track groups
- caller-supplied lane hints are ignored for dynamic tracks so actual time overlap alone determines compact lanes; mixed dynamic/custom track-name groups are invalid
- require every saved dynamic marker segment's `clip.transform.y` to remain inside normalized `0.7..0.9` in both root and active timeline content
- call `review_marker_top_layout_problems(content)` for one saved draft-content object per call; strict validation calls it separately for root and each declared active timeline copy
- preserve canonical spaces, punctuation, and explicit line breaks while wrapping or reducing font size for layout
- if a manifest is exported, it must correspond to the in-draft labels

### 5. Draft Safety

When continuing on an existing draft:

- use three explicit UI modes: `offline` is the default for compile, ASR, draft writing, and saved-JSON validation; `opened_verify` is conditional compositor verification; `export` is UI-required export only
- offline mode must not construct JianyingController, activate JianYing, or navigate it; inspect the target `.locked` file and create one fallback draft when that lock blocks same-name overwrite
- prefer editing the same working draft name when feasible
- do not force JianYing back to the home page
- do not close other open editing sessions
- if JianYing is currently editing another project or showing export UI, do not force overwrite
- if same-name overwrite is unsafe, automatically write to one fallback draft so the revision still produces an output
- if that fallback fails validation or final acceptance, remove the failed fallback while preserving the previous accepted draft or fallback
- after a successful accepted revision, run draft retention so the project family keeps only the normal latest drafts plus at most one fallback
- For an externally written new draft, returning to the draft home page is not a refresh. Only when the target draft is not visible, protect unsaved work, perform one complete JianYing process exit and relaunch, then confirm the exact draft name and timeline ID. Once the target draft is open, do not exit it merely to refresh or repeat evidence capture.

## Execution Guidance

### Before Editing

- inspect the draft structure with the local draft tools first
- identify whether the current draft format is patchable plain JSON or a runtime-encoded format
- if direct in-place patching is unsupported for the current format, use one maintained working draft rather than creating many throwaway drafts

### During Editing

- preserve separate tracks for original video, separated original audio, replacement audio, and review labels when those elements exist
- prefer deterministic draft-writing APIs over ad hoc JSON surgery
- if JianYing has rewritten the saved draft into a runtime-encoded or opaque format, do not patch that content directly; rebuild a new version from the maintained generator, request JSON, or another readable source of truth
- if a requested edit starts at time zero, keep the post-cut segment boundary visible in the resulting track structure
- when replacing audio, add transitions or fades where needed so joins are not hard cuts unless the user explicitly wants a hard cut
- for spoken-word deletion, keep source-time physical cut windows for video/audio sync; when the full processed narration is only for ASR/listening QA, keep it under `tmp/` and deliver explicit audible source, replacement-video, and local-repair segments through `audio_delivery_plan`
- preserve the approved separated source audio as a muted reference track; use a full replacement-audio track only when the user explicitly requests whole-track replacement
- when post-delete pause timing is adjusted, run the visual-aware pause gate from `auto-cut-review-audio-precision`: do not shorten a pause only because the audio gap is long if that beat carries animation, pointer, page-turn, or key visual reading time
- when a valid deletion makes the next sentence or replacement bridge arrive too fast, add a separate `semantic_pause_adjustment` instead of widening the delete window: insert a short no-audio still-frame hold from the current source/replacement frame, keep it as its own segment, and shift later records through the same timeline plan
- for each semantic pause, bind the immutable request ASR path/hash/identity, current source audio/video hashes, and alignment-audio hash; require byte identity for `preprocessing=none` or a receipt binding transformed hashes/tool/version/parameters, require the utterance midpoint to preserve the configured guard from the nearest real word end/start, and require a still decoded within one frame interval of that midpoint with matching pixels; reject multiple pauses under one stable item ID until one-to-many receipts exist
- compile segmented audio after alignment and bind every segmented reverse-ASR report through `revision-bind-audio-report` so its audio delivery plan SHA-256 matches the exact normalized plan
- write any skipped pause-compression row as `visual_hold_review` in the report or marker trace, including the conflicting visual event and source-time guard window
- for pointer overlays, require target point, anchor rule, scale rule, in/out timing, duration rule, and disappearance reason before calling the item complete
- for every course pointer, defer placement and scale validation to `auto-cut-pointer-targeting`; set `acceptance.require_pointer_profile_binding=true` and require `review_items[*].evidence.subject_profile_receipt` with `project-bindings.json` evidence for the current `project_key`, stored `project_path`, and bound profile, followed by a fresh exact `stage_id + subject_id` check with `status=ready`, matching asset SHA-256, `scale_reference_path`, scale-reference hash, approved preview evidence, and receipt-bound current-layout evidence before writing the overlay. The receipt's `project_key`, stored `project_path`, `profile_key`, `stage_id`, and `subject_id` must all match the same binding, while `profile_path` must match that binding's fresh registry result.
- for `target_anchor=text_bottom`, record in-canvas `target_geometry` and derive the target point from the text lower edge plus the bound gap; always write `underline_layers` and `clean_layers` arrays even when empty
- for animation timing overlays, prefer full-screen or whole-section evidence over local crop patches unless the crop is explicitly justified and visually compared
- if a verified render is correct but the opened JianYing draft shows pointer/visual display drift, allow only a documented local display-safe baked window: no audio, full canvas, scale 1, transform 0, exact short window, topmost, with editable tracks preserved underneath and `opened_draft_drift_artifact_sha256` plus timeline/window binding evidence

### After Editing

Validate all of the following:

- media bin includes the expected video and audio materials
- timeline includes the expected video segments
- timeline includes the expected separated-audio segments
- replacement audio exists on the intended track
- review markers exist for each requested source item (or each legacy/manual marker when no source ledger exists)
- source-ledger markers use dynamic `Review Marker N` tracks and return receipts with item/text/status/segment/material/track/timing identity
- dynamic marker labels share one horizontal top safe band on centerline `y=0.8`, and every saved `clip.transform.y` is inside `0.7..0.9`; legacy/manual track names remain compatible and are not reclassified as dynamic tracks
- caller-supplied `x_positions` cannot override dynamic marker layout; only explicitly non-dynamic track groups retain custom horizontal positions
- caller-supplied lane hints do not expand dynamic layouts, and mixed dynamic/custom track-name groups are invalid
- the marker-layout helper receives one saved draft-content object per call, while strict validation invokes it independently for root and active timeline copies
- the saved root and every declared active timeline copy pass full editable structure, material, audio-delivery, exact marker text, count, receipt, and mapped-start validation; a missing declared active file fails, and an explicit empty ledger has no extra old markers
- no accidental duplicate stacked video track was introduced
- the main timeline was not flattened into a baked preview-style project
- if local baked windows were required, they are limited to the smallest effect-specific regions and the rest of the timeline remains segmented
- if local display-safe windows were required because opened JianYing display disagreed with the verified render, document the drift cause, baked media path, time range, and preserved editable evidence
- for precise audio revision, include the fully decoded full-candidate reverse-ASR validation report from `auto-cut-review-audio-precision`, with matching candidate SHA-256, provider/model/adapter identity, normalized plan digest, duration coverage allowing only adjacent two-sided crossfades, and local transcript/cut/join/delete/keep/semantic evidence that matches each spoken-delete item contract; aggregate counts and generic item-level pass rows are invalid
- when segmented audio delivery is used, confirm every planned segment matches the saved track/material/range/volume/fade data, no validation-only file appears in materials, and no unplanned or near-full-length narration segment exists
- for pause timing revision, include the adjusted pause count, `semantic_pause_adjustment` rows, and any `visual_hold_review` rows so the user can inspect why a pause was inserted, shortened, or kept
- for pointer, hand, underline, animation timing, or visual-delete rows, include per-item overlay/material/timing evidence in `review_items[*].evidence`
- confirm no unrelated old markers or traces were carried over when the user requested a fresh baseline
- run strict acceptance validation when the source came from a Feishu/Lark review document
- run the `auto-cut-final-acceptance` checklist before reporting the draft path as complete
- run draft retention only after the acceptance checks pass; failed fallback drafts should be cleaned without deleting the previous accepted fallback
- do not claim completion if that report has unresolved `fail` rows

## Local Tool Surface

For this repository, prefer these surfaces:

- `python scripts/jy_wrapper.py revision-validate --request-json "<request.json>" --json`
- `python scripts/jy_wrapper.py revision-validate --request-json "<request.json>" --doc-items-json "<doc_items.json>" --strict --json`
- `python scripts/jy_wrapper.py revision-summary --request-json "<request.json>" --json`
- `python scripts/jy_wrapper.py revision-run --request-json "<request.json>" --drafts-root "<draft root>" --json`
- `python scripts/jy_wrapper.py revision-run --request-json "<request.json>" --doc-items-json "<doc_items.json>" --strict --drafts-root "<draft root>" --json`
- `python scripts/draft_inspector.py summary --name "<DraftName>"`
- `python scripts/draft_inspector.py show --name "<DraftName>" --kind content --json`
- `python scripts/jy_wrapper.py list-segments --name "<DraftName>" --json`
- the project-specific revision scripts when a job already has a maintained script

Read [references/checklist.md](references/checklist.md) before final verification.

Read [references/preservation-rules.md](references/preservation-rules.md) when implementing or reviewing a revision script.

Use the packaged revision request format defined in [../auto-cut-lite/references/revision-input-spec.md](../auto-cut-lite/references/revision-input-spec.md).

Start from [../auto-cut-lite/references/revision_request_example.json](../auto-cut-lite/references/revision_request_example.json) when preparing a new review job.

## Distribution Rule

When sharing this repository with other users:

- keep the skill bundle inside the repository
- keep the revision rules versioned with the repository
- do not rely on a separate global Codex skill for correctness
- if another environment wants the same behavior, it should use this repository copy of the skill or copy the bundle into its own project on purpose

## Common Failure Modes

- media bin shows only audio because metadata was not synced from timeline materials
- separated audio exists in processing but is missing from the final draft tracks
- top and bottom tracks both show the same video because the draft duplicated the source layer instead of separating video and audio responsibilities
- a 0-second deletion happened logically but no cut is visible in the timeline
- overwrite succeeded only after forcing JianYing UI state, which risks disrupting another unsaved project
- the result is actually one rendered preview video plus one rendered preview audio track, which destroys second-stage editability
- self-rendered preview passed, but the opened JianYing draft showed a different pointer size or position; this requires opened-draft calibration or a documented local display-safe window, not more preview-only coordinate nudges
- a runtime-encoded opened draft was patched as if it were readable JSON; rebuild from the maintained generator or readable source instead

## Success Standard

This skill is successful only when the user can open the draft in JianYing and verify all of the following inside the editor:

- source video is visible in materials
- source/separated/replacement audio is visible in materials
- timeline still shows the actual cut points
- review labels show what was changed
- the draft is usable for second-stage manual editing without rebuilding the structure
- the user can identify the edit process directly inside JianYing instead of reverse-engineering a baked result
