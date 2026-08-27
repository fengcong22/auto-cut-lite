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
- every source review item has one start-aligned verbatim marker; an audio-identifiable item first
  attempts ASR, then a non-executing item falls back to its review timestamp when ASR fails or is
  non-unique;
- no unresolved item falls back to `0:00`;
- spoken deletions retain ASR boundary, delete/must-keep, reverse-ASR, and semantic-join evidence;
- pause additions, extensions, shortenings, and adjustments remain `execution_required=false`,
  use a unique ASR point or review-time fallback for their labels, and create no executable edit;
- A2 contains exactly one independent, audible source-aligned clip per logical ASR/V2 delete
  window, with no pending/empty plan and no full-length, continuous, muted, or cross-item-merged
  A2 segment;
- visible marker text equals only `source_text`; internal execution status such as
  `label_only_unresolved` is kept in receipts/reports and never appears in the label;
- supplied pointer/image items have a saved local material and editable segment at the requested
  start, with default geometry and no keyframes;
- a missing hand/pointer attachment is reused only from a unique row with the same normalized
  modification name; multiple candidates return structured `user_action_required`;
- animation/picture-timing, text-position/animation, and cleanup-only pointer items are
  `execution_required=false` and represented by their labels;
- no `Lite Timing Adjusted`, animation overlay, clean-cover/cleanup layer, pointer movement,
  transformed visual, or visual keyframe was produced;
- no `pause_adjustments`, hold, still-frame pause segment, inserted audio gap, pause-derived
  duration change, or later-track offset was produced;
- portable delivery, when requested, passes CRC, isolated extraction, tree-hash, relink-tool,
  local-material, and external-receipt checks.
- `job_state.json` and `job_timing.json` show every fixed `review-document-run` phase complete or
  validly resumed, with no failed/skipped phase, stale digest, corrupt receipt, or unresolved
  executable item.

Do not require profile binding, target/hotspot geometry, pointer lifecycle, cleanup evidence,
animation stable-frame evidence, opened JianYing screenshots, or target-open proof for ordinary
Lite acceptance. Do not fail a correct label-only Lite item merely because it has no execution
evidence.

Report draft path, item coverage, audio result, executed supplied assets, label-only visual and
pause items, source-equals-final duration, forbidden-output checks, root/active-timeline status,
and ZIP/receipt hashes when applicable.
