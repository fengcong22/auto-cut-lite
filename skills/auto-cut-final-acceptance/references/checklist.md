# Final Acceptance Checklist

Run this checklist before reporting a JianYing revision draft as complete.

## Resumable Evidence

- `job_state.json` has no incomplete, corrupt, or stale required phase; `job_timing.json` accounts for retries, cache decisions, item IDs, digests, and unresolved items.
- Local evidence windows include configured before/after context and remain tied to affected item IDs.
- At most one full preview exists, and only because the user or acceptance profile required it.
- Local evidence did not replace global editable-structure, root/active-timeline, marker, or final full-candidate reverse-ASR checks.
- Cache reuse points to the identical validated artifact and never changes a required gate to skipped.

## Routed Gate Profile

- Always run the cheap `source_coverage`, `execution_evidence`, `draft_exists`, `editable_structure`, `verbatim_markers`, and `audio_delivery` gates.
- Route expensive `audio_precision`, `audio_join`, `pause_fit`, `visual`, `pointer`, and `animation` gates only when detected item kinds/operations or explicit requirements need them.
- BGM, level, or noise changes skip reverse ASR unless explicitly required.
- Explicit `true` requires attributable evidence on at least one execution-required source item.
- `false` cannot disable a gate required by detected work, including pointer binding requirements.
- The opened-state gate is conditional. Ordinary revisions do not launch JianYing; route it only for pointer cover, opened-draft display drift, or another compositor-sensitive requirement.
- Execution modes are `offline`, `opened_verify`, and `export`. `offline` is the default; the other two are separate explicit UI phases. Record `ui_mode`, `opened_state_required`, `opened_state_reason`, `opened_state_status`, and `controller_calls`.
- Unknown execution types become structured `review` or `fail` results; they never disappear into a skipped conditional gate.
- Strict final CLI validation fails when the draft is missing.
- Active source-text recovery remains nonblocking: warn with `verbatim_status=unverified_source_unavailable`, keep the item and draft generation active, and do not claim exactness.

## Source Coverage

- The source review document was read through the proper document tooling, not browser scraping.
- Every source item exists in `review_items` or `doc_items.json`.
- `expected_review_item_count` and `expected_review_item_ids` came from the source document.
- The latest `doc_items` read is canonical, including an explicit empty ledger.
- Every visible source-ledger marker equals `source_text` code-point-for-code-point and has one receipt for its stable item ID.
- Duplicate source item IDs fail; identical marker text under different IDs remains separately receipt-bound.
- Root and active timeline draft content pass exact marker text/count/receipt/mapped-start checks; an explicit empty ledger rejects extra old markers.
- Every exact `Review Marker N` saved segment has finite `clip.transform.y` in fixed band `0.7..0.9`; the renderer centerline is `y=0.8`, and root plus every declared active timeline are checked independently.
- `修改` items have execution evidence.
- `校对` items are marker-only only when the item is truly review-only.
- The working draft was built from the requested source baseline. If the user asked to pull fresh video from `PPT定稿+翻录`, the accepted draft must not inherit unrelated old markers, deletion comments, or experimental overlays from an earlier draft.

## Editable Structure

- `draft_inspector.py summary` shows a multi-edit project is not flattened.
- Source video material is present.
- Separated original audio is present when the workflow used replacement narration.
- Replacement audio material is present and on its own track when used.
- Segmented delivery keeps the full QA narration outside the material bin and matches every planned source/replacement/repair segment one-to-one; the `processed_audio` complete candidate and byte-identical aliases are validation-only automatically.
- Segmented delivery has no unplanned audio segment and no narration segment at or above the configured full-length ratio.
- Main video track keeps visible segment boundaries.
- Muted original audio reference track keeps corresponding segment boundaries.
- Review markers are independently traceable and not merged into one generic label.

## Audio Precision

