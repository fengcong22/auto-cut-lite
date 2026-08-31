# Revision Input Spec

This document defines the repository-scoped input format for review-driven JianYing draft revision.

For `workflow_mode=lite`, the [Lite execution contract](lite-execution-contract.md) is
authoritative and overrides every full-workflow example or field described below. In particular,
animation/text-timing and cleanup-only pointer items are label-only, supplied visual assets use
default geometry, and every audio-identifiable item attempts ASR first. Only uniquely ASR-located
spoken deletion may write a non-destructive split/cut trace; no spoken deletion removes media or
shortens the timeline. Non-executing items use the review timestamp when ASR cannot locate a unique
point. Every new or unrecognized instruction is label-only by default;
every pause addition, extension, shortening, or adjustment is label-only. A visible JianYing label
always equals only the ledger item's `source_text`; execution status and diagnostic wording remain
internal metadata and never become label text.

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

For a natural-language Feishu/Lark source-document job, run `review-document-run` with the
document snapshot and project description. It owns the fixed phase DAG from canonical compilation
through strict acceptance and ZIP publication. Resume through `job_state.json`; do not rebuild
canonical ledgers, valid ASR/cache evidence, processed media, or a valid saved draft merely
because a later phase was interrupted. `review-job-compile` and direct `revision-run` remain
low-level compatibility commands, but they do not establish optimized/source-document final
acceptance unless they produce the same cache identities, checkpoints, receipts, and acceptance
evidence.

Keep compiled requests, cache entries, checkpoints, evidence plans, and timing files under an ignored `tmp/` job directory. Missing source text follows the nonblocking recovery contract below and never removes the item or stops unrelated editing work.

### Replacement-video timebases

Review documents may contain timestamps from more than one clock. The compiler treats the main
video clock as global timeline seconds and a replacement/补录 clock as local seconds relative to
its declared replacement anchor. Extraction must preserve document order and either provide
`source_role`/`replacement_anchor_id` fields or retain the declaration text so the compiler can
resolve them with its state machine. A compiled item records both ranges:

```json
{
  "source_role": "replacement_video",
  "source_time_range": [5.0, 13.0],
  "timeline_time_range": [458.0, 466.0],
  "timebase": {
    "kind": "replacement_local",
    "offset_seconds": 453.0,
    "status": "resolved"
  },
  "replacement_anchor_id": "replacement-01",
  "review_timestamp_role": "search_hint"
}
```

The downstream edit builder consumes only `timeline_time_range`; it must never reinterpret a
replacement-local `start/end` as main-video time. Explicit handoff phrases such as “延长至原视频
09:42” close the replacement section and use `09:42` as a main-video timestamp. Multiple sections
are independent. If a declaration, anchor, local range, replacement duration, or handoff is
ambiguous or out of range, the compiler removes executable `start/end`, records
`timebase.status=unresolved_*`, and lists the item in `unresolved_timebase_item_ids`. Strict
acceptance fails closed until that item is resolved; no heuristic threshold or fixed timestamp is
allowed to promote it.

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

Checkpoint these public-runner phases in order:

1. `preflight`
2. `document_fetch`
3. `asset_download`
4. `input_compile`
5. `source_hash`
6. `source_asr`
7. `classification`
8. `reverse_asr`
9. `draft_write_validate`
10. `package_publish`

Write an atomic `job_state.json` checkpoint only after validating a phase's input/output digests and artifacts. A corrupt or mismatched checkpoint reruns only that phase; completed independent phases remain reusable. Missing source text recovery never cancels unrelated phases or draft generation.

## Pointer attachment ambiguity

Ordinary Lite editing never opens a pointer-profile gate or onboarding wait. Use the current row's
structurally associated attachment. When a hand/pointer row omitted its attachment, reuse the best
matching attachment from another row with the same compiler-normalized modification name. Candidate
groups are selected automatically from explicit recommendations, description/name cues, and safe
local image features; routine multiple candidates do not return `user_action_required`. Zero
candidates leaves the item label-only and reports the missing insertion. Never infer a candidate
from OCR, ASR, subject identity, or repository search.

## JSON Shape

