---
name: auto-cut-lite
description: Use for the packaged Auto-Cut Lite compact workflow, including editable JianYing review drafts, fixed trace tracks, source-text-exact two-second labels, simple supplied local overlays, duration-preserving cuts, ASR-located pause labels, and portable ZIP delivery.
---

# Auto-Cut Lite

Read [the Lite execution contract](references/lite-execution-contract.md) before compiling,
executing, or validating a review item. Read
[the portable runtime contract](references/portable-runtime.md) before running commands.

## Fixed Workflow

- Keep `workflow_mode=lite` and default `lite_cut_layout=split_gap`.
- Reject `lite_cut_layout=copy` for new execution; it is historical read/validation compatibility
  only.
- Preserve editable source/cut/audio structure. Deletion does not compress the timeline, and final
  duration always equals source duration.
- Treat every request to add, extend, shorten, or otherwise adjust a pause, including `+Ns`,
  `-Ns`, and `semantic_pause_adjustment`, as label-only. Use word/character ASR to resolve its
  authoritative point, then keep one exact `source_text` label there. Do not create
  `pause_adjustments`, holds, still frames, audio gaps, duration changes, or later-track offsets.
- Keep exactly one two-second review label per source item, clamped at final project end. Its
  visible text must equal only `source_text` code-point-for-code-point.
- Keep `execution_status`, `label_only_unresolved`, item IDs, warnings, and diagnostics only in
  internal metadata, receipts, validation output, and reports; never put them in a visible label.
- Every issue locatable through speech or audio must use authoritative word/character ASR before
  executable routing or label placement. This includes spoken deletion, pause requests,
  pronunciation, breath, mouth noise, and speech timing. Place its verbatim label at the final
  ASR-resolved window or point; the review-document timestamp is only a search hint. Only spoken
  deletion or another explicitly maintained non-pause operation may execute. Missing ASR fails
  closed.
- Only completely non-speech items use the review timestamp. If the comment says
  `07:14 ... 提前到 07:12`, use the target `07:12`. Accept a point timestamp for the label and
  never fall back to `0:00` for unresolved timing.
- If a newly encountered issue has a reliable authoritative start but no safe maintained
  implementation, leave one original-text label, record
  `execution_status=label_only_unresolved` internally, skip that execution, and continue
  independent items. If timing is unreliable, or an audio-identifiable issue lacks authoritative
  ASR, fail before opening or writing the draft.
- Insert supplied pointer or picture files on `Lite Visual Assets` at the requested start using
  JianYing default geometry and no keyframes.
- Keep animation/picture-timing, text-position/animation, and existing-hand cleanup requests
  label-only.
- Keep `Lite Timing Adjusted` empty. Ignore clean-cover/cleanup asset specs.
- Do not require profile binding, target landing, lifecycle, opened-editor, or screenshot evidence.
- In `split_gap`, A1 contains the kept windows and A2 contains exactly one independent, audible,
  source-aligned clip per logical ASR delete window. Merge overlaps only within one source item;
  adjacent different-item windows stay separate and true cross-item overlap fails before writing.
  Reject pending/empty segmented plans, full-length or cross-item-merged A2, muted A2, and any A2
  source or source-aligned target window that differs from V2.

## Execution

For a natural-language source-document job, use the maintained public runner:

```powershell
python scripts/jy_wrapper.py review-document-run `
  --snapshot-json "<document_snapshot.json>" `
  --project-json "<project.json>" `
  --job-root "<workspace>\tmp\<job-name>" `
  --drafts-root "<JianYing drafts root>" `
  --package-zip "<output>\<name>.zip" `
  --json
```

It owns compilation, strict cache identities, ASR, editable draft writing, final acceptance,
packaging, and phase recovery. Run the same command again after interruption. A completed phase
resumes only while its digest and receipt-bound artifacts still validate.

When a maintained caller already has the canonical request and ledger, the low-level strict
editable-draft command remains:

```powershell
python scripts/jy_wrapper.py revision-run `
  --request-json "<revision_request.json>" `
  --doc-items-json "<doc_items.json>" `
  --drafts-root "<JianYing drafts root>" `
  --workflow-mode lite `
  --strict `
  --json
```

For the low-level command, add `--package-zip "<output>\\<name>.zip"` when the user requests a
complete portable delivery. The public runner already requires that path. A complete Lite delivery
ends only after strict saved
draft validation, ZIP CRC validation, isolated extraction, tree-hash comparison, and an external
receipt. Packaging stays offline and must not open JianYing or rewrite draft JSON.

Report the draft path, review-item coverage, editable structure, visual insertions versus
label-only items, confirmation that final duration equals source duration, pause requests kept as
ASR-located labels, strict acceptance result, and delivery ZIP/receipt when requested. Before
non-mock execution, validate the deployment report,
package-manifest anchor, and managed runtime inventory. Runtime drift is a pre-execution hard stop
that requires redeployment from a verified package; never hot-patch it or use another checkout.
