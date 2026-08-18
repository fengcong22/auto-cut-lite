---
name: auto-cut-pointer-targeting
description: "Use when adding or revising pointer-like visual assets in JianYing/剪映 editable course drafts, including hands, arrows, circles, magnifiers, highlight stickers, or any asset that must point to or call attention to a target region. Trigger for requests such as “加小手指向这里”, “手的大小不对”, “箭头指到老师标注的位置”, “按原视频参考大小”, “按修改意见提示这个知识点”, “位置还是不对”, “根据截图精准对准”, “入点有了但不知道停留多久/什么时候消失”, or when review comments mention pointer size, source-video reference sizing, pointing, highlighting, circling, marking, duration/out-point decisions, or visually guiding attention to text, map regions, table cells, timeline nodes, buttons, or screenshot annotations."
---

# Auto-Cut Pointer Targeting

## Overview

Use this repository-local skill to turn loose course-review comments into precise editable JianYing pointer overlays. A pointer is any visual asset with an anchor that should land on a target: a fingertip, arrow head, circle center, magnifier focus, highlight center, or custom marker hotspot.

This skill does not assume a fixed hand asset, fixed wording, or fixed course layout. It separates comment parsing, target localization, pointer-asset calibration, draft writing, and post-open screenshot calibration.

## Required Companion Skills

Load these when relevant:

- `auto-cut-subject-pointer-onboarding`: load for an explicit library create/update/supplement/view/bind/rebind operation, or when this skill opens the required same-task onboarding handoff after an ordinary pointer gate fails.
- `auto-cut-revision-draft`: always for review-driven editable draft delivery and traceability.
- `auto-cut-text-safezone-animation-revision`: when the user reports saved/opened draft position drift or asks “位置还是不对”.
- `auto-cut-review-audio-precision`: when pointer in/out timing depends on spoken-word alignment, subtitles, or review-document ASR evidence.
- `auto-cut-animation-timing-revision`: when the pointer may collide with page turns, animation entrances, state overlays, or other timed visual events.

## Manual Project Binding And Profile Gate

Every ordinary pointer job resolves the current project-family binding before targeting begins:

Use `project.project_key` from the current revision request when present. Otherwise call the repository's existing `infer_project_family` helper on the current draft name. If neither a current job nor draft family exists, ask for the project context; never derive `project_key` from the course subject or content.

```powershell
python skills/auto-cut-subject-pointer-onboarding/scripts/project_bindings.py get --project-key <project_key> --root <registry-root> --json
```

Read `data/subject-pointer-profiles.local/project-bindings.json`; never infer the subject from course content. The binding must provide the exact `project_key`, stored project_path, `stage_id + subject_id`, and `profile_key`. Then run a fresh registry `check` against `data/subject-pointer-profiles.local/` and accept the profile only when all of these conditions hold:

- its exact `stage_id + subject_id` and canonical `profile_key` match the current binding; the binding is the only subject identity source
- the fresh check returns `status=ready`
- its canonical `profile_key` equals `<stage_id>-<subject_id>` and the current registry profile's `key`
- its `profile_path` resolves to the fresh check's exact profile file under that stage-and-subject directory
- its registered asset path belongs to that profile and the asset SHA-256 matches the profile record, receipt, and current asset bytes
- for the `hand` role, the selected asset is a decodable PNG with an alpha channel and the receipt's derived `media_contract` (`format`, `has_alpha`, `width`, `height`) matches the fresh profile
- when the user names a requested pointer role such as `hand` or `arrow`, the selected asset's registered role matches it exactly; never substitute another registered role merely because the profile is ready
- its `scale_reference_path` belongs to that profile, its scale reference SHA-256 matches the profile record, receipt, and current file bytes, and it is the confirmed scale reference for the current layout
- its approved preview evidence path, approval state, and SHA-256 match the current profile record and file bytes
- the receipt carries the profile's matching `placement_policies` for the selected asset role and current layout plus the fresh role-wide `placement_policy_target_kinds`; stale, omitted, or self-authored policy evidence fails the fresh check

A missing binding, missing profile, incomplete, stale, cross-stage, cross-subject, or hash-mismatched input fails closed for placement. Stop before overlays or draft writing. 禁止跨学科、跨学段复用素材、热点或比例，即使文件名相同或用户只要求快速处理也不能绕过此门禁。

## Same-Task Onboarding Handoff

