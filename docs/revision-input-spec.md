# Revision Input Spec

This document defines the repository-scoped input format for review-driven JianYing draft revision.

Use this format when the task is:

- continue revising an existing draft
- revise by screenshot comments or review notes
- preserve visible cut points
- preserve source video, separated audio, and replacement audio in the media bin
- leave one label per review issue

## Goals

The request format must make these requirements explicit:

- what draft is being revised
- what source media must remain visible
- which timeline regions are deleted
- which timeline regions replace narration or audio
- whether narration is delivered as explicit editable source/replacement/repair segments
- which review labels must remain independently traceable
- whether cut points and materials must be preserved
- whether the output must remain a real editable project instead of a flattened export

## Default Source-document Workflow

For a natural-language Feishu/Lark source-document job, compile the document snapshot and project description with `review-job-compile` before editing. Resume through `job_state.json`; do not rebuild canonical ledgers or valid evidence phases merely because a later phase was interrupted. Legacy request builders and direct revision scripts remain compatible, but they do not establish optimized/source-document final acceptance unless they produce the same cache identities, checkpoints, receipts, and acceptance evidence.

Keep compiled requests, cache entries, checkpoints, evidence plans, and timing files under an ignored `tmp/` job directory. Missing source text follows the nonblocking recovery contract below and never removes the item or stops unrelated editing work.

## Cache Identity And Invalidation

Use content-addressed identities for every reusable artifact:

| Artifact | Complete identity |
| --- | --- |
| Source ASR | source audio SHA256 + preprocessing + provider + model/resource ID + adapter version |
| Reverse ASR | complete final candidate audio SHA256 + provider/model/resource ID + adapter version |
| Visual frame | video SHA256 + source window/time + extraction parameters + tool version |
| Local render | draft/timeline digest + affected window + render settings + renderer version |
| Document snapshot | document token + revision identity + extraction schema |

Missing versions, a hash mismatch, a changed cut, or changed preprocessing invalidate the affected entry. Source ASR can survive an edit-plan change only while its complete source identity remains unchanged. Reverse ASR can be reused only for the identical complete final candidate audio hash and ASR identity; a spoken-audio change or new hash still requires one final full-candidate result. Cache reuse supplies required evidence and never converts a required gate into skipped evidence.

## Atomic Phases And Resume

Checkpoint these atomic phases in order:

1. `document_snapshot_ledger`
2. `source_materials_hashes`
3. `source_asr_visual_index`
4. `classified_edit_acceptance_plans`
5. `processed_local_media_evidence`
6. `saved_editable_draft_marker_receipts`
7. `final_acceptance`

Write an atomic `job_state.json` checkpoint only after validating a phase's input/output digests and artifacts. A corrupt or mismatched checkpoint reruns only that phase; completed independent phases remain reusable. Missing source text recovery never cancels unrelated phases or draft generation.

## Subject-pointer external wait and return

When a source-document review job reaches the subject-pointer gate without an exact ready profile and confirmed current-project binding, persist a nullable `external_wait` in `job_state.json` and mirror it in `job_timing.json`. The closed wait uses `code=awaiting_subject_profile`, `phase=subject_pointer_profile_gate`, the exact unique pending item IDs, the job `input_digest`, relative artifact path `subject_pointer_onboarding.json`, its `artifact_sha256`, and the UTC `started_at` time. Interactive work retains the same pending request in the originating Codex task even when it has no persisted review-job wait.

The sidecar preserves the project identity, pending pointer item IDs and source-ledger IDs, requested asset roles, current layout names, explicit identity values, current profile problems, and the mapped user action. It does not contain source text, inferred identity, a fallback profile, or secrets. The artifact must be a regular JSON file reached through a relative path contained by the ignored job directory. On status, restart, resume, replacement, and resolution, verify the current sidecar SHA-256 against `external_wait.artifact_sha256`; a missing, changed, non-regular, symlinked, out-of-root, or non-JSON artifact rejects stale resume instead of reconstructing context from process memory.

