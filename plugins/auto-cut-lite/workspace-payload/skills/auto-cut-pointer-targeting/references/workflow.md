# Pointer Targeting Workflow

Use this workflow for adding or revising pointer-like overlays in editable JianYing course drafts.

## 1. Normalize Review Comments

Convert loose comments into pointer tasks. Accept varied wording:

- “05:10 指一下奥地利”
- “箭头指到老师圈的位置”
- “这里加提示”
- “第三个知识点用手指点出来”
- “右侧 1866 年那里要标注”
- “位置还是不对，看截图”

Extract when available:

- time or time range
- target text or semantic target
- pointer asset request or inferred role
- target hint such as screenshot mark, map region, table cell, button, or timeline node
- in point, explicit duration, out point, or words like “停留到/消失在/指完就走”
- priority and confidence

If no exact time is present, infer from nearby screenshot, current draft marker, source comment context, spoken-word alignment, or ask only when the target cannot be found safely.

## 2. Choose Source Baseline

Use the latest valid editable draft or the user-specified source video/draft. Do not reuse a previous experimental draft if the user requested a clean source such as “PPT定稿+翻录”.

Reject or rebuild from source when:

- old deletion/revision markers are present and unrelated
- the draft is runtime-encoded and cannot be safely patched
- source media changed
- the visible opened draft differs from the JSON coordinates

## 3. Extract Frames

For each task, extract a frame at the target time and optionally nearby frames. Use the frame where the target text or annotation is most visible.

Keep debug artifacts under `tmp/`.

For duration-sensitive tasks, extract a short contact sheet around the in point:

- start at least 1 second before the requested in point
- continue until the next review item, next obvious visual change, or 5 seconds after the in point
- sample densely enough to see page turns, text entrances, and pointer collisions

## 4. Localize Target

Use this priority order:

1. Teacher/user screenshot annotations.
2. OCR or visible target text.
3. Layout adapter.
4. Image feature matching.
5. Preview confirmation.

Target points should land on the intended semantic point:

- hand/arrow: point to the text, region center, button center, or annotation center
- circle/highlight: center on the whole target area
- magnifier: center the lens over the target

When the user says “老师截图中添加了标注”, use the annotation as stronger evidence than generic object detection.

If the review comment is vague, combine the comment text with the frame content before choosing the target. Examples:

- “指到这个位置” plus a teacher circle means use the circle center or marked edge, not the nearest OCR match.
- “指到奥地利” on a map means locate the visible country/label in the relevant frame, not a fixed coordinate from another lesson.
- “第三个知识点” means count the visible bullet/label structure in the current frame, then choose the semantic item.
- “这里有相应文字” means OCR and layout grouping should confirm the annotation target.

## 5. Calibrate Pointer Asset

Resolve the manual project binding first:

```powershell
python workspace-payload/skills/auto-cut-profile-onboarding/scripts/project_bindings.py get --project-key <project_key> --root <registry-root> --json
```

The result must match `project-bindings.json` and provide the current `project_key`, stored `project_path`, and bound `profile_key`. Do not infer a subject from the review document. Then start asset calibration with a fresh registry `check` for the binding's exact `stage_id + subject_id`. Validate the receipt against the current registry files and continue only when all of these conditions hold:

- the fresh result is `status=ready`, and `profile_key` and `profile_path` resolve to the exact stage-and-subject profile
- the receipt's `asset_path`, asset SHA-256, role, and hotspot match the current profile and current asset bytes
- `scale_reference_path`, scale reference SHA-256, layout, and confirmed current-layout ratio match the current profile and current file bytes
- approved preview evidence, approval state, path, and hash match the current profile and current file bytes

If the project is unbound or any profile, asset, hotspot, scale-reference, layout, ratio, preview, path, role, or hash evidence is missing or invalid, stop before overlays and draft writing, then open the same-task onboarding handoff defined in `auto-cut-profile-onboarding/references/handoff-contract.md`. Preserve the original pointer and source-ledger item IDs, requested role, current layout names, and project context. Never infer stage or subject from the review document, OCR, ASR, filename, title, layout, or media; ask only for the missing identity. Direct library requests still use the explicit create/update command, but an ordinary pointer gate failure continues through onboarding in this same task.

For a resumable source-document job, call `review-job-wait-open` before draft writes and leave all pointer-dependent phases pending. After explicit binding confirmation, reload the binding, rerun a fresh registry `check`, recalculate the asset, scale-reference, and preview hashes, and verify the requested role and current layout before resuming calibration. No stale handoff receipt on return: the earlier handoff result is never a ready receipt. If any return check fails, open a new handoff and do not write the overlay.

