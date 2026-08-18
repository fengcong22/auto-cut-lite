---
name: auto-cut-high-school-history-lite
description: Use when the user explicitly asks for the High School History Auto-Cut lite or compact workflow. Produces a non-destructive editable JianYing draft with fixed trace tracks, verbatim two-second review labels, simple local overlays, unchanged source duration, and no destructive delete, animation, or automatic scaling.
---

# High School History Auto-Cut Lite

Use this as the named compact entrypoint for `stage=高中`, `subject=历史`. Set
`workflow_mode=lite` in every revision request and hand off to the canonical `auto-cut` router.
Do not fork or copy the full Auto-Cut implementation.

## Lite Draft Contract

- Import local video, audio, images, text, subtitles, flower text, and local BGM through the
  maintained repository APIs.
- Keep the complete source video on `Original Video` and separated source audio on
  `Separated Source Audio`.
- Convert every delete request into a source-range copy on `Lite Cut Segments`. Preserve the
  original range and total project duration.
- Put downloaded pointers and other local visual assets on `Lite Visual Assets`. Use their
  imported size and transform without animation, keyframes, automatic scaling, or target
  optimization. Clamp the requested duration to the source project duration.
- Copy timing-adjusted source ranges to `Lite Timing Adjusted` at the requested target time.
  Never remove the original range.
- Keep V1/V2/V3/V4 visible together in JianYing preview.
- Keep full source audio on A1. Add source-range audio to `Lite Reused Audio` only when the
  edit explicitly or implicitly reuses audio. `reuse_audio=false`, visual-only reuse, and
  still-frame reuse must not create an A2 segment.
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
- A2 exists only when at least one source range reuses audio;
- copied source and target ranges stay inside the unchanged project duration;
- review labels are verbatim, start-aligned, top-safe, and no longer than `2s`;
- lite visual assets contain no generated animation, keyframes, or automatic scale adjustment.

Do not claim that delete requests were executed as removals. Report them as cut boundaries and
trace-track copies for secondary manual editing.
