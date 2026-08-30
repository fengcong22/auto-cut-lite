# Auto-Cut Lite Execution Contract

This is the single authoritative visual execution contract for the packaged Auto-Cut Lite
workspace and also defines its label, timeline, audio-layout, and runtime-integrity invariants. It
overrides conflicting instructions in every router, focused skill, checklist, example, or
user-selected subskill whenever `workflow_mode=lite`.

## Classification Matrix

| Review request | Lite behavior | `execution_required` | Acceptance |
| --- | --- | --- | --- |
| Animation, page turn, reveal, release, or other picture-timing change | Keep one two-second label whose text is exactly `source_text` at the requested start | `false` | Label only; no animation evidence |
| Supplied hand, arrow, pointer, or other local visual asset | Insert one ordinary editable asset segment at the requested start | `true` | Saved asset, editable segment, and start alignment only |
| Supplied PNG/JPG replacement or local picture | Insert one ordinary editable asset segment at the requested start | `true` | Saved asset, editable segment, and start alignment only |
| Existing-hand occlusion, removal, cleanup, clean-cover, or residual cleanup | Keep one two-second label whose text is exactly `source_text` at the requested start | `false` | Label only; no cleanup evidence |
| Text/sticker safe-zone movement or text/flower intro/outro animation | Keep one two-second label whose text is exactly `source_text` at the requested start | `false` | Label only; no field mutation or animation evidence |
| Spoken deletion | Resolve the real boundary with word/character ASR, then write a non-destructive editable split/cut trace while retaining the complete source interval | `true` | ASR boundary, must-keep, source-preservation, and editable-trace gates; the label uses the word/character ASR-resolved cut start; no duration change |
| Any other duration-changing request, including pause, speed, hold, still-frame, or stretch changes | Attempt source word/character ASR, then keep one exact `source_text` label | `false` | Label at a unique ASR point, otherwise at the review-comment time; no duration mutation or later-track offset |
| Pronunciation, breath, mouth noise, or other audio-identifiable timing | Attempt the actual word/character boundary with source ASR | `false` unless an explicit maintained non-duration executor exists | Label at the unique ASR boundary, otherwise at the review-comment time |
| Review-only or ASR-unresolved item | Parse the review timestamp or explicitly requested target | Per visual rule | Point timestamps are valid; `old time ... 提前到 target` uses the target |
| New or unrecognized issue with reliable authoritative time but no safe maintained implementation | Keep one label whose visible text is exactly `source_text`; skip ad-hoc execution | `false` | Record `execution_status=label_only_unresolved` only in internal metadata/receipts/reports and continue independent items |
| Item with neither a unique ASR point nor a valid review-comment time | Do not create or update the draft | N/A | Fail before draft open/write; never guess `0:00` |

An item that explicitly asks both to insert a supplied pointer and to clean the original hand
executes only the insertion. It must not create a cleanup layer; the same verbatim review label
records the whole source comment. Internal status is not part of that visible text.

## Default Visual Geometry

For every supplied visual asset:

- use JianYing's default geometry;
- preserve `scale=1`, `transform_x=0`, `transform_y=0`, `rotation=0`, and `alpha=1`;
- do not add position, scale, opacity, or motion keyframes;
- do not calibrate size, move the asset, target a hotspot, optimize landing, or infer a subject;
- do not read or require subject profiles, project bindings, screenshots, opened-editor evidence,
  lifecycle receipts, target geometry, or pointer-placement receipts;
- clamp only the source-aligned target duration to the source-length project duration.

If a requested pointer or image asset is unavailable, keep the verbatim label, report that the
asset was not inserted, and do not start profile onboarding or synthesize a substitute.
For a hand/pointer row whose attachment is missing, the only allowed reuse is a unique attachment
from another row with the same normalized modification name. Multiple candidates require a
structured `user_action_required` response.

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

- Keep exactly one visible label per source review item. Its text must equal only that item's
  `source_text` code-point-for-code-point, including timestamps, punctuation, whitespace, line
  breaks, and literal question marks.
- Keep `execution_status`, `label_only_unresolved`, item IDs, warnings, and diagnostics only in
  internal request metadata, marker receipts, validation output, and reports. Never prefix,
  suffix, replace, or otherwise annotate a visible label with them.
