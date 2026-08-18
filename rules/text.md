---
name: text-subtitles
description: Rules for adding text, subtitles, captions, and query-driven styled text.
metadata:
  tags: text, subtitles, captions, font, rich-text, complex-text, flower-text
---

# Text & Subtitles

Use:

- `add_text_simple()` for plain text
- `add_rich_text()` for styled captions and keyword highlighting
- `add_complex_text()` for bubble text / flower text / query-driven decorative titles
- `scripts/jy_wrapper.py import-srt` for SRT subtitles in saved drafts

## 1. Plain Text

```python
project.add_text_simple(
    text="Hello World",
    start_time="0s",
    duration="3s",
    clip_settings=draft.ClipSettings(transform_y=-0.8),
    anim_in="Typewriter",
)
```

Constraints:

- `clip_settings.transform_y=-0.8` is the normal subtitle baseline.
- `duration` should always be explicit.
- Prefer `add_rich_text()` instead of overloading `add_text_simple()` for styling-heavy requests.

## 2. Rich Text

Use `add_rich_text()` when the user asks for:

- highlighted subtitles
- specific fonts
- text intro/outro/loop animations
- text effect styling without bubble/flower composition

```python
project.add_rich_text(
    "Hello world Hello",
    start_time="0s",
    duration="3s",
    keyword="Hello",
    keyword_color="#ff7100",
    font_query="文轩体",
    anim_in_query="复古打字机",
    anim_out_query="向上擦除",
    anim_loop_query="VHS",
    text_effect_query="1998",
)
```

Query-driven write surface:

- `font_query`
- `anim_in_query`
- `anim_out_query`
- `anim_loop_query`
- `text_effect_query`

Rules:

- Prefer query parameters over hard-coded enum names when the user describes an effect semantically.
- If the user already provides a precise enum / id, pass the direct parameter (`font`, `anim_in`, `text_effect`, etc.).
- Query parameters only fill missing direct parameters; explicit direct values still win.

## 3. Complex Text

Use `add_complex_text()` when the user asks for:

- bubble text
- flower text
- decorative title cards
- query-driven text styling that mixes font, bubble/effect, and animation

```python
project.add_complex_text(
    "Complex title",
    start_time="0s",
    duration="3s",
    font_query="文轩体",
    bubble_query="6838834573413978631",
    text_effect_query="1998",
    anim_loop_query="VHS",
)
```

Supported query surface:

- `font_query`
- `anim_in_query`
- `anim_out_query`
- `anim_loop_query`
- `text_effect_query`
- `bubble_query`
- `flower_query`

Important constraint:

- `flower_*` and `text_effect*` are mutually exclusive in one call. They both map to the same underlying text effect slot.

Decision guide:

- User says "subtitle / caption" -> `add_text_simple()` or `add_rich_text()`
- User says "highlight keywords / special font / typing animation" -> `add_rich_text()`
- User says "bubble text / 花字 / decorative title / 气泡字" -> `add_complex_text()`

## 4. Auto-Layering

If multiple text clips overlap in time on the same logical track, the wrapper will create separate layers automatically to avoid collisions.

## 5. Importing SRT

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py import-srt \
  --name "DraftName" \
  --srt-path "C:\path\to\subs.srt" \
  --json
```

Use `export-srt` for round-trips. Do not use a Python-side SRT import method here; SRT import is exposed through the current CLI patch surface.

## Data Context

- `data/cloud_text_styles.csv`: cloud text style references
- `data/text_animations.csv`: text intro animation references
- `references/README.md`: bubble text / flower text resource ids
- `scripts/jy_wrapper.py search-assets` and `resolve-asset`: query-to-write bridge
