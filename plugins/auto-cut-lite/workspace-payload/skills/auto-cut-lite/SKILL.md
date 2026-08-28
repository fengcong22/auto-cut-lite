---
name: auto-cut-lite
description: Use for the packaged Auto-Cut Lite compact workflow, including editable JianYing review drafts, fixed trace tracks, source-text-exact two-second labels, simple supplied local overlays, duration-preserving cuts, ASR-first timing with review-time fallback, and portable ZIP delivery.
---

# Auto-Cut Lite

Read [the Lite execution contract](references/lite-execution-contract.md) before compiling,
executing, or validating a review item. Read
[the portable runtime contract](references/portable-runtime.md) before running commands.

## Fixed Workflow

- Keep `workflow_mode=lite` and default `lite_cut_layout=split_gap`.
- Reject `lite_cut_layout=copy` for new execution; it is historical read/validation compatibility
  only.
- Preserve editable source/cut/audio structure. A precise spoken deletion is represented only by a
  source-aligned, non-destructive editable split/cut trace; it never removes source media or
  compresses the timeline. Final duration always equals source duration.
- Every physical duration-changing operation is label-only in Lite. This includes the physical
  removal that a full workflow might perform for an ASR-located spoken deletion, plus pause
  changes, `+Ns`, `-Ns`, speed changes, holds, still frames, and `semantic_pause_adjustment`.
  For spoken deletion, ASR still resolves the logical boundary and the split trace is
  execution-required; no media is removed. For all other items, use a uniquely resolved ASR point
  when available, otherwise the time written in the review comment. Keep one exact `source_text` label. Do not create `pause_adjustments`,
  holds, still frames, audio gaps, duration changes, or later-track offsets.
- Keep exactly one two-second review label per source item, clamped at final project end. Its
  visible text must equal only `source_text` code-point-for-code-point.
- Keep `execution_status`, `label_only_unresolved`, item IDs, warnings, and diagnostics only in
  internal metadata, receipts, validation output, and reports; never put them in a visible label.
- Every issue locatable through speech or audio must attempt authoritative word/character ASR
  before routing. A uniquely located spoken deletion may create only a non-destructive logical
  split boundary and never changes duration. Place a non-executing item's verbatim label at its
  unique ASR point, or at the review-comment time when ASR cannot locate it.
- Review-only and ASR-unresolved items use the review timestamp. If the comment says
  `07:14 ... 提前到 07:12`, use the target `07:12`. Accept a point timestamp for the label and
  never fall back to `0:00` for unresolved timing.
- Every newly encountered or unrecognized issue is label-only by default. If it has a reliable
  start but no safe maintained implementation, leave one original-text label, record
  `execution_status=label_only_unresolved` internally, skip that execution, and continue
  independent items. If ASR cannot locate it, use the review-comment time. Fail before draft
  writing only when neither source supplies a valid time; never fall back to `0:00`.
- Insert supplied pointer or picture files on `Lite Visual Assets` at the requested start using
  JianYing default geometry and no keyframes.
- When one hand/pointer row omitted its attachment, reuse an attachment only from another row with
  the same normalized modification name and only when the candidate is unique. Multiple candidates
  return structured `user_action_required`; never guess.
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
ASR-first/review-time-fallback labels, strict acceptance result, and delivery ZIP/receipt when requested. Before
non-mock execution, validate the deployment report,
package-manifest anchor, and managed runtime inventory. Runtime drift is a pre-execution hard stop
that requires redeployment from a verified package; never hot-patch it or use another checkout.
