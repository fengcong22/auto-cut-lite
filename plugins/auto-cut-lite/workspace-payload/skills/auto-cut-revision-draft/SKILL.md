---
name: auto-cut-revision-draft
description: Use when revising an editable JianYing/CapCut draft from review comments while preserving source media, audio, cut structure, verbatim review labels, and the fixed Auto-Cut Lite visual behavior.
---

# Auto-Cut Lite Revision Draft

Read the
[Lite execution contract](../auto-cut-lite/references/lite-execution-contract.md) before any
focused visual skill. It overrides full-workflow visual instructions and acceptance gates.

## Workflow

1. Read the current source review document as the user's own identity and preserve one stable ID
   and verbatim source text per item.
2. Compile `workflow_mode=lite` with explicit source-ledger coverage.
3. Resolve spoken deletions from word/character ASR. Treat review timestamps as search hints,
   protect declared `must_keep` phrases, run reverse validation, and start the verbatim label at
   the final ASR-resolved cut start rather than the review timestamp.
4. Insert only supplied local pointer/image assets, at their requested starts, with default
   geometry and no keyframes.
5. Keep animation/picture-timing, text-position/animation, and existing-hand cleanup items
   label-only.
6. Save the editable split-gap draft with the unchanged source duration and isolated marker
   families.
7. Validate root and active timeline content, source coverage, audio evidence, asset start
   alignment, default visual geometry, forbidden Lite outputs, and package integrity when
   delivery was requested.

Do not load full pointer-targeting, profile-onboarding, animation-retiming, local-image precision,
or safe-zone execution rules. Do not require target geometry, profile binding, pointer lifecycle,
hotspot landing, cleanup layers, animation evidence, or opened-draft calibration.

Use the installed runtime identified by `deployment-report.json`. Keep execution offline unless
the user explicitly requests export; ordinary Lite validation does not launch JianYing.