- Every spoken-delete item has `delete`, `must_keep`, `strategy`, cut window, ASR source, and reverse validation status.
- The full-candidate reverse-ASR report hashes to the actual fully decoded candidate audio, records provider/model/adapter identity, binds the normalized plan SHA-256 for every segmented candidate, and contains at least one real result row with an explicit status; aggregate counts alone are invalid.
- Candidate duration covers the normalized segmented delivery timeline, allowing only exactly adjacent two-sided crossfades and a small decode tolerance; isolated fades add no allowance.
- Every spoken-delete row includes attributable local transcript, strategy, source cut windows, mapped join times, explicit `delete_hits`, `keep_hits`, and passing semantic-join validation that match the same item's request contract and plan-derived mapping; a generic item-level pass is invalid.
- The local transcript must contain alphanumeric content, transcript aliases must agree after normalization, and the transcript must prove every `must_keep` phrase. Every retained occurrence has one positive `delete_hit` and exactly one `delete_hit_adjudications` receipt with `classification=kept_recurrence`, the correct `occurrence_role`, `phrase`, independent `local_context`, `context_anchor`, `reason`, and an exact `hit` object containing `phrase`, `text`, `start`, `end`, and `match_method`. Each receipt matches one reported hit exactly, uses an allowed item-contract delete phrase, and lies outside every target cut window. Missing, duplicate, tampered, overlapping, or unreported occurrences fail. A single legacy `delete_hit_adjudication` is accepted only for exactly one positive hit. A colored-span row uses its ordered item-contract `delete_phrases`, not its aggregate display phrase, and protects uncolored inter-span ASR text through automatic `must_keep`.
- Every spoken-delete row is checked against a non-empty item contract containing strategy and delete plus an explicit `must_keep` field. Each positive `delete_hit` must match the item delete phrase. The candidate SHA-256 participates in the duration cache key so same-path media replacement cannot reuse stale timing.
- The latest canonical doc item kind determines whether the spoken contract applies. Spoken-delete and semantic-pause evidence are validated independently, even under the same item ID. A forbidden semantic-join phrase is waived only by `pass_adjudicated` with a non-empty reason, `final_gap` between 0 and 0.2 seconds, and `no_extra_deletion_contract=pass`.
- Reverse ASR has zero unresolved `fail` rows.
- Same-word recurrence is adjudicated only with local context.
- Semantic-join scan has zero unresolved rows. Local joined text must not contain residual fragments, duplicated characters, dangling predicates, or broken phrase continuity such as `阶段性。成就`, `发发明`, `禅让制。于`, `它说明。`, or `夏朝。立的`.
- Any false `must_keep` hit is either fixed or explicitly adjudicated with nearby audible evidence; delete-absent alone is not enough.
- Tail residue is handled by a precise physical cut or a documented exact duck/mute cleanup.
- Protected boundary words before and after the cut remain audible.
- Automatic repair ran at most once on a deep copy; its raw result passed scope before normalization/preflight, its prepared result passed scope again, it changed no unrelated item and did not widen the physical delete window; an invalid repair save restored the pre-repair draft, while a second media failure remains explicitly unresolved with the valid draft path and item IDs reported.
- The repair callback must not mutate the live project and must not write saved draft files directly; it must return a scoped `RevisionRequest` for the two scope checks.
- Pause-fit validation is separate from delete validation.
- `semantic_pause_adjustment` rows for too-fast joins record item id, source cut window, timeline start/end, duration, still-frame source or frame path, semantic reason, and protected-word status.
- Each semantic pause binds a current source ASR path and source ASR SHA-256 plus ASR identity equal to the immutable request configuration, current source audio SHA-256 and source video SHA-256, exact alignment-audio hash, and either byte identity for `preprocessing=none` or a receipt binding transformed hashes/tool/version/parameters; preserves requested source time and resolved source time; resolves outside every spoken word to the utterance-gap midpoint with positive recomputable guard time from the nearest real word end/start; records frame source time at that midpoint, proves successful decode within one frame interval, and confirms the frame matches source video at pixel level; uses an audio delivery plan compiled after pause alignment and bound through the maintained producer by audio delivery plan SHA-256; and has full-candidate reverse-ASR proof that the preceding sentence tail and following sentence onset remain complete.
- Utterance-only ASR is insufficient: each semantic pause requires real word or character timing, and its semantic pause edit and `pause_adjustments` entry must correspond one-to-one before draft generation.
- One stable source item ID maps to at most one semantic pause until one-to-many pause receipts are supported.
- `visual_hold_review` rows name the conflicting visual event and source-time guard window.

## Visual Evidence

