# Auto-Cut Lite Workspace Instructions

## Workspace Identity

- This directory is the deployed Auto-Cut Lite workspace.
- Keep `workflow_mode=lite` for every Auto-Cut request.
- Treat `.codex/skills` as deployment-managed runtime copies. Plugin upgrades refresh them.

## Runtime Discovery

- Read `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite\deployment-report.json` before execution.
- Require `deployment_status=installed`, a valid `plugin_manifest_path`, `runtime_root`, and
  `components.python.runtime_path`.
- Validate the deployment report, package-manifest anchor, and managed runtime inventory before
  every non-mock task. A missing, changed, extra, or hash-mismatched managed file is runtime drift.
- Runtime drift is a hard stop before task execution. Tell the user to redeploy from a verified
  package; do not hot-patch the installed runtime or fall back to another checkout.
- Run maintained commands from the reported `runtime_root` with the reported Python environment.
- Never fall back to a source checkout, another Auto-Cut installation, or system Python.

## Editing Contract

- Deliver editable JianYing projects unless the user explicitly requests a baked export.
- Preserve source media, separated or replacement audio, visible cut structure, and traceable
  review markers.
- The visible JianYing text for each source-ledger marker must equal only that item's `source_text`
  code-point-for-code-point. Keep item IDs, statuses, warnings, and diagnostics in internal
  metadata, receipts, validation output, and reports, never in visible label text.
- Validate the saved root and active timeline before reporting completion.
- Use the `auto-cut` router for natural-language or mixed requests and the focused `auto-cut-*`
  skill when the user names one.

## Lite Execution Precedence

- Before classifying or executing any visual review item, read
  `.codex/skills/auto-cut-lite/references/lite-execution-contract.md`.
- That contract overrides conflicting text in every router, focused skill, reference, checklist,
  example, inherited instruction, and directly named `auto-cut-*` skill in this workspace.
- Animation and other picture-timing requests are label-only. Never create a
  `Lite Timing Adjusted` segment or require animation execution evidence.
- Supplied hands, pointers, and local images are ordinary editable assets at their requested
  start with JianYing default geometry. Do not move, scale, rotate, keyframe, calibrate, target,
  or optimize them.
- Existing-hand occlusion, removal, clean-cover, cleanup, and residual-cover requests are
  label-only. Do not create cleanup media or layers.
- Do not start pointer-profile onboarding or require bindings, target geometry, lifecycle,
  hotspot, opened-editor, or screenshot evidence for a Lite editing request.
- Every issue that can be located through speech or audio retains authoritative ASR timing;
  executable spoken deletion also retains must-keep, reverse-validation, and editable-cut
  requirements. This includes spoken deletion, pause requests, pronunciation, breath, mouth
  noise, and speech timing. Review timestamps are search hints only. Spoken deletion uses the
  final word/character ASR-resolved boundary for execution and its label; every pause request uses
  that boundary only for a verbatim label and is never executed in Lite. Missing authoritative
  ASR fails before the draft is opened or written.
- Only completely non-speech items use review-comment timestamps. When a comment names an old
  time and a requested target such as `07:14 ... 提前到 07:12`, place the label at `07:12`.
  Point timestamps are valid label starts; unresolved timing must never fall back to `0:00`.
- If a new issue cannot be solved safely but has a reliable authoritative start, create exactly
  one visible label equal only to its `source_text`, record
  `execution_status=label_only_unresolved` only in internal metadata/receipts/reports, and continue
  independent items. Do not improvise an edit. If its time is unreliable, or it is
  audio-identifiable and lacks authoritative ASR, fail before opening or writing a draft.
- Lite deletion never compresses the timeline, and final duration always equals source duration.
  Every request to add, extend, shorten, or otherwise adjust a pause, including `+Ns`, `-Ns`, and
  `semantic_pause_adjustment`, is label-only at its ASR-resolved point. Do not create a
  `pause_adjustments` row, hold, still frame, audio gap, duration change, or offset for any later
  video, audio, visual, or label target.
- In `split_gap`, A2 is one track containing one independent, audible source-aligned clip per
  logical ASR delete window. Merge overlapping windows only within the same source item; keep
  adjacent windows from different items separate and fail true cross-item overlap before writing.
  Reject pending/empty plans, full-length or cross-item-merged A2, muted A2, and A2 windows that
  differ from their source-aligned V2 windows.
- `lite_cut_layout=copy` is historical read/validation compatibility only and must be rejected for
  every new Lite execution. New tasks use `split_gap`.

## Local Data

- Keep credentials, login state, JianYing account state, private media, project bindings, and
  pointer profiles on the target computer.
- The runtime integrity check permits only target-local asset caches to change: `data/*.local.csv`,
  `data/jy_cached_audio.csv`, refreshed packaged cloud indexes, and `assets/jy_sync/**`. Any
  change outside those cache paths is runtime drift and requires redeployment.
- Never copy target-local secrets or absolute source-machine paths into a plugin or delivery ZIP.
