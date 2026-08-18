# Local Image Overlay Evidence Contract

## Item Shape

Store one review item per stable source-ledger ID:

```json
{
  "item_id": "stable-source-item-id",
  "source_text": "exact document text",
  "action": "replace_local_image",
  "review_timestamp_role": "search_hint",
  "asset_sources": [],
  "overlay_units": [],
  "marker_receipt": {}
}
```

`source_text` must match the latest canonical ledger code-point-for-code-point. One item has exactly one marker even when `overlay_units` contains multiple entries.

## Asset Source

Each `asset_sources` entry records:

```json
{
  "document_id": "doc-token",
  "block_id": "image-block-id",
  "media_token": "image-token",
  "document_order": 1,
  "original_path": "absolute-path",
  "original_sha256": "64-lowercase-hex",
  "format": "png",
  "asset_receipt": {
    "width": 467,
    "height": 92,
    "channels": 4,
    "has_alpha": true,
    "visible_bbox": {"x": 27, "y": 19, "width": 333, "height": 50},
    "nonzero_alpha_pixels": 3107
  },
  "derived_assets": []
}
```

Bind every derived asset to the original path/hash and record the operation that created it. Copy `asset_receipt.visible_bbox` into each saved unit's `asset_visible_bbox`; these names distinguish decoded file evidence from placement evidence. A document alt description is supporting text, not attachment mapping or reuse authority.

## Overlay Unit

Each saved replacement segment has one unit:

```json
{
  "overlay_unit_id": "item-id-overlay-01",
  "asset_block_id": "image-block-id",
  "source_target_bbox": {
    "x": 100,
    "y": 200,
    "width": 420,
    "height": 64,
    "canvas_width": 1920,
    "canvas_height": 1080
  },
  "asset_visible_bbox": {"x": 27, "y": 19, "width": 333, "height": 50},
  "scale_reference_kind": "source_target_formula",
  "source_formula_baseline": 244,
  "asset_formula_baseline": 58,
  "aspect_ratio_preserved": true,
  "protected_regions": [],
  "source_window": {"start": 1140.0, "end": 1141.0},
  "timeline_window": {"start": 1122.5, "end": 1123.5},
  "track_name": "Local Image Replacement",
  "segment_id": "saved-segment-id",
  "material_id": "saved-material-id",
  "render_index": 30,
  "coverage_status": "pass",
  "canvas_containment_status": "pass",
  "unintended_overlap_status": "pass",
  "clean_layers": [],
  "opened_draft_samples": []
}
```

Use `source_target_text` for normal text/labels and `source_target_formula` for formulas. Formula evidence requires a finite baseline rule; do not use a phrase-level box alone when fractions or superscripts change the visible extent.

## Cleanup Evidence

When the transparent replacement would expose old pixels, `clean_layers` contains the exact saved segment below it. Record:

- cleanup mode: `static_local_cover` or `source_synchronous_cleanup`
- asset/media path and SHA-256
- positive cleanup rectangle and safety margin
- source and timeline windows
- saved track, segment, material, render index, volume, and timeranges
- first/middle/last source-clean comparison samples
- protected-region clearance

Static cleanup requires three or more samples proving the entire rectangle's background is static. Source-synchronous cleanup must be muted, consume its complete media duration from source time zero, match the overlay timeline window exactly, and preserve motion outside the cleanup region.

## Opened-Draft Evidence

For each distinct page/formula state, bind first/middle/last samples to:

- artifact path and SHA-256
- full-editor context path and SHA-256
- JianYing timeline ID and playhead time
- editor canvas rectangle
- expected overlay unit and saved segment
- old-content absence result
- exactly-one-replacement result
- scale/baseline result
- protected-region overlap result

Do not reuse one artifact under multiple sample roles.

## Acceptance Invariants

- Every stable item ID has exactly one exact-text marker.
- Every document image block maps to zero or one source item unless explicit reuse evidence exists.
- Every `overlay_unit` maps to one saved image segment and one asset source.
- `clean_layers` is present even when empty; saved order is replacement above clean above source.
- Saved image/cleanup timeranges equal `timeline_window`, not merely its duration.
- Root and active timeline variants pass the same material, geometry, lifecycle, marker, and opened-state checks.
- Ordinary local image work does not require `subject_profile_receipt`; a pointer-like request must be rerouted rather than bypassing the pointer gate.
- Marker-only delivery, a self-declared pass, or a full-length flattened preview fails.

## Regression Receipt

For document `UXwddzEC1o9Aahx8uEecd2TBnnd`, acceptance must prove:

- the `19:41` item keeps all three following PNG blocks as separate overlay units
- alpha-visible bounds, not canvas dimensions, drive scaling
- `18:03` and `19:41` page changes split or release stale overlays
- the corrected `19:00` overlay window accounts for the actual `19:01` state transition
- animation-only and rerecorded-video items are routed out of this skill
