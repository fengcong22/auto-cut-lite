# Auto-Cut Lite Execution Contract

This is the single authoritative visual execution contract for the packaged Auto-Cut Lite
workspace. It overrides conflicting instructions in every router, focused skill, checklist,
example, or user-selected subskill whenever `workflow_mode=lite`.

## Classification Matrix

| Review request | Lite behavior | `execution_required` | Acceptance |
| --- | --- | --- | --- |
| Animation, page turn, reveal, release, or other picture-timing change | Keep one verbatim two-second label at the requested start | `false` | Label only; no animation evidence |
| Supplied hand, arrow, pointer, or other local visual asset | Insert one ordinary editable asset segment at the requested start | `true` | Saved asset, editable segment, and start alignment only |
| Supplied PNG/JPG replacement or local picture | Insert one ordinary editable asset segment at the requested start | `true` | Saved asset, editable segment, and start alignment only |
| Existing-hand occlusion, removal, cleanup, clean-cover, or residual cleanup | Keep one verbatim two-second label at the requested start | `false` | Label only; no cleanup evidence |
| Text/sticker safe-zone movement or text/flower intro/outro animation | Keep one verbatim two-second label at the requested start | `false` | Label only; no field mutation or animation evidence |
| Spoken deletion | Resolve the real cut with word/character ASR, then execute the editable cut | `true` | ASR cut, must-keep, reverse-validation, and editable-cut gates; label starts at the ASR cut |
| Semantic pause, pronunciation, breath, mouth noise, or other audio-identifiable timing | Resolve the actual word/character or adjacent-utterance boundary with source ASR | `true` | Both execution and label start use the ASR boundary; review time is search-only |
| Completely non-speech item | Parse the review timestamp or explicitly requested target | Per visual rule | Point timestamps are valid; `old time ... 提前到 target` uses the target |

An item that explicitly asks both to insert a supplied pointer and to clean the original hand
executes only the insertion. It must not create a cleanup layer; the same verbatim review label
records the whole source comment.

## Default Visual Geometry

For every supplied visual asset:

- use JianYing's default geometry;
- preserve `scale=1`, `transform_x=0`, `transform_y=0`, `rotation=0`, and `alpha=1`;
- do not add position, scale, opacity, or motion keyframes;
- do not calibrate size, move the asset, target a hotspot, optimize landing, or infer a subject;
- do not read or require subject profiles, project bindings, screenshots, opened-editor evidence,
  lifecycle receipts, target geometry, or pointer-placement receipts;
- clamp only the timeline duration to the unchanged source-project duration.

If a requested pointer or image asset is unavailable, keep the verbatim label, report that the
asset was not inserted, and do not start profile onboarding or synthesize a substitute.

## Forbidden Lite Outputs

Lite must not create or modify any of the following for visual review work:

- `Lite Timing Adjusted` segments;
- animation overlays, stable-frame composites, page-turn retiming, or reveal acceleration;
- clean-cover, cleanup, residual-pointer-cover, baked-window, or original-hand removal layers;
- pointer movement, scaling, rotation, alpha changes, target landing, or visual keyframes;
- text/sticker coordinates, safe-zone transforms, `anim_in`, `anim_out`, or flower animations.

The fixed trace tracks may still exist as empty editable tracks. Empty `Lite Timing Adjusted` is
correct Lite output.

## Labels And Acceptance

- Keep exactly one verbatim label per source review item.
- For every audio-identifiable issue, the review-document timestamp is only a search hint. Start
  the label at the final word/character ASR-resolved window or point, which must equal the saved
  edit/pause boundary. Never start an audio label from the rough review timestamp when ASR differs.
- For spoken deletion specifically, this remains the word/character ASR-resolved cut start.
- Only completely non-speech items use review timestamps. Prefer a requested target after cues
  such as `提前到`, `推迟到`, `移到`, or `调到`; a point timestamp is sufficient.
- Reject every unresolved item before draft writing. A missing time source must never produce a
  marker at `0:00`.
- Put animation/timing labels on `Review Marker Animation N`, supplied visual/pointer labels on
  `Review Marker Visual N`, and other review-only items on the documented fallback family.
- Start each label at the source item's requested start and use two seconds unless clamped by the
  unchanged project end. The spoken-deletion exception above uses the ASR-resolved edit start.
- Lite strict acceptance removes full visual, pointer, and animation gates. It must not reject a
  correct label-only animation, safe-zone, or cleanup item as marker-only.
- Execution-required visual items still need a real editable asset segment with matching item ID,
  local material, and start time. Default geometry and absence of keyframes are required.
- Strict acceptance must reject prohibited timing, cleanup, or transformed/keyframed Lite output.

These rules do not weaken spoken-word deletion, audio restoration, source preservation, marker
coverage, editable structure, package integrity, or portable-delivery checks.

## Split-Gap Audio Acceptance

- A1 contains exactly the complement of the merged ASR delete windows.
- A2 is one track with exactly one independent source-aligned clip per merged ASR delete window;
  its source start, timeline start, and duration equal the corresponding V2 window.
- Reject pending or empty segmented plans, full-length/continuous A2 clips, extra A2 clips, and
  any plan whose A1/A2 windows do not match the authoritative delete layout.