The `external_wait.item_ids` values are the pending pointer item IDs, not completed edit receipts. Preserve them and their source-ledger mappings unchanged through every onboarding question. Independent non-pointer phases may continue, but pointer-dependent phases remain pending and must not write pointer overlays or a pointer-dependent draft. Cancellation or incomplete evidence leaves those items traceable as pending and cannot be reported as executed edits.

After the exact profile is ready and the user has confirmed the applicable bind or rebind, return to `auto-cut-pointer-targeting`. Reload the current project binding, run a fresh registry `check` for the exact stage and subject, recalculate current asset, scale-reference, and approved-preview hashes, and verify every requested role and current layout carried by the pending items. Only then resolve the external wait by compare-and-swap with its current `input_digest` and `artifact_sha256` plus the current project key, draft path, and draft root. Resolution clears `external_wait`, attributes the elapsed duration to application/user wait, leaves `subject_pointer_profile_gate` pending, and must rerun `subject_pointer_profile_gate` from its normal entry before any pointer draft write. A changed project identity, input digest, sidecar hash, role, layout, or profile hash opens a new handoff instead of using the earlier check as a ready receipt.

## JSON Shape

```json
{
  "workflow_mode": "full",
  "project": {
    "draft_name": "ReviewDraft",
    "source_video": "<media-root>/source.mp4",
    "source_audio": "<media-root>/source.wav",
    "replacement_audio": "<media-root>/replacement.mp3"
  },
  "edits": [
    {
      "type": "delete",
      "start": 0.0,
      "end": 3.0,
      "label": "Remove opener",
      "detail": "Delete the title opener"
    },
    {
      "type": "replace_audio",
      "start": 197.0,
      "end": 205.0,
      "audio_path": "<media-root>/replacement.mp3",
      "label": "Replace narration",
      "detail": "Use the provided MP3 for this narration window"
    }
  ],
  "markers": [
    {
      "label": "Animation check",
      "start": 220.0,
      "end": 221.0,
      "detail": "Verify the animation timing"
    }
  ],
  "review_items": [
    {
      "id": "修改01",
      "kind": "spoken_delete",
      "source_text": "00:10-00:22 删除“同学们知道哈...”",
      "execution_required": true,
      "evidence": {
        "executed": true,
        "cut_window": [10.0, 22.0],
        "validation": {
          "status": "pass"
        }
      }
    },
    {
      "id": "校对05",
      "kind": "pointer_overlay",
      "source_text": "05:17-05:19 删除原小手并重新添加，指向/下划线“记有5000余单字”",
      "execution_required": true,
      "evidence": {
        "executed": true,
        "track_name": "Pointer Overlay",
        "segment_id": "seg-pointer-05",
        "asset_path": "<media-root>/hand.png"
      }
    }
  ],
  "acceptance": {
    "expected_review_item_count": 2,
    "expected_review_item_ids": ["修改01", "校对05"],
    "require_review_items": true,
    "require_execution_evidence": true,
    "require_audio_validation": true,
    "require_visual_evidence": true,
    "require_subject_pointer_binding": true,
    "require_pause_validation": true,
    "require_final_acceptance": true
  },
  "preserve": {
    "source_video_material": true,
    "separated_audio_material": true,
    "replacement_audio_material": true,
    "keep_cut_points": true,
    "keep_review_markers_separate": true
  }
}
```

## Workflow Mode

- `workflow_mode=full` is the default and preserves the existing full-capability behavior.
- `workflow_mode=lite` selects the High School History compact workflow. It keeps the source
  duration unchanged. Its default `lite_cut_layout=split_gap` writes non-delete intervals to
  `Original Video` and `Separated Source Audio`, delete intervals to `Lite Cut Segments` and
  `Lite Reused Audio`, simple downloaded assets to `Lite Visual Assets`, and timing copies to
  `Lite Timing Adjusted`. Set `lite_cut_layout=copy` for the older full-V1/A1 reference layout.
- In lite mode, set `visual_plan.reuse_audio=false` for visual-only source reuse or still frames.
  Omitted `reuse_audio` defaults to true for source delete/timing copies and false for external
  visual assets.
