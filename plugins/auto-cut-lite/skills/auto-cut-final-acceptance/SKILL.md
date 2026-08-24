---
name: auto-cut-final-acceptance
description: Use when validating a JianYing/CapCut revision, final review version, complete project delivery, or migratable project across editable structure, source-item coverage, audio/visual changes, local materials, naming, portability, and target-open evidence.
---

# Auto-Cut Final Acceptance

## Packaged Runtime

Before executing this skill from the distributed plugin, read the [portable runtime contract](../auto-cut-lite/references/portable-runtime.md). Use the installed isolated runtime and `workflow_mode=lite`; do not fall back to a source checkout or another Auto-Cut installation.


## Overview

Use this packaged skill as the final gate before reporting a review-driven JianYing draft as done. It does not perform the edits itself; it checks that the saved draft and reports prove the edits were executed and remain inspectable.

Read [references/checklist.md](references/checklist.md) before running a real acceptance pass.

## When To Combine

Combine this skill with the smallest execution skills that created the draft:

- `auto-cut-revision-draft` for editable-project structure, source material, cut points, and markers
- `auto-cut-review-audio-precision` for spoken-word deletion, tail cleanup, reverse ASR, and pause timing
- `auto-cut-animation-timing-revision` for page turns, animation advance/speed-up, state overlays, and release boundaries
- `auto-cut-pointer-targeting` for the manual project binding, exact ready profile, hands, arrows, circles, magnifiers, underlines, and target placement
- `auto-cut-text-safezone-animation-revision` for flower text, safe-zone, sticker, and animation-in/out revisions

## Acceptance Contract

Do not claim a draft is complete from a marker count or a successful save alone. Require evidence at three levels:

- source coverage: every source review item is present in `review_items` or a separate `doc_items.json`
- execution evidence: every execution-required item has a real timeline edit, overlay, audio window, material segment, or documented local baked window
- output validation: the saved draft and processed audio/video reports pass the relevant checks

`修改` and `校对` remain distinct. A `校对` marker is valid only for review-only rows; if it asks to delete, add, retime, point, underline, replace, or remove anything, it needs execution evidence.

Final acceptance must also prove the draft was built from the intended source baseline. If the user asked to start from a clean source such as `PPT定稿+翻录`, fail drafts that carry unrelated markers, old deletion-review traces, or prior experimental overlays unless those are explicitly part of the new task.

Run saved-state gates independently on the root and every declared active timeline. Each variant must pass marker layout, pointer geometry, pointer layer identity/order, pointer media contract, and display-safe checks when routed; root/active equality or one passing copy is not a substitute for those checks.

## Required Checks

Run these checks when available:

```powershell
python scripts/draft_inspector.py summary --name "<DraftName>"
python scripts/jy_wrapper.py revision-validate --request-json "<request.json>" --doc-items-json "<doc_items.json>" --strict --json
```

For precise audio revisions, also require the reverse-ASR report from `auto-cut-review-audio-precision`. The report must identify the actual fully decoded candidate by SHA-256, name the ASR provider/model/adapter version, bind every segmented plan digest, and contain real result rows whose strategy, delete/must-keep phrases, cut windows, mapped joins, transcript, and hit evidence match each request item; aggregate counts and a generic per-item `pass` are insufficient. For visual revisions, require item-level visual evidence in `review_items[*].evidence`.

## Routed Gate Profile

Always-on cheap gates cover source coverage, execution evidence, draft existence, editable structure, verbatim markers, and audio delivery.

Conditional expensive gates cover audio precision/join, pause, visual, pointer, and animation evidence. Route them from the latest source-ledger kind and executed operation, then apply any explicit true requirements. Read the reference checklist for skip rules, attributable-evidence requirements, and nonblocking source recovery.

The opened-state gate is conditional. Ordinary revisions do not launch JianYing; require it only for pointer cover, opened-draft display drift, or another routed check whose saved JSON/self-render cannot prove the compositor result.

The execution modes are `offline`, `opened_verify`, and `export`. `offline` is the default revision mode; `opened_verify` and `export` are separate, explicit UI phases. Acceptance reports `ui_mode`, `opened_state_required`, `opened_state_reason`, `opened_state_status`, and `controller_calls`.

