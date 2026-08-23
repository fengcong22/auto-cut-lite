---
name: auto-cut-review-audio-precision
description: Use for review-document spoken-word deletion and audio repair with ASR-resolved Chinese word or character boundaries.
---

# Review Audio Precision

Use this companion skill when a document requests spoken-word deletion, pause cleanup or a phrase correction.

For every item, record `item_id`, `source_role`, `source_time_range`, `timeline_time_range`, `delete`, `must_keep`, `strategy` and `validation`. Treat document timestamps as search hints. Resolve the physical cut from the local source audio through the bundled word/character ASR adapter, bind the result to the source-audio SHA-256 and adapter receipt, then run reverse ASR to confirm the deleted phrase is absent and every `must_keep` phrase remains.

For a replacement or supplemental clip, resolve the local clip clock before ASR matching. Never reuse a main-video timestamp as a supplemental-clip timestamp. Preserve a post-delete pause only when the saved pause evidence and visual hold check support it.