Read [references/handoff-contract.md](../auto-cut-subject-pointer-onboarding/references/handoff-contract.md) and open a same-task onboarding handoff to `auto-cut-subject-pointer-onboarding` before draft writes whenever the exact gate above cannot pass. This handoff continues the current request; do not end with a command for the user to start another task.

Read the shared [user-action-required contract](../auto-cut/references/user-action-required.md) for every `subject_identity`, `subject_evidence`, `preview_approval`, or `project_binding` blocker. Keep the complete blocking question visible in the originating Codex task. For a resumable job, the `review-job-wait-open` handler sends the optional notification internally only after durable persistence succeeds; do not send it again from this skill. Feishu replies, reactions, and messages do not approve or resume pointer work.

1. Preserve the original pointer and source-ledger item IDs, requested asset roles, current layout names, project key, absolute draft path, configured draft root, and original input digest.
2. Carry only stage or subject identity explicitly supplied by the user. Never infer either value from title, filename, OCR, ASR, course content, layout, or another profile. Ask only for the missing stage or subject.
3. For a resumable source-document job, persist the closed sidecar before any overlay write with `review-job-wait-open`; interactive work keeps the same handoff in the current task. No pointer draft write while waiting.
4. Let onboarding collect only missing evidence, require preview approval, and obtain explicit project-binding confirmation while this skill retains ownership of target localization, lifecycle timing, placement, and saved-state validation.
5. Preserve the pending original items through cancellation or insufficient evidence; a handoff marker is not evidence that their edits were executed.

On return, reload `project-bindings.json`, run a fresh registry `check` for the exact bound identity, recalculate the current asset, scale-reference, and approved-preview hashes, and verify the requested pointer role plus every current layout before target localization or draft writing resumes. No stale handoff receipt on return: never treat the handoff's earlier profile result as the ready receipt. Any failed return check opens a new handoff without modifying the pointer draft.

This gate selects the validated subject-owned asset, hotspot, and layout scale evidence. Keep target localization, per-item placement, duration/out-point calculation, screenshot calibration, and animation collision decisions in this skill; do not duplicate those calculations in onboarding.

For every course pointer revision request, set `acceptance.require_subject_pointer_binding=true`. Store the complete fresh result under `review_items[*].evidence.subject_profile_receipt`, including the absolute repository-local `registry_root`. Strict validation treats every pointer item as binding-required even if the flag is omitted or false, so the flag is an explicit contract record, not an opt-out. The validator reloads the canonical repository-local binding and profile; a self-declared `status=ready` value or alternate registry root is never sufficient.

## Workflow

Read [references/workflow.md](references/workflow.md) before executing a pointer-targeting job.

Use this shape:

1. Resolve the manual project binding and verify its fresh profile receipt against the gate above; if it fails, complete the same-task onboarding handoff and rerun this step.
2. Normalize every natural-language review comment into pointer tasks.
3. Extract the video frame at each task time.
4. Localize the target region from screenshots, annotations, OCR, visual matching, or semantic layout cues.
5. Select the receipt-bound pointer asset, hotspot, and current-layout scale reference.
6. Decide the pointer life window: in point, out point, duration rule, and disappearance reason.
7. Compute placement by aligning the pointer hotspot to the target point.
8. Write a new editable draft; do not overwrite a user-opened working draft.
9. Validate root `draft_content.json`, main `Timelines/<id>/draft_content.json`, and marker traceability.
10. If the user sends a screenshot showing drift, run screenshot calibration and rebuild or patch a new version.

## Pointer Task Contract

Normalize varied comments to this internal shape:

```json
{
  "time": "05:10",
  "action": "add_pointer",
  "target": "奥地利",
  "target_hint": "老师圈选区域 / 地图下方文字 / 右侧年份节点",
  "in_point": "05:10.0",
  "out_point": "auto: sentence_end | next_visual_change | next_pointer_target",
  "duration_rule": "hold through the relevant spoken/visual beat; do not use an arbitrary 3s default",
  "out_reason": "speech moved on / next animation starts / target leaves frame",
  "pointer_asset": "auto_or_path",
  "asset_role": "arrow | hand | circle | magnifier | highlight | custom",
  "placement": "anchor_to_target",
  "scale_rule": "ready_profile_current_layout_reference",
  "confidence": "high | medium | low",
  "needs_preview_confirmation": false
}
```

Do not require the user to use this exact wording. Infer the shape from comments like “这里加箭头”, “指到老师圈的位置”, “第三个知识点加提示”, “把这个地方圈出来”, or “位置不对，看截图”.

