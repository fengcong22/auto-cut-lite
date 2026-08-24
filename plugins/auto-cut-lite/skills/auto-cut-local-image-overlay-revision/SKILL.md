---
name: auto-cut-local-image-overlay-revision
description: "Use when JianYing/CapCut review comments provide PNG/JPG images to replace formulas, labels, text blocks, icons, or another precise local region while matching the original visual scale and avoiding neighboring content; trigger for 修改意见文档里用下面图片替换、式子有误、局部素材覆盖、比例和原字号一致、覆盖时间不准, or transparent image overlays."
---

# 局部图片覆盖修订

## Packaged Runtime

Before executing this skill from the distributed plugin, read the [portable runtime contract](../auto-cut-lite/references/portable-runtime.md). Use the installed isolated runtime and `workflow_mode=lite`; do not fall back to a source checkout or another Auto-Cut installation.


## Overview

Use this packaged skill for document-provided PNG/JPG replacements that must land on a precise local region of an editable JianYing draft. Preserve the source, original image assets, independent cleanup/overlay layers, visible cut structure, and exact review markers.

Read [references/workflow.md](references/workflow.md) before executing a job. Read [references/evidence-contract.md](references/evidence-contract.md) before compiling the request or claiming acceptance.

## Ownership Boundary

| Request | Owner |
| --- | --- |
| Local formula, label, text block, or icon replaced by document PNG/JPG | This skill |
| Hand, arrow, circle, magnifier, or another hotspot pointer | `auto-cut-pointer-targeting` |
| Full-screen PPT state, page advance, or animation timing | `auto-cut-animation-timing-revision` |
| B-roll, replacement video, or whole video window | `auto-cut-revision-draft` plus the relevant video workflow |

Always use `auto-cut-revision-draft` for review-driven editable delivery and `auto-cut-final-acceptance` before final reporting. Use the latest source-ledger read as canonical. For Feishu/Lark documents, use `lark-doc` to fetch the relevant section and download original media.

Ordinary document-provided local images are job-scoped. This skill does not require `subject_profile_receipt`, does not read `project-bindings.json`, and must not infer or create a subject profile. Route genuine pointer assets to the pointer skill instead.

## Hard Gates

1. Bind each stable source item to its exact `source_text` and document block identity.
2. Preserve nested images and every contiguous image after the item up to the next independent review checkbox. One item may produce multiple `overlay_units` but still has exactly one marker.
3. Hash and decode every original asset. Run `scripts/inspect_image_asset.py` and use `asset_visible_bbox`; never size a transparent PNG from its padded canvas.
4. Locate every `source_target_bbox` from source frames. Match visible text/formula height and, for formulas, baseline/fraction/subscript geometry while preserving aspect ratio.
5. Inventory `protected_regions`. Complete coverage, canvas containment, and zero unintended overlap must all pass.
6. Put a clean layer below a transparent replacement whenever old pixels remain visible. A static cover is valid only for a proven static background; moving content requires source-synchronous cleanup media.
7. Treat review timestamps as search hints. Scan the actual state transition, split on PPT page changes, and map the accepted `source_window` to the saved `timeline_window`.
8. Validate the saved root and active timeline content plus opened-JianYing first/middle/last evidence. Planning coordinates or a self-declared pass are insufficient.

If target localization, scale, coverage, protected-region clearance, or lifecycle evidence is unresolved, generate a preview and report the item as unresolved. Do not stretch, silently shrink, guess a center position, or cover unrelated content.

## Editable Draft Contract

- Keep source video and existing main-track split boundaries.
- Keep every original PNG/JPG in draft metadata; keep derived assets separately hash-bound to the original.
- Use independent editable image segments for each `overlay_unit`.
- Inventory `clean_layers` even when empty and keep saved order `replacement > clean > source`.
- Write exactly one source-faithful marker per stable item ID; multiple overlay units share it.
- Patch and validate both root `draft_content.json` and `Timelines/<main_timeline_id>/draft_content.json`.
- Never deliver only `Final Video` / `Final Audio` or one full-length flattened replacement.

Run the strict revision gate before reporting completion:

```powershell
python scripts/jy_wrapper.py revision-validate --request-json "<request.json>" --doc-items-json "<doc_items.json>" --strict --json
```

## Common Mistakes

- Scaling from the padded PNG canvas instead of `asset_visible_bbox`.
- Treating a review timestamp as an exact in/out boundary without frame scanning.
- Putting a transparent formula over old glyphs without a clean layer.
- Extending one still overlay across a PPT page or animation state change.
- Requiring a subject pointer binding for ordinary job-scoped formulas or labels.
- Flattening several local replacements into one full-length preview track.

## Reporting

Report the editable draft name and absolute path, item-to-asset mapping, original and visible asset bounds, target and protected regions, scale/baseline rule, source/timeline windows, cleanup representation, overlay/marker tracks, opened-draft evidence, unresolved items, and any smallest local baked fallback.
