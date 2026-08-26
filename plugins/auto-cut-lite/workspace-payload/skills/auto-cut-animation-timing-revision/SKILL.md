---
name: auto-cut-animation-timing-revision
description: Use in Auto-Cut Lite when review comments mention animation, page-turn, reveal, release, advance, delay, speed, or other picture timing. Lite records these requests as verbatim labels and does not execute animation changes.
---

# Lite Animation Timing Labels

Read and follow the
[Lite execution contract](../auto-cut-lite/references/lite-execution-contract.md). It overrides
full-workflow animation methods.

For every animation, page-turn, reveal, release, whole-section shift, speed, collision, or other
picture-timing comment:

- classify it as `animation_timing`;
- set `execution_required=false`;
- preserve the complete source text verbatim;
- place one label on `Review Marker Animation N` at the requested start for two seconds,
  clamped at project end;
- leave `Lite Timing Adjusted` empty.

Do not retime source clips, create overlays, capture stable frames, accelerate reveals, move a
whole section, inspect collisions, add keyframes, or request animation/opened-editor evidence.
Strict Lite acceptance passes this item from its source-ledger label and must not treat the
marker-only result as missing execution.