## Resumable Evidence Gate

Generate local evidence windows only for affected regions, including configured context before and after each edit. Produce one optional full preview only when explicitly requested by the user or required by the acceptance profile.

Local checks never replace global structure and timeline validation, saved root/active-timeline inspection, or the final full-candidate reverse ASR after spoken-audio changes. Cache hits must identify the matching artifact and candidate digest; they satisfy evidence only when that same required gate is actually evaluated.

Acceptance also inspects `job_state.json` and `job_timing.json` for incomplete phases, stale digests, retries, and unresolved item IDs. A legacy direct-script result cannot claim optimized/source-document final acceptance without equivalent evidence.

## Portable Lite Delivery

Apply this additional gate only when the user asks for a complete project, migratable project, formal delivery, or transfer to another computer. Intermediate drafts are exempt unless delivery was explicitly requested.

Run the editable-draft gates first, then create the offline ZIP with `revision-run --workflow-mode lite --strict --package-zip <output.zip> --json`. Require `completion_boundary=lite_zip_delivery`, `delivery.delivery_mode=lite_zip`, a passing ZIP CRC check, a matching extracted-tree SHA-256, `json_rewritten=false`, `ui_invoked=false`, and `opened_jianying=false`. The ZIP must contain the complete editable draft directory, localized materials, the relink tool, and `使用说明.txt`.

The external receipt remains outside the ZIP and binds the archive SHA-256, package-tree hash, extracted-tree hash, file counts, and byte size. Refuse overwrite, unsafe ZIP paths, reparse points, source changes during packaging, external material references, or any checksum mismatch. Do not continue into the full-workflow fixed-path native mirror after this lite delivery passes.

Cloud-only effects, fonts, and templates remain explicit non-file dependencies. `target_open_verified` is reserved for another physical computer that opened the delivered draft, left the loading state, previewed it, and confirmed that the timeline remained editable. A local inspection, receipt, UI self-check, or extraction simulation never qualifies.
`revision` is required for Auto-Cut full-workflow and review-driven deliveries. `standard` is only for an explicitly non-revision editable project and only relaxes the multi-cut and marker requirements. It still requires editable video/audio tracks, valid time ranges, material binding, root/active-timeline agreement, and the anti-flattening gate. Record `structure_policy`, `video_segment_count`, and `marker_segment_count` in the final report.

When a full-length `Final Video` or `Final Audio` preview exists, the visible primary video track supplies full-timeline coverage and real cut boundaries. A visible editable non-preview audio track independently supplies full-timeline coverage; the audio track need not expose real cut boundaries. Short decoy or hidden tracks do not satisfy editable structure. Under `revision`, revision markers have unique non-empty segment and material identities and non-empty visible text in both root and active timeline content.

Cloud-only effects, fonts, and templates remain explicit non-file dependencies and are not silently claimed to be present by a byte mirror. Use a trusted transfer channel for the external receipt and mirrored directory.

`target_open_verified` is reserved for another physical computer: it must open the mirrored draft at the same absolute path, left the loading state, previewed it, and confirmed that the timeline remained editable. A local inspection, a receipt, a UI self-check, or an import simulation never qualifies.

## Pass/Fail Rules

Fail the acceptance pass if any of these are true:

