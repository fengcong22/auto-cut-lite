---
name: auto-cut-editable-ad-revision
description: Use when revising JianYing or 剪映 editable drafts for short ad, promo, e-commerce, and oral-video projects while preserving an editable timeline. Trigger this skill for requests like “换个更合适的音乐”, “把花字/字幕移到红框位置”, “不要重叠”, “画面去黄提亮”, or “只保留最近 3 个草稿”, especially when BGM, flower text, subtitles, color, and draft retention must be handled together without flattening the project.
---

# JianYing Editable Ad Revision

## Overview

Use this skill for recurring JianYing ad-draft revision work where the user wants the project to stay editable after BGM swaps, flower-text moves, subtitle moves, overlap fixes, and portrait color tuning.

This rule set is repository-scoped. Keep it in this repository under `skills/auto-cut-editable-ad-revision/` so other users working in this repo inherit the same editing contract instead of relying on a machine-wide skill.

## Core Rules

- deliver a JianYing editable draft, not a baked export disguised as a draft
- preserve source video, separated/source audio, replacement audio when used, and visible split boundaries
- do not collapse a revision draft into only `Final Video` and `Final Audio` unless the user explicitly asks for a baked export
- bake only a local effect window when the requested effect cannot exist as a normal editable timeline operation
- default to no `修改` / `校对` marker tracks for this user unless they explicitly ask for markers
- keep only the latest 3 drafts in the same project family such as `C2597*`
- interpret audio requests like `-5dB` or `-18dB` as peak alignment to that `dBFS` target, not as setting the volume slider to a negative number
- reuse only materials the current JianYing account and machine can actually access; favor downloaded, cached, favorited, or locally resolvable membership assets

## When To Use

Use this skill when the user cares about one or more of these outcomes:

- the draft must remain editable after replacing BGM
- flower text or subtitles need to move based on a screenshot
- the user wants global overlap inspection instead of single-frame tweaking
- portrait footage needs yellow-cast reduction and gentle brightening
- a project family needs versioned drafts with automatic retention
- previously accepted changes must stay intact while one aspect is revised again

Do not use this skill for a one-shot baked export where the user does not need a reusable draft.

## Workflow

1. Start from the latest validated editable draft, not from an experimental or flattened branch.
2. Create a new versioned draft instead of destructively mutating the current best baseline.
3. Preserve all previously accepted changes unless the user explicitly asks to revert one.
4. Apply only the requested BGM, flower-text, subtitle, or color adjustments.
5. Sync both `draft_content.json` and `draft_meta_info.json` so materials remain visible and stable.
6. If the draft contains `Timelines/<main_timeline_id>/draft_content.json`, patch and validate that timeline copy too instead of touching only the root draft file.
7. If a shared helper rewrites flower text after save, sync that helper's global position constants first so the final pass does not snap flower text back to an older default height.
8. Validate structure before reporting success.
9. Keep only the latest 3 drafts in the project family.

## BGM Replacement

When the user asks to replace BGM from the JianYing music library:

- keep BGM as a separate editable music track
- do not flatten audio into a final mixed track
- search the local cloud-music index first through the Auto-Cut repo tools
- prefer tracks that fit short e-commerce oral videos: modern, rhythmic, clean, supportive, not too heavy, not too cinematic
- good search themes: `corporate`, `funk`, `shopping`, `tech`, `e-commerce`, `groove`
- favor tracks already cached or locally available when possible
- preserve the target duration of the BGM segment
- if replacing with a new song, start from the head unless the user asks otherwise

Useful command:

```powershell
python scripts/jy_wrapper.py search-assets <query> --category cloud_music --json
```

When a cloud-music URL fails locally but the draft can still reference the track:

- keep the editable cloud-music material shape
- keep `music_id` and the matching `materials.audios` entry
- ensure `draft_meta_info.json` still carries the referenced material entry

## Text Placement And Overlap

For screenshot-driven requests about flower text or subtitles:

- treat the red-box request as a lower-center safe zone over the torso/table area
- move only the requested text tracks there without changing unrelated styling
- keep flower text and subtitles visually separated
- check the whole timeline for overlaps, not only the shown frame

Use these heuristics:

- flower tracks are usually `FlowerTemplate` and `FlowerTemplate_2`
- subtitle track is usually `Subtitles`
- if two flower segments overlap in time, shift the later flower start so they never appear at the same time
- if a subtitle overlaps a flower during the same time window, keep the flower position and push only that subtitle segment lower
- after text moves, re-check all flower segments for any remaining same-screen overlap

## Color Tuning

For portrait oral-video color adjustments:

- remove yellow cast first, then raise brightness
- prefer native JianYing adjustments over approximate filter-only replacements when stable
- use `基础 > 色温` to move slightly toward blue for yellow correction
- use `曲线 > RGB曲线` for gentle luma lift
- keep skin smoothing and whitening subtle; the target is cleaner and brighter, not obvious beauty processing
- avoid stacking strong filters if native temperature and curves already solve the request

## Validation Gate

Before reporting completion, inspect the saved draft and fail the task if any of these are true:

- main video collapsed into one full-length segment for a multi-edit job
- draft contains only preview-style `Final Video` / `Final Audio`
- required source or replacement materials are missing from content or meta
- BGM is no longer a separate editable segment
- flower text still overlaps in the same time window
- the saved draft no longer exposes the edit process for second-stage manual editing

Check at minimum:

- `Main Video` segment count
- `Source Audio` segment count
- BGM track presence and segment count
- subtitle and flower track counts when touched
- absence of `Final Video` / `Final Audio`
- material references in both `draft_content.json` and `draft_meta_info.json`

## Local Tool Surface

Prefer these repository surfaces:

- `python scripts/jy_wrapper.py search-assets <query> --category cloud_music --json`
- `python scripts/draft_inspector.py summary --name "<DraftName>"`
- `python scripts/draft_inspector.py show --name "<DraftName>" --kind content --json`
- `python scripts/jy_wrapper.py list-segments --name "<DraftName>" --json`
- the project-specific rebuild or patch script when a job already has a maintained script under `tmp/` or the repo root

If a draft is stored in a runtime-encoded form, rebuild from the last clean editable generator path rather than patching opaque bytes directly.

## Reporting

When done, report:

- draft name and absolute path
- what changed
- whether marker tracks were written; for this user it is usually `none`
- whether any local windows had to be baked
- which 3 drafts were kept and which older family drafts were removed

If the request was about a visual issue from a screenshot, say whether the fix was global or only segment-specific.

## Distribution Rule

When sharing this repository with other users:

- keep this skill bundle inside the repository
- treat this repository copy as the authoritative version
- do not rely on a separate global Codex installation for the behavior contract
