# Agent Playbook

Task routing matrix for reliable execution.

## 0) Quick Edit Runtime Template (Default for "来个剪辑")

Use this fixed sequence:

1. Environment check (minimal):
   - verify Python works
   - verify draft root exists
2. Asset resolution:
   - use explicit local/cloud assets
   - prefer `scripts/jy_wrapper.py search-assets ... --json` for Codex/agent-driven lookup
   - use `search-assets --list-categories` first when the namespace is unclear
   - use `resolve-asset` when the next step is a direct write and you want one best write-ready match
   - `asset_search.py` remains available as a read-only thin wrapper over the same index
3. Script assembly:
   - generate one runnable script with deterministic APIs
4. Execute once:
   - run script and capture success/failure output
5. Acceptance check:
   - validate draft and track structure (see checklist below)
6. Review marker default:
   - when the task is revision-driven ("修改意见", "按意见剪", "删/移/调序/静帧/过渡"), add review-marker tracks by default
   - export a matching markdown marker manifest for human proofing

### Acceptance Checklist (Mandatory)

- Draft folder exists under JianYing drafts root
- `project.save()` completed successfully
- At least one `video` track segment exists
- For revision-driven jobs, the result is an editable draft project rather than a baked final export shell
- For multi-edit revision jobs, the main video track is not a single full-length segment
- If BGM exists, it must be on `audio` track (not `video`)
- If narration exists, subtitle segments should exist and be time-aligned
- If the request is revision-driven, `校对标记` tracks should exist or the agent must explain why they were intentionally skipped
- If the request is revision-driven, the user must be able to inspect what changed directly from timeline cuts, review markers, or trace lanes

## 1) Cloud Video + BGM Draft

- Read: `rules/setup.md`, `rules/media.md`, `rules/audio-voice.md`
- Reference: `examples/cloud_video_music_tts_demo.py`
- Required checks:
  - BGM goes to audio track.
  - project saved successfully.

## 2) Narration + Subtitle Alignment

- Read: `rules/text.md`, `rules/audio-voice.md`
- APIs: `add_tts_intelligent`, `add_narrated_subtitles`
- Required checks:
  - subtitle timing aligns with narration.
  - subtitle track exists.

## 3) Recording + Smart Zoom

- Read: `rules/recording.md`
- Execute: `tools/recording/recorder.py`
- Required checks:
  - recording output exists.
  - generated draft can be opened.

## 4) Effects / Transitions

- Read: `rules/effects.md`, `rules/keyframes.md`
- Tools: `scripts/jy_wrapper.py search-assets`, `scripts/asset_search.py`
- Required checks:
  - selected IDs resolved.
  - no None returns from effect/transition methods.

## 5) Draft-Writing Primitives

Use this lane when the user wants direct draft JSON writing rather than a full generated editing script.

Primary surfaces:

- `scripts/jy_wrapper.py search-assets`
- `scripts/jy_wrapper.py add-image`
- `scripts/jy_wrapper.py add-sticker`
- `scripts/jy_wrapper.py add-mask`
- `scripts/jy_wrapper.py add-rich-text`
- `scripts/jy_wrapper.py add-complex-text`
- `scripts/jy_wrapper.py add-keyframes`
- `scripts/jy_wrapper.py add-filter`
- `scripts/jy_wrapper.py add-video-effect`
- `scripts/jy_wrapper.py add-face-effect`
- `scripts/jy_wrapper.py attach-material`
- `scripts/jy_wrapper.py list-segments`

Recommended execution order for saved-draft mutation:

1. Resolve or create the draft.
2. Discover categories or candidate ids with `search-assets`, or jump straight to `resolve-asset`.
3. If targeting an existing clip, run `list-segments` first.
4. Apply sticker, rich text, mask, or keyframes through `jy_wrapper.py`.
5. Prefer query-driven write flags when available, such as `--sticker-query` or `--mask-query`.
   For rich text, prefer `--font-query`, `--anim-in-query`, `--anim-out-query`, `--anim-loop-query`, and `--text-effect-query`.
   For complex text, prefer `--font-query`, `--bubble-query`, `--flower-query`, `--text-effect-query`, and animation query flags.
6. Validate the saved `draft_content.json` with `draft_inspector.py show --kind content --json`.

Required checks:

- resolved asset ids come from the unified index layer, not ad hoc literals when the request is ambiguous
- mask writes add an entry under `materials.masks`
- sticker writes add an entry under `materials.stickers`
- rich text keyword styles are present in `materials.texts[*].content`
- query-driven rich text writes resolve to the expected `font` / `effectStyle` / animation payloads
- complex text writes add the expected `text_shape`, `text_effect`, or `material_animations` entries
- keyframes exist in the target segment's `common_keyframes`

## 6) Commentary from Long Video

- Read: `rules/generative.md`
- Prompt: `prompts/movie_commentary.md`
- Execute: `scripts/movie_commentary_builder.py`
- Required checks:
  - input media pre-optimized (<=30 min, 360p preferred).
  - storyboard JSON parsed and applied.

## 7) Export and CI-facing Validation

- Read: `rules/core.md`, `rules/cli.md`
- Execute:
  - `scripts/auto_exporter.py`
  - `scripts/api_validator.py --json`
- Required checks:
  - output file exists.
  - CLI return code and JSON contract are valid.
