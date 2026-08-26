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
- source media, editable tracks, split-gap cut structure, separated/reused audio, and unchanged
  project duration satisfy the Lite draft contract;
- every source review item has one start-aligned verbatim marker; a spoken-delete marker is
  aligned to the final ASR-resolved cut start, not the rough review timestamp;
- spoken deletions retain ASR boundary, delete/must-keep, reverse-ASR, and semantic-join evidence;
- supplied pointer/image items have a saved local material and editable segment at the requested
  start, with default geometry and no keyframes;
- animation/picture-timing, text-position/animation, and cleanup-only pointer items are
  `execution_required=false` and represented by their labels;
- no `Lite Timing Adjusted`, animation overlay, clean-cover/cleanup layer, pointer movement,
  transformed visual, or visual keyframe was produced;
- portable delivery, when requested, passes CRC, isolated extraction, tree-hash, relink-tool,
  local-material, and external-receipt checks.

Do not require profile binding, target/hotspot geometry, pointer lifecycle, cleanup evidence,
animation stable-frame evidence, opened JianYing screenshots, or target-open proof for ordinary
Lite acceptance. Do not fail a correct label-only Lite item merely because it has no execution
evidence.

Report draft path, item coverage, audio result, executed supplied assets, label-only visual items,
forbidden-output checks, root/active-timeline status, and ZIP/receipt hashes when applicable.
