# API Reference

This document is the canonical API and CLI index for this skill.

## Python Wrapper (`JyProject`)

Bootstrap:

```python
import os
import sys

skill_root = os.getenv("JY_SKILL_ROOT", r"<SKILL_ROOT>")
sys.path.insert(0, os.path.join(skill_root, "scripts"))
from jy_wrapper import JyProject
```

### Constructor

```python
JyProject(
    project_name: str,
    width: int = 1920,
    height: int = 1080,
    drafts_root: str | None = None,
    overwrite: bool = True,
)
```

### Core Lifecycle

- `save()`: persist and patch draft content.
- `get_track_duration(track_name: str) -> int`: timeline end in microseconds.
- `find_segment_by_id(segment_id: str) -> object | None`: locate a live segment before save.
- `search_assets(query: str, category: str | None = None, limit: int = 20) -> list[dict]`
- `AssetIndex(...).list_categories() -> list[dict]`: list indexed categories with record counts and sources.
- `hold_lock(timeout: float | None = None, lock_root: str | None = None)`: async cross-process draft lock.
- `add_review_markers(...)` / `export_review_markers_manifest(...)`: add proofreading markers and export a markdown manifest for revision-driven work.

Saved-draft reopen behavior:

- `JyProject(..., overwrite=False)` now prefers a stable JSON patch session when upstream template loading fails.
- In that mode, `add_rich_text`, `add_sticker_simple`, `find_segment_by_id`, `add_mask_to_segment`, `add_segment_keyframes`, `add_filter_segment`, `attach_internal_material`, `add_complex_text`, and `save()` keep working against saved `draft_content.json`.
- For direct saved-draft editing and inspection, prefer the wrapper CLI and adapter tools backed by `DraftPatchSession`.

### Media APIs

- `add_media_safe(media_path, start_time=None, duration=None, track_name=None, source_start=0, **kwargs)`
- `add_audio_safe(media_path, start_time=None, duration=None, track_name="AudioTrack", **kwargs)`
- `add_image_simple(image_path, start_time=None, duration="3s", track_name="ImageTrack", *, scale_x=1.0, scale_y=1.0, transform_x=0.0, transform_y=0.0, rotation=0.0, alpha=1.0, background_blur=None)`
- `add_clip(media_path, source_start, duration, target_start=None, track_name="VideoTrack", **kwargs)`
- `add_cloud_media(query, start_time=None, duration=None, track_name=None)`
- `add_cloud_music(query, start_time=None, duration=None, name=None, duration_s=None)`

### Text APIs

- `add_text_simple(text, start_time=None, duration="3s", track_name="Subtitles", **kwargs)`
- `add_rich_text(text, start_time=None, duration="3s", track_name="Subtitles", *, text_color="#ffffff", border_color=None, font=None, font_size=5.0, bold=False, italic=False, underline=False, transform_x=0.0, transform_y=-0.8, scale_x=1.0, scale_y=1.0, keyword=None, keyword_color="#ff7100", keyword_font_size=None, keyword_border_color=None, shadow=None, text_effect=None, anim_in=None, anim_out=None, anim_loop=None, font_query=None, anim_in_query=None, anim_out_query=None, anim_loop_query=None, text_effect_query=None, return_resolved_assets=False)`
- `add_text_template(template_id=None, template_query=None, text=None, texts=None, start_time=None, duration="3s", track_name="TextTemplateTrack", template_payload=None, template_payload_path=None, return_resolved_asset=False)`
- `add_tts_intelligent(text, speaker="zh_male_huoli", start_time=None, track_name="VoiceOver")`
- `add_narrated_subtitles(text, speaker="zh_female_xiaopengyou", start_time=None, track_name="Subtitles")`

Notes:

- `add_text_simple(..., **kwargs)` supports `style`, `border`, `clip_settings`, `font`, `background`, `shadow`.
- `add_rich_text(...)` writes keyword highlight styles back into saved `draft_content.json`.
- `add_text_template(...)` writes official `text_template` materials into the draft and is intentionally different from `save-template` / `apply-template`.
- `add_text_template(...)` accepts either `text` or `texts`, plus one of `template_id`, `template_query`, `template_payload`, or `template_payload_path`.
- Full template expansion prefers `JY_TEXT_TEMPLATE_ADAPTER`. If no adapter is available, pass `template_payload` or `template_payload_path`.
- `transform_x` is pixel offset and is saved to JianYing half-canvas units (`x / 960` on a 1920x1080 canvas). `transform_y` in `add_rich_text` is the underlying clip transform ratio used by JianYing text clips.
- `add_image_simple` and `add_sticker_simple` treat `transform_x`/`transform_y` as pixel offsets and save them to JianYing half-canvas units (`x / 960`, `y / 540` on a 1920x1080 canvas).

### Draft Writing Primitives