- the main video timeline for a multi-edit job collapsed into one full-length segment
- the draft uses only preview-style `Final Video` / `Final Audio` tracks as the deliverable structure
- source video, separated source audio, or replacement audio materials are missing when required
- a segmented-audio request imports any validation-only full narration into `materials.audios`, including the complete candidate declared by `processed_audio` or a byte-identical renamed/copy path even when it was omitted from the explicit validation-only list
- a segmented-audio request contains an unplanned audio segment or a narration segment at or above its configured full-length ratio
- any source review item is missing from the ledger
- an execution-required item has only a marker and no execution evidence
- reverse ASR has unresolved `fail` rows
- the full candidate is not fully decodable, lacks the segmented plan digest, or its duration does not cover the normalized segmented timeline after accounting only for exactly adjacent two-sided crossfades and decode tolerance; isolated fades never reduce the required duration
- a spoken-delete reverse-ASR row is only a generic status, lacks local transcript, source cut windows, mapped join times, explicit delete/keep hits, or passing semantic-join evidence, or those fields do not match the same item's request contract and plan-derived join mapping
- reverse ASR or processed-audio summary has non-empty `semantic_join_anomalies`, false `must_keep` hits without written adjudication, or local joined text containing broken fragments such as `阶段性。成就`, `发发明`, `它说明。`, or `夏朝。立的`
- a protected word was clipped or swallowed even if the delete phrase is gone
- the request includes a `timeline_plan` / final timing map but any visual overlay, replacement clip, pointer, animation overlay, visual delete, or marker keeps an explicit old timeline time without passing timeline-plan mapping evidence
- a pause was shortened near an animation, pointer, page turn, or key visual hold without a visual-aware guard decision
- a too-fast semantic join was repaired by widening a delete window, muting live speech, or flattening adjacent segments instead of a documented `semantic_pause_adjustment` no-audio still-frame hold
- a `semantic_pause_adjustment` lacks item id, source cut window, timeline start/end, duration, still-frame source, and semantic reason
- a semantic pause lacks a current source ASR path and source ASR SHA-256 plus identity equal to the immutable request configuration, current source audio/video hashes, exact alignment-audio hash, byte-identity proof for `preprocessing=none` or a hash-bound transformation receipt, does not preserve requested source time and resolved source time, does not resolve to the interior midpoint of a real utterance gap with the configured guard time from the nearest real word end/start, resolves on an ASR edge or inside a spoken word, has a mismatched frame source time, decoded seek time beyond one frame interval, or pixels that do not match source video, uses an audio delivery plan that was not compiled after pause alignment or whose audio delivery plan SHA-256 was not bound through the maintained report producer, duplicates one stable pause item ID, or lacks full-candidate reverse-ASR proof that the preceding sentence tail and following sentence onset remain complete
- utterance-only ASR is insufficient: each semantic pause requires real word or character timing, and its semantic pause edit and `pause_adjustments` entry must correspond one-to-one before draft generation
- a pointer or visual asset lacks target point, anchor/scale rule, segment id, or placement evidence
- any exact `Review Marker N` saved segment lacks a finite `clip.transform.y` in fixed band `0.7..0.9`; generated markers use centerline `y=0.8`, and root plus every declared active timeline are checked independently
- a course pointer request does not set `acceptance.require_pointer_profile_binding=true`, lacks `review_items[*].evidence.subject_profile_receipt`, lacks `project-bindings.json` evidence for the current `project_key`, stored `project_path`, and bound profile, or lacks a fresh exact `stage_id + subject_id` profile check through `auto-cut-pointer-targeting`; the result must be `status=ready`. The receipt's `project_key`, stored `project_path`, `profile_key`, `stage_id`, and `subject_id` must all match the same binding, and its `profile_path` must match that binding's fresh registry result
- `require_pointer_profile_binding=true` is required audit metadata on a correctly compiled pointer request; strict validation still derives the pointer gate from the item/operation and must reject every pointer item without a canonical packaged receipt when the flag is omitted or false, so the flag cannot disable this gate
- pointer evidence does not bind the current asset SHA-256 to the current asset bytes, or does not bind `scale_reference_path`, scale reference SHA-256, current-layout confirmed ratio, and approved preview evidence to current verified files
- saved pointer geometry does not resolve the receipt-bound unique track, segment, and material path; does not recompute `clip.scale`, `clip.transform`, decoded asset aspect ratio, anchor/hotspot, and landing error; or relies on a self-declared landing `pass`
- the receipt-bound hand is not a decodable PNG with an alpha channel, its decoded dimensions or alpha state differ from `media_contract`, or evidence does not bind `target_kind`, `selected_placement_policy_id`, `target_anchor`, and `gap_px` to the fresh placement policy; `text_bottom` also requires in-canvas `target_geometry` that recomputes the exact target point
- saved pointer identity checks find an additional, renamed, or differently bound pointer-like track, segment, or material; `underline_layers` or `clean_layers` is absent instead of an explicit array; or an overlapping layer lacks `render_index` or violates `pointer > underline > clean`
- a pointer or visual asset lacks in point, out point, duration rule, or disappearance reason
- detected pointer work is not lifecycle-validated because a request omitted or cleared an opt-in flag; `review_timestamp_role` is not `search_hint`; structured `boundary_control` is missing; `lifecycle_mode` is missing, outside the closed vocabulary, or lacks `source_pointer_window`; a pointer lifecycle uses a rough timestamp as an exact boundary; a speech/hybrid item self-declares `visual_only`; `start_alignment` is missing, late, arithmetically inconsistent, or not bound to `source_pointer_window.start`; first-character anchoring lacks a literal one-code-point `first_character_target` matching the speech phrase and saved target geometry; `source_pointer_handoff` is missing its pointer-window pair or overlaps a recorded source hand; a replacement/removal/cleanup mode omits recorded visibility/`clean_cover_window` wholesale, leaves a wrong-hand frame uncovered, or has `motion_preservation` not attributable to the exact saved `clean_layers` track/segment/material, decodable no-audio media bytes, source/media samples, mute state, and duration; its first/middle/last frames contain no real pixel change; its clean media duration differs from the receipt source window; its saved `source_timerange` does not start at zero and consume the complete local media window; its saved `target_timerange` start/end differs from the cleanup item's `timeline_window`; `tail_scan` ends before known recorded visibility or does not resolve to exactly one absent/newly-relevant outcome; or `timeline_window` disagrees with the saved pointer segment timerange
- a cleanup receipt omits `source_media_path`, `source_media_sha256`, `source_track_name`, `source_material_id`, or valid positive in-canvas `comparison_exclusion_regions`; the named track/material does not resolve to the actual saved source-video material and matching bytes; or mapped first/middle/last clean frames differ from the bound source frames outside those exclusion regions. Same-duration unrelated dynamic media is not source-synchronous cleanup
- a recorded source hand lacks a valid `residual_pointer_cover` decision: source material is not bound by `source_media_path` plus `source_media_sha256`; a passing `trajectory_receipt` with `method=source_clean_frame_sequence_v1`, the complete contiguous hash-bound source/clean frame sequence, and a recomputable hash-bound trajectory mask is missing; the recomputed bounds differ from `trajectory_bounds`; the `transparent_roi_still_cover` branch does not prove a static ROI with three or more bound `background_samples`, does not declare `region_shape=hard_edge_rectangle`, does not use exactly one rectangle in `opaque_regions`, lacks positive `safety_margin_px`, lacks alpha exactly 255 inside the rectangle, or does not keep alpha outside it zero; sampling reports `background not static` but the draft does not switch to a source-synchronous cleanup video; the added pointer remains independently editable is not proven by separate pointer and cover materials; non-`opened_jianying` pre-open `final_composite_samples` do not pass strict residual/pointer-region pixel comparison; or opened JianYing evidence does not contain exactly one `recorded_first_visible`/`recorded_midpoint`/`recorded_last_visible` row with distinct artifact paths and hashes, distinct hash-bound full-editor context through `editor_context_path`/`editor_context_sha256`/`editor_canvas_rect`, `visual_inspection.method=opened_jianying_canvas_manual_inspection_v2`, automatic template matching of exactly one saved editable pointer instance, the current timeline ID, and each sample's `timeline_time` mapped directly from that same sample's `source_time` through the saved source-video segments. Zero hands, two hands, one-pixel-modified or perceptually duplicated moving-pointer screenshots, self-declared only one hand, start-only, relabeled, or self-render-only evidence fails. JianYing preview interpolation exempts only the opened artifacts from pre-open background pixel comparison
- saved-state pointer-cover validation does not stream-decode every bound source frame by real PTS with proven half-open window boundaries; does not require background samples bound to trajectory clean-frame rows and every clean frame to match the hard-cover background; accepts ambiguous low-texture/multi-peak cover registration, skips the registered opened-canvas residual comparison, or accepts low-opacity recorded-hand remnants; does not enforce that the registration mask excludes the recorded-hand trajectory and editable pointer region before affine alignment; lacks positive rounded editor crop dimensions or a `windows_jianying_window_capture_v1` receipt bound to JianYingPro, the context hash, timeline ID, playhead time, and canvas rectangle; or fails to bind each detected pointer position to saved position keyframes using linear `curveType=Line` evaluation at that sample time. Nonlinear curves without a supported evaluator fail
- full-canvas template matching does not reject a second pointer outside the cover ROI; an external hash-bound capture record does not equal the inline capture receipt; no evaluable recorded-hand pixels is accepted instead of failing closed; background comparison does not reuse receipt source frames; or root and active timeline variants independently do not pass the same saved-state gates
- any clean frame lacks zero registered-hand instances in every clean frame under full-template matching or connected residual evidence; isolated glyph-color coincidence is treated as residual evidence; `trajectory_bounds` omits the source-template detection union; a declared recorded-pointer window yields zero source-frame template detections instead of failing closed with `pointer_cover.source_pointer_trajectory_unverifiable`; additional opened JianYing samples do not cover every trajectory extrema and significant direction changes within one decoded-frame interval; or `pointer_cover.clean_frame_recorded_pointer_present`, `pointer_cover.trajectory_bounds_miss_detected_pointer`, or `pointer_cover.opened_jianying_trajectory_samples_incomplete` remains unresolved
- an externally written new draft is treated as refreshed merely by returning to the draft home page. Returning to the draft home page is not a refresh: only when the target draft is not visible, protect unsaved work, perform one complete JianYing process exit and relaunch, and confirm the exact draft name and timeline ID. Once the target draft is open, do not exit it merely to refresh or repeat evidence capture
- an animation timing edit lacks edit mode, first visible/stable/release timing, overlay segment id, or collision check when applicable
- an animation timing edit lacks `target_scope`, `completion_policy`, `target_anchor_state`, `confidence`, or `scope_basis` with source text, a `screenshot_roi` field, and at least two labeled/timed `frame_events`
- a `dependency_group` is not `group_complete + stable_complete`, an `roi_target` is not `target_only` with a positive in-canvas ROI, or completion evidence violates `first_visible <= stable_frame <= release <= next_animation_start`
- animation completion evidence lacks `stable_sample_count>=2`, complete `required_elements_present`, empty `forbidden_future_elements_present`, or low-confidence `preview_receipts` containing distinct passing group/ROI previews with traceable locators
- a local crop was used for a course/PPT animation when a full-screen or whole-section method was available and no crop justification is recorded
- the user reported that preview/render was correct but the opened JianYing draft was wrong, and acceptance has no opened-draft calibration evidence or documented local display-safe baked window
- a pointer item is accepted only because keyframes or overlay segments exist, without evidence that the hotspot lands on the intended target in the saved/opened result
- a pointer item with `target_point` or `anchor` has only `planned_anchor_overlay` / `planned_editable_overlay` evidence and no rendered/opened hotspot landing proof, landing error, screenshot calibration, or display-safe baked-window evidence
- a local display-safe baked window exists but its decoded media does not match the current saved canvas, does not prove absence of an audio stream, lacks saved `volume=0`, scale 1, transform 0, or topmost order, is not contained in an overlapping editable pointer window, or lacks `opened_draft_drift_artifact_sha256`, `opened_draft_drift_timeline_id`, and `opened_draft_drift_window` that bind a real opened-draft drift artifact to the saved timeline and exact window
- a source-baseline request was ignored, such as reusing a previous draft after the user said to rebuild from the new source material
- a declared active timeline is missing or fails any full editable-structure/material/audio-delivery gate that the root draft passes

## Reporting

Report the result as an acceptance summary, not just "done":

- draft name and absolute path
- source review item count and coverage result
- audio delete/reverse-ASR result
- pause-fit result, including `semantic_pause_adjustment` and `visual_hold_review` rows
- visual/pointer/animation evidence summary
- any opened-draft calibration or local display-safe baked-window evidence
- editable structure summary: tracks, segments, source materials, replacement audio, markers
- independent saved-state results for root and every declared active timeline
- any unresolved `review`, `fail`, or low-confidence rows
- for complete-project delivery: ZIP path, external receipt path, archive/package/extracted-tree SHA-256 values, file and byte counts, no-UI/no-JSON-rewrite flags, local material coverage, non-file dependencies, and whether second-computer `target_open_verified` remains pending