- Lite review labels use source text verbatim, start at the edit time, remain in the top safe band,
  and last `2s` unless clamped by the unchanged project end.
- Lite mode does not weaken spoken-delete or pointer evidence. Review-document timestamps are
  `search_hint` values only. A spoken delete must carry a passing word/character `asr_alignment`
  receipt whose `resolved_cut_window` equals the edit start/end, together with `delete`, explicit
  `must_keep`, and `strategy`. The receipt must bind the source-audio SHA-256, provider plus
  model/resource identity, adapter version, ordered positive-duration matched word/character
  rows, and `authoritative_cut_boundary=true`. Candidate transcripts, prompted local ASR, or a
  rough-time match must keep `authoritative_cut_boundary=false` and cannot be written as a cut.
  Pointer rows remain execution-required and retain the ordinary pointer binding, lifecycle,
  placement, and visual acceptance gates.

## Required Fields

### `project`

- `draft_name`: target draft name
- `source_video`: source video path

Optional but recommended:

- `source_audio`: separated original audio path
- `replacement_audio`: replacement narration/audio path

`draft_name` is still the preferred working draft name. If JianYing is currently busy with another edit/export session and the same-name draft cannot be safely overwritten, the repository runner may automatically write the revision to one fallback draft name instead of stopping with no output.

To reduce draft clutter without lowering correctness, revision runners should defer retention cleanup until after saved-draft validation and final acceptance pass. A fallback draft that fails validation should be removed, while the previous accepted draft or fallback remains available for rollback. After success, keep the latest normal drafts plus at most one fallback for the project family.

### `edits`

Each edit must include:

- `type`
- `start`
- `end`
- `label`

Supported first-pass edit types:

- `delete`
- `replace_audio`

For `replace_audio`, also include:

- `audio_path`

### `markers`

Use explicit markers for review items that are not just implied by an edit window.

`markers` is the legacy/manual marker surface. For a source-document ledger, derive visible markers from `review_items` or the latest `doc_items` instead; the visible value is the ledger row's exact `source_text`, not a rewritten `label` or `detail`.

Each marker should include:

- `label`
- `start`
- `end`

Optional:

- `detail`

### `review_items`

Use `review_items` as the source-of-truth ledger for Feishu/Lark review documents. It must be generated before editing, directly from the document content, and preserved through execution and validation.

Each item should include:

- `id`: stable review id, such as `修改02` or `校对05`
- `kind`: routed kinds such as `spoken_delete`, `pause_delete`, `pause_timing_review`, `pointer_overlay`, `animation_timing`, `visual_delete`, `visual_overlay`, `bgm_replace`, `audio_level`, `noise_cleanup`, or `review_only`
- `source_text`: original review-document wording
- `execution_required`: `true` when the item is an actual change request
- `evidence`: proof that the request was executed
- `validation`: proof that the output was checked

Evidence examples:

- audio deletion: `cut_window`, ASR source, strategy, deleted phrase, must-keep phrase, reverse-ASR `status`
- semantic pause repair: `semantic_pause_adjustment` with item id, source cut window, timeline start/end, duration, still-frame source or frame path, semantic reason, and protected-word status
- pointer/hand/underline: overlay `track_name`, `segment_id`, `asset_path`, target point, placement method, scale rule, and source-video/reference frame when available
- animation timing: overlay or shifted segment ids, edit mode, first visible frame, stable frame, release time
- visual deletion: mask/overlay/patch segment ids or the exact local baked window

### Source-ledger marker identity and recovery

For a source-ledger marker, render `source_text` code-point-for-code-point. This includes timestamps, punctuation, whitespace, line breaks, and literal question marks. Never substitute a summary, prefix, ID, translation, or truncation unless those exact characters are already present in the source.

Generate one marker per stable source item ID; multiple internal edits share that marker rather than creating more visible markers. Duplicate source item IDs are validation failures. Identical `source_text` under different IDs remains separate and produces one marker for each ID.