- `add_sticker_simple(sticker_id, start_time=None, duration="3s", track_name="StickerTrack", scale=1.0, transform_x=0.0, transform_y=0.0, rotation=0.0, alpha=1.0)`
- `add_mask_to_segment(segment, mask_type, *, center_x=0.0, center_y=0.0, size=0.5, rotation=0.0, feather=0.0, invert=False, rect_width=None, round_corner=None)`
- `add_segment_keyframes(segment, keyframes)`
- `add_effect_segment(effect_name=None, start_time=None, duration="3s", track_name="EffectTrack", *, effect_category="scene", params=None, effect_query=None, return_resolved_asset=False)`
- `add_video_effect(effect_name=None, start_time=None, duration="3s", track_name="EffectTrack", *, params=None, effect_query=None, return_resolved_asset=False)`
- `add_face_effect(effect_name=None, start_time=None, duration="3s", track_name="EffectTrack", *, params=None, effect_query=None, return_resolved_asset=False)`
- `add_audio_effect_to_segment(segment, *, effect_name=None, effect_category="scene", effect_query=None, params=None, return_resolved_asset=False)`
- `add_audio_fade_to_segment(segment, *, fade_in=0, fade_out=0)`
- `add_filter_segment(filter_name=None, start_time=None, duration="3s", track_name="FilterTrack", intensity=100.0, filter_query=None, return_resolved_asset=False)`
- `attach_internal_material(segment, *, kind, material_name=None, duration=None, animation_role="in", query=None, return_resolved_asset=False)`
- `add_complex_text(text, start_time=None, duration="3s", track_name="Subtitles", *, bubble_resource_id=None, bubble_effect_id=None, flower_id=None, font=None, font_size=5.0, text_color="#ffffff", border_color=None, shadow=None, transform_x=0.0, transform_y=-0.8, scale_x=1.0, scale_y=1.0, text_effect=None, anim_in=None, anim_out=None, anim_loop=None, font_query=None, anim_in_query=None, anim_out_query=None, anim_loop_query=None, text_effect_query=None, bubble_query=None, flower_query=None, return_resolved_assets=False)`
- `add_green_screen_background(segment, *, background_path, background_query=None, chroma_color="#00ff00", chroma_strength=20, edge_feather=10, edge_cleanup=10, return_resolved_asset=False)`
- `add_review_markers(markers, *, track_names=None, x_positions=None)`
- `export_review_markers_manifest(output_path, markers)`

Protocol material notes:

- `add_image_simple(...)` creates a photo-backed visual segment and supports `background_blur=1..4`.
- `add_effect_segment(...)` supports both `scene` and `character` categories.
- `add_video_effect(...)` is the explicit scene-effect alias over `add_effect_segment(..., effect_category="scene")`.
- `add_face_effect(...)` is the explicit face-effect alias over `add_effect_segment(..., effect_category="character")`.
- `add_audio_effect_to_segment(...)` supports `scene`, `tone`, and `speech_to_song` categories.
- `add_audio_effect_to_segment(...)` supports `effect_query`. When `return_resolved_asset=True`, it returns `(segment_or_result, resolved_asset)`.
- `add_audio_fade_to_segment(...)` writes fade-in/fade-out settings and backing `audio_fade` material into the draft.
- `add_effect_segment(...)` supports `effect_query`. When `return_resolved_asset=True`, it returns `(segment, resolved_asset)`.
- `add_filter_segment(...)` writes a filter track segment and its backing filter material.
- `attach_internal_material(...)` currently supports `kind="transition"` and `kind="animation"`.
- `animation_role` supports `in`, `out`, `group`, and `loop`. Text segments reject `group`.
- `add_complex_text(...)` composes a rich text segment plus optional bubble (`text_shape`), flower (`text_effect`), and animation materials.
- `add_rich_text(...)` and `add_complex_text(...)` both support query-driven write parameters. When `return_resolved_assets=True`, they return `(segment, resolved_assets)`.
- `add_text_template(...)` uses the same JSON patch route in both saved-draft and live-save flows; it does not add a second vendor-side template implementation.
- `add_text_template(...)` supports `template_query`. When `return_resolved_asset=True`, it returns `(segment_or_result, resolved_asset)`.
- `set-text` remains compatible with text-template segments by rewriting the linked `materials.texts` entry referenced from `materials.text_templates[*].text_info_resources`.
- `add_filter_segment(...)` supports `filter_query`. When `return_resolved_asset=True`, it returns `(segment, resolved_asset)`.
- `attach_internal_material(...)` supports `query` for transition and animation lookup. When `return_resolved_asset=True`, it returns `(material_id, resolved_asset)`.
- In `add_complex_text(...)`, `flower_*` and `text_effect*` are mutually exclusive because they target the same underlying text effect slot.
- `add_green_screen_background(...)` attaches a `chroma` material to the target video segment and adds a separate background segment on a dedicated video track.
- `add_green_screen_background(...)` currently requires `background_path`; `background_query` is reserved and not implemented yet.
- `add_review_markers(...)` writes visible text markers onto dynamically allocated `Review Marker N` tracks. These are actual saved text tracks; allocation expands with overlap instead of reusing a fixed set of Chinese-named lanes.
- `export_review_markers_manifest(...)` writes the same markers to markdown for out-of-band review.

