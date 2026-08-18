# Pointer Asset And Target Adapters

Use this reference when a new course layout or pointer asset appears.

## Manual Project Binding And Subject Registry Receipt

Read the current project from `data/subject-pointer-profiles.local/project-bindings.json`, then read profiles only from the same repository-local registry. `auto-cut-pointer-targeting` consumes the manually bound `profile_key` and a fresh `status=ready` receipt; it does not create, promote, repair, or automatically bind profiles. A direct library operation still requires an explicit create/update command. In ordinary pointer work, missing or invalid exact evidence opens the same-task onboarding handoff before draft writes; onboarding collects explicit user input and returns through a fresh gate.

Store this receipt under `review_items[*].evidence.subject_profile_receipt` and set `acceptance.require_subject_pointer_binding=true`:

```json
{
  "subject_profile_receipt": {
    "registry_root": "<registry-root>",
    "project_key": "History_C1001",
    "project_path": "<draft-root>/History_C1001_v01",
    "profile_key": "senior-high-history",
    "profile_path": "<registry-root>/senior-high-history/profile.json",
    "stage_id": "senior-high",
    "subject_id": "history",
    "status": "ready",
    "asset_path": "<registry-root>/senior-high-history/assets/aaaaaaaaaaaa-history-pointer.png",
    "asset_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "asset_role": "hand",
    "anchor": [0.03, 0.03],
    "media_contract": {
      "format": "png",
      "has_alpha": true,
      "width": 354,
      "height": 354
    },
    "anchor_meaning": "fingertip",
    "natural_direction": "up_left",
    "rotation_allowed": false,
    "scale_reference_path": "<registry-root>/senior-high-history/scale-references/bbbbbbbbbbbb-history-map-reference.png",
    "scale_reference_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "scale_reference_layout": "full-frame-map",
    "scale_reference_id": "history-map-lesson-12",
    "visible_width_ratio": 0.15,
    "visible_height_ratio": 0.20,
    "placement_policies": [
      {
        "policy_id": "hand-text-bottom-full-frame-map",
        "asset_role": "hand",
        "target_kind": "text",
        "layout": "full-frame-map",
        "target_anchor": "text_bottom",
        "gap_px": 8
      }
    ],
    "placement_policy_target_kinds": ["text"],
    "approved_preview_path": "<registry-root>/senior-high-history/approved-previews/cccccccccccc-history-approved-preview.png",
    "approved_preview_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "approved_preview_approved": true
  }
}
```

`anchor` is normalized `[x, y]` in asset coordinates from top-left to bottom-right.
`registry_root` and every receipt evidence path must be absolute. `project_key` and `project_path` must match the stored manual binding. `profile_key` is the one canonical profile identifier and must equal both the binding value and registry profile's `key`; do not introduce a parallel profile id. Resolve every receipt path inside that exact profile directory. Run a fresh registry check, then recalculate the asset, scale-reference, and approved-preview hashes from their current bytes.

For `hand`, `media_contract` is registry-derived and must describe the current decodable alpha PNG bytes. `placement_policies` is the exact fresh subset matching the receipt's `asset_role` and `scale_reference_layout`; `placement_policy_target_kinds` is the sorted fresh set of every target kind configured for that asset role. Do not copy a policy from another layout or author either field in the job. Select a policy for an item only when its `asset_role`, `target_kind`, and `layout` form one unique receipt policy. `target_anchor=text_bottom` and `gap_px` apply only after that three-part match.

Every pointer item records `target_kind`, `selected_placement_policy_id`, `target_anchor`, and `gap_px`. When no unique policy matches, the item is valid only if its target kind is absent from `placement_policy_target_kinds` and it records `placement_policy_status=not_required` plus a nonempty `placement_policy_not_required_reason`.

Bind both the hotspot and scale ratios to the exact asset SHA-256 and exact `stage_id + subject_id`. A matching filename is not identity evidence.

## Asset Roles

| Role | Anchor | Scale Hint | Rotation |
| --- | --- | --- | --- |
| `hand` | fingertip | receipt ratio for the current layout | usually false unless asset supports it |
| `arrow` | arrow head | receipt ratio for the current layout | usually true |
| `circle` | circle center | receipt ratio for the current layout | false |
| `magnifier` | lens focus | receipt ratio for the current layout | false or limited |
| `highlight` | visual center | receipt ratio for the current layout | false |
| `custom` | configured hotspot | receipt ratio for the current layout | configured |

## Hotspot Use

Use the hotspot from the exact ready profile after verifying its asset SHA-256. If the profile, hotspot, hash, requested role, or current-layout scale reference is missing or invalid, stop and open the same-task onboarding handoff. Never infer or persist a replacement inside pointer targeting. After explicit binding confirmation, reload the binding, run a fresh registry `check`, recalculate all evidence hashes, and verify the requested role and current layout before using this adapter.