The latest `doc_items` document read is canonical for item identity, kind, and source text. If an internal request item is missing `source_text`, perform automatic document re-read/recovery before rendering markers. If the document cannot be read again, use the most complete unmodified candidate from the available row fields, set `verbatim_status=unverified_source_unavailable`, and preserve the item. This fallback must never stop editing or prevent draft generation. It must emit a warning and must not claim exactness.

Source recovery is deliberately different from final acceptance. Active editing continues with the warned fallback; strict final validation still enforces draft existence, editable structure, and all other required evidence gates. `verbatim_status=unverified_source_unavailable` is warning/review evidence, not a standalone final failure: delivery may pass when the chosen candidate is saved unchanged and all other gates pass, but the report must not describe that text as source-verified exact.

### Marker receipts and saved-draft validation

Marker tracks are actual dynamically allocated `Review Marker N` text tracks. Keep one receipt per source item with `item_id`, `source_text`, `verbatim_status`, `segment_id`, `material_id`, `track_name`, `start_time`, and `duration`.

After saving, validate both the root `draft_content.json` and the active `Timelines/<main_timeline_id>/draft_content.json`. Each saved representation must preserve exact marker text, marker count, receipt mapping, and mapped timeline start. An explicit empty source ledger rejects extra markers left by older work; an empty ledger is not permission to accept unbound marker tracks.

### `acceptance`

Use `acceptance` to make the final gate explicit. For Feishu/Lark review documents, set:

- `expected_review_item_count`
- `expected_review_item_ids`
- `require_review_items: true`
- `require_execution_evidence: true`
- `require_audio_validation: true` when spoken deletion/audio repair exists
- `require_visual_evidence: true` when pointer, hand, underline, animation, visual deletion, or overlay edits exist
- `require_subject_pointer_binding: true` for course pointer jobs; each pointer item must carry a fresh `evidence.subject_profile_receipt`
- strict validation always enforces the canonical subject-pointer receipt for pointer items; omitting or setting this field to false is not an opt-out
- `require_pause_validation: true` when post-delete pauses are shortened, extended, preserved, repaired with `semantic_pause_adjustment`, or marked `visual_hold_review`
- `require_final_acceptance: true` when the draft is being delivered as complete

Every acceptance profile runs the low-cost source coverage, execution evidence, draft existence, editable structure, verbatim marker, and audio delivery gates. It conditionally routes the expensive audio precision/join, pause, visual, pointer, and animation gates from the latest ledger kind and executed operations. BGM replacement, level/loudness, and noise-only work skips reverse ASR unless a true acceptance flag explicitly requires it.

An explicitly true conditional gate requires attributable evidence on an execution-required source item. A false flag cannot disable a gate required by detected work, including the canonical subject-pointer gate. Unknown execution-required types produce a structured review/failure instead of silently skipping validation. Strict final CLI validation fails when the named draft does not exist; source-text recovery during active editing remains nonblocking.

`expected_review_item_count` and `expected_review_item_ids` may also come from a separate `--doc-items-json` file produced directly from the source document. This is the preferred way to catch items lost while translating the document into an edit request.

Final acceptance evidence should summarize source coverage, audio validation, visual evidence, pause-fit result, editable structure, marker traceability, and unresolved review/fail rows. Use `auto-cut-final-acceptance` for this delivery gate.

`require_final_acceptance: true` is effective even when the caller omits `--strict`; the CLI must still reject a missing or invalid saved draft.

### `audio_delivery_plan`

Use `audio_delivery_plan.mode: "segmented"` when a full processed narration file is needed for ASR or listening QA but must not be imported into the JianYing draft.

```json
{
  "audio_delivery_plan": {
    "mode": "segmented",
    "forbid_full_length_segments": true,
    "max_single_segment_ratio": 0.9,
    "validation_only_audio_paths": ["<qa-root>/final_narration_qa.wav"],
    "segments": [
      {
        "id": "source-001",
        "role": "source",
        "asset_path": "<audio-segments-root>/source-001.wav",
        "track_name": "Narration - Source Segments",
        "source_start": 0.0,
        "timeline_start": 0.0,
        "duration": 12.4,
        "volume": 1.0,
        "fade_in": 0.0,
        "fade_out": 0.0
      }
    ]
  }
}
```

