# Auto-Cut Lite Acceptance Checklist

Use this checklist together with the
[Lite execution contract](../../auto-cut-lite/references/lite-execution-contract.md).

## Source And Structure

- The latest source ledger supplies unique stable item IDs and verbatim source text.
- Root and active timeline drafts exist and agree.
- Project duration equals source duration.
- Required editable video/audio/marker tracks and split-gap cut boundaries exist.
- One start-aligned, at-most-two-second verbatim label exists per source item. Completely
  non-speech point timestamps are valid; target wording such as `07:14 ... 提前到 07:12`
  resolves to `07:12`, and unresolved timing never falls back to `0:00`.

## Audio

- Every issue locatable through speech or audio uses word/character ASR boundaries, not review
  timestamps. This includes spoken cuts, semantic pauses, pronunciation, breath, mouth noise,
  and speech timing.
- Audio-related labels start at the same ASR-resolved window or point as the saved edit/pause,
  even when the review-document timestamp differs.
- Delete and `must_keep` contracts match the physical cut windows.
- Reverse-ASR, candidate decode, segmented-audio, and semantic-join checks pass when required.
- A1 equals the complement of merged ASR delete windows. A2 has exactly one independent clip per
  merged ASR/V2 delete window with matching source start, timeline start, and duration. Pending,
  empty, full-length, continuous, extra, or differently merged A2 delivery fails.

## Lite Visual Behavior

- Animation/picture-timing items are label-only and `execution_required=false`.
- Text/sticker movement and text/flower animation items are label-only.
- Existing-hand occlusion, removal, clean-cover, cleanup, and residual-cover items are label-only.
- A supplied pointer/image item has one attributable local material and editable segment at its
  requested start.
- Every supplied visual keeps scale 1, transform 0/0, rotation 0, alpha 1, and no keyframes.
- `Lite Timing Adjusted` has no segments.
- No animation overlay, cleanup layer, baked window, pointer motion, calibration, target landing,
  or profile binding was introduced.
- No profile, lifecycle, hotspot, animation, opened-editor, or screenshot evidence is required.

## Portable Delivery

- All local draft materials and the relink tool are included.
- ZIP paths are safe, CRC passes, isolated extraction succeeds, and package/extracted tree hashes
  match.
- Draft JSON was not rewritten during packaging and JianYing was not opened.
- The external receipt binds the final ZIP hash and package tree.
