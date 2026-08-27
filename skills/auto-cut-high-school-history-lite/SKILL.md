---
name: auto-cut-high-school-history-lite
description: Use when the user explicitly asks for the High School History Auto-Cut lite or compact workflow. Produces a non-destructive editable JianYing draft with fixed trace tracks, source-text-exact two-second review labels, simple local overlays, duration-preserving cuts, ASR-located pause labels, and no destructive animation or automatic scaling.
---

# High School History Auto-Cut Lite

Use this as the named compact entrypoint for `stage=高中`, `subject=历史`. Set
`workflow_mode=lite` in every revision request and hand off to the canonical `auto-cut` router.
Do not fork or copy the full Auto-Cut implementation.

## Repository Identity Gate

Before any task input is read, compiled, or executed, resolve the execution repository and report
`repository`, `branch`, and `workflow_mode`. In the current development checkout, require:

- `repository=E:/codex/Auto-cut-高中历史/worktrees/auto-cut-lite`
- `branch=feature/auto-cut-lite`
- `workflow_mode=lite`

The first repository command for every lite task must be
`python scripts/assert_lite_workspace.py`, run from that repository. Require exit code zero and
the JSON fields `ok=true`, `repository=auto-cut-lite`, `branch=feature/auto-cut-lite`, and
`workflow_mode=lite`, then report those fields before continuing. Read the repository's
`AGENTS.md` and use its scripts and runtime files for the complete task. If the identity does not
match, stop before task execution; never search for or fall back to `Auto-Cut-v1.7.0`. Both the
nested and top-level 1.7.0 directories are read-only comparison sources for lite work. Do not
reuse task intermediates produced by a mistaken full-version run.

## Lite Acceptance Scope

- `lite` keeps the editable timeline and audio/marker safeguards that are useful
  for this compact workflow, but it deliberately does **not** use the full
  visual/pointer evidence contract.
- For every supplied visual or pointer asset, the only visual acceptance rules
  are: the local material is saved as an editable segment and its timeline start
  equals the edit's requested start (within the normal timing tolerance).
- Lite does not inspect or require scale, position, transform, rotation, alpha,
  target geometry, fingertip landing, occlusion, recorded-pointer lifetime,
  clean-cover/cleanup layers, subject-pointer binding receipts, or opened-state
  screenshots. Strict mode and explicit full-workflow visual flags do not turn
  these checks back on for `workflow_mode=lite`.
- Every issue locatable through speech or audio uses the Lite workflow's authoritative
  word/character ASR boundary and reverse-audio checks where applicable. Spoken deletion remains
  executable. Every request to add, extend, shorten, or otherwise adjust a pause is label-only;
  pronunciation, breath, mouth noise, and other unsupported audio issues follow the maintained
  execution routing. Review labels and the editable cut structure remain required.
- A visible JianYing label contains only the source item's exact `source_text`. Internal status,
  item IDs, warnings, and diagnostics never appear in visible label text.

## Lite Draft Contract

- The only executable layout for new Lite tasks is `split_gap`. In this layout, delete windows are
  real editable cut boundaries and deletion never shortens the project: V1 (`Original Video`)
  and A1 (`Separated Source Audio`) contain only the non-delete intervals; V2 (`Lite Cut
  Segments`) and A2 (`Lite Reused Audio`) contain the source-aligned delete intervals. This is
  written directly to the JianYing timeline and does not require opening the editor. Save
  `config.maintrack_adsorb=false` in root and active-timeline content so JianYing does not
  reinterpret V1 gaps through its default magnetic-main-track state. Linked-edit is a local
  JianYing toolbar preference (`ToolbarCfg.linkageEnable`), not a portable per-draft field;
  verify or disable it in the local application configuration when an opened-draft check is
  required, and never claim that a draft JSON alone controls it on another computer.
- Feishu/Lark review documents must be read as the current operator's own user identity. Use
  `lark-cli docs +fetch --as user`; on a new computer configure `lark-cli config default-as user`
  and `lark-cli config strict-mode user` after the one-time user login. Never use an app/bot
  identity for document reads, copy another computer's login state, or include tokens in a
  plugin/package. Feishu/Lark timestamps are search hints only, exactly as in the full workflow. Never copy a
  review timestamp directly into a spoken-word cut. Resolve every final boundary from Chinese
  word/character ASR, bind the resolved window to the edit, declare the delete phrase, adjacent
  `must_keep` phrases and strategy, then run reverse validation. Start that review item's label
  at the final ASR-resolved cut start rather than the rough review timestamp. Lite draft generation fails
  closed when that boundary receipt is absent or does not match the saved cut.