Supported roles are `reference`, `source`, `replacement_video`, and `repair`. Segment ids must be unique, segments on the same track must not overlap, and a validation-only path cannot also be a segment asset. In segmented mode the saved-draft gate requires a one-to-one match for every planned segment and rejects any unplanned audio segment.

The full QA WAV/MP3 may remain in `processed_audio` metadata, but it must stay outside `materials.audios`. Use a muted `reference` lane for the approved source audio, audible `source` chunks for normal narration, `replacement_video` chunks for supplied replacement clips, and short `repair` chunks only where local cleanup is required.

In segmented mode, the complete candidate path declared by `processed_audio` is implicitly validation-only even when it is omitted from `validation_only_audio_paths`. A matching segment asset is rejected before draft writing, and saved-draft validation rejects the same bytes if they appear in the material bin under multiple short segments.

The canonical compiler may initially emit an empty segmented plan with `pending: true`. That file is a planning skeleton only: `revision-run` rejects it before opening a draft until a maintained media planner replaces it with explicit segments and removes `pending`.

For every final spoken-audio candidate, `processed_audio` must identify the complete candidate file and its reverse-ASR summary. The summary must contain `candidate_audio_sha256`, a complete `asr_identity` (`provider`, model/resource ID, and `adapter_version`), and attributable spoken-delete rows with strategy, source cut windows, mapped join times, local transcript, explicit `delete_hits`, `keep_hits`, and semantic-join validation. Every row must match that source item's strategy, delete phrase, `must_keep` phrases, physical delete windows, and join times derived from the segmented plan; truthy delete hits or unproven keep hits fail. Aggregate counts and generic item-level pass rows are invalid. Validation hashes and fully decodes the candidate bytes, including an FFmpeg full-stream decode for non-WAV media. Candidate duration must cover the normalized segmented delivery timeline. Only a two-sided `min(previous.fade_out, next.fade_in)` at an exactly adjacent audible-segment boundary counts as declared crossfade overlap; an isolated fade never shortens the required duration.

The local transcript must contain alphanumeric content, and transcript aliases must agree after normalization. Cross-check each row's hit fields against its normalized local transcript: the transcript must not contain the delete phrase and must support every `must_keep` phrase. A retained same-word occurrence is valid only with exactly one positive `delete_hit` and structured `delete_hit_adjudication` fields `classification=kept_recurrence`, `occurrence_role`, `phrase`, `local_context`, `context_anchor`, and `reason`; the local context must occur in the transcript, the context anchor is not a substring of the delete phrase, and that anchor remains after removing the delete phrase from the local context. Multiple positive `delete_hits` or multiple local transcript delete occurrences, including overlapping occurrences, require per-hit adjudication and fail until one-to-many receipts are supported.

Every spoken-delete row is checked against a non-empty item contract containing strategy and delete plus an explicit `must_keep` field. Each positive `delete_hit` must match the item delete phrase. The candidate SHA-256 participates in the duration cache key so same-path media replacement cannot reuse stale timing.

The latest canonical doc item kind determines whether the spoken contract applies. Spoken-delete and semantic-pause evidence are validated independently, even under the same item ID. A forbidden semantic-join phrase is waived only by `pass_adjudicated` with a non-empty reason, `final_gap` between 0 and 0.2 seconds, and `no_extra_deletion_contract=pass`.

For every segmented candidate, the summary must also contain
`audio_delivery_plan_sha256`, referred to by the request contract as
`processed_audio.audio_delivery_plan_sha256`. Produce it with the maintained
binding command after audio-plan compilation and any pause alignment, never by
copying a private validator value:

```powershell
python scripts/jy_wrapper.py revision-bind-audio-report `
  --request-json path/to/request.json `
  --report-json path/to/reverse_asr_raw.json `
  --output-json path/to/reverse_asr_bound.json `
  --json
```