Apply the receipt's confirmed current-layout width and height ratios to the current canvas. The hotspot is the only point that must land on the target; align by the asset center only when the receipt defines a center-based hotspot. Set `acceptance.require_pointer_profile_binding=true`, then record the absolute `registry_root`, `project_key`, stored `project_path`, `profile_key`, `asset_path`, asset SHA-256, hotspot, `scale_reference_path`, scale reference SHA-256, layout, ratios, and approved preview evidence in `review_items[*].evidence.subject_profile_receipt`.

When the selected policy uses `target_anchor=text_bottom`, record an in-canvas `target_geometry` rectangle (`x`, `y`, `width`, `height`, `canvas_width`, `canvas_height`) and derive the target point from its horizontal center and lower edge plus the receipt-bound gap. Do not hand-author a target point that disagrees with this geometry.

When the review asks the pointer to follow narration, keep one continuous pointer segment and add position keyframes so the hotspot moves smoothly between spoken targets. Only split into multiple pointer segments when the pointer intentionally disappears and reappears. If the pointer covers relevant content, revise its target-side orientation or placement while preserving the receipt ratio; if the scale evidence itself needs correction, ask the user to explicitly update the named library before rebuilding.

## 6. Decide Duration And Disappearance

If the reviewer gives an explicit out point, use it after checking it does not collide with a new visual state. If the reviewer gives only an in point, infer a safe out point instead of defaulting to a fixed 3 seconds.

Treat every review timestamp as a `search hint` and record `review_timestamp_role=search_hint`. Set structured `boundary_control` to `speech`, `visual`, or `hybrid`, and select `lifecycle_mode` only from the closed list in the main skill; semantic aliases such as `replace_source_hand` are invalid. Every mode records `source_pointer_window`. For a speech/hybrid pointer, record a passing `speech_anchor` and `start_alignment`; the latter contains `speech_start`, `pointer_first_visible`, the recomputed signed error, and early/late tolerances, and its first-visible time equals `source_pointer_window.start`. `visual_only` is valid only for `boundary_control=visual`. If the hotspot must begin on the first spoken glyph, add `first_character_target` with exactly that literal first character and the same in-canvas glyph geometry used for saved hotspot placement instead of targeting the whole phrase box.

Inspect the source hand before and after the requested range. Record the editable `source_pointer_window`; use `source_pointer_handoff` to release before a recorded source hand enters, and require both fields together. A replacement/removal/cleanup `lifecycle_mode` requires both `recorded_pointer_visibility` and a containing `clean_cover_window`; omission of the whole pair is invalid. Add `motion_preservation.status=pass`, `mode=source_synchronous_motion_preserving`, `clean_media_path`, `clean_media_sha256`, matching source window, and at least two source/media samples whose time offsets agree. Bind the receipt to the exact source video with `source_media_path`, `source_media_sha256`, `source_track_name`, and `source_material_id`. Record `comparison_exclusion_regions` as positive in-canvas rectangles restricted to the pointer repair area. Put the one actual saved cleanup track/segment in `clean_layers`; acceptance resolves its material path/hash, samples the first/middle/last frames for real pixel change, rejects audio, checks `volume=0`, and matches clean media duration to the source window. The saved `source_timerange` starts at zero and consumes the complete local media window; do not stretch, loop, or take a different slice. Map the same first/middle/last clean sample times to source-video frames and require the pixels outside `comparison_exclusion_regions` to remain consistent with that bound source. Generate a source-synchronous clean window so slide motion and animation continue; a static full-screen replacement frame or unrelated same-duration dynamic clip is not valid cleanup for a moving PPT.