## Pointer Asset Contract

Read [references/adapters.md](references/adapters.md) when using a new pointer asset type, a new course layout, or any non-hand asset.

Every pointer asset must have a hotspot anchor:

```json
{
  "asset_path": "path/to/asset.png",
  "asset_role": "arrow",
  "anchor": [0.92, 0.50],
  "anchor_meaning": "arrow_head",
  "natural_direction": "right",
  "rotation_allowed": true,
  "scale_rule": "ready_profile_current_layout_reference"
}
```

Examples:

- hand: anchor is the fingertip
- arrow: anchor is the arrow head
- circle: anchor is the center or ring center
- magnifier: anchor is the lens focus
- highlight sticker: anchor is the visual center

Use only the hotspot and asset hash carried by the accepted binding-backed receipt. If either is absent or no longer matches the profile, stop and open the same-task onboarding handoff to request only that evidence; do not infer a hotspot from transparency, shape, or filename inside this skill.

Resolve placement only from the one receipt-bound `placement_policies` entry whose `asset_role`, `target_kind`, and `layout` match the current task. Record `target_kind`, `selected_placement_policy_id`, `target_anchor`, and `gap_px` in the item evidence. For `target_anchor=text_bottom`, also record `target_geometry` with numeric `x`, `y`, `width`, `height`, `canvas_width`, and `canvas_height`; derive `target_point` as the text rectangle's horizontal center and lower edge plus `gap_px`. The rectangle must be positive and fully inside the saved canvas. This policy is not a repository-global default and must not affect map, table, timeline, or another subject's placement. When there is no match, `placement_policy_status=not_required` is valid only with a nonempty reason and only when that target kind is absent from the fresh receipt's `placement_policy_target_kinds`.

## Duration And Out Point Rules

When the teacher gives only an in point, do not use a fixed arbitrary duration. Decide how long the pointer stays by reading the visual and spoken context.

Choose the out point from the earliest safe boundary:

- the sentence or explanation beat that names the target ends
- the target stops being relevant, leaves the frame, or is replaced by a new board state
- the next independent animation, page turn, or pointer target begins
- the pointer would cover new information that the viewer needs to read
- the source segment ends

Use conservative visibility bounds when there is no stronger evidence:

- static hand/arrow: keep visible for at least 1.2 to 1.5 seconds, usually 2.0 to 3.0 seconds
- circle/highlight/magnifier: hold until the target reading beat ends or the visual state changes
- continuous narration-following pointer: use one segment with position keyframes and one final out point, not many short flashing segments
- do not exceed the current explanation beat or about 5 seconds without explicit evidence

If ASR, subtitles, or the review document can locate the spoken target, align the out point to the end of that spoken beat plus a small 0.2 to 0.4 second reading buffer. If only visuals are available, use the next visual change or next review item as the boundary and mark confidence accordingly.

## Pointer Lifecycle Evidence Gate

A review timestamp is a `search hint`, not an exact pointer boundary. Record `review_timestamp_role=search_hint`, a structured `boundary_control` (`speech`, `visual`, or `hybrid`), and one closed-vocabulary `lifecycle_mode`: `editable_pointer`, `visual_only_pointer`, `handoff_to_recorded_pointer`, `replace_recorded_pointer_then_handoff`, `cleanup_recorded_pointer`, `remove_recorded_pointer_until_absent`, or `remove_recorded_pointer_until_relevant`. Every mode requires `source_pointer_window`. Derive the saved in/out window from speech alignment and source-frame inspection, then record the evidence below for every pointer add, replacement, or disappearance item. Strict validation derives this gate from detected pointer work; an omitted or false request flag cannot disable it.