- Pointer, hand, arrow, underline, circle, magnifier, and highlight requests have real editable overlay segments or documented local baked windows.
- Pointer evidence includes segment id, track name, target point, anchor rule, scale rule, placement method, in point, out point, duration rule, and out reason.
- Pointer evidence proves the hotspot landed on the target in a rendered/opened frame, screenshot calibration, landing-error measurement, or documented display-safe baked window. Planned coordinates alone are not acceptance evidence.
- The checklist must reject pointer evidence unless `acceptance.require_subject_pointer_binding=true`, `review_items[*].evidence.subject_profile_receipt` is present, `project-bindings.json` contains the current `project_key`, stored `project_path`, and bound `profile_key`, and a fresh exact `stage_id + subject_id` profile check returns `status=ready`. The receipt's `project_key`, stored `project_path`, `profile_key`, `stage_id`, and `subject_id` must all match the same binding, and its `profile_path` must match that binding's fresh registry result. The flag is required audit metadata; omitted or false metadata cannot disable the strict gate inferred from pointer work.
- Receipt evidence binds the current asset SHA-256 and hotspot to that profile, and binds `scale_reference_path`, scale reference SHA-256, current-layout confirmed ratio, and approved preview evidence to current verified files.
- Saved pointer geometry resolves one receipt-bound track, segment, and material path; recomputes `clip.scale`, `clip.transform`, decoded asset aspect ratio, anchor/hotspot, and landing error; and never accepts a self-declared landing `pass` as authority.
- The current hand material is a decodable PNG with an alpha channel, and decoded width, height, format, and alpha state equal its `media_contract`.
- Evidence binds `target_kind`, `selected_placement_policy_id`, `target_anchor`, and `gap_px` to the fresh placement policy, or supplies the validator's permitted `not_required` proof. A `text_bottom` policy also carries in-canvas `target_geometry` and recomputes the target point from its lower edge plus the gap.
- Material ID, material path, SHA-256, and pointer-like saved-role checks reject additional, renamed, or differently bound pointer materials, tracks, and segments.
- `underline_layers` and `clean_layers` are explicit arrays even when empty. Every referenced overlapping pointer, underline, and clean segment has `render_index`, with saved order `pointer > underline > clean`.
- Screenshot calibration was used when the user reported drift or supplied a correction screenshot.
- If render/preview is correct but the opened JianYing draft is wrong, acceptance includes opened-draft calibration evidence or a documented local display-safe baked window.
- Pointer keyframes or overlay segments alone are not enough; the hotspot must be proven to land on the intended text/region in the saved/opened result.
- Local display-safe baked media decodes to the current saved canvas (1920x1080 for this project), proves absence of an audio stream, has saved `volume=0`, scale 1, transform 0, and topmost order, and stays within an overlapping editable pointer window. `opened_draft_drift_artifact_sha256`, `opened_draft_drift_timeline_id`, and `opened_draft_drift_window` bind the real opened-draft drift artifact bytes to the same saved timeline and exact window while editable evidence remains underneath.
- Pointer disappearance was checked against speech, subtitles, next visual change, next pointer target, and page/animation boundaries.
- Every pointer item records `review_timestamp_role=search_hint`, structured `boundary_control`, a closed-vocabulary `lifecycle_mode`, and `source_pointer_window`; strict validation derives the lifecycle gate from pointer work, so omitted or false opt-in metadata cannot skip it.
- Speech/hybrid pointer items have a complete `speech_anchor` and bounded start-alignment receipt tied to the source pointer-window start; only visual boundary control can use `visual_only`. First-character rules bind exactly one literal first glyph and the same geometry used by saved placement rather than a phrase center.
- Wrong source-hand replacement records the required `recorded_pointer_visibility` plus `clean_cover_window` pair, and its clean window contains every visible wrong-hand frame.
- Tail inspection continues beyond the review timestamp and proves either `pointer_absent_after` the last useless frame or `pointer_relevant_after` with a reason for the next purposeful action.
- Clean replacement media carries a passing `motion_preservation` receipt bound to its path, SHA-256, source window, and source/media time samples, and is source-synchronous. Its exact saved `clean_layers` segment resolves to the same material bytes, samples the first/middle/last frames with at least one real pixel-changing pair, has no audio, has `volume=0`, and matches clean media duration to the source window. Its saved `source_timerange` starts at zero and consumes the complete local media window; stretching, looping, or selecting another slice fails. Its saved `target_timerange` start/end also equals the cleanup item's top-level `timeline_window`; matching duration alone does not pass. A static full-screen frame does not pass when it freezes PPT motion or animation.
- The cleanup receipt binds `source_media_path`, `source_media_sha256`, `source_track_name`, and `source_material_id` to the actual saved source-video material. `comparison_exclusion_regions` contains only positive fully in-canvas pointer-repair rectangles. First/middle/last clean frames map back to the same source times and match the bound source frames outside those regions; an unrelated same-duration dynamic clip fails.
- A recorded source hand has a `residual_pointer_cover` decision for the complete trajectory. It binds `source_media_path` and `source_media_sha256` to the saved source material and includes `trajectory_receipt.method=source_clean_frame_sequence_v1`, every contiguous hash-bound source/clean frame in the window, and a hash-bound trajectory mask whose recomputed bounds equal `trajectory_bounds`. The `transparent_roi_still_cover` branch has at least three bound static `background_samples`, `region_shape=hard_edge_rectangle`, exactly one rectangle in `opaque_regions`, positive `safety_margin_px`, alpha exactly 255 throughout that rectangle, and alpha outside it zero; sparse rectangle unions, arbitrary stills, invented undersized trajectories, or feathered masks fail. If sampling reports `background not static`, the draft uses a source-synchronous cleanup video. In both branches the added pointer remains independently editable as a separate material and keyframed segment. Non-`opened_jianying` pre-open `final_composite_samples` rows pass strict residual/pointer-region pixel comparison. Opened JianYing evidence has exactly one `recorded_first_visible`, `recorded_midpoint`, and `recorded_last_visible` row; all three use distinct artifact paths and hashes, bind distinct full-editor screenshots through `editor_context_path`, `editor_context_sha256`, and `editor_canvas_rect`, use `visual_inspection.method=opened_jianying_canvas_manual_inspection_v2`, decode to the saved canvas, and match the current timeline ID. Automatic template matching requires exactly one saved editable pointer instance, so zero hands, two hands, one-pixel-modified clones, perceptually duplicated moving-pointer screenshots, or a self-declared only one hand fail. Each row's `timeline_time` equals the mapping of that same row's `source_time` through the saved source-video segments. Editor interpolation exempts only those opened artifacts from pre-open background pixel comparison; start-only, relabeled, duplicated-artifact, or self-render-only acceptance fails.
- Saved-state validation stream-decodes every bound source frame by real PTS with proven half-open window boundaries, requires background samples bound to trajectory clean-frame rows and every clean frame to match the hard-cover background within tolerance, rejects ambiguous low-texture/multi-peak cover registration, and compares the registered opened-canvas residual against source/clean evidence including low-opacity recorded-hand remnants. The registration mask excludes the recorded-hand trajectory and editable pointer region before affine alignment, so neither hand can steer the transform away from the clean background. Validation also checks positive rounded editor crop dimensions, validates a `windows_jianying_window_capture_v1` receipt bound to JianYingPro, the context hash, timeline ID, playhead time, and canvas rectangle, and binds each detected pointer position to saved position keyframes using linear `curveType=Line` evaluation at that sample time. Nonlinear pointer curves require a supported evaluator before acceptance.
- Full-canvas template matching rejects any second pointer outside the cover ROI. An external hash-bound capture record equals the inline capture receipt. No evaluable recorded-hand pixels fails closed, background comparison reuses receipt source frames, and root and active timeline variants independently run the same saved-state gates.
- Every clean frame proves zero registered-hand instances in every clean frame through full-template matching or connected residual evidence; isolated glyph-color coincidence is not residual evidence. `trajectory_bounds` contains the source-template detection union. A declared recorded-pointer window yields at least one source-frame template detection; zero detections fail closed with `pointer_cover.source_pointer_trajectory_unverifiable`. Additional opened JianYing samples cover every trajectory extrema and significant direction changes within one decoded-frame interval. No `pointer_cover.clean_frame_recorded_pointer_present`, `pointer_cover.trajectory_bounds_miss_detected_pointer`, or `pointer_cover.opened_jianying_trajectory_samples_incomplete` failure remains.
- For an externally written new draft, returning to the draft home page is not a refresh. Only when the target draft is not visible, protect unsaved work, perform one complete JianYing process exit and relaunch, then confirm the exact draft name and timeline ID. Once the target draft is open, do not exit it merely to refresh or repeat evidence capture.
- Saved pointer `timeline_window` equals the resolved editable segment timerange; source-aligned evidence cannot hide a late saved overlay.
- If a final `timeline_plan` or timing map exists, every visual overlay, replacement video segment, animation overlay, visual delete, pointer, and marker with explicit timeline time has passing timeline-plan mapping evidence. Old pre-audio-compression times are a hard failure.
- Root and main timeline JSON agree after patching.
- Root and every declared active timeline independently pass marker layout, pointer geometry, pointer layer, media contract, and display-safe saved-state gates; equality alone is not acceptance evidence.

