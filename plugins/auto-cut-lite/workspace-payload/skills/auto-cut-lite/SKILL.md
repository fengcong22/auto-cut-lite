---
name: auto-cut-lite
description: Use for the packaged Auto-Cut Lite compact workflow, including editable JianYing review drafts, fixed trace tracks, verbatim two-second labels, simple supplied local overlays, unchanged source duration, and portable ZIP delivery.
---

# Auto-Cut Lite

Read [the Lite execution contract](references/lite-execution-contract.md) before compiling,
executing, or validating a review item. Read
[the portable runtime contract](references/portable-runtime.md) before running commands.

## Fixed Workflow

- Keep `workflow_mode=lite` and default `lite_cut_layout=split_gap`.
- Preserve the unchanged project duration and editable source/cut/audio structure.
- Keep exactly one verbatim two-second review label per source item, clamped at project end.
- Every issue locatable through speech or audio must use authoritative word/character ASR before
  execution or label placement. This includes spoken deletion, semantic pauses, pronunciation,
  breath, mouth noise, and speech timing. Place its verbatim label at the final ASR-resolved
  window or point; the review-document timestamp is only a search hint. Missing ASR fails closed.
- Only completely non-speech items use the review timestamp. If the comment says
  `07:14 ... 提前到 07:12`, use the target `07:12`. Accept a point timestamp for the label and
  never fall back to `0:00` for unresolved timing.
- Insert supplied pointer or picture files on `Lite Visual Assets` at the requested start using
  JianYing default geometry and no keyframes.
- Keep animation/picture-timing, text-position/animation, and existing-hand cleanup requests
  label-only.
- Keep `Lite Timing Adjusted` empty. Ignore clean-cover/cleanup asset specs.
- Do not require profile binding, target landing, lifecycle, opened-editor, or screenshot evidence.
- In `split_gap`, A1 contains the kept windows and A2 contains exactly one independent,
  source-aligned clip per merged ASR delete window. Reject pending/empty segmented plans,
  full-length A2 clips, and any A2 window that differs from V2.

## Execution

Build the canonical request and ledger through the maintained runtime. For strict editable draft
execution use:

```powershell
python scripts/jy_wrapper.py revision-run `
  --request-json "<revision_request.json>" `
  --doc-items-json "<doc_items.json>" `
  --drafts-root "<JianYing drafts root>" `
  --workflow-mode lite `
  --strict `
  --json
```

When the user requests a complete portable delivery, add
`--package-zip "<output>\\<name>.zip"`. A complete Lite delivery ends only after strict saved
draft validation, ZIP CRC validation, isolated extraction, tree-hash comparison, and an external
receipt. Packaging stays offline and must not open JianYing or rewrite draft JSON.

Report the draft path, review-item coverage, editable structure, visual insertions versus
label-only items, strict acceptance result, and delivery ZIP/receipt when requested.
