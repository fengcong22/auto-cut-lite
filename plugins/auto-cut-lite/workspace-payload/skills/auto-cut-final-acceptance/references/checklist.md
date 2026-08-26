# Auto-Cut Lite Acceptance Checklist

Use this checklist together with the
[Lite execution contract](../../auto-cut-lite/references/lite-execution-contract.md).

## Source And Structure

- The latest source ledger supplies unique stable item IDs and verbatim source text.
- Root and active timeline drafts exist and agree.
- Project duration equals source duration.
- Required editable video/audio/marker tracks and split-gap cut boundaries exist.
- One start-aligned, at-most-two-second verbatim label exists per source item.

## Audio

- Spoken cuts use word/character ASR boundaries, not review timestamps.
- Spoken-delete labels start at the same ASR-resolved cut start as the saved edit, even when the
  review-document timestamp differs.
- Delete and `must_keep` contracts match the physical cut windows.
- Reverse-ASR, candidate decode, segmented-audio, and semantic-join checks pass when required.

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
