---
name: auto-cut-revision-draft
description: Use when applying review comments to an existing JianYing/CapCut draft while preserving editable source media, cut structure and traceable markers.
---

# Editable Revision Draft

Keep source video, separated audio, replacement media and review markers independently editable. Use `workflow_mode=lite` for this plugin. A real delete must appear as split source intervals on the editable tracks; a marker-only review item must remain visibly distinguishable from an executed change.

Before writing a replacement item, compile its timebase. Main-video times are global. Supplemental-video times are local to the supplemental clip and must be mapped through a unique anchor or explicit return-to-main time. Unresolved mappings fail closed for that item.

After saving, validate both the root draft content and active timeline content. Confirm duration preservation, V1/A1/V2/A2 structure, ASR receipts, local material references and one verbatim marker per source item.