Review marker item shape:

```python
from core.review_marker_ops import ReviewMarkerItem

source_text = "00:10? Keep punctuation exactly.\nSecond source line."
markers = [
    ReviewMarkerItem(
        label=source_text,
        source_text=source_text,
        item_id="review-001",
        verbatim_status="verified",
        start_time="12.4s",
        duration="1.2s",
    ),
]
receipts = project.add_review_markers(markers)
project.export_review_markers_manifest("review_markers.md", receipts)
```

For source-ledger work, `label` and `source_text` must contain the same code-point-for-code-point value. Each returned receipt carries `item_id`, `source_text`, `verbatim_status`, `segment_id`, `material_id`, `track_name`, `start_time`, and `duration`; persist these receipts for saved root and active-timeline validation.

Keyframe item shape:

```python
[
    {"property": "position_x", "offset": "0.5s", "value": 0.2},
    {"property": "uniform_scale", "offset": 1500000, "value": 1.1},
]
```

Supported keyframe properties:

- `position_x`
- `position_y`
- `rotation`
- `scale_x`
- `scale_y`
- `uniform_scale`
- `alpha`
- `saturation`
- `contrast`
- `brightness`
- `volume`

### Search / Index APIs

`search_assets()` now uses a unified asset retrieval layer. It merges:

- vendor enums from `pyJianYingDraft`
- `mask`
- `text_animation`
- `font`
- `filter`
- `transition`
- `video_intro_animation`
- `video_outro_animation`
- `video_effect`
- `video_character_effect`
- `audio_effect`
- `tone_effect`
- `speech_to_song`
- `sticker`

- CSV-backed libraries under `data/`
  - `cloud_music`
  - `cloud_sound_effect`
  - `cloud_text_style`
  - `cloud_video_asset`
  - `tts_speaker`
  - `cached_audio`

- reference metadata under `references/README.md`
  - `sticker`
  - `bubble_text`
  - `flower_text`
  - `text_template`

Every result uses one normalized shape:

```python
{
    "category": "mask",
    "identifier": "circle",
    "name": "圆形",
    "title": "",
    "resource_id": "6791700663249146381",
    "effect_id": "636075",
    "enum_name": "圆形",
    "enum_type": "MaskType",
    "description": "",
    "source": "vendor_enum",
    "source_file": "MaskType",
    "tags": ["圆形"],
    "metadata": {},
    "write_field": "mask_type",
    "write_value": "circle",
    "score": 226,
}
```

Write-ready fields:

- `write_field`: the CLI/API parameter name that a downstream write path expects
- `write_value`: the canonical value to pass to that parameter

This is the bridge that lets Codex go from search results to direct mutation without hand-picking ids.

Supported category filters:

- `mask`
- `font`
- `filter`
- `transition`
- `text_animation`
- `text_outro_animation`
- `text_loop_animation`
- `video_intro_animation`
- `video_outro_animation`
- `video_effect`
- `video_character_effect`
- `sticker`
- `bubble_text`
- `flower_text`
- `text_template`
- `cloud_music`
- `cloud_sound_effect`
- `cloud_text_style`
- `cloud_video_asset`
- `tts_speaker`
- `cached_audio`

Example:

```python
project = JyProject("SearchDemo", overwrite=True)
print(project.search_assets("circle", category="mask", limit=5))
print(project.search_assets("1980", category="filter", limit=5))
print(project.search_assets("小孩", category="tts_speaker", limit=5))
```

Category introspection:

```python
from utils.asset_index import AssetIndex

index = AssetIndex(skill_root=skill_root)
print(index.list_categories())
```

Best-match resolution:

```python
match = project.resolve_asset("枫叶", category="sticker")
print(match["write_field"], match["write_value"])
```

### Lock API

`hold_lock()` is an async cross-process lock backed by a filesystem lock directory:

```python
async with project.hold_lock(timeout=1.0):
    project.add_text_simple("serialized write")
    project.save()
```

You can also override the lock root:

```python
async with project.hold_lock(timeout=1.0, lock_root=r"D:\tmp\jy-locks"):
    project.save()
```

### Saved-Draft Inspection / Patch Surfaces

These second-batch capabilities are exposed through the CLI and adapter layer on top of the stable JSON patch session:

- `list-tracks`: list track ids, names, types, segment counts, and durations
- `list-texts`: list text segments with resolved caption text
- `show-segment`: show one segment with `_track_*` and `_material` context
- `shift-all`: move segment start times in bulk, optionally filtered by `track_type`
- `batch`: apply a JSON array of patch operations
- `cut`: keep only one timeline range and rebase the project to `0`

Third-batch single-segment patch capabilities:

- `set-text`: rewrite one text segment's material content
- `shift`: move one segment start time by an offset
- `trim`: rewrite one segment's `source_timerange` and derived target duration
- `speed`: rewrite one segment's `speed` and source duration
- `volume`: rewrite one segment's `volume`
- `opacity`: rewrite one segment clip's `alpha`

Current `batch` operations:

- `{"cmd":"set-text","segment_id":"...","text":"..."}`
- `{"cmd":"shift-all","offset":"2s","track_type":"text"}`
- `{"cmd":"shift","segment_id":"...","offset":"-0.5s"}`
- `{"cmd":"trim","segment_id":"...","start_time":"1s","duration":"2.5s"}`
- `{"cmd":"speed","segment_id":"...","multiplier":1.25}`
- `{"cmd":"volume","segment_id":"...","level":0.5}`
- `{"cmd":"opacity","segment_id":"...","alpha":0.6}`

## CLI Contract

Common `--json` output:

```json
{
  "ok": true,
  "code": "ok",
  "reason": "",
  "data": {}
}
```

Exception: `apply-zoom --json` is the recording events JSON path kept for recorder compatibility, so that command prints human-readable output plus `DRAFT_PATH=<path>`.

Exit codes:

- `0`: success
- `1`: runtime or infrastructure failure
- `2`: invalid input or missing target draft/segment

## CLI Additions

### Inspect Draft Structure

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py list-tracks --name "DraftName" --json
python <SKILL_ROOT>/scripts/jy_wrapper.py list-texts --name "DraftName" --json
python <SKILL_ROOT>/scripts/jy_wrapper.py show-segment --name "DraftName" --segment-id "<segment-id>" --json
```

### Patch Saved Drafts

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py shift-all --name "DraftName" --offset 1s --track-type text --json
python <SKILL_ROOT>/scripts/jy_wrapper.py batch --name "DraftName" --ops-json "[{\"cmd\":\"set-text\",\"segment_id\":\"<segment-id>\",\"text\":\"Updated\"}]" --json
python <SKILL_ROOT>/scripts/jy_wrapper.py cut --name "DraftName" --start-time 1s --end-time 5s --json
```

### Patch One Segment

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py set-text --name "DraftName" --segment-id "<segment-id>" --text "Updated" --json
python <SKILL_ROOT>/scripts/jy_wrapper.py set-text --name "DraftName" --segment-id "<segment-id>" --texts-json "[\"Title\",\"Subtitle\"]" --json
python <SKILL_ROOT>/scripts/jy_wrapper.py shift --name "DraftName" --segment-id "<segment-id>" --offset 2s --json
python <SKILL_ROOT>/scripts/jy_wrapper.py trim --name "DraftName" --segment-id "<segment-id>" --start-time 1s --duration 1.5s --json
python <SKILL_ROOT>/scripts/jy_wrapper.py speed --name "DraftName" --segment-id "<segment-id>" --multiplier 1.5 --json
python <SKILL_ROOT>/scripts/jy_wrapper.py volume --name "DraftName" --segment-id "<segment-id>" --level 0.25 --json
python <SKILL_ROOT>/scripts/jy_wrapper.py opacity --name "DraftName" --segment-id "<segment-id>" --alpha 0.4 --json
python <SKILL_ROOT>/scripts/jy_wrapper.py add-audio-effect --name "DraftName" --segment-id "<audio-segment-id>" --effect-query "8bit" --effect-category scene --json
python <SKILL_ROOT>/scripts/jy_wrapper.py add-audio-fade --name "DraftName" --segment-id "<audio-segment-id>" --fade-in 0.2s --fade-out 0.4s --json
```

`set-text` notes:

- For plain text segments, use `--text`.
- For text-template segments with multiple text slots, use `--texts-json`.
- `--text` and `--texts-json` are mutually exclusive.

### Add Official Text Template

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-text-template \
  --name "DraftName" \
  --template-query "Sample Title Template" \
  --text "Headline" \
  --start-time 0s \
  --duration 3s \
  --json

python <SKILL_ROOT>/scripts/jy_wrapper.py add-text-template \
  --name "DraftName" \
  --template-payload-path "C:\\templates\\title-template-payload.json" \
  --texts-json "[\"Headline\"]" \
  --start-time 0s \
  --duration 3s \
  --json
```

Notes:

- `add-text-template` is the official text-template resource path. It is not the same thing as `apply-template`.
- Pass either `--text` or `--texts-json`.
- Pass at least one of `--template-id`, `--template-query`, or `--template-payload-path`.
- If `JY_TEXT_TEMPLATE_ADAPTER` is not available locally, `--template-id` / `--template-query` alone is not enough; provide `--template-payload-path`.
- This repository also ships an example adapter at `scripts/text_template_adapter_example.py`. Set `JY_TEXT_TEMPLATE_ADAPTER` to that script path to validate the adapter flow locally.

