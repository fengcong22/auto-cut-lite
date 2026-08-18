---
name: effects
description: Searching for and applying effects, filters, and transitions.
metadata:
  tags: effects, filters, transitions, search, asset_id
---

# Effects, Filters & Transitions

Jianying resources use version-sensitive IDs. Do not guess IDs. Resolve them through the unified index before writing.

## 1. Resolve Assets

Preferred CLI:

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py search-assets "1980" --category filter --json
python <SKILL_ROOT>/scripts/jy_wrapper.py resolve-asset "Blur" --category video_effect --json
python <SKILL_ROOT>/scripts/jy_wrapper.py resolve-asset "Fade" --category transition --json
```

`scripts/asset_search.py` remains available as a read-only thin wrapper, but `jy_wrapper.py search-assets` / `resolve-asset` is the write-ready surface.

Common categories:

- `filter`
- `video_effect`
- `video_character_effect`
- `transition`
- `text_animation`
- `text_loop_animation`

## 2. Write Effects

Use documented wrapper methods or CLI commands:

```python
project.add_filter_segment(filter_query="1980", start_time="0s", duration="5s")
project.add_video_effect(effect_query="Blur", start_time="0s", duration="2s")
project.add_face_effect(effect_query="Blur", start_time="0s", duration="2s")

segment = project.find_segment_by_id("<segment-id>")
project.attach_internal_material(segment, kind="transition", query="Fade", duration="0.5s")
project.attach_internal_material(segment, kind="animation", animation_role="in", query="Kira")
```

Equivalent CLI commands:

```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py add-filter --name "DraftName" --filter-query "1980" --start-time 0s --duration 5s --json
python <SKILL_ROOT>/scripts/jy_wrapper.py add-video-effect --name "DraftName" --effect-query "Blur" --start-time 0s --duration 2s --json
python <SKILL_ROOT>/scripts/jy_wrapper.py add-face-effect --name "DraftName" --effect-query "Blur" --start-time 0s --duration 2s --json
python <SKILL_ROOT>/scripts/jy_wrapper.py attach-material --name "DraftName" --segment-id "<segment-id>" --kind transition --query "Fade" --duration 0.5s --json
```

Full option details live in `docs/api.md`; this rule is only the routing guide.
