---
name: auto-cut-music-library-bgm
description: Use when selecting, replacing, or validating BGM for JianYing/剪映 editable drafts from the JianYing music library. Trigger for requests about matching BGM to a new video's visuals, mood, pacing, oral-video rhythm, product category, or ad style, especially when the BGM must come from 剪映音乐库 and must not fall back to arbitrary local audio files.
---

# JianYing Music-Library BGM

Use this repository skill whenever a draft needs BGM chosen or replaced from the JianYing music library while preserving an editable timeline.

## Core Rule

BGM is selected per project. Do not reuse a fixed track unless the user explicitly asks for that exact song.

Match the music to:

- video subject and product category
- picture mood and color style
- oral pacing and cut density
- selling-point intensity
- desired platform feel, such as e-commerce, education, tech, brand, lifestyle, or promo

## Source Rules

- Search and match from JianYing music library first.
- Use repository tools such as:

```powershell
python scripts/jy_wrapper.py search-assets <query> --category cloud_music --json
```

- If the cloud-music CSV URL is expired, look for the same music ID in JianYing client cache metadata, such as `Cache/ressdk_db/**/rp.db`, and use the current `preview_url`.
- A downloaded file is acceptable only when it is downloaded from a JianYing music-library entry and the saved material keeps the matching `music_id`.
- Do not substitute arbitrary repo-local or machine-local audio just because it exists and opens.
- If no suitable JianYing music-library track can be resolved, fail the build and report the reason.

## Matching Heuristics

For short oral-video ads, prefer:

- clean groove, funk, retail, light tech, upbeat corporate, or shopping rhythm
- steady energy that supports speech instead of competing with it
- short intro or immediately usable beat
- midrange that leaves voice clear

Avoid:

- cinematic trailer swells
- heavy trap or aggressive club tracks
- emotional piano unless the video is explicitly sentimental
- cute ukulele unless the product tone calls for it
- any track that dominates the speaker

Useful search themes:

- `funk`
- `groove`
- `shopping`
- `tech`
- `upbeat`
- `corporate`
- `retail`
- `e-commerce`

## Editable Draft Requirements

- Keep BGM as a separate editable audio track, usually named `BGM`.
- Do not bake BGM into the source voice or final mixed audio.
- Preserve the target timeline duration and fade-out behavior.
- Set BGM loudness under the source voice; for dense oral video, it should usually sit low and supportive.
- Interpret requests like `BGM 到 -30dB` as an adjusted peak target in `dBFS`, not as directly setting the JianYing segment volume slider to `-30dB`.
- To satisfy a peak target, measure the selected BGM asset's actual peak over the used timeline window, then set segment gain from `target_peak_dbfs - source_peak_dbfs`; validate the saved draft by confirming `source_peak_dbfs + gain_db == target_peak_dbfs` within tolerance.
- When saving a library BGM via a local downloaded cache file, patch the saved audio material so:
  - `id` equals the JianYing music ID
  - `music_id` equals the JianYing music ID
  - `type` is `music`
  - `name` is the JianYing music-library title
  - the BGM segment `material_id` equals that same music ID
  - `draft_virtual_store.json` includes the music ID when that file is present or needs to be created

## Validation Gate

Before reporting completion, inspect the saved draft and fail if:

- the BGM track is missing
- BGM is not a separate editable segment
- the BGM material uses a placeholder path such as `cloud_music_<id>.mp3`
- the BGM material is a generic local fallback with no JianYing `music_id`
- the BGM path does not exist when a local cache path is used
- the selected song does not match the video style or user brief
- the saved BGM segment gain does not produce the requested adjusted peak target when combined with the measured BGM source peak

For scripts, add a specific guardrail test when possible:

- local fallback BGM must be rejected
- known JianYing music-library IDs must be accepted
- saved BGM material and BGM segment must share the same music ID
- BGM peak-target requests must be validated against measured source peak plus saved segment gain, not against slider value alone

## Reporting

When reporting a completed BGM change, include:

- draft name and absolute path
- chosen music title and music ID
- why it fits this project
- confirmation that BGM remains a separate editable track
- measured source peak, saved segment gain, and adjusted peak when the user requested a target dB value
- confirmation that no arbitrary local fallback audio was used
