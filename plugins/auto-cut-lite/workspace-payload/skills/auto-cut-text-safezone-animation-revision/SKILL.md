---
name: auto-cut-text-safezone-animation-revision
description: Use in Auto-Cut Lite for red-box or safe-zone text/sticker movement and flower-text intro/outro animation comments. Lite records these requests as verbatim labels only.
---

# Lite Text And Safe-Zone Labels

Read and follow the
[Lite execution contract](../auto-cut-lite/references/lite-execution-contract.md).

Text, sticker, subtitle, flower-text safe-zone movement and intro/outro/disappear animation
comments are label-only in Lite:

- set `execution_required=false`;
- preserve one two-second label whose visible text is exactly the source item's `source_text` at
  the requested source-aligned start; Lite pause requests never offset later labels;
- do not change text or sticker coordinates, transforms, scale, `anim_in`, `anim_out`, or any
  animation material;
- do not compensate for save-time drift or request opened-editor screenshots.

Strict Lite acceptance checks label coverage and confirms that prohibited position/animation
mutations were not introduced. It does not require visual or animation execution evidence.
