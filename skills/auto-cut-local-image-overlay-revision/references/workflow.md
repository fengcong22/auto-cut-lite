# Local Image Overlay Workflow

Use this workflow after the main skill routes a review item to local PNG/JPG replacement.

## 1. Compile The Canonical Source Ledger

For natural-language source-document jobs, run the maintained `review-job-compile` pipeline. Treat the latest `doc_items` read as canonical and preserve `source_text` code-point-for-code-point in the one marker for each stable item ID.

For Feishu/Lark documents, use `lark-doc` to fetch the relevant review section with block IDs. Re-read automatically when an internal row lacks source text or image provenance. Recovery failure does not remove the item; mark it unresolved and keep the most complete unmodified source candidate.

## 2. Map Document Images

Build `asset_sources` from document structure:

1. Include images nested inside the current checkbox.
2. Include every contiguous image following the checkbox before the next independent review checkbox.
3. Preserve document order and image block/token identity.
4. Convert each image to an independent `overlay_unit`; multiple units share the item's one marker.
5. Reuse one image across different item IDs or windows only when the document text or a user confirmation says so. Generated alt text is not reuse authority.

Do not associate a nearby image by global list position when container or sibling structure disagrees.

## 3. Download And Inspect Original Assets

Download the original document media without recompression. Keep the original filename when safe, store runtime copies under ignored `tmp/`, and record document ID, block ID, media token, local path, and SHA-256.

Inspect each image:

```powershell
python skills/auto-cut-local-image-overlay-revision/scripts/inspect_image_asset.py "<image-path>"
```

Reject undecodable files, non-PNG/JPG bytes, and fully transparent PNGs. Copy the inspector receipt's `visible_bbox` into the overlay evidence's `asset_visible_bbox`; for JPG/JPEG this is the complete canvas. Copy its `sha256` into the source receipt's `original_sha256`. If crop, background removal, or another derived asset is required, preserve the original and record the transformation plus both hashes.

## 4. Find The Source State And Target

Treat the review timestamp as `search_hint`. Extract frames before, at, and after it until the old content's first and last relevant frames are known. For page changes or later review corrections, sample densely enough to identify the actual transition frame.

Locate one `source_target_bbox` per overlay unit using this priority:

1. Exact review text and unambiguous local description.
2. OCR/formula matching in the source frame.
3. Current-page layout and same-level visual geometry.
4. Hash-bound reference frame or manual preview confirmation.

Record the canvas dimensions, method, confidence, and evidence artifact. A target rectangle must be positive and fully inside the canvas. Low-confidence targets stop at preview.

## 5. Match Scale And Baseline

Preserve the image aspect ratio. Compute placement from visible content, not transparent padding.

- Normal text or label: match visible glyph height/line height and the target alignment edge.
- Formula: match visible formula height plus the source formula baseline, fraction-line height, and superscript/subscript extent.
- Icon or diagram: match the old visible target bounds or an explicit same-layout reference.

Never independently scale X and Y. Do not silently shrink a formula to solve a collision; unresolved scale/space conflicts require preview confirmation.

## 6. Protect Neighboring Content

Write positive in-canvas rectangles in `protected_regions` for subtitles, adjacent formulas/text, labels, diagrams, table lines, pointer assets, animation content, and safe margins. Require all of:

- the cleanup region contains all pixels that must disappear
- the replacement visible bounds remain inside the intended target region/canvas
- replacement and cleanup do not enter protected regions
- the saved transform and scale recompute the accepted target geometry

## 7. Clear Old Pixels Under Transparent Assets

Transparent formula/label PNGs usually expose the old wrong glyphs. Put a clean layer under the replacement:

```text
replacement image
clean layer
source video
```

Use a local still cover only after first/middle/last source samples prove the complete cleanup region is static. Keep the rectangle no larger than required plus a documented positive safety margin. When the source region moves or animates, use muted source-synchronous cleanup media covering the same `source_window`; a frozen frame is invalid.

Record every cleanup segment in `clean_layers`, including material path/hash, saved track/segment ID, target/source timerange, render index, and comparison exclusion region. Use an explicit empty array when no cleanup is required.

## 8. Derive Lifecycle Boundaries

Start no later than the first wrong frame that should be replaced. Release at the earliest verified boundary:

- the source becomes correct
- the PPT page or formula state changes
- the target leaves the frame
- the next independent animation or visual item begins
- the source segment ends

When one comment spans multiple pages, split it into multiple overlay units/windows while retaining the one source marker. Map every accepted `source_window` through the final timeline plan to `timeline_window`; compare that exact window with the saved image and cleanup segment timeranges.

## 9. Write An Editable Draft

Create a new version rather than overwriting the draft currently open in JianYing. Preserve source video, main-track cuts, separated/source audio, original image materials, derived materials, and review traces.

Use independent editable image segments for overlay units. Multiple images under one item may share a track but must retain separate segment/material identities and placement receipts. Patch both root and active timeline copies.

## 10. Validate Saved And Opened State

Validate the saved root and active timeline independently. Confirm material hashes, segment windows, scale/transform, render order, markers, and absence of undocumented extra replacement layers.

Capture opened-JianYing first/middle/last samples for each distinct visual state. Bind full-editor context, timeline ID, playhead time, canvas rectangle, artifact path, and SHA-256. Confirm old content is absent, exactly one intended replacement is visible, scale/baseline match, and no protected content is covered.

Run:

```powershell
python scripts/jy_wrapper.py revision-validate --request-json "<request.json>" --doc-items-json "<doc_items.json>" --strict --json
```

Do not claim final acceptance from a self-rendered preview alone.

## Junior-High Mathematics Regression Case

Use document `UXwddzEC1o9Aahx8uEecd2TBnnd` as a regression source, not as a subject profile:

- `19:41` has three contiguous formula PNGs under one review item; retain all three overlay units.
- Alpha-visible bounds differ materially from canvas dimensions: `467x92 -> 333x50`, `848x98 -> 770x52`, `223x111 -> 81x74`, and `1173x729 -> 1092x671`.
- `18:03` and `19:41` mention PPT page changes; split/release overlays by real state.
- The later `19:00` review says the picture changes at `19:01`; re-scan and correct the physical window rather than preserving the old duration.
- Route the `22:42` delayed green triangle/formula to animation timing and the `24:40-24:46` rerecorded clip to video replacement.