## Target Capability, Volcengine ASR, And TTS CLI

The target-machine first-use surface is machine-readable:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py status --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-config
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-status --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py tts-status --json
```

`status --json` reports target-owned first-use status only. It does not replace
`tmp/install/install-report.json`, which records dependency, Chromium,
FFmpeg/FFprobe, audio-doctor, and JianYing installation evidence.
`volc-config` asks for one authentication mode and writes either the target
user's masked `VOLC_ASR_API_KEY`, or `VOLC_ASR_APP_ID` plus masked
`VOLC_ASR_ACCESS_TOKEN`, together with `VOLC_ASR_RESOURCE_ID`, to the ignored
local `.env`. Switching modes clears the other mode's fields.
`volc-status --json` reports only whether those required values are present; its
`service_probe` remains `not_run`, and secret values are never returned.
Configuration presence is therefore not proof of authorization or a successful
ASR request. `tts-status --json` reports SAMI and Edge TTS boundaries separately;
an import, JianYing identity, or client installation is not proof of a successful
target-network voice request.

The bundled adapter supports two mutually exclusive authentication modes for
the same v3 API: new-console `X-Api-Key`, or legacy-console `X-Api-App-Key` plus
`X-Api-Access-Key`. Obtain the API key from the
[new console](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default),
or obtain legacy values after creating an application and enabling
recording-file ASR in the
[legacy speech console](https://console.volcengine.com/speech/app). See the
[legacy quickstart](https://www.volcengine.com/docs/6561/163043),
[credential FAQ](https://www.volcengine.com/docs/6561/196768), and current
[HTTP API reference](https://www.volcengine.com/docs/6561/1354868). Model 1.0
uses `volc.bigasr.auc`; model 2.0 uses `volc.seedasr.auc`. Both modes still send
the selected resource ID. The default is not entitlement evidence, credentials
are never mixed, and all configured service endpoints must be HTTPS.

The maintained Volcengine adapter is bundled at
`scripts/audio/volc_word_align.py`:

```powershell
.\.venv\Scripts\python.exe scripts/audio/volc_word_align.py ".\media\voice.wav" `
  --output "tmp\volc\voice.words.json" `
  --phrase "delete phrase" `
  --anchor 12.0,18.0
```

Useful options are `--env`, `--output`, optional `--raw-output`, repeatable
`--phrase`, `--anchor START,END`, and the timeout/poll controls. Keep normalized
and raw reports under ignored `tmp/` paths; do not add service results or local
credentials to a release.

The normalized success object includes:

```json
{
  "provider": "volc_asr",
  "resource_id": "volc.seedasr.auc",
  "adapter_version": "auto-cut-volc-asr-v4",
  "input_sha256": "<sha256-of-exact-input-bytes>",
  "service_job_id": "<provider-request-id>",
  "service_result_sha256": "<sha256-of-canonical-service-result>",
  "words": [
    {"text": "word", "start": 0.0, "end": 0.24}
  ],
  "word_timing_count": 1
}
```

The adapter rejects a result unless all five attribution fields are present and
valid. Word timing must be non-empty; every `start` and `end` must be finite;
`start` must be non-negative; every interval must have positive duration
(`end > start`); and starts and ends must be monotonic across the complete word
list. An empty, missing, non-finite, zero/negative, or nonmonotonic timing row
fails the command. Missing target authorization, a service error, timeout, or
invalid evidence also exits nonzero and must never be reported as successful
alignment.

## Runtime Environment

The wrapper now has a runtime environment probe for local JianYing/CapCut installs.

What it detects:

- installed app root
- detected app version
- executable path
- `Resources/DefaultAdjustBundle/combine_adjust`
- drafts root
- whether a live JianYing process/window is currently discoverable

Detection priority:

1. running process path
2. Windows uninstall registry
3. local app-data install roots
4. standard install directory scan

New drafts and patch-mode saves now re-stamp:

- `draft_content.json -> platform.app_version`
- `draft_content.json -> last_modified_platform.app_version`
- `draft_meta_info.json -> draft_name`
- `draft_meta_info.json -> draft_root_path`
- `draft_meta_info.json -> draft_fold_path`

## `scripts/jy_wrapper.py` CLI

### Detect Runtime Environment

```bash
python scripts/jy_wrapper.py detect-env --json
```

Example response fields:

```json
{
  "environment": {
    "found": true,
    "app_name": "JianyingPro",
    "app_version": "10.5.0.13988",
    "install_root": "F:/剪映/JianyingPro/10.5.0.13988",
    "combine_adjust_path": "F:/剪映/JianyingPro/10.5.0.13988/Resources/DefaultAdjustBundle/combine_adjust",
    "drafts_root": "C:/Users/<user>/AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft",
    "process_running": true,
    "ui_automation_available": true
  }
}
```