- Apply the same timing-source rule to every other audio-identifiable problem. For any request to
  add, extend, shorten, or otherwise adjust a pause, resolve the real adjacent utterance gap from
  source ASR and use that boundary only for its verbatim label; never create a pause edit. For
  pronunciation, breath, mouth noise, or other speech/audio timing, use the matching ASR
  word/character window or point. Never execute or label these items from the review timestamp
  alone.
- Only completely non-speech review items use the timestamp written in the review comment. Parse
  the requested target when the text contains both a current time and a move target: for example,
  `07:14 ... 提前到 07:12` labels at `07:12`. A point timestamp does not require a fabricated
  end time. Missing or unresolved timing must fail before writing, never fall back to `0:00`.
- For source ASR, use the same local path as the full workflow: extract audio from the local source
  video when no separate source audio is present, then submit it through the bundled
  `volc.bigasr.auc` adapter. Do not ask the user to create TOS storage, a bucket, a signed URL,
  or storage credentials.
- Recognize `lite_cut_layout=copy` only while reading or validating an older Lite draft that
  intentionally kept a full V1/A1 reference layout. Reject `copy` for every new execution.

- Final project duration always equals source duration. Treat `+Ns`, `-Ns`, pause extension,
  pause shortening, and every `semantic_pause_adjustment` as non-executing review items. Resolve
  the authoritative point with word/character ASR, place exactly one `source_text` label there,
  and do not create `pause_adjustments`, holds, still frames, gaps, or timeline offsets.

- Import local video, audio, images, text, subtitles, flower text, and local BGM through the
  maintained repository APIs.
- In the default split-gap layout, write the non-delete source intervals to `Original Video`
  and `Separated Source Audio`, and write every logical delete interval (after same-item overlap
  merging only) to `Lite Cut Segments` and `Lite Reused Audio` at the same source and target time.
  Preserve the full source range; deletion and pause-review items do not change duration.
- Lite execution applies ASR-resolved delete cuts and supplied visual/pointer assets only. Every
  pause addition, extension, shortening, or adjustment is label-only. A
  supplied pointer insertion is execution-required, but an existing-hand occlusion, removal,
  clean-cover, cleanup, or residual-cover request is label-only. Animation or other
  picture-timing requests are label-only and do not create `Lite Timing Adjusted` segments.
- Review labels are all retained verbatim. Lite uses three isolated marker track families rather
  than the full workflow's dynamic horizontal lanes:
  - `Review Marker Delete 1/2/...` contains spoken/delete/noise items; an overlap only adds
    another Delete lane.
  - `Review Marker Visual 1/2/...` contains pointer or other visual-material items and is green.
  - `Review Marker Animation 1/2/...` contains animation-timing items.
  The three families never share a track. Unknown review-only items remain visible in the Delete
  family as a safe three-family fallback.
- For every source-ledger item, render exactly and only `source_text` code-point-for-code-point.
  Keep `execution_status`, including `label_only_unresolved`, solely in internal metadata, marker
  receipts, validation output, and reports. Never prefix, suffix, or otherwise annotate visible
  label text with a status, item ID, warning, or diagnostic.
- If a new issue cannot be solved safely but its authoritative start is reliable, keep exactly one
  original-text label, set `execution_status=label_only_unresolved` internally, do not improvise an
  edit, and continue independent items. If timing is unreliable, or an audio-identifiable issue
  lacks authoritative ASR, fail before opening or writing the draft instead of guessing a label.
- Lite marker text is left-aligned (`alignment=0`), rendered at a 4–5 font-size range, and uses
  the full normalized safe width with a clamped background/transform so neither stage edge is
  crossed. These grouped lanes are the intentional lite exception to the full workflow's
  `Review Marker N` horizontal-lane contract; long marker text must force JianYing's line-width
  wrapping without changing the verbatim text. Delete/Visual/Animation families use stable
  red/green/amber colors (`#B42318`/`#15803D`/`#B45309`), and the saved lite validator checks
  these colors, wrapping, text alignment, font range, and both stage bounds.
- Put downloaded pointers and other local visual assets on `Lite Visual Assets`. Insert them
  with JianYing's default geometry; do not calibrate or optimize size, position, transform,
  occlusion, or target landing. Clamp the target duration to the source-length project duration.
- Keep `Lite Timing Adjusted` empty. Animation, page-turn, reveal, release, and other
  picture-timing requests retain only their verbatim review labels.
- Keep V1/V2/V3/V4 visible together in JianYing preview.
- Keep A1 and A2 source-aligned by source and target range in split-gap layout. The delete lane's
  audio is placed on A2
  even when the old `reuse_audio` flag is false; that flag only controls the legacy `copy`
  layout's optional audio copies. A2 deleted-source clips keep normal volume (`1.0`) for manual
  review; do not silently mute them because their segmented-audio role is `reference`.