- `speech_anchor`: the aligned phrase and finite source start/end. Use `status=visual_only` with a nonempty reason only when no speech/ASR rule controls the boundary; a speech-led item cannot self-declare this exemption.
- `start_alignment`: `speech_start`, `pointer_first_visible`, `alignment_error_seconds`, `max_early_seconds`, and `max_late_seconds`. A passing row recomputes `alignment_error_seconds = pointer_first_visible - speech_start`, binds `pointer_first_visible` to `source_pointer_window.start`, and stays inside both tolerances; a pointer that appears after the allowed late tolerance fails.
- `first_character_target`: required when `anchor_rule` says the hotspot lands on the first spoken target character. Record `status=pass`, exactly one literal `character`, and its positive in-canvas glyph `geometry`; the character must equal the first glyph of `speech_anchor.phrase`, and its geometry must equal the target geometry used by saved hotspot placement. A phrase-level box or phrase center is not first-character evidence.
- `source_pointer_window`: the exact source-time window occupied by the replacement/editable pointer.
- `source_pointer_handoff`: when a source-recorded hand enters, record its first visible frame and prove the replacement releases no later than that boundary. Handoff and `source_pointer_window` are a required pair.
- `recorded_pointer_visibility` plus `clean_cover_window`: when `lifecycle_mode` replaces, removes, or cleans a recorded source pointer, both fields are mandatory. Record its first/last visible frames and prove the clean coverage contains all of them. Add a passing `motion_preservation` receipt with `mode=source_synchronous_motion_preserving`, `clean_media_path`, matching `clean_media_sha256`, the same source window, and at least two source/media time samples with equal offsets. Bind the receipt to the exact saved source video with `source_media_path`, `source_media_sha256`, `source_track_name`, and `source_material_id`. Record `comparison_exclusion_regions` as positive, fully in-canvas rectangles covering only the repaired pointer area. The cleanup is source-synchronous. Saved-state validation samples the first/middle/last frames and requires at least one pair with real pixel change, so a static full-screen frame cannot masquerade as motion-preserving cleanup.
- `residual_pointer_cover`: when the recorded hand is baked into source pixels, inspect the complete recorded-pointer trajectory before choosing the cleanup representation. Bind `source_media_path` and `source_media_sha256` to the saved source material. Require a passing `trajectory_receipt` with `method=source_clean_frame_sequence_v1`, a contiguous hash-bound source/clean frame sequence for the complete window, and a hash-bound trajectory mask whose recomputed bounds equal `trajectory_bounds`; three arbitrary stills or a declared undersized trajectory do not pass. If three or more `background_samples` from that bound sequence prove the complete `opaque_regions` background is static, use `mode=transparent_roi_still_cover` with `region_shape=hard_edge_rectangle`: require exactly one rectangular `opaque_regions` entry, bind it to `trajectory_bounds` plus a positive `safety_margin_px`, create a full-canvas RGBA photo with alpha exactly 255 at every pixel inside that rectangle and alpha outside it equal to zero, and never use a sparse rectangle union, feathered edge, or transparent hole. If sampling reports `background not static`, reject the still branch and use a source-synchronous cleanup video. In both branches the added pointer remains independently editable as its own PNG segment and keyframes. `final_composite_samples` has two distinct evidence classes: non-`opened_jianying` pre-open composites keep strict residual/pointer-region pixel comparison against their hash-bound expected backgrounds, while opened JianYing evidence contains exactly one sample for each of `recorded_first_visible`, `recorded_midpoint`, and `recorded_last_visible`. Those three samples use distinct artifact paths and hashes, bind distinct full-editor screenshots through `editor_context_path`, `editor_context_sha256`, and `editor_canvas_rect`, and carry `visual_inspection.method=opened_jianying_canvas_manual_inspection_v2`. Saved-state validation performs automatic template matching against the bound editable pointer material and requires exactly one instance; zero hands, two hands, one-pixel-modified clones, perceptually duplicated moving-pointer frames, or a self-declared only one hand fail. Each sample's `timeline_time` must equal the mapping of that same sample's `source_time` through the saved source-video segments, not merely sit near a separately mapped role time. Editor scaling/interpolation exempts only those opened artifacts from pre-open background pixel comparison; a start-only, duplicated-artifact, relabeled, or self-render-only pass fails.
- Saved-state validation stream-decodes every bound source frame by real PTS with proven half-open window boundaries, requires background samples bound to trajectory clean-frame rows and every clean frame to match the hard-cover background within tolerance, rejects ambiguous low-texture/multi-peak cover registration, and compares the registered opened-canvas residual against source/clean evidence including low-opacity recorded-hand remnants. The registration mask excludes the recorded-hand trajectory and editable pointer region before affine alignment, so neither hand can steer the transform away from the clean background. Validation also checks positive rounded editor crop dimensions, validates a `windows_jianying_window_capture_v1` receipt bound to JianYingPro, the context hash, timeline ID, playhead time, and canvas rectangle, and binds each detected pointer position to saved position keyframes using linear `curveType=Line` evaluation at that sample time. Nonlinear pointer curves require a supported evaluator before acceptance.
- Full-canvas template matching rejects any second pointer outside the cover ROI. An external hash-bound capture record must equal the inline capture receipt. No evaluable recorded-hand pixels fails closed, background comparison reuses receipt source frames, and root and active timeline variants independently run the same saved-state gates.
- Clean-frame and trajectory sampling is a structural gate. Require zero registered-hand instances in every clean frame using full-template matching or connected residual evidence; isolated glyph-color coincidence is not residual evidence. Build `trajectory_bounds` from the source-template detection union, not only source-minus-clean deltas. A declared recorded-pointer window must yield at least one source-frame template detection; zero detections fail closed with `pointer_cover.source_pointer_trajectory_unverifiable` instead of skipping trajectory sampling. Require additional opened JianYing samples at every trajectory extrema and significant direction changes within one decoded-frame interval. Fail with `pointer_cover.clean_frame_recorded_pointer_present`, `pointer_cover.trajectory_bounds_miss_detected_pointer`, or `pointer_cover.opened_jianying_trajectory_samples_incomplete` when any part is missing.
- For an externally written new draft, returning to the draft home page is not a refresh. Only when the target draft is not visible, protect unsaved work, perform one complete JianYing process exit and relaunch, then confirm the exact draft name and timeline ID before capture. Once the target draft is open, do not exit it merely to refresh or repeat evidence capture.
- `tail_scan`: continue past the teacher-marked end and record `scan_end`, `last_pointer_visible`, and exactly one of `pointer_absent_after` or `pointer_relevant_after` with a nonempty relevance reason. The scan must reach the inspected pointer/clean-window end, and its last-visible/release values cannot precede `recorded_pointer_visibility.last_visible`. Release only after the last useless frame, or exactly when a new purposeful source-pointer action begins.