## Animation Timing

- Each animation timing request has an edit mode: full-screen video advance, state overlay, page-turn hold, shifted segment, local baked window, or review-only with frame evidence.
- Each executed item has `target_scope`, `completion_policy`, `target_anchor_state`, `confidence`, and `scope_basis` with source text, a `screenshot_roi` field, and at least two labeled/timed `frame_events`.
- `dependency_group` uses `group_complete + stable_complete`; `roi_target` uses `target_only` and a positive in-canvas ROI.
- Completion order is `first_visible <= stable_frame <= release <= next_animation_start` when the next boundary is known; the receipt has `stable_sample_count>=2`, covers every `required_elements_present`, and has empty `forbidden_future_elements_present`.
- Low-confidence items have distinct passing group/ROI `preview_receipts`, each with a traceable preview, artifact, or segment locator.
- Boundary-sensitive items record `first_visible`, `stable_frame`, `release`, and `next_animation_start` when available.
- State overlays are full-screen 1920x1080 with scale 1 and transform 0 unless a local effect is explicitly documented.
- Full-screen video overlays are muted.
- Release does not collide with the original entrance or cover the next independent animation.
- Whole-section advances document frame-level alignment, future-content leakage checks, audio/pointer impact, and why they are better than local crop.
- Local crop overlays have an explicit justification and screenshot/frame comparison proving size and position match the source.

