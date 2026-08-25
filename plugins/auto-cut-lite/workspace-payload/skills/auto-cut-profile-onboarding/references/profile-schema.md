# Subject Pointer Profile Schema

Use lowercase ASCII hyphen IDs for `stage_id` and `subject_id`. Generate them from the user-supplied names with the `identity` command; do not derive names from course content. `stage_name` and `subject_name` are required user-facing names. Relative evidence paths in an intake resolve from the intake file directory; absolute paths are accepted. An optional supplied `sha256` is a pre-copy integrity assertion and must match the source bytes. The repeated SHA-256 值是格式占位示例; replace every one with the real 64-character digest before registration.

For compatibility, `aliases` contains only the explicit Chinese display name and `detection_evidence` contains only the user's command with `source: explicit_user_command`. Both are audit metadata and 不得用于自动选择档案. 不得从课程内容生成 detection_evidence. 不得从标题、文件名、OCR、ASR、课程内容或版式选择学科.

Registration performs 增量 intake 合并 for a structurally readable exact profile. It retains verified existing evidence, unions aliases and detection evidence deterministically, and replaces evidence by stable logical ID. 不要求重新提交已满足项.

- `asset_id` identifies one logical pointer. Reuse it when replacing that pointer. For backward compatibility, an omitted ID is derived as `<role>:<direction>`, with `direction=default` when absent.
- `reference_id` identifies one logical scale reference. Reuse it for a corrected screenshot. When omitted, it is derived as `<layout>:<lesson>:<time>`.
- Optional `remove_aliases` is a list of obsolete aliases. Registration removes them from stored aliases before unioning the incoming `aliases`; an alias also present in incoming `aliases` is added again.
- Optional `placement_policies` stores subject-specific placement keyed by stable lowercase-hyphen `policy_id`. Every entry requires `asset_role`, `target_kind`, `layout`, `target_anchor`, and nonnegative `gap_px`. Repeated `policy_id` values are deduplicated deterministically with the latest entry replacing the earlier value. After that merge, `(asset_role, target_kind, layout)` must also be unique; two different policy IDs for the same tuple fail with `placement_policy_key_ambiguous`.
- `target_anchor: text_bottom` is valid only for an item whose bound asset role, target kind, and current layout match that policy. It is not a global text-placement fallback.
- An asset with `role: hand` must be a decodable PNG with an alpha channel. Registration derives its `media_contract`; do not author that field in intake JSON.

For a stored legacy hand profile that predates `media_contract`, run `profile_registry.py migrate-hand-media --stage-id <stage_id> --subject-id <subject_id> --root <registry-root> --json`. Migration derives the contract only from the already stored, hash-matching asset and commits transactionally. It never depends on the original intake path and refuses changed or unsafe stored evidence without modifying the profile.

## Complete Intake Example

```json
{
  "stage_id": "stage-a",
  "subject_id": "topic-a",
  "stage_name": "阶段A",
  "subject_name": "类别A",
  "display_name": "阶段A类别A",
  "aliases": ["阶段A类别A"],
  "detection_evidence": [
    {
      "source": "explicit_user_command",
      "value": "建立阶段A类别A指向物素材库",
      "confirmed": true
    }
  ],
  "assets": [
    {
      "path": "D:/evidence/topic-a-pointer.png",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "asset_id": "primary-topic-a-pointer",
      "role": "hand",
      "anchor": [0.92, 0.48]
    }
  ],
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
  "scale_references": [
    {
      "path": "D:/evidence/topic-a-map-reference.png",
      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "reference_id": "topic-a-map-lesson-12",
      "lesson": "示例单元 12",
      "time": "00:08:14",
      "layout": "full-frame-map",
      "full_frame": true,
      "confirmed": true,
      "canvas_size": {"width": 1600, "height": 900},
      "visible_bbox": {"x": 1110, "y": 290, "width": 240, "height": 180}
    },
    {
      "path": "D:/evidence/topic-a-timeline-reference.png",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "reference_id": "topic-a-timeline-lesson-13",
      "lesson": "示例单元 13",
      "time": "00:16:42",
      "layout": "full-frame-timeline",
      "full_frame": true,
      "confirmed": true,
      "canvas_size": {"width": 1600, "height": 900},
      "visible_bbox": {"x": 980, "y": 515, "width": 208, "height": 144}
    }
  ],
  "approved_previews": [
    {
      "path": "D:/evidence/topic-a-approved-preview.png",
      "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "approved": true,
      "note": "素材与两个参考画面的比例均已确认"
    }
  ]
}
```

