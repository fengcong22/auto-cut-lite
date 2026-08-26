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
- Execute spoken deletion only with ASR-resolved boundaries, declared delete/must-keep phrases,
  and reverse validation. Place its verbatim label at the final ASR-resolved cut start; the
  review-document timestamp is only a search hint.
- Insert supplied pointer or picture files on `Lite Visual Assets` at the requested start using
  JianYing default geometry and no keyframes.
- Keep animation/picture-timing, text-position/animation, and existing-hand cleanup requests
  label-only.
- Keep `Lite Timing Adjusted` empty. Ignore clean-cover/cleanup asset specs.
- Do not require profile binding, target landing, lifecycle, opened-editor, or screenshot evidence.

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