Run `pointer_lifecycle_evidence_problems(evidence)` before saved geometry and layer checks. A marker, rough timestamp, planned window, or teacher-supplied duration cannot substitute for this evidence.

For editable pointer segments, record `timeline_window` after source-to-timeline mapping. Saved geometry acceptance compares that window to the resolved saved segment `target_timerange`; aligned source evidence paired with a late saved overlay is a hard failure.

For every `motion_preservation` receipt, list exactly one saved cleanup segment in `clean_layers`. Saved-state acceptance resolves that track/segment/material, requires its path and SHA-256 to match the receipt, decodes the first/middle/last frames, rejects frozen media and audio-bearing media, requires saved `volume=0`, and matches the clean media duration to the receipt source window. The saved `source_timerange` must start at zero and consume that complete media window, so a short clip cannot be stretched or looped to impersonate source-synchronous cleanup. It also requires the saved segment `target_timerange` start/end to equal the cleanup item's top-level `timeline_window`; matching duration alone cannot hide an offset segment. Resolve `source_track_name` and `source_material_id` to the saved source-video segment, verify its material path and SHA-256 against `source_media_path` and `source_media_sha256`, map the clean first/middle/last sample times back to that source, and compare those frame pairs outside `comparison_exclusion_regions`. Unrelated dynamic media fails even when its duration and motion checks pass.

## Target Localization Priority

Use the highest-confidence available source:

1. User/teacher screenshot annotation or visible correction markup.
2. Text/OCR target match inside the frame.
3. Layout adapter: map, table, timeline, PPT text block, UI control, chart, or diagram.
4. Visual feature matching from a reference screenshot.
5. Manual confirmation preview when confidence is low.

Prefer a semantically correct target over a geometric center. For example, “法国” in a map should point to the country label or teacher-highlighted France region, not a nearby unrelated city label.

## Saved-State Validation Gate

Planning coordinates and a self-declared landing `status=pass` are not acceptance authority. After saving both root and active timeline content, call `pointer_saved_geometry_problems(evidence, receipt, content)` from `scripts/utils/pointer_validation.py` and require an empty problem list. The gate reads the evidence `track_name` and `segment_id`, then verifies:

- saved `clip.scale.x/y` both equal the receipt's current-layout `visible_height_ratio`
- the receipt `visible_width_ratio` equals the width derived from canvas size, decoded asset aspect ratio, and `visible_height_ratio`
- saved `clip.transform.x/y` recompute the registered normalized anchor onto `target_point`
- a `text_bottom` placement recomputes `target_point` from the in-canvas `target_geometry` and receipt-bound `gap_px`
- the recomputed Euclidean landing error is within evidence `max_landing_error_px`, defaulting to 2 pixels
- the saved segment exposes a numeric `render_index` and its material path is the receipt-bound asset