- For every audio-identifiable issue, attempt word/character ASR first. For an executable spoken
  deletion its unique ASR window is mandatory and equals the saved non-destructive split boundary;
  the source interval remains present and the project duration is unchanged. A non-executing item
  uses its unique ASR point when available and otherwise the time written in the review comment.
- A spoken deletion may resolve through a time-anchored high-confidence fuzzy match only when one
  continuous ASR word span retains at least 80% ordered keyword coverage and wins uniquely by time
  and text confidence. Permit one non-tail omitted review character for short phrases and two for
  phrases of at least 16 normalized characters; a detached optional discourse tail such as `对吧`
  may also be absent when it belongs to another utterance. The cut covers only recognized ASR
  words, records omitted review text, and never skips intervening ASR words. An inter-word gap over
  1.5 seconds breaks continuity; a provider utterance boundary alone does not.
- For spoken deletion specifically, this remains the word/character ASR-resolved logical split start.
- Review-only and ASR-unresolved items use review timestamps. Prefer a requested target after cues
  such as `提前到`, `推迟到`, `移到`, or `调到`; a point timestamp is sufficient.
- Reject every item whose authoritative time is unresolved before draft writing. A missing time
  source must never produce a marker at `0:00`. This timing failure is distinct from
  `label_only_unresolved`, which is allowed only after authoritative time is reliable.
- Put animation/timing labels on `Review Marker Animation N`, supplied visual/pointer labels on
  `Review Marker Visual N`, and other review-only items on the documented fallback family.
- Start each label at the source item's authoritative source start without a pause-derived offset
  and use two seconds unless clamped by the source-length project end. A pause item's label starts
  at its unique ASR point or, when ASR cannot locate it, its review-comment time. Spoken deletion
  uses the ASR-resolved cut.
- Lite strict acceptance removes full visual, pointer, and animation gates. It must not reject a
  correct label-only animation, safe-zone, or cleanup item as marker-only.
- Execution-required visual items still need a real editable asset segment with matching item ID,
  local material, and start time. Default geometry and absence of keyframes are required.
- Strict acceptance must reject prohibited timing, cleanup, or transformed/keyframed Lite output.

These rules do not weaken spoken-word deletion, audio restoration, source preservation, marker
coverage, editable structure, package integrity, or portable-delivery checks.

## Timeline Duration And Pause Labels

- Logical delete windows remain on the editable timeline as source-aligned trace ranges and do not
  remove source media or compress it. Final project duration always equals source duration.
- Every physical duration-changing operation is non-executing in Lite. A precisely ASR-proved
  spoken deletion is execution-required only for its non-destructive split/cut trace; pause changes,
  `+1s`, `-1s`, speed changes, holds, still frames, and input named
  `semantic_pause_adjustment` are label-only.
- Attempt to resolve the request with authoritative word/character ASR. Place exactly one visible
  label equal only to `source_text` at a unique ASR point, otherwise at the review-comment time.
- Do not emit executable `pause_adjustments`, a hold, a still-frame pause segment, an audio gap, or
  any pause-derived target mapping. Do not change project duration or shift a later V1/V2/A1/A2
  range, visual asset, or review label.
- Source and target ranges remain on the same source-aligned timebase. Strict acceptance rejects
  any pause-derived duration extension or offset.
- `lite_cut_layout=copy` is historical read/validation compatibility only. Reject it before every
  new Lite execution; new tasks use `split_gap`.

## Split-Gap Audio Acceptance

- A1 contains exactly the complement of the authoritative ASR delete windows.
- A2 is one track with exactly one independent, audible, source-aligned clip per logical ASR
  delete window. Overlapping windows may merge only within the same stable source item. Adjacent
  windows from different items remain separate; true cross-item overlap fails before draft writing
  and requires disambiguation.
- Each A2 source range equals the corresponding V2 source window, and each A2 target range equals
  the same source-aligned V2 target window. A2 volume remains `1.0`.
- Reject pending or empty segmented plans, full-length/continuous/cross-item-merged or muted A2,
  extra A2 clips, and any plan whose A1/A2 windows do not match the authoritative delete layout.

## Installed Runtime Integrity

Before non-mock execution, validate the deployment report, package-manifest anchor, and managed
runtime inventory. A missing, changed, extra, or hash-mismatched managed file is runtime drift.
Stop before task execution and require redeployment from a verified package; do not hot-patch the
installed runtime, accept drift, or fall back to another checkout.