For non-hand assets:

- arrow: align the arrow head to the target and rotate only if the asset supports rotation without looking distorted
- circle: align center to target box center, but scale only from the receipt's current-layout ratios; the target box supplies position geometry, not size
- magnifier: align lens focus to the target; treat decorative handle as non-target geometry
- underline: align to the text baseline or lower edge; duration follows the target text beat
- custom sticker: require the receipt-bound hotspot; if it is unknown, stop and request an explicit library update before placement

## Target Layout Adapters

Use the adapter matching the visual target:

| Layout | Targeting Strategy |
| --- | --- |
| PPT text | OCR text block, title/body segmentation, teacher annotation |
| map | place names, highlighted regions, arrows, colored annotation boxes |
| table | row/column headers, cell grid, target text inside cell |
| timeline | year labels, nodes, horizontal or vertical connectors |
| UI recording | button/menu text, rectangular control detection, cursor context |
| chart | data label, axis label, marked point, legend entry |
| diagram | node labels, colored shapes, connector endpoints |

## Confidence Rules

Use high confidence when:

- annotation and target text agree
- OCR target is unique
- layout geometry is clear

Use medium confidence when:

- multiple matching text instances exist but context narrows the choice
- annotation exists but not exactly on the target

Use low confidence when:

- the target word is absent
- the screenshot is cropped or blurry
- multiple plausible regions remain
- the pointer asset anchor is unknown

For low confidence, generate a preview and ask for confirmation instead of writing a final-looking draft.

## Scaling Rules

Scale priority:

1. Select the exact ready profile by `stage_id + subject_id`.
2. Verify the selected asset SHA-256 against the current bytes.
3. Choose ratio by the current layout from the receipt-bound profile.
4. Apply that reference's visible width and height ratios to the current canvas, then record the profile key, asset hash, layout, reference id/path, and ratios in evidence.
5. After saving, read the actual segment: `clip.scale.x/y` must equal `visible_height_ratio`, while `visible_width_ratio` must agree with the decoded asset aspect ratio and canvas.

Recompute the saved hotspot from the clip transform, normalized anchor, canvas, and rendered footprint. Compare it with `target_point` using `max_landing_error_px` or 2 pixels when omitted. For a receipt-bound `target_anchor=text_bottom` policy, require `target_geometry` with `x`, `y`, `width`, `height`, `canvas_width`, and `canvas_height`, then independently derive `target_point=(x + width/2, y + height + gap_px)`. Do not accept a planned coordinate or declared pass without this saved-state calculation.

Detect duplicate or stale pointer layers from the registered material identity: material ID, canonical path, or current SHA-256, and reject a differently bound pointer-like hand/display-safe track even when it uses another material and name. Renaming is not a bypass. The `underline_layers` and `clean_layers` inventories are required even when empty; every nonempty entry has `track_name` and `segment_id`, resolves to exactly one saved track and segment, exposes `render_index`, and preserves saved order `pointer > underline > clean`.

A `display_safe_baked_window` is accepted only from saved state. Its material must be a readable full-canvas video with no audio stream; its segment must have `volume=0`, scale 1, transform 0, a window contained by and overlapping the corresponding editable pointer, and the topmost render index. Evidence includes `opened_draft_drift_artifact_path`, `opened_draft_drift_artifact_sha256`, `opened_draft_drift_timeline_id`, and `opened_draft_drift_window`; the saved timeline, segment, exact window, artifact contents, and current artifact bytes must agree. Boolean claims such as `no_audio` or `full_canvas` are not substitutes for inspecting the saved media.

If there is no confirmed scale reference for the current layout, stop and open the same-task onboarding handoff to request only that layout evidence. Do not write an overlay while waiting.

Forbidden fallbacks:

- another subject
- another stage
- filename-only inheritance
- target-relative default
- conservative default
- centered or fixed `0.22` fallback

禁止跨学科或跨学段复用素材、热点和比例。The old “hand size = target text height * 2” rule is also forbidden. User feedback such as “比现在小一点” becomes reusable only after an explicit library update stores confirmed layout evidence.

## Duration Adapters

Asset role affects how long it should stay visible:

| Role | Duration behavior |
| --- | --- |
| `hand` | appear for the spoken/visual beat, then disappear before the next target or animation |
| `arrow` | may hold slightly longer than a hand, but still release before stale context |
| `circle` | hold through the reading beat; can remain while the target stays central and no new reveal is blocked |
| `magnifier` | keep only while the magnified detail is being discussed |
| `highlight` | can span the full explanation sentence when it does not hide new information |
| `custom` | use hotspot role plus the safest matching behavior above |

Do not let any pointer-like asset persist across a page turn, target removal, or unrelated new animation unless the review comment explicitly wants a persistent marker.
