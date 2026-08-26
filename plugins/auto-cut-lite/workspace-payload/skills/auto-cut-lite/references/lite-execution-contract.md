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
| Other audio repair | Execute through the applicable Lite audio safeguards | `true` | Existing audio evidence and editable-project gates |

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
- For spoken deletion, the review-document timestamp is only a search hint. Start the label at
  the final word/character ASR-resolved cut start, which must equal the saved edit start. Never
  start an audio-cut label from the rough review timestamp when the ASR window differs.
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