## Stored Profile Example

```json
{
  "schema_version": 1,
  "key": "generic-course-profile",
  "stage_id": "stage-a",
  "subject_id": "topic-a",
  "stage_name": "阶段A",
  "subject_name": "类别A",
  "display_name": "阶段A类别A",
  "aliases": ["阶段A类别A"],
  "detection_evidence": [
    {
      "source": "explicit_user_command",
      "value": "建立阶段A类别A指向物素材库",
      "confirmed": true
    }
  ],
  "status": "ready",
  "missing_items": [],
  "problems": [],
  "assets": [
    {
      "path": "assets/aaaaaaaaaaaa-topic-a-pointer.png",
      "source_name": "topic-a-pointer.png",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "asset_id": "primary-topic-a-pointer",
      "role": "hand",
      "anchor": [0.92, 0.48],
      "media_contract": {
        "format": "png",
        "has_alpha": true,
        "width": 354,
        "height": 354
      }
    }
  ],
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
  "scale_references": [
    {
      "path": "scale-references/bbbbbbbbbbbb-topic-a-map-reference.png",
      "source_name": "topic-a-map-reference.png",
      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "reference_id": "topic-a-map-lesson-12",
      "lesson": "示例单元 12",
      "time": "00:08:14",
      "layout": "full-frame-map",
      "full_frame": true,
      "confirmed": true,
      "canvas_size": {"width": 1600, "height": 900},
      "visible_bbox": {"x": 1110, "y": 290, "width": 240, "height": 180},
      "visible_width_ratio": 0.15,
      "visible_height_ratio": 0.2
    },
    {
      "path": "scale-references/cccccccccccc-topic-a-timeline-reference.png",
      "source_name": "topic-a-timeline-reference.png",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "reference_id": "topic-a-timeline-lesson-13",
      "lesson": "示例单元 13",
      "time": "00:16:42",
      "layout": "full-frame-timeline",
      "full_frame": true,
      "confirmed": true,
      "canvas_size": {"width": 1600, "height": 900},
      "visible_bbox": {"x": 980, "y": 515, "width": 208, "height": 144},
      "visible_width_ratio": 0.13,
      "visible_height_ratio": 0.16
    }
  ],
  "approved_previews": [
    {
      "path": "approved-previews/dddddddddddd-topic-a-approved-preview.png",
      "source_name": "topic-a-approved-preview.png",
      "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "approved": true,
      "note": "素材与两个参考画面的比例均已确认"
    }
  ]
}
```

## Readiness And Integrity

- `ready`: required identity fields are valid, every optional placement policy is valid and deduplicated, at least one asset exists, at least two structurally valid full-frame scale references have `confirmed: true`, at least one preview has `approved: true`, and every copied file still matches its stored hash. Every `hand` asset must still decode as an alpha PNG whose width, height, format, and alpha state match its stored `media_contract`.
- `incomplete`: identity/schema fields are missing or malformed, or evidence categories do not contain the minimum number of structurally valid entries. Preserve the source profile and report explicit `missing_items` and `problems`.
- `needs_confirmation`: the entries exist but scale confirmation or preview approval is insufficient.
- `stale`: a copied evidence file is missing or its bytes no longer match `sha256`.
- `missing`: no exact stage-and-subject profile file exists yet.

Registration derives each scale reference's visible ratios as `visible_bbox.width / canvas_size.width` and `visible_bbox.height / canvas_size.height`. Do not author these ratios in intake JSON.

A fresh pointer receipt carries the selected asset's derived `media_contract`, only the `placement_policies` whose `asset_role` and `layout` match that receipt, and the sorted role-wide `placement_policy_target_kinds`. Pointer targeting performs the final unique `target_kind` match before applying `target_anchor` and `gap_px`.

A full-frame reference requires `full_frame: true`, nonempty `lesson`, `time`, and `layout`, a positive 16:9 `canvas_size`, and a positive `visible_bbox` fully inside the canvas. Catalog JSON retains names, aliases, detection evidence, counts, status, missing items, and problems; catalog Markdown presents the same state in Chinese.