For a recorded hand baked into source pixels, also write `residual_pointer_cover` evidence after inspecting the complete hand trajectory. Bind `source_media_path` and `source_media_sha256` to the saved source material, then write `trajectory_receipt.method=source_clean_frame_sequence_v1` with every contiguous hash-bound source/clean frame in the cover window plus a hash-bound trajectory mask. Recompute that mask and require its bounds to equal `trajectory_bounds`; arbitrary stills or an invented small box fail. If at least three bound `background_samples` show that the complete `opaque_regions` background is static, use `mode=transparent_roi_still_cover` with `region_shape=hard_edge_rectangle`: store exactly one rectangle in `opaque_regions`, require a positive `safety_margin_px`, create a full-canvas RGBA photo with alpha exactly 255 throughout that rectangle and alpha outside it zero, place it above Original Video and below the added pointer, and keep the pointer PNG/keyframes separate. Sparse rectangle unions, feathered edges, irregular contour masks, and transparent holes are invalid because preview interpolation can reveal the source hand. If sampling reports `background not static`, reject the still cover and use a source-synchronous cleanup video. In both branches the added pointer remains independently editable. Record two `final_composite_samples` classes: non-`opened_jianying` pre-open composites retain strict hash-bound pixel comparison in the residual region with the editable pointer region excluded; opened JianYing evidence contains exactly one sample for each of `recorded_first_visible`, `recorded_midpoint`, and `recorded_last_visible`. The three opened rows use distinct artifact paths and hashes and bind distinct full-editor screenshots through `editor_context_path`, `editor_context_sha256`, and `editor_canvas_rect`. Each row uses `visual_inspection.method=opened_jianying_canvas_manual_inspection_v2`; saved-state validation performs automatic template matching against the bound pointer material and requires exactly one instance, so zero hands, two hands, one-pixel-modified clones, or a self-declared only one hand fail. Bind all three to the current saved `timeline_id`, and require each row's `timeline_time` to equal the mapping of that same row's `source_time` through the saved source-video segments. Do not compare opened artifacts pixel-for-pixel with the pre-open background; a start-only, duplicated-artifact, relabeled, or self-render-only result cannot pass.

Saved-state validation stream-decodes every bound source frame by real PTS with proven half-open window boundaries, requires background samples bound to trajectory clean-frame rows and every clean frame to match the hard-cover background within tolerance, rejects ambiguous low-texture/multi-peak cover registration, and compares the registered opened-canvas residual against source/clean evidence including low-opacity recorded-hand remnants. The registration mask excludes the recorded-hand trajectory and editable pointer region before affine alignment, so neither hand can steer the transform away from the clean background. Validation also checks positive rounded editor crop dimensions, validates a `windows_jianying_window_capture_v1` receipt bound to JianYingPro, the context hash, timeline ID, playhead time, and canvas rectangle, and binds each detected pointer position to saved position keyframes using linear `curveType=Line` evaluation at that sample time. Nonlinear pointer curves require a supported evaluator before acceptance.

Full-canvas template matching rejects any second pointer outside the cover ROI. An external hash-bound capture record must equal the inline capture receipt. No evaluable recorded-hand pixels fails closed, background comparison reuses receipt source frames, and root and active timeline variants independently run the same saved-state gates.

Clean-frame and trajectory sampling is a structural gate. Require zero registered-hand instances in every clean frame using full-template matching or connected residual evidence; isolated glyph-color coincidence is not residual evidence. Build `trajectory_bounds` from the source-template detection union, not only source-minus-clean deltas. A declared recorded-pointer window must yield at least one source-frame template detection; zero detections fail closed with `pointer_cover.source_pointer_trajectory_unverifiable` instead of skipping trajectory sampling. Capture additional opened JianYing samples at every trajectory extrema and significant direction changes within one decoded-frame interval. Treat `pointer_cover.clean_frame_recorded_pointer_present`, `pointer_cover.trajectory_bounds_miss_detected_pointer`, and `pointer_cover.opened_jianying_trajectory_samples_incomplete` as blocking failures.

For an externally written new draft, returning to the draft home page is not a refresh. Only when the target draft is not visible, protect unsaved work, perform one complete JianYing process exit and relaunch, then confirm the exact draft name and timeline ID before capture. Once the target draft is open, do not exit it merely to refresh or repeat evidence capture.

Finish with a frame-level `tail_scan` beyond the marked end and through the inspected pointer/clean-window end. Its last-visible/release evidence must not precede the recorded visibility end. Record exactly one of `pointer_absent_after` or `pointer_relevant_after` with the reason the later source hand is now purposeful. The edit ends after useless tail frames disappear, not merely when the teacher's marked range ends.

Map the accepted source window to `timeline_window`, then compare it with the saved pointer segment timerange. For cleanup media, compare both saved `target_timerange` start/end with that top-level `timeline_window`; equal duration is insufficient when the whole segment is shifted. Source alignment does not pass when the editable pointer or cleanup segment itself starts late.

Decision priority:

