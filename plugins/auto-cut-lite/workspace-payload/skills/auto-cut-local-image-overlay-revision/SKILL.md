---
name: auto-cut-local-image-overlay-revision
description: Use in Auto-Cut Lite when a review comment supplies a PNG/JPG to add or replace a local visual. Lite inserts it as an editable default-geometry asset without precise covering, scaling, or cleanup.
---

# Lite Local Image Overlay

Read and follow the
[Lite execution contract](../auto-cut-lite/references/lite-execution-contract.md).

For a supplied PNG/JPG:

- set `execution_required=true`;
- verify that the local file is readable;
- insert it on `Lite Visual Assets` at the requested start;
- use the requested duration, clamped at project end;
- preserve default scale, transform, rotation, and alpha;
- add no keyframes;
- keep one verbatim `Review Marker Visual N` label.

Do not match the original font/image size, crop to visible alpha bounds, calculate a protected
region, move or scale the asset, erase the old formula/text/icon, create a clean cover, or request
opened-editor evidence. If the file is unavailable, keep the label and report the missing
insertion without inventing a substitute.
