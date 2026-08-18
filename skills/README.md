# Repository-scoped Auto-Cut Skills

`skills/` is the source of truth for this repository's Auto-Cut skill bundle.

- Skill folders use the `auto-cut` / `auto-cut-*` naming scheme.
- `auto-cut` is the stable router and entrypoint.
- Focused subskills carry detailed editing rules.
- Use `docs/skill-catalog.md` for a human-readable Chinese catalog.
- Use `skills/auto-cut/references/skill-catalog.md` for the router's compact selection table.

## Usage Modes

1. Natural language:
   - Say what you want edited. The agent routes through `auto-cut` to the right `auto-cut-*` skill.
   - Example: “帮我把这个草稿换个更适合的 BGM，花字不要挡字幕。”

2. Router:
   - Use `$auto-cut` when you want the agent to analyze which skill fits.
   - Example: “用 `auto-cut` 分析一下这个需求该怎么做。”
   - The router decides whether audio assessment/restoration, BGM, revision, audio peak, safe-zone, or multiple skills are needed.

3. Targeted:
   - Use a specific skill name, such as `$auto-cut-music-library-bgm`, when you already know the exact workflow.
   - Example: “用 `auto-cut-music-library-bgm` 换 BGM。”

## Current Bundle

- `auto-cut`: repository-level router for natural-language selection and mixed requests
- `auto-cut-basic-oral-video`: 基础口播视频 style rules
- `auto-cut-editable-ad-revision`: editable ad/oral-video revision rules
- `auto-cut-revision-draft`: review-driven revision with traceability, visible cut preservation, and clean source-baseline handling
- `auto-cut-audio-restoration`: explicit spoken-word restoration, runtime diagnostics, spectrogram acceptance, and final WAV/MP3 delivery; the proposed automatic review-job quality gate is not wired yet
- `auto-cut-review-audio-precision`: Feishu/Lark review-document precise audio deletion, reverse ASR, tail cleanup, semantic pause repair, and visual-aware pause timing
- `auto-cut-animation-timing-revision`: PPT/course animation timing, page-turn, whole-section advance, state-overlay, and release-boundary revision
- `auto-cut-subject-pointer-onboarding`: explicit create/update/view/bind commands for subject pointer libraries, asking only for missing stage/subject identity, plus the same-task onboarding handoff opened by a failed ordinary pointer gate
- `auto-cut-pointer-targeting`: reads `project-bindings.json`, fresh-checks the bound `status=ready` profile, and uses registered asset SHA-256 and current-layout ratios for target placement and timing; a failed exact gate opens a same-task onboarding handoff before draft writes, and source screenshots become evidence only after explicit registration
- `auto-cut-final-acceptance`: final gate for source coverage, audio/visual/pause validation, editable structure, and marker traceability
- `auto-cut-text-safezone-animation-revision`: red-box/safe-zone text and flower animation fixes
- `auto-cut-music-library-bgm`: JianYing music-library BGM selection and validation
- `auto-cut-audio-peak-target`: dBFS peak-target semantics
- `auto-cut-favorite-text-assets`: favorite flower text and text-template asset workflow
- `auto-cut-draft-retention`: keep latest 3 drafts per project family, with at most 1 fallback draft after accepted revisions

Explicit library example: “为当前项目建立高中历史指向物素材库” -> `auto-cut-subject-pointer-onboarding`.
Ordinary pointer example: “给这里加小手指向地图目标” -> `auto-cut-pointer-targeting` -> failed exact gate only: `auto-cut-subject-pointer-onboarding` -> fresh pointer gate.

## Repository-local Installation

Install or refresh the repository-local skill copy under ignored `.codex/skills`:

```powershell
python scripts/install_repo_skills.py
```

List without copying:

```powershell
python scripts/install_repo_skills.py --list
```

Copy to an explicit skills root only when you really want a separate installed copy:

```powershell
python scripts/install_repo_skills.py --dest C:\path\to\skills
```

After a successful install, the installer prints the three usage modes above. Keep that prompt current when changing the router behavior.