Point `processed_audio.validation_summary` at the bound output. The command
normalizes the request first and writes the same public
`audio_delivery_plan_sha256` that strict acceptance recomputes.

Automatic media repair is limited to one targeted attempt. It snapshots the immutable request, invokes the callback on a deep copy, validates the raw callback result against the authorized scope, then normalizes/preflights it and validates scope a second time before saving. It reruns the affected expensive gates plus every global gate, cannot change unrelated items, and cannot widen an audio delete window across a protected boundary. Root and declared active-timeline drafts both receive full structure validation. A structurally invalid repair save restores the pre-repair draft; a second media failure remains unresolved for manual handling and reports the preserved draft path plus unresolved item IDs.

The repair callback must not mutate the live project and must not write saved draft files directly; it must return a scoped `RevisionRequest` for the raw and prepared scope checks.

### ASR-bound semantic pause adjustments

For every `semantic_pause_adjustment`, the rough timestamp is a search hint rather
than an insertion point. The request must include:

The source ASR path and source ASR SHA-256 identify the timing artifact itself;
the source-media and adapter fields below bind that artifact to this project.

- `project.media_duration_seconds`: current source duration; it must agree with
  media probing and the source duration derived from the saved main timeline
- `pause_alignment.source_asr_path`: the real source ASR path
- `pause_alignment.source_asr_sha256`: SHA-256 of the current ASR bytes
- `pause_alignment.source_video_sha256`: SHA-256 of `project.source_video`
- `pause_alignment.source_audio_sha256`: SHA-256 of `project.source_audio`
- `pause_alignment.alignment_audio_path`: the exact audio bytes submitted to ASR
- `pause_alignment.alignment_audio_sha256`: SHA-256 of those submitted bytes
- `pause_alignment.source_asr_identity`: provider, model/resource ID,
  `adapter_version`, and preprocessing identity. String `none` is valid only when
  source and alignment audio SHA-256 are equal. Transformed alignment audio uses
  a preprocessing object with `source_audio_sha256`, `alignment_audio_sha256`,
  `tool`, `tool_version`, and non-empty `parameters`.
- `pause_adjustments[*].requested_source_time`: the original rough source time
- `pause_adjustments[*].source_time`: the resolved adjacent-utterance-gap midpoint
- `pause_adjustments[*].frame_source_time`: the still sampling time, equal to the midpoint
- `pause_adjustments[*].frame_path`: the extracted still-frame file
- `pause_adjustments[*].frame_sha256`: SHA-256 of the current still bytes

The resolved point must be strictly inside the utterance gap, never on the
ASR-reported previous tail or next onset, and must not be inside a spoken word
or physical delete window. The midpoint must also preserve the configured guard
from the nearest real word end and word start, not merely from the provider's
utterance envelope. Evidence records `previous_word_end`, `next_word_start`,
`previous_utterance_end`, `next_utterance_start`, `previous_guard_seconds`, `next_guard_seconds`,
`minimum_edge_guard_seconds`, and `placement=gap_midpoint` so both edge guards
and guard time can be recomputed. It also records requested source time, resolved source time,
and frame source time in addition to the machine field names above.
Utterance-only ASR is insufficient: each semantic pause requires real word or character timing, and its semantic pause edit and `pause_adjustments` entry must correspond one-to-one before draft generation.
Compile the audio delivery plan after pause alignment, write it to
`audio_delivery_plan`, split source/reference audio at
the resolved source time, and keep audible segments outside the no-audio hold.
Standalone `revision-validate` recomputes this evidence from the current ASR bytes.
It also requires evidence ASR path/hash/identity and resolution settings to equal
the immutable request configuration, hashes the current source video, source audio,
and alignment audio, validates byte identity or the preprocessing receipt, seeks
and decodes the source video within one frame interval plus 2 ms of
`frame_source_time`, and requires the still pixels to match that source frame.
One stable source item may carry only one semantic pause until list-valued
pause receipts are supported; duplicate normalized item IDs fail closed.
The full-candidate reverse-ASR gate must also confirm that the preceding sentence
tail and following sentence onset remain complete; silence-overlap checks alone
cannot accept an edge-clipped candidate.

