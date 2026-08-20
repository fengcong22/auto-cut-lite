---
name: auto-cut-high-school-history-lite
description: Use when the user explicitly asks for the High School History Auto-Cut lite or compact workflow. Produces a non-destructive editable JianYing draft with fixed trace tracks, verbatim two-second review labels, simple local overlays, unchanged source duration, and no destructive delete, animation, or automatic scaling.
---

# High School History Auto-Cut Lite

Use this as the named compact entrypoint for `stage=高中`, `subject=历史`. Set
`workflow_mode=lite` in every revision request and hand off to the canonical `auto-cut` router.
Do not fork or copy the full Auto-Cut implementation.

## Full-Capability Quality Engine

- `lite` changes the editable timeline/output policy only. It must not weaken
  source ASR, word/character boundary resolution, reverse-ASR, subject-pointer
  binding, pointer placement/lifecycle evidence, visual attribution, or final
  acceptance gates owned by the canonical full workflow.
- Run the canonical full-workflow preflight before opening JianYing whenever a
  compiled request enables audio, visual, pointer, pause, source-coverage, or
  final-acceptance gates. A missing or stale receipt fails before draft writes.
- After saving, run canonical acceptance independently against the root draft
  and every active timeline variant. Lite track validation is necessary but is
  never a substitute for item-level full-capability acceptance.
- Preserve the lite prohibitions on destructive deletion, generated animation,
  keyframes, and automatic scaling. Full capability means full precision and
  evidence, not silently changing the lite editing contract.

## Lite Draft Contract

- The default `lite_cut_layout` is `split_gap`. In this layout, delete windows are real
  editable cut boundaries while the project duration stays unchanged: V1 (`Original Video`)
  and A1 (`Separated Source Audio`) contain only the non-delete intervals; V2 (`Lite Cut
  Segments`) and A2 (`Lite Reused Audio`) contain the source-aligned delete intervals. This is
  written directly to the JianYing timeline and does not require opening the editor. Save
  `config.maintrack_adsorb=false` in root and active-timeline content so JianYing does not
  reinterpret V1 gaps through its default magnetic-main-track state. Linked-edit is a local
  JianYing toolbar preference (`ToolbarCfg.linkageEnable`), not a portable per-draft field;
  verify or disable it in the local application configuration when an opened-draft check is
  required, and never claim that a draft JSON alone controls it on another computer.
- Feishu/Lark timestamps are search hints only, exactly as in the full workflow. Never copy a
  review timestamp directly into a spoken-word cut. Resolve every final boundary from Chinese
  word/character ASR, bind the resolved window to the edit, declare the delete phrase, adjacent
  `must_keep` phrases and strategy, then run reverse validation. Lite draft generation fails
  closed when that boundary receipt is absent or does not match the saved cut.
- For source ASR, use the same local path as the full workflow: extract audio from the local source
  video when no separate source audio is present, then submit it through the bundled
  `volc.bigasr.auc` adapter. Do not ask the user to create TOS storage, a bucket, a signed URL,
  or storage credentials.
- Set `lite_cut_layout=copy` only for compatibility with older lite drafts that intentionally
  keep a full V1/A1 and use V2 copies as a manual reference lane.

- Import local video, audio, images, text, subtitles, flower text, and local BGM through the
  maintained repository APIs.
- In the default split-gap layout, write the non-delete source intervals to `Original Video`
  and `Separated Source Audio`, and write every merged delete interval to `Lite Cut Segments`
  and `Lite Reused Audio` at the same timeline/source time. Preserve the original range and
  total project duration.
- Lite execution applies ASR-resolved delete cuts and supplied visual/pointer assets only. A
  pointer request is always execution-required and cannot be downgraded to a marker-only item.
  Animation or other picture-timing requests are label-only and do not create `Lite Timing
  Adjusted` segments.
- Review labels are all retained verbatim. Their text backgrounds are color-coded by kind:
  spoken delete (red), visual delete (rose), pointer/visual addition (blue), animation timing
  (amber), and review-only (gray).
- Put downloaded pointers and other local visual assets on `Lite Visual Assets`. Use their
  imported size and transform without animation, keyframes, automatic scaling, or target
  optimization. Clamp the requested duration to the source project duration.
- Copy timing-adjusted source ranges to `Lite Timing Adjusted` at the requested target time.
  Never remove the original range.
- Keep V1/V2/V3/V4 visible together in JianYing preview.
- Keep A1 and A2 source-aligned in split-gap layout. The delete lane's audio is placed on A2
  even when the old `reuse_audio` flag is false; that flag only controls the legacy `copy`
  layout's optional audio copies. A2 deleted-source clips keep normal volume (`1.0`) for manual
  review; do not silently mute them because their segmented-audio role is `reference`.
- Write exactly one review label per source item. The text must equal the current source
  review text verbatim, the start must equal the edit start, and the duration is `2s` except
  when clamped at the unchanged source-video end.
- Place review labels in the existing top safe-band review-marker layout.
- Save `workflow_mode=lite` in execution reports and keep this mode for later revisions unless
  the user explicitly requests the full-capability workflow.

## Required Acceptance

Before delivery, validate the saved root and active timeline variants:

- project duration equals the source video duration;
- the fixed lite video and A1 tracks exist and remain editable;
- In split-gap layout, A2 exists when at least one delete window intersects the source audio;
  in copy layout, A2 follows the legacy `reuse_audio` rule;
- root and active-timeline content both save `config.maintrack_adsorb=false`, and every A2
  deleted-source segment has normal volume;
- copied source and target ranges stay inside the unchanged project duration;
- review labels are verbatim, start-aligned, top-safe, and no longer than `2s`;
- lite visual assets contain no generated animation, keyframes, or automatic scale adjustment.
- every spoken delete has a passing word/character ASR boundary receipt; the review timestamp is
  recorded only as `search_hint`, and audio precision/reverse-ASR gates remain enabled. The
  receipt binds source-audio SHA-256, provider/model or resource, adapter version, ordered
  matched word/character rows, and `authoritative_cut_boundary=true`; fallback candidates cannot
  be promoted to cuts merely because their rough time or transcript text looks plausible;
- every pointer item has a real editable overlay plus the normal pointer binding, lifecycle,
  placement, and visual-evidence gates. A label by itself is a failure.

Report split-gap delete requests as editable cut boundaries with the deleted source intervals
isolated on V2/A2; the source project duration is intentionally unchanged for secondary manual
editing.