### Smoke Test Against Local Runtime

```bash
python scripts/jy_wrapper.py smoke-test --name CodexSmoke --json
```

What it does:

1. detects the local JianYing runtime
2. creates a tiny draft
3. saves it through the normal wrapper path
4. verifies runtime version/meta stamping
5. probes the live JianYing window through `JianyingController`

Notes:

- `--cleanup` removes the temporary smoke-test draft after verification
- this validates attachability to the live app window, but it does not promise zero compatibility issues across all JianYing releases
- the repository is still Windows-local and JianYing-oriented; version sensitivity has been reduced, not eliminated

### One-Shot Self Check

For another user or another machine, the recommended first command is:

```bash
python scripts/jy_wrapper.py self-check --cleanup --json
```

This combines:

1. environment detection
2. resource-path resolution
3. real draft write
4. live app probe

It returns:

- `usable`: whether the machine is ready for normal draft-writing use
- `summary.status`: `ready` or `blocked`
- `checks.*`: per-step evidence

If `usable=true`, this repo is ready to use on that machine for normal local draft writing.

### Recording to Smart-Zoom Draft

The recorder UI can call this command directly after recording. It creates a new draft, imports the recording video, applies smart zoom from the captured event JSON, and prints `DRAFT_PATH=` in human-readable mode for UI parsing.

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py apply-zoom \
  --name "RecordingDraft" \
  --video "C:\recordings\demo.mp4" \
  --json "C:\recordings\demo_events.json" \
  --scale 150
```

Notes:

- `--json` is the recording events JSON path for this command, not the output-mode flag.
- In human-readable mode the command prints `DRAFT_PATH=<path>` after the result payload.

### Search Indexed Assets

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py search-assets "circle" --category mask --json
python <SKILL_ROOT>/scripts/jy_wrapper.py search-assets "typewriter" --category text_animation --json
python <SKILL_ROOT>/scripts/jy_wrapper.py search-assets "1980" --category filter --json
python <SKILL_ROOT>/scripts/jy_wrapper.py search-assets --list-categories --json
python <SKILL_ROOT>/scripts/jy_wrapper.py resolve-asset "枫叶" --category sticker --json
python <SKILL_ROOT>/scripts/jy_wrapper.py resolve-asset "文轩体" --category font --json
python <SKILL_ROOT>/scripts/jy_wrapper.py resolve-asset "VHS" --category text_loop_animation --json
python <SKILL_ROOT>/scripts/jy_wrapper.py resolve-asset "6838834573413978631" --category bubble_text --json
python <SKILL_ROOT>/scripts/asset_search.py --list --json
```

### List Segment IDs in a Saved Draft

Use this before applying a mask or keyframes to an existing saved segment.

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py list-segments --name "DraftName" --json
```

### Inspect a Saved Draft

Use this when the agent needs a quick project overview before mutating the draft.

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py draft-info --name "DraftName" --json
python <SKILL_ROOT>/scripts/jy_wrapper.py list-materials --name "DraftName" --json
python <SKILL_ROOT>/scripts/jy_wrapper.py list-materials --name "DraftName" --type texts --json
python <SKILL_ROOT>/scripts/jy_wrapper.py show-material --name "DraftName" --material-id "<material-id>" --json
```

These commands expose:

- draft duration / fps / canvas
- track and segment counts
- material bucket counts
- one material by id for patch-oriented inspection

### Add a Sticker

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-sticker \
  --name "DraftName" \
  --sticker-id "7405878923323641129" \
  --start-time 0s \
  --duration 2s \
  --scale 1.1 \
  --transform-x 120 \
  --transform-y -80 \
  --json

python <SKILL_ROOT>/scripts/jy_wrapper.py add-sticker \
  --name "DraftName" \
  --sticker-query "枫叶" \
  --start-time 0s \
  --duration 2s \
  --json
```

### Add an Image

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-image \
  --name "DraftName" \
  --image-path "C:\\assets\\cover.png" \
  --start-time 0s \
  --duration 2s \
  --background-blur 2 \
  --json
```

### Add a Mask to an Existing Segment

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-mask \
  --name "DraftName" \
  --segment-id "<segment-id>" \
  --mask-type circle \
  --center-x 10 \
  --center-y -20 \
  --size 0.3 \
  --feather 20 \
  --invert \
  --json

python <SKILL_ROOT>/scripts/jy_wrapper.py add-mask \
  --name "DraftName" \
  --segment-id "<segment-id>" \
  --mask-query circle \
  --json