- One A2 track may contain many clips, but it must contain exactly one independent, audible clip
  for each logical ASR delete window. Merge overlaps only within the same stable source item;
  adjacent windows from different items stay separate, and true cross-item overlap fails before
  draft writing for disambiguation. Each A2 source and target range must equal its matching V2
  window. Reject full-length/continuous/cross-item-merged A2 and pending or empty
  segmented plans before opening or writing the draft.
- Write exactly one review label per source item. The text must equal the current source
  review text verbatim. Its start equals the edit start for an executable item and the
  authoritative resolved start for a non-executing item; a pause item's start is its ASR-resolved
  point even though no pause edit is created. Duration is `2s` except when clamped at the
  source-length project end.
- Place grouped review labels in the existing top safe-band review-marker layout. Keep each
  marker aligned to its edit start and preserve the exact source text; do not re-enable the
  full-workflow horizontal compression or move a label into the picture body.
- Save `workflow_mode=lite` in execution reports and keep this mode for later revisions unless
  the user explicitly requests the full-capability workflow.

## Required Acceptance

Before delivery, validate the saved root and active timeline variants:

- project duration equals source duration, including when the review asks to add, extend,
  shorten, or otherwise adjust a pause;
- the fixed lite video and A1 tracks exist and remain editable;
- In split-gap layout, A2 exists when at least one delete window intersects the source audio;
  `copy` is not executable for a new task;
- root and active-timeline content both save `config.maintrack_adsorb=false`, and every A2
  deleted-source segment has normal volume;
- root and active-timeline A2 segment count and windows exactly equal the logical ASR delete
  windows; no A2 segment spans the full source, merges different items, or overlaps an unrelated
  A1 interval;
- source and target ranges stay inside source duration; no pause request creates an offset for a
  later track, asset, or label;
- no executable `pause_adjustments`, hold, still-frame pause segment, inserted audio gap, or
  pause-derived duration extension exists;
- review labels equal only `source_text`, are mapped-start-aligned, top-safe, and no longer than
  `2s`; internal execution status never appears in their text;
- lite visual assets contain no generated animation, keyframes, or automatic scale adjustment.
- every spoken delete has a passing word/character ASR boundary receipt; the review timestamp is
  recorded only as `search_hint`, and audio precision/reverse-ASR gates remain enabled. The
  receipt binds source-audio SHA-256, provider/model or resource, adapter version, ordered
  matched word/character rows, and `authoritative_cut_boundary=true`; fallback candidates cannot
  be promoted to cuts merely because their rough time or transcript text looks plausible;
- every visual/pointer item has a real editable overlay whose material is saved and whose
  timeline start equals the edit start. No pointer binding, lifecycle, placement, geometry,
  occlusion, clean-cover, or opened-state evidence is required in lite mode.

Report split-gap delete requests as editable cut boundaries with the deleted source intervals
isolated on V2/A2. Report that final duration stayed equal to source duration and list pause
requests as ASR-located, source-text-only labels rather than executed edits.

Before a non-mock installed-runtime execution, validate the deployment report, package-manifest
anchor, and managed runtime inventory. Any missing, changed, extra, or hash-mismatched managed
runtime file is drift: stop and require redeployment from a verified package. Do not hot-patch the
installed runtime or fall back to another checkout.

## Final Lite Delivery Boundary

For an explicit full lite workflow, the final acceptance result is not the saved draft directory;
the workflow ends only after a validated ZIP is published. Run the editable revision and package
as one unattended offline command after the canonical request and review-document evidence are
ready:

```powershell
python scripts/jy_wrapper.py revision-run `
  --request-json "<revision_request.json>" `
  --doc-items-json "<doc_items.json>" `
  --drafts-root "<JianYing drafts root>" `
  --workflow-mode lite `
  --strict `
  --package-zip "<Desktop>\\<final-name>.zip" `
  --json
```

`--package-zip` is accepted only for `workflow_mode=lite`. The runner writes and strictly
validates the editable draft first, then creates the ZIP in the background without launching
JianYing or waiting for a user action. The repository-bundled
`tools/relink_tool/Auto-Cut剪映素材重链工具.exe` is included automatically; if it is missing, the
package step fails rather than producing a misleading final delivery.

The ZIP contains the complete editable draft directory, the material relink tool, and
`使用说明.txt`. Before returning success, the packager rejects reparse points and unsafe ZIP
paths, checks CRCs, extracts to an isolated temporary directory, and compares the extracted tree
SHA-256 with the staged package tree. It never rewrites draft JSON or opens JianYing. A package
failure leaves the draft available for diagnosis but the lite workflow remains incomplete and
must not be reported as delivered. The machine-readable result records
`completion_boundary=lite_zip_delivery`, the ZIP SHA-256, and an external `.receipt.json`.
