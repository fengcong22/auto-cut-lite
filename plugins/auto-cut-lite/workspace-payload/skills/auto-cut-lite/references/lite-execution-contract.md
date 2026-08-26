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
| Spoken deletion | Resolve the real cut with word/character ASR, then execute the editable cut | `true` | ASR cut, must-keep, reverse-validation, and editable-cut gates; label starts at the ASR cut |
| Semantic pause, pronunciation, breath, mouth noise, or other audio-identifiable timing | Resolve the actual word/character or adjacent-utterance boundary with source ASR | `true` | Both execution and label start use the ASR boundary; a requested `+Ns` pause adds `N` seconds to the existing pause |
| Completely non-speech item | Parse the review timestamp or explicitly requested target | Per visual rule | Point timestamps are valid; `old time ... 提前到 target` uses the target |
| New or unrecognized issue with reliable authoritative time but no safe maintained implementation | Keep one label whose visible text is exactly `source_text`; skip ad-hoc execution | `false` | Record `execution_status=label_only_unresolved` only in internal metadata/receipts/reports and continue independent items |
| Item without reliable authoritative time, or audio-identifiable item without authoritative ASR | Do not create or update the draft | N/A | Fail before draft open/write; never guess a review time or `0:00` |

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
- clamp only the pause-mapped target duration to the final project duration.

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

- Keep exactly one visible label per source review item. Its text must equal only that item's
  `source_text` code-point-for-code-point, including timestamps, punctuation, whitespace, line
  breaks, and literal question marks.
- Keep `execution_status`, `label_only_unresolved`, item IDs, warnings, and diagnostics only in
  internal request metadata, marker receipts, validation output, and reports. Never prefix,
  suffix, replace, or otherwise annotate a visible label with them.
- For every audio-identifiable issue, the review-document timestamp is only a search hint. Start
  the label at the final word/character ASR-resolved window or point, which must equal the saved
  edit/pause boundary. Never start an audio label from the rough review timestamp when ASR differs.
- For spoken deletion specifically, this remains the word/character ASR-resolved cut start.
- Only completely non-speech items use review timestamps. Prefer a requested target after cues
  such as `提前到`, `推迟到`, `移到`, or `调到`; a point timestamp is sufficient.
- Reject every item whose authoritative time is unresolved before draft writing. A missing time
  source must never produce a marker at `0:00`. This timing failure is distinct from
  `label_only_unresolved`, which is allowed only after authoritative time is reliable.
- Put animation/timing labels on `Review Marker Animation N`, supplied visual/pointer labels on
  `Review Marker Visual N`, and other review-only items on the documented fallback family.
- Start each label at the source item's authoritative source start mapped through preceding added
  pauses and use two seconds unless clamped by the final project end. The pause item's own label
  stays at its insertion boundary before the new hold. Spoken deletion uses the ASR-resolved cut.
- Lite strict acceptance removes full visual, pointer, and animation gates. It must not reject a
  correct label-only animation, safe-zone, or cleanup item as marker-only.
- Execution-required visual items still need a real editable asset segment with matching item ID,
  local material, and start time. Default geometry and absence of keyframes are required.
- Strict acceptance must reject prohibited timing, cleanup, or transformed/keyframed Lite output.

These rules do not weaken spoken-word deletion, audio restoration, source preservation, marker
coverage, editable structure, package integrity, or portable-delivery checks.

## Timeline Duration And Pause Mapping

- Delete windows remain on the editable timeline and do not compress it. With no added semantic
  pause, final project duration equals source duration.
- A semantic-pause `duration` is the amount added to the existing source pause, not the desired
  total pause. `+1s` therefore preserves the existing pause and inserts one additional second.
- Final duration equals source duration plus the sum of added-pause durations. Apply the same
  cumulative added-pause offset to every later V1/V2/A1/A2 target range, visual asset, and review
  label. The inserted hold is an editable still-frame segment.
- Source ranges always remain in the source timebase; only target timeline ranges receive the
  cumulative offset. Validation checks the identical mapping across video, audio, visuals, and
  markers.
- `lite_cut_layout=copy` is historical read/validation compatibility only. Reject it before every
  new Lite execution; new tasks use `split_gap`.

## Split-Gap Audio Acceptance

- A1 contains exactly the complement of the authoritative ASR delete windows.
- A2 is one track with exactly one independent, audible, source-aligned clip per logical ASR
  delete window. Overlapping windows may merge only within the same stable source item. Adjacent
  windows from different items remain separate; true cross-item overlap fails before draft writing
  and requires disambiguation.
- Each A2 source range equals the corresponding V2 source window, and each A2 target range equals
  the same pause-mapped V2 target window. A2 volume remains `1.0`.
- Reject pending or empty segmented plans, full-length/continuous/cross-item-merged or muted A2,
  extra A2 clips, and any plan whose A1/A2 windows do not match the authoritative delete layout.

## Installed Runtime Integrity

Before non-mock execution, validate the deployment report, package-manifest anchor, and managed
runtime inventory. A missing, changed, extra, or hash-mismatched managed file is runtime drift.
Stop before task execution and require redeployment from a verified package; do not hot-patch the
installed runtime, accept drift, or fall back to another checkout.