```

Supported `--mask-type` values:

- `line`
- `mirror`
- `circle`
- `rectangle`
- `heart`
- `star`

### Add Rich Text / Keyword Highlight

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-rich-text \
  --name "DraftName" \
  --text "Hello world Hello" \
  --start-time 0s \
  --duration 3s \
  --keyword "Hello" \
  --keyword-color "#ff0000" \
  --shadow-json '{"color":"#111111","alpha":0.8,"distance":6.0}' \
  --json

python <SKILL_ROOT>/scripts/jy_wrapper.py add-rich-text \
  --name "DraftName" \
  --text "Hello world Hello" \
  --font-query "文轩体" \
  --anim-in-query "复古打字机" \
  --anim-out-query "向上擦除" \
  --anim-loop-query "VHS" \
  --text-effect-query "全息扫描" \
  --json
```

### Apply Segment Keyframes

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-keyframes \
  --name "DraftName" \
  --segment-id "<segment-id>" \
  --keyframes-json '[{"property":"position_x","offset":"0.5s","value":0.2},{"property":"uniform_scale","offset":"1.5s","value":1.1}]' \
  --json
```

### Add a Filter Track Segment

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-filter \
  --name "DraftName" \
  --filter-name "_1980" \
  --start-time 0s \
  --duration 5s \
  --intensity 100 \
  --json

python <SKILL_ROOT>/scripts/jy_wrapper.py add-filter \
  --name "DraftName" \
  --filter-query "1980" \
  --start-time 0s \
  --duration 5s \
  --json
```

### Add an Effect Track Segment

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-effect \
  --name "DraftName" \
  --effect-query "Blur" \
  --effect-category character \
  --start-time 0s \
  --duration 2s \
  --json
```

Alias commands with explicit intent:

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-video-effect \
  --name "DraftName" \
  --effect-query "Blur" \
  --start-time 0s \
  --duration 2s \
  --json

python <SKILL_ROOT>/scripts/jy_wrapper.py add-face-effect \
  --name "DraftName" \
  --effect-query "Blur" \
  --start-time 0s \
  --duration 2s \
  --json
```

Notes:

- `add-video-effect` is equivalent to `add-effect --effect-category scene`.
- `add-face-effect` is equivalent to `add-effect --effect-category character`.

### Attach Transition or Animation Material

Use `list-segments` first to discover the target `segment_id`.

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py attach-material \
  --name "DraftName" \
  --segment-id "<segment-id>" \
  --kind transition \
  --query "Fade" \
  --duration 0.5s \
  --json

python <SKILL_ROOT>/scripts/jy_wrapper.py attach-material \
  --name "DraftName" \
  --segment-id "<segment-id>" \
  --kind animation \
  --animation-role in \
  --query "Kira" \
  --json
```

Notes:

- `--kind transition` resolves from the `transition` category.
- `--kind animation --animation-role in|out|group|loop` resolves from text or video animation categories based on the target segment type.
- For saved drafts this command writes directly through the JSON patch layer and appends material ids into `extra_material_refs`.

### Add Complex Text

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-complex-text \
  --name "DraftName" \
  --text "Hello complex" \
  --font-query "文轩体" \
  --bubble-query "6838834573413978631" \
  --text-effect-query "1998" \
  --anim-loop-query "VHS" \
  --json

python <SKILL_ROOT>/scripts/jy_wrapper.py add-complex-text \
  --name "DraftName" \
  --text "Hello complex" \
  --bubble-resource-id "6838834573413978631" \
  --bubble-effect-id "763870" \
  --flower-id "7342020000812731658" \
  --anim-loop "VHS" \
  --json
```

This command builds on the rich-text path and then attaches optional:

- bubble text material (`text_shape`)
- flower text effect (`text_effect`)
- text animations (`material_animations`)

Python example:

```python
segment, resolved = project.add_complex_text(
    "Complex title",
    font_query="文轩体",
    bubble_query="6838834573413978631",
    text_effect_query="1998",
    anim_loop_query="VHS",
    return_resolved_assets=True,
)
print(resolved.keys())
```

### Add Green Screen / Chroma Background

Use `list-segments` first to discover the target foreground video `segment_id`.

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-green-screen \
  --name "DraftName" \
  --segment-id "<segment-id>" \
  --background-path "C:\\assets\\background.mp4" \
  --chroma-color "#00ff00" \
  --chroma-strength 20 \
  --edge-feather 10 \
  --edge-cleanup 10 \
  --json