## Fixed-Path Native Delivery

- Run this section for a complete/migratable project, formal delivery, full Auto-Cut delivery, or a request to move the project to another computer. Intermediate drafts are exempt unless delivery was explicitly requested.
- Ordinary editable-draft gates pass first: the main timeline stays segmented and editable, editable timeline source video and source audio plus separated/replacement audio remain visible, markers remain independently traceable, and all local materials used by the draft are present under `Resources/local` or `Resources/audioAlg` before material references are written.
- Read JianYing `currentCustomDraftPath` from `JianyingPro/User Data/Config/globalSetting`. On first use, run `draft-root-check` with the source path; `configured_target_path_missing`, ambiguity, a non-directory, a reparse point, or any mismatch fails closed.
- For a missing recipient path, show exactly `请在对方电脑创建与源电脑相同的剪映草稿路径：<path>` and wait for the recipient to create the same fixed absolute path. Never silently select a conventional default.
- Deliver only through the byte-preserving mirror to the Desktop transfer folder; do not open JianYing, invoke UI automation, rewrite draft JSON, or use a package/import workflow.
- `capture_draft_tree_receipt` inventories the complete source tree and target tree, including empty directories. The external receipt is outside both draft trees and records `schema_version`, `source_tree_sha256`, `target_tree_sha256`, `tree_sha256`, `file_count`, `directory_count`, `byte_size`, and the job-input digest.
- Refuse overwrite and a destination that already exists (`destination already exists`), reparse points, source/target overlap, source changes during copy, or any tree SHA-256 mismatch. Source and target tree records must match byte-for-byte.
- Require a continuous source-stability gate before copying and after the temporary sibling is verified. The complete receipt must remain unchanged for the configured quiet window; post-copy or promotion-time mutation deletes only the generated copy and retries from a fresh stable receipt. Timeout or retry exhaustion leaves no final target and no misleading success receipt. The receipt records `stability_policy`, observation counts/timestamps or elapsed seconds, `source_stable_before_copy=true`, `source_stable_after_copy=true`, `source_stable_at_promotion=true`, and the actual `retry_count`.
- The receipt must record the flags `json_rewritten` and `ui_invoked` as false (`json_rewritten=false`, `ui_invoked=false`), plus `native_editor_invoked=false` and `portable_package_invoked=false`; source and mirrored `draft_content.json` bytes remain identical.
- Keep `portable_project_tool` package/import code only as historical/internal code. `portable_static_ready` and `imported_static_ready` are not routed acceptance states and must not appear in final-delivery evidence.
- Cloud-only effects, fonts, and templates remain explicit `non_file_dependencies`; the mirror receipt cannot claim them as local files. Use a trusted transfer channel for the external receipt and mirrored directory.
- `target_open_verified` remains pending until another physical computer opens the mirrored draft at the same absolute path, left the loading state, previewed it, and confirmed the timeline remained editable. Local inspection or import simulation does not qualify.

## Final Report

- Include draft name and absolute path.
- Include independent saved-state results for root and every declared active timeline.
- Include validation command outputs or summarized counts.
- Include adjusted pause count, inserted semantic-pause count, and skipped visual-context count.
- Include unresolved review/fail/low-confidence items, if any.
- Do not claim completion when strict validation or reverse-ASR validation fails.