### UI execution modes

The three modes are `offline`, `opened_verify`, and `export`:

- `offline` is the default for document compilation, ASR, timeline construction,
  draft writing, saved-JSON inspection, and reverse ASR. It must not construct
  JianyingController, launch JianYing, foreground it, or navigate it.
- `opened_verify` is a separate conditional phase for residual pointer cover,
  reported opened-draft display drift, or an explicit user request.
- `export` is the separate UI-required export phase.

Offline same-name overwrite checks the target `.locked` file and creates one
fallback draft when locked. Results report `ui_mode`, `opened_state_required`,
`opened_state_reason`, `opened_state_status`, and `controller_calls`.

### `preserve`

All fields default to `true`, but include them explicitly for review jobs so the contract is visible in the request itself.

In this repository, review jobs are editable-project jobs by default. A flattened preview export is invalid unless the user explicitly asks for export-only output.

- `source_video_material`
- `separated_audio_material`
- `replacement_audio_material`
- `keep_cut_points`
- `keep_review_markers_separate`

## Rules

1. Use one edit item per review issue.
2. Use one source-ledger marker per stable source item ID; multiple internal edits for that ID share it.
3. Do not encode multiple unrelated fixes into one giant time window.
4. If a screenshot note is truly review-only, use `markers`; if it asks to add, remove, shift, retime, point, underline, or replace anything, add a `review_items` row with `execution_required=true` and execution evidence.
5. If the user says materials must remain visible, keep the matching `preserve` fields set to `true`.
6. If the user needs second-stage manual editing, the resulting draft must keep inspectable cut structure and must not collapse into one full-length preview clip.
7. Do not delete a review-document item because a previous attempt mishandled it or its internal `source_text` is missing. Re-read the document automatically; if unavailable, preserve the most complete unmodified candidate with an unverified warning and continue draft generation.
8. In segmented audio mode, do not import a full merged QA narration, add unplanned audio segments, or use one narration segment that reaches the configured full-length ratio.

## Marker vs Edit Semantics

- `edits` are executed operations. They must change the main editable timeline and should leave visible cut structure when `keep_cut_points=true`.
- `markers` are review items. They are independently traceable labels for manual verification or later adjustment and do not automatically require a main-track split.
- `修改` and `校对` remain semantic categories in the item ID/kind and execution evidence. Do not add either prefix to visible source-ledger marker text unless it is already present in `source_text`.
- A review-only item without a nearby cut is valid when its source-ledger marker remains independently traceable and its receipt identifies the item.
- A visible `校对` or `修改` label is not proof that a requested visual asset, animation timing change, or spoken-word deletion was executed. Strict validation must find execution evidence.
- For Feishu/Lark review documents, every source item must appear in `review_items` or a separate `doc_items` ledger. Missing items are validation failures even if the saved draft has enough marker segments.

## Validation Commands

Summarize a request:

```bash
python scripts/jy_wrapper.py revision-summary --request-json path/to/request.json --json
```

Validate a request:

```bash
python scripts/jy_wrapper.py revision-validate --request-json path/to/request.json --json
```

Run strict source-document acceptance validation:

```bash
python scripts/jy_wrapper.py revision-validate --request-json path/to/request.json --doc-items-json path/to/doc_items.json --strict --json
```

Execute a request:

```bash
python scripts/jy_wrapper.py revision-run --request-json path/to/request.json --drafts-root "<draft root>" --json
```

Execute with the strict post-save gate:

```bash
python scripts/jy_wrapper.py revision-run --request-json path/to/request.json --doc-items-json path/to/doc_items.json --strict --drafts-root "<draft root>" --json
```

For dry-run or test environments without real media probing:

```bash
python scripts/jy_wrapper.py revision-run --request-json path/to/request.json --mock-media --json
```