```json
{
  "workflow_mode": "lite",
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
    "require_pointer_profile_binding": true,
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
- `workflow_mode=lite` selects the High School History compact workflow. It always keeps the source
  duration unchanged. Its executable
  `lite_cut_layout=split_gap` writes non-delete intervals to
  `Original Video` and `Separated Source Audio`, delete intervals to `Lite Cut Segments` and
  `Lite Reused Audio`, and simple downloaded assets to `Lite Visual Assets`. It keeps
  `Lite Timing Adjusted` empty. `lite_cut_layout=copy` is historical read/validation compatibility
  for older full-V1/A1 reference drafts and must be rejected for every new execution.
- Lite deletion does not compress the timeline. Except for uniquely ASR-proved spoken deletion,
  every duration-changing request is label-only. This includes pause, speed, hold, still-frame,
  `+Ns`, `-Ns`, and `semantic_pause_adjustment` requests. Its label uses a unique ASR point when
  available, otherwise the review-comment time. It creates no timeline duration change or offset.
- In lite mode, set `visual_plan.reuse_audio=false` for visual-only source reuse or still frames.
  Omitted `reuse_audio` defaults to true for source delete/timing copies and false for external
  visual assets.
- Lite review labels use source text verbatim, start at the authoritative item time, remain in the
  top safe band, and last `2s` unless clamped by the source-length project end. For every issue
  locatable through speech or audio, attempt the final ASR-resolved window or point first. A
  non-executing item uses the review timestamp when ASR cannot locate a unique point.
  When a timing comment contains both an original and destination clock, the first/original
  clock is the marker point for either direction; the destination is internal `target_time`
  evidence only.
  A non-speech point timestamp is valid without an end. Unresolved timing never maps to `0:00`.
- Lite mode does not weaken executable cut evidence. A spoken delete must carry a passing
  word/character `asr_alignment`
  receipt whose `resolved_cut_window` equals the edit start/end, together with `delete`, explicit
  `must_keep`, and `strategy`. The receipt must bind the source-audio SHA-256, provider plus
  model/resource identity, adapter version, ordered positive-duration matched word/character
  rows, and `authoritative_cut_boundary=true`. Candidate transcripts, prompted local ASR, or a
  rough-time match must keep `authoritative_cut_boundary=false` and cannot be written as a cut.
  Pause requests, pronunciation, breath, mouth noise, and other speech/audio timing must carry
  the equivalent authoritative source-ASR identity and a resolved point/window matching the
  label. A pause request has no saved edit in Lite.
- In Lite `split_gap`, `Separated Source Audio` contains the kept windows and `Lite Reused Audio`
  contains exactly one independent, audible source-aligned clip per logical ASR/V2 delete window.
  Overlapping windows may merge only within the same stable source item; adjacent windows from
  different items remain separate, and true cross-item overlap fails before draft writing for
  disambiguation. Reject pending or empty segmented plans, full-length/continuous/cross-item-merged
  or muted A2, extra clips, and mismatched source or source-aligned target ranges before writing.
  A supplied pointer row remains execution-required but uses default geometry and start-only
  acceptance. Cleanup-only pointer rows are label-only. Lite never enables binding, lifecycle,
  placement, hotspot, opened-editor, or full visual/animation acceptance gates.

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
- `execution_required`: `true` only for a closed-list Lite executor, currently a uniquely
  ASR-proved precise spoken deletion or an attributable supplied local visual insertion; new,
  unknown, and duration-changing non-delete requests default to `false`
- `evidence`: proof that the request was executed
- `validation`: proof that the output was checked

`execution_status` is optional internal state. In Lite,
`execution_status=label_only_unresolved` means the item's authoritative time is reliable but no
safe maintained implementation exists: set effective `execution_required=false`, create exactly
one original-text label, skip ad-hoc execution, and continue independent items. Persist this value
only in request/evidence metadata, marker receipts, validation output, and reports. Never insert it
into `source_text` or visible JianYing text. An unresolved authoritative time is not this status;
it fails before draft open/write only when neither ASR nor the review comment supplies a valid
time. An audio-identifiable non-executing item falls back to its review-comment time when ASR fails
or is non-unique; an executable spoken deletion still requires unique ASR evidence.

Evidence examples:

- audio deletion: `cut_window`, ASR source, strategy, deleted phrase, must-keep phrase, reverse-ASR `status`
- Lite pause request: non-executing source item with authoritative ASR point and one exact
  `source_text` marker receipt at a unique ASR point or review-time fallback; executable
  `semantic_pause_adjustment` and `pause_adjustments`
  evidence are forbidden
- pointer/hand/underline: overlay `track_name`, `segment_id`, `asset_path`, default geometry, start
  time, and automatic candidate-selection/reuse receipt when applicable
- animation timing: non-executing source item and one exact `source_text` marker receipt; overlay,
  shifted-segment, stable-frame, and release evidence are forbidden in Lite
- visual deletion: mask/overlay/patch segment ids or the exact local baked window

### Source-ledger marker identity and recovery

For a source-ledger marker, render `source_text` code-point-for-code-point. This includes timestamps, punctuation, whitespace, line breaks, and literal question marks. Never substitute a summary, prefix, ID, translation, or truncation unless those exact characters are already present in the source.

The visible marker value is exactly and only `source_text`. Keep `execution_status`,
`label_only_unresolved`, `verbatim_status`, IDs, warnings, and diagnostic notes outside that value;
they belong only to internal metadata, receipts, validation output, and reports.

Generate one marker per stable source item ID; multiple internal edits share that marker rather than creating more visible markers. Duplicate source item IDs are validation failures. Identical `source_text` under different IDs remains separate and produces one marker for each ID.

The latest `doc_items` document read is canonical for item identity, kind, and source text. If an internal request item is missing `source_text`, perform automatic document re-read/recovery before rendering markers. If the document cannot be read again, use the most complete unmodified candidate from the available row fields, set `verbatim_status=unverified_source_unavailable`, and preserve the item. This fallback must never stop editing or prevent draft generation. It must emit a warning and must not claim exactness.

Source recovery is deliberately different from final acceptance. Active editing continues with the warned fallback; strict final validation still enforces draft existence, editable structure, and all other required evidence gates. `verbatim_status=unverified_source_unavailable` is warning/review evidence, not a standalone final failure: delivery may pass when the chosen candidate is saved unchanged and all other gates pass, but the report must not describe that text as source-verified exact.

### Marker receipts and saved-draft validation

Marker tracks are actual dynamically allocated `Review Marker N` text tracks. Keep one receipt per source item with `item_id`, `source_text`, `verbatim_status`, `segment_id`, `material_id`, `track_name`, `start_time`, and `duration`; retain `execution_status` there when present without changing the visible text.

After saving, validate both the root `draft_content.json` and the active `Timelines/<main_timeline_id>/draft_content.json`. Each saved representation must preserve exact marker text, marker count, receipt mapping, and mapped timeline start. An explicit empty source ledger rejects extra markers left by older work; an empty ledger is not permission to accept unbound marker tracks.

### `acceptance`

Use `acceptance` to make the final gate explicit. For Feishu/Lark review documents, set:

- `expected_review_item_count`
- `expected_review_item_ids`
- `require_review_items: true`
- `require_execution_evidence: true`
- `require_audio_validation: true` when spoken deletion/audio repair exists
- In Lite, keep `require_visual_evidence: false` and `require_pointer_profile_binding: false`.
  Supplied visual execution is validated by the fixed Lite asset/start/default-geometry checks.
- Lite never enforces a pointer-profile receipt. Supplied visual insertion uses default geometry;
  cleanup-only pointer items remain label-only.
- In Lite, keep `require_pause_validation: false`; pause additions, extensions, shortenings, and
  adjustments are label-only. Strict Lite acceptance instead rejects any executable
  `pause_adjustments`, pause hold/still, duration change, or later-target offset.
- `require_final_acceptance: true` when the draft is being delivered as complete

Every acceptance profile runs the low-cost source coverage, execution evidence, draft existence,
editable structure, verbatim marker, and audio delivery gates. It conditionally routes expensive
audio precision/join, visual, pointer, and animation gates from the latest ledger kind and executed
operations. Lite pause requests route only to ASR-label and forbidden-output checks, never to an
executable pause gate. BGM replacement, level/loudness, and noise-only work skips reverse ASR
unless a true acceptance flag explicitly requires it.

An explicitly true conditional gate requires attributable evidence on an execution-required source
item. A false flag cannot disable a gate required by an allowlisted executed operation. New or
unknown types default to `execution_required=false` and one exact source-text label; an explicitly
malformed request that still declares an unknown type executable produces structured review rather
than being run. Strict final CLI validation fails when the named draft does not exist; source-text
recovery during active editing remains nonblocking.

`expected_review_item_count` and `expected_review_item_ids` may also come from a separate `--doc-items-json` file produced directly from the source document. This is the preferred way to catch items lost while translating the document into an edit request.

Final acceptance evidence should summarize source coverage, audio validation, visual evidence,
pause-label coverage and forbidden-pause-output checks, editable structure, marker traceability,
and unresolved review/fail rows. Use `auto-cut-final-acceptance` for this delivery gate.

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

For every final spoken-audio candidate, `processed_audio` must identify the complete candidate file and its reverse-ASR summary. In Lite this candidate is a source-time-preserving diagnostic probe, never a replacement or delivery asset. The summary must contain `candidate_audio_sha256`, a complete `asr_identity` (`provider`, model/resource ID, and `adapter_version`), and attributable spoken-delete rows with strategy, source-aligned logical cut windows, mapped join times, local transcript, explicit `delete_hits`, `keep_hits`, and semantic-join validation. Every row must match that source item's strategy, delete phrase, `must_keep` phrases, logical source-aligned windows, and join times derived from the segmented plan; truthy delete hits or unproven keep hits fail. Aggregate counts and generic item-level pass rows are invalid. Validation hashes and fully decodes the candidate bytes, including an FFmpeg full-stream decode for non-WAV media. Candidate duration must equal the source duration and cover the normalized segmented delivery timeline. Only a two-sided `min(previous.fade_out, next.fade_in)` at an exactly adjacent audible-segment boundary counts as declared crossfade overlap; an isolated fade never shortens the required duration.

The local transcript must contain alphanumeric content, and transcript aliases must agree after normalization. Cross-check each row's hit fields against its normalized local transcript: the transcript must not contain the delete phrase and must support every `must_keep` phrase. A retained same-word occurrence is valid only with exactly one positive `delete_hit` and structured `delete_hit_adjudication` fields `classification=kept_recurrence`, `occurrence_role`, `phrase`, `local_context`, `context_anchor`, and `reason`; the local context must occur in the transcript, the context anchor is not a substring of the delete phrase, and that anchor remains after removing the delete phrase from the local context. Multiple positive `delete_hits` or multiple local transcript delete occurrences, including overlapping occurrences, require per-hit adjudication and fail until one-to-many receipts are supported.

Every spoken-delete row is checked against a non-empty item contract containing strategy and delete plus an explicit `must_keep` field. Each positive `delete_hit` must match the item delete phrase. The candidate SHA-256 participates in the duration cache key so same-path media replacement cannot reuse stale timing.

The latest canonical doc item kind determines whether the spoken-delete contract applies. A pause
request is validated independently as an ASR-first, review-time-fallback label-only item and must
not create executable pause evidence. A forbidden semantic-join phrase is waived only by
`pass_adjudicated` with a
non-empty reason, `final_gap` between 0 and 0.2 seconds, and
`no_extra_deletion_contract=pass`.

For every segmented candidate, the summary must also contain
`audio_delivery_plan_sha256`, referred to by the request contract as
`processed_audio.audio_delivery_plan_sha256`. Produce it with the maintained
binding command after audio-plan compilation. Lite performs no pause alignment that mutates the
timeline; never obtain this value by
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

### ASR-bound Lite pause labels

For every request to add, extend, shorten, or otherwise adjust a pause, including input named
`semantic_pause_adjustment`, attempt to resolve the real adjacent utterance point with authoritative
word/character ASR. When ASR returns a unique point, bind the ASR path, current SHA-256,
provider/model or resource identity, adapter version, source-media identity, and ordered timing
rows. Utterance-only or transcript-only ASR is insufficient for an ASR-resolved point.

An ASR-resolved point must be inside the real adjacent-utterance gap, outside spoken words and
logical source-aligned delete windows, and attributable to the current source bytes. Place exactly one visible
label equal only to the item's `source_text` at that point. If ASR fails or cannot locate one unique
point, place the label at the time written in the review comment and record
`review_timestamp_fallback` evidence instead of guessing an ASR word.

Set effective `execution_required=false` for the pause request. Do not add it to executable edits
or `pause_adjustments`; do not split audio for a pause, create a hold or still-frame segment, add
an audio gap, change project duration, or offset any later track, asset, or label. Strict Lite
validation requires project duration to equal source duration and rejects every such prohibited
pause output. Fail before draft open/write only when neither ASR nor the review comment supplies a
valid time; never fall back to `0:00`.

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
4. Apply the Lite classification matrix: animation/timing, text movement/animation, cleanup-only
   pointer notes, `pause_delete`, `pause_timing_review`, and every pause addition/extension/
   shortening/adjustment use `execution_required=false`; supplied pointer/image insertions use
   `execution_required=true`; spoken deletion remains execution-required with ASR evidence.
5. If the user says materials must remain visible, keep the matching `preserve` fields set to `true`.
6. If the user needs second-stage manual editing, the resulting draft must keep inspectable cut structure and must not collapse into one full-length preview clip.
7. Do not delete a review-document item because a previous attempt mishandled it or its internal `source_text` is missing. Re-read the document automatically; if unavailable, preserve the most complete unmodified candidate with an unverified warning and continue draft generation.
8. In segmented audio mode, do not import a full merged QA narration, add unplanned audio segments, or use one narration segment that reaches the configured full-length ratio.
9. In Lite, merge overlapping delete windows only within one stable source item. Keep adjacent
   windows from different items as independent V2/A2 clips; reject true cross-item overlap before
   draft writing and require disambiguation.
10. A reliable-time item that cannot be executed safely may use internal
    `execution_status=label_only_unresolved` and one exact `source_text` label while other items
    continue. Do not improvise the edit or expose that status in the label.
11. Reject unreliable timing before draft writing. Attempt configured ASR for every
    audio-identifiable item. A uniquely located spoken deletion requires ASR and may execute;
    a non-executing item falls back to its review timestamp when ASR fails or is non-unique. Reject
    the item only when neither source supplies a valid time.
12. Reject `lite_cut_layout=copy` for new execution. It may be recognized only when reading or
    validating historical drafts.

## Marker vs Edit Semantics

- `edits` are executed operations. They must change the main editable timeline and should leave visible cut structure when `keep_cut_points=true`.
- `markers` are review items. They are independently traceable labels for manual verification or later adjustment and do not automatically require a main-track split.
- `修改` and `校对` remain semantic categories in the item ID/kind and execution evidence. Do not add either prefix to visible source-ledger marker text unless it is already present in `source_text`.
- A review-only item without a nearby cut is valid when its source-ledger marker remains independently traceable and its receipt identifies the item.
- `label_only_unresolved` describes internal execution state, not marker content. Its visible label
  remains exactly `source_text`, with no status prefix, suffix, or explanation.
- A visible label is not proof of a supplied visual insertion or spoken deletion. Strict
  validation must find their applicable execution evidence. Animation/timing, text-position, and
  cleanup-only pointer items and every pause adjustment request are intentionally label-only in
  Lite and require no execution proof. Any internal status remains in metadata/receipts/reports;
  the visible label still equals only `source_text`.
- For Feishu/Lark review documents, every source item must appear in `review_items` or a separate `doc_items` ledger. Missing items are validation failures even if the saved draft has enough marker segments.

## Installed Runtime Integrity

Before a non-mock packaged Lite execution, validate the deployment report, package-manifest anchor,
and managed runtime inventory. Any missing, changed, extra, or hash-mismatched managed file is
runtime drift. Stop before task execution and require redeployment from a verified package; do not
hot-patch the installed runtime, accept drift, or fall back to a source checkout or another
installation.

## Validation Commands

Run the complete public Lite source-document workflow:

```powershell
python scripts/jy_wrapper.py review-document-run `
  --snapshot-json path/to/document_snapshot.json `
  --project-json path/to/project.json `
  --job-root tmp/review-job `
  --drafts-root "<draft root>" `
  --package-zip "<output>\Auto-Cut-Lite.zip" `
  --json
```

Run that same command again after an interruption. Completed phases resume only when their input
digests and receipt-bound artifact/tree hashes still validate; a missing or changed artifact
reruns the affected phase and its dependents.

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

For a low-level complete Lite delivery from already compiled inputs, append `--workflow-mode
lite` and `--package-zip "<Desktop>\\<final-name>.zip"` to `revision-run`. The public
`review-document-run` command already fixes Lite mode and requires the package path. Both paths
keep the whole final stage offline:
it runs the saved-draft acceptance first, then packages the editable draft, the bundled material
relink tool, and `使用说明.txt`, validates the ZIP CRC and extracted tree hash, and returns only
after the ZIP and external receipt are published. It never opens JianYing or rewrites draft JSON.
If packaging fails, the draft may remain for diagnosis but the run is not a delivered lite job.

For dry-run or test environments without real media probing:

```bash
python scripts/jy_wrapper.py revision-run --request-json path/to/request.json --mock-media --json
```