1. Speech or subtitle beat: if ASR/subtitles mention the target, hold through the spoken target phrase or explanation sentence plus 0.2 to 0.4 seconds.
2. Visual beat: if the pointer explains a map label, table cell, button, or diagram node, hold until the viewer has enough time to read it and before the next independent visual change.
3. Next event boundary: release before page turns, animation entrances, new pointer targets, text reveals, or teacher cursor actions that would make the pointer stale.
4. Minimum visibility: keep a static hand/arrow visible for at least 1.2 to 1.5 seconds unless the clip itself is shorter.
5. Maximum no-evidence hold: avoid holding beyond about 5 seconds without a spoken, visual, or review-comment reason.

For narration-following pointer motion, keep one overlay segment from the first target to the last target and use position keyframes. Add the out point after the last spoken/visual target beat. Do not create a strobing series of short pointer clips unless the source intent is separate appear/disappear moments.

Record `duration_rule`, `out_reason`, and all applicable lifecycle fields above in evidence. Run `pointer_lifecycle_evidence_problems(evidence)` before presenting the item as final. If the out point is uncertain, mark the item `medium` or `low` confidence and generate a preview instead of presenting it as final.

## 7. Compute Placement

For JianYing visual clips, transform coordinates are ratios in the saved JSON. For image overlays created by this repository, verify whether wrapper arguments are pixel offsets or already-normalized values before patching.

Practical rule from this repository:

- saved `clip.transform.x` is horizontal offset in half-canvas units
- saved `clip.transform.y` is vertical offset in half-canvas units
- when correcting from a screenshot delta in frame pixels, use `dx / 960` and `-dy / 540` for a 1920x1080 canvas

Always validate by reading the saved draft, not only the in-memory plan.

## 8. Screenshot Calibration

Use screenshot calibration when the user reports “位置还是不对” or sends a screenshot from JianYing.

Process:

1. Detect the current pointer anchor in the user screenshot.
2. Pick the intended target point from annotation, OCR, or semantic target.
3. Map screenshot delta to frame delta.
4. Patch a new version or rebuild from the clean source.
5. Sync root and main timeline copies.

Do not silently overwrite the draft the user is currently inspecting.

When possible, measure both the displayed pointer anchor and intended target in the same screenshot space, then convert only the delta. This avoids re-solving the whole placement problem from a potentially scaled JianYing preview.

For a 1920x1080 canvas:

```text
delta_transform_x = delta_pixels_x / 960
delta_transform_y = -delta_pixels_y / 540
```

Use the pointer hotspot, not the asset center, for the measured current point.

## 9. Display-Safe Local Bake Fallback

Use this only after the normal editable pointer path has been tried and the verified render is correct, but the opened JianYing draft still displays the pointer with the wrong size or position.

Trigger conditions:

- the pointer target, anchor, size, and timing are already verified in a rendered preview or frame export
- JianYing's opened draft display disagrees with that verified render
- another small coordinate nudge would be based on the self-rendered preview rather than opened-draft evidence

Fallback method:

1. Generate the shortest no-audio 1920x1080 snippet from the verified render for the affected pointer window.
2. Place it on the topmost video track with scale 1 and transform 0.
3. Keep the original source video, editable pointer track, markers, and evidence underneath for audit and second-stage editing.
4. Limit the baked window to the exact pointer display range; never use it to flatten the full draft.
5. Record the window path, time range, reason, and the fact that the fallback was caused by opened-draft display drift.
6. Bind the real drift artifact with `opened_draft_drift_artifact_sha256`, `opened_draft_drift_timeline_id`, and `opened_draft_drift_window`; require its saved timeline, segment, and exact time range to match the fallback.

Do not use this fallback to hide unresolved target selection, wrong OCR matching, wrong hand anchor, or wrong timing. It is only for representation drift after the visual result itself is known correct.

## 10. Validate

Before reporting completion:

- list tracks and segments
- confirm source video is still present
- confirm pointer assets are separate overlay segments
- if a local baked display-safe window was used, confirm it is full-screen, muted/no-audio, scale 1, transform 0, topmost, and limited to the shortest affected pointer window
- confirm pointer scale and anchor evidence exist for every pointer task
- confirm every `text_bottom` task carries valid `target_geometry`
- confirm `underline_layers` and `clean_layers` are explicit arrays even when empty
- confirm pointer in/out/duration evidence exists for every pointer task
- confirm the pointer does not cover a later independent animation, page turn, subtitle, or new target
- confirm marker/trace segments match pointer task count
- confirm no unrelated old markers such as deletion/proofreading notes
- confirm no `Final Video` / `Final Audio`
- confirm root and `Timelines/<main_timeline_id>/draft_content.json` agree

Report any low-confidence target item honestly.
