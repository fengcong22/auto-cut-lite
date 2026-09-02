# Auto-Cut Lite Acceptance Checklist

Use this checklist together with the
[Lite execution contract](../../auto-cut-lite/references/lite-execution-contract.md).

## Source And Structure

- The latest source ledger supplies unique stable item IDs and verbatim source text.
- Root and active timeline drafts exist and agree.
- Project duration equals source duration even when a review asks to add, extend, shorten, or
  otherwise adjust a pause. Deletions never compress the Lite timeline.
- Required editable video/audio/marker tracks and split-gap cut boundaries exist.
- One start-aligned, at-most-two-second verbatim label exists per source item. Review-only and
  ASR-unresolved non-executing items use review-comment times; target wording such as
  `07:14 ... 提前到 07:12` resolves to `07:12`, and timing never falls back to `0:00`.

## Audio

- Every issue locatable through speech or audio attempts word/character ASR. Only a uniquely
  ASR-located spoken deletion may execute. A non-executing item uses the unique ASR point when
  available and otherwise the review-comment time.
- Delete and `must_keep` contracts match the authoritative source-aligned logical cut windows;
  source media remains complete and the windows are visible through the complementary V1/V2 and
  A1/A2 split traces.
- Reverse-ASR, candidate decode, segmented-audio, and semantic-join checks pass when required.
- Every retained same-word occurrence has one positive `delete_hit` and exactly one attributable
  `delete_hit_adjudications` receipt whose exact `hit` object matches phrase, text, start, end,
  and match method, stays outside all target windows, and carries the correct earlier/later/between
  role plus independent local context. A single legacy receipt is accepted only for one hit.
- Colored-span rows use the ordered item-contract `delete_phrases`; aggregate display text cannot
  authorize a hit, and uncolored inter-span ASR text is protected through automatic `must_keep`.
- A1 equals the complement of logical ASR delete windows. A2 has exactly one independent, audible
  clip per logical ASR/V2 delete window with matching source start and source-aligned timeline start
  and duration. Same-item overlaps may merge; adjacent different-item windows stay separate and
  true cross-item overlaps fail. Pending, empty, full-length, continuous, muted, extra, or
  differently merged A2 delivery fails.
- Every pause addition, extension, shortening, or adjustment is `execution_required=false` and has
  one source-text-only label at its unique ASR point or review-time fallback.
- No executable `pause_adjustments`, hold, still-frame pause segment, inserted audio gap,
  pause-derived duration change, or offset to a later track, asset, or label exists.
- No spoken-delete item physically removes or compresses source media. Any reverse-ASR candidate is
  a source-time-preserving diagnostic artifact only and is not a delivery/replacement track.

## Lite Visual Behavior

- Animation/picture-timing items are label-only and `execution_required=false`.
- Text/sticker movement and text/flower animation items are label-only.
- Existing-hand occlusion, removal, clean-cover, cleanup, and residual-cover items are label-only.
- A supplied pointer/image item has one attributable local material and editable segment at its
  requested start.
- A hand/pointer row with no attachment may reuse only the unique candidate from another row with
  the same normalized modification name. Multiple candidates require `user_action_required`.
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

## Marker State

- Every visible marker is exactly the source ledger `source_text`.
- `execution_status=label_only_unresolved`, item IDs, warnings, and diagnostics remain internal
  metadata/receipts/reports and never become visible marker text.