```

Python example:

```python
foreground = project.find_segment_by_id("<segment-id>")
project.add_green_screen_background(
    foreground,
    background_path=r"C:\assets\background.mp4",
    chroma_color="#00ff00",
    chroma_strength=20,
    edge_feather=10,
    edge_cleanup=10,
)
```

Notes:

- This writes a `chroma` material onto the target video segment and appends a background media segment on a separate video track.
- The current implementation supports `background_path` only.
- `background_query` is intentionally not implemented yet.

### Import / Export SRT

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py import-srt \
  --name "DraftName" \
  --srt-path "C:\\captions\\subs.srt" \
  --json

python <SKILL_ROOT>/scripts/jy_wrapper.py import-srt \
  --name "DraftName" \
  --srt-path "C:\\captions\\subs.srt" \
  --style-ref-segment-id "<text-segment-id>" \
  --json

python <SKILL_ROOT>/scripts/jy_wrapper.py export-srt \
  --name "DraftName" \
  --json
```

Notes:

- `import-srt` writes one text segment per cue through the same saved-draft patch path used by the other CLI mutations.
- `--style-ref-segment-id` clones the reference text segment's base style into the imported cues.
- `export-srt` returns the generated SRT text in the JSON payload.

### Patch Existing Rich Text Ranges

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py text-ranges \
  --name "DraftName" \
  --segment-id "<text-segment-id>" \
  --styles '[{"range":[0,5],"color":"#ff0000","bold":true}]' \
  --json
```

This command mutates the saved text material in place and currently supports per-range:

- `range`
- `color`
- `font_size`
- `bold`
- `italic`
- `underline`
- `text_effect`

### Save / Apply Segment Templates

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py save-template \
  --name "DraftName" \
  --segment-id "<segment-id>" \
  --template-name "title-template" \
  --out "C:\\templates\\title-template.json" \
  --json

python <SKILL_ROOT>/scripts/jy_wrapper.py apply-template \
  --name "DraftName" \
  --template-path "C:\\templates\\title-template.json" \
  --start-time 1s \
  --duration 3s \
  --text "Replacement title" \
  --json
```

Template application currently targets the existing patch layer and is best suited for text-like segments whose attached materials live in the saved draft JSON.

Official text-template intake is a parallel surface:

- `save-template` / `apply-template` reuse segment snapshots captured from an existing draft.
- `add-text-template` writes official template materials from a resolved resource id or a pre-expanded payload bundle.

## Minimal End-to-End Example

```python
import os
import sys

skill_root = r"<SKILL_ROOT>"
sys.path.insert(0, os.path.join(skill_root, "scripts"))
from jy_wrapper import JyProject

project = JyProject("API_Demo", overwrite=True)
project.add_media_safe(os.path.join(skill_root, "assets", "video.mp4"), "0s", "5s", "VideoTrack")
project.add_rich_text(
    "CapCut Mate style primitives",
    start_time="0.5s",
    duration="3s",
    keyword="Mate",
    keyword_color="#ff7100",
)
project.add_sticker_simple("7405878923323641129", start_time="0.8s", duration="1.8s")
project.save()
```

## Notes on Saved-Draft Mutation

Both CLI and Python now support stable saved-draft mutation through the JSON patch layer.

CLI commands such as `list-segments`, `add-mask`, `add-keyframes`, `add-filter`, `attach-material`, and `add-complex-text` operate on saved `draft_content.json`.
When `JyProject(..., overwrite=False)` cannot safely reload the draft through `pyJianYingDraft`, it automatically falls back to patch mode instead of recreating the project.

This is intentional:

- it gives stable segment targeting for CLI/Skill automation
- it avoids depending on `pyJianYingDraft.load_template()` for complex saved drafts

Example reopen/edit flow:

```python
project = JyProject("ExistingDraft", drafts_root=r"<draft-root>", overwrite=False)
video_seg = project.find_segment_by_id("<segment-id>")
project.add_rich_text("Patch mode works", start_time="0s", duration="3s", keyword="works")
project.add_mask_to_segment(video_seg, "circle", size=0.3, feather=20)
project.save()
```

## Codex-First Flow

For Codex or any agent caller, the shortest stable path is now:

1. `search-assets --list-categories --json`
2. `resolve-asset "<query>" --category <category> --json`
3. call the write command directly, or use query-driven write flags such as:
   - `add-sticker --sticker-query "<query>"`
   - `add-mask --mask-query "<query>"`
   - `add-rich-text --font-query "<query>" --anim-in-query "<query>"`
   - `add-rich-text --anim-out-query "<query>" --anim-loop-query "<query>"`
   - `add-rich-text --text-effect-query "<query>"`
   - `add-filter --filter-query "<query>"`
   - `add-video-effect --effect-query "<query>"`
   - `add-face-effect --effect-query "<query>"`
   - `add-text-template --template-query "<query>"`
   - `attach-material --kind transition --query "<query>"`
   - `attach-material --kind animation --animation-role in --query "<query>"`
   - `add-complex-text --font-query "<query>" --bubble-query "<query>" --text-effect-query "<query>" --anim-loop-query "<query>"`

This is the recommended machine-callable surface for the skill.

For `text_effect_query`, the resolved effect is currently written into the rich-text material payload under `content.styles[*].effectStyle.id`. This path is now covered by regression tests.