Across all pointer items, call `pointer_saved_layer_problems(evidences, content)`. Items sharing one bound asset role use one editable pointer track and one material. Reject every undocumented extra pointer material, segment, or track that reuses the registered pointer material ID, path, or SHA-256, plus any differently bound pointer-like hand/display-safe track detected by its saved track or material role; renaming is not a bypass. The `underline_layers` and `clean_layers` inventories are required even when empty. Each nonempty entry records `track_name` and `segment_id`, resolves to exactly one saved segment, and exposes a render index; saved order is `pointer > underline > clean`.

A local `display_safe_baked_window` is allowed only after reading the saved video and proving the real canvas dimensions, absence of an audio stream, saved `volume=0`, scale 1, transform 0, containment within an overlapping editable pointer window, and topmost render index. It must bind the real opened-draft drift artifact with `opened_draft_drift_artifact_path`, `opened_draft_drift_artifact_sha256`, `opened_draft_drift_timeline_id`, and `opened_draft_drift_window`; the artifact payload, saved timeline, segment, and exact window must agree.

## Draft Rules

Always deliver a JianYing editable project:

- keep source video visible
- keep pointer assets as separate editable overlay segments
- choose the pointer size from the accepted receipt's scale reference for the current layout; record the project key, stored project path, profile key, asset SHA-256, layout, reference id/path, and width/height ratios in evidence
- do not use the old default rule "hand size = target text height * 2", any target-relative default, or a centered/fixed scale. If the current layout has no matching scale reference, stop and open the same-task onboarding handoff to request only that layout evidence before writing overlays.
- when the comment says the hand should follow the speech, use one continuous pointer overlay with position keyframes so the fingertip moves smoothly with the spoken targets; do not simulate this with multiple static pointer segments unless the pointer truly disappears and reappears
- when replacing a wrong built-in hand, cover the old hand with a clean same-frame patch until the correct source state returns, then switch back to the original picture
- keep one marker or trace label per pointer task unless the user asks for no markers
- for every pointer task, write `review_items[*].evidence` with the overlay track name, segment id, asset path or sticker id, target point, anchor rule, placement method, in point, out point, duration rule, out reason, and the validated `subject_profile_receipt`
- for every pointer task with a `target_point` or `anchor`, also write rendered/opened landing proof: `hotspot_landing.status=pass`, `landing_error_px` within the documented tolerance, screenshot calibration evidence, or a documented display-safe baked window. `planned_anchor_overlay` and `planned_editable_overlay` are planning evidence only and must fail final acceptance by themselves
- treat that proof as supporting evidence only; saved geometry and layer checks above remain mandatory and may reject a self-declared pass
- choose pointer/material size only from the receipt-bound current-layout evidence. A new screenshot correction becomes reusable only after the user explicitly updates the named library and confirms it.
- if the asset covers important visual information or feels too dominant at the target, revise scale and record the before/after scale reason instead of leaving it as a subjective placement
- do not introduce unrelated review markers from previous drafts
- do not flatten into `Final Video` / `Final Audio`
- when patching an existing generated draft, patch both root and main timeline copies
- when a draft has become runtime-encoded or has stale UI cache risk, rebuild a new clearly named draft
- if a self-rendered preview is correct but the opened JianYing draft still shows wrong pointer size or position, treat it as an opened-draft display drift problem, not as another ordinary target-location guess
- when opened-draft display drift cannot be made reliable with editable pointer coordinates, use a documented local baked display-safe window from the verified render: no audio, 1920x1080, scale 1, transform 0, topmost track, exact short pointer window only, with the editable source and pointer evidence preserved underneath

Marker-only pointer delivery is invalid. If the request says to add, delete, replace, or reposition a hand, arrow, underline, circle, magnifier, or highlight, the saved draft must contain a real editable overlay segment or a documented local baked window. Run the revision strict gate before reporting completion:

```powershell
python scripts/jy_wrapper.py revision-validate --request-json "<request.json>" --doc-items-json "<doc_items.json>" --strict --json
```

## Reporting

Report:

- draft name and absolute path
- pointer asset type and scale rule
- hotspot anchor and landing proof, including landing error or screenshot/render/opened-draft evidence
- in point, out point, and disappearance reason for each pointer
- target localization method for each pointer
- whether screenshot calibration was used
- whether any local baked display-safe window was used because the opened JianYing display differed from the verified render
- marker or trace track name
- any low-confidence items that need visual review
