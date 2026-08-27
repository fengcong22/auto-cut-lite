---
name: auto-cut-final-acceptance
description: Use when validating an Auto-Cut Lite revision, complete project delivery, or migratable package across editable structure, source-item coverage, audio safeguards, fixed Lite visual behavior, local materials, and package integrity.
---

# Auto-Cut Lite Final Acceptance

Read the
[Lite execution contract](../auto-cut-lite/references/lite-execution-contract.md) and
[Lite checklist](references/checklist.md). The Lite contract overrides full-workflow visual,
pointer, and animation acceptance.

## Required Gates

- the saved draft and declared active timeline exist and agree;
- source media, editable tracks, split-gap cut structure, separated/reused audio, and the Lite
  duration contract satisfy the draft contract: final duration always equals source duration;
- every source review item has one start-aligned verbatim marker; every audio-identifiable item
  is aligned to its final ASR-resolved window or point, not the rough review timestamp; only a
  completely non-speech item may use the review timestamp or requested target;
- no unresolved item falls back to `0:00`;
- spoken deletions retain ASR boundary, delete/must-keep, reverse-ASR, and semantic-join evidence;
- pause additions, extensions, shortenings, and adjustments retain authoritative source-ASR
  identity and boundary evidence for their labels, remain `execution_required=false`, and create
  no executable pause edit;
- A2 contains exactly one independent, audible source-aligned clip per logical ASR/V2 delete
  window, with no pending/empty plan and no full-length, continuous, muted, or cross-item-merged
  A2 segment;
- visible marker text equals only `source_text`; internal execution status such as
  `label_only_unresolved` is kept in receipts/reports and never appears in the label;
- supplied pointer/image items have a saved local material and editable segment at the requested
  start, with default geometry and no keyframes;
- animation/picture-timing, text-position/animation, and cleanup-only pointer items are
  `execution_required=false` and represented by their labels;
- no `Lite Timing Adjusted`, animation overlay, clean-cover/cleanup layer, pointer movement,
  transformed visual, or visual keyframe was produced;
- no `pause_adjustments`, hold, still-frame pause segment, inserted audio gap, pause-derived
  duration change, or later-track offset was produced;
- portable delivery, when requested, passes CRC, isolated extraction, tree-hash, relink-tool,
  local-material, and external-receipt checks.

Do not require profile binding, target/hotspot geometry, pointer lifecycle, cleanup evidence,
animation stable-frame evidence, opened JianYing screenshots, or target-open proof for ordinary
Lite acceptance. Do not fail a correct label-only Lite item merely because it has no execution
evidence.

Report draft path, item coverage, audio result, executed supplied assets, label-only visual and
pause items, source-equals-final duration, forbidden-output checks, root/active-timeline status,
and ZIP/receipt hashes when applicable.
