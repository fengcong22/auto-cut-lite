---
name: auto-cut
description: Use when the user asks in natural language to make, revise, validate, or deliver a JianYing/CapCut project, needs a complete or migratable project, does not know which Auto-Cut skill fits, or combines multiple repository editing capabilities.
---

# Auto-Cut

Use this as the stable repository-level entrypoint for Auto-Cut. It routes natural-language JianYing editing requests to the smallest useful set of `auto-cut-*` subskills, then executes through the repository tools and validation rules.

`auto-cut-high-school-history-lite` is the explicit compact subject entrypoint. It sets
`workflow_mode=lite`, declares `stage=高中` and `subject=历史`, and returns to this router. The
existing high-school-history entrypoint and unspecified mode continue to use the full workflow.

## Three Supported Invocation Modes

1. Natural language mode:
   - The user does not need to name a skill.
   - Infer intent from the request, inspect the repository context, then load the relevant `auto-cut-*` skill before acting.
   - Example: “帮我把这个草稿换个更适合的 BGM，花字不要挡字幕。”

2. Router mode:
   - Use `$auto-cut` when the user asks which skill should handle a task, wants the full Auto-Cut workflow, or gives a mixed request.
   - First explain the selected route briefly, then load and follow the selected subskills.
   - Example: “用 auto-cut 分析一下这个需求该怎么做。”
   - Decide whether BGM, draft revision, audio peak targeting, safe-zone animation, or multiple skills are needed.

3. Targeted skill mode:
   - Use a named subskill such as `$auto-cut-music-library-bgm` when the user wants precise behavior.
   - Follow that subskill directly, and only add another subskill when the task clearly crosses boundaries.
   - Example: “用 auto-cut-music-library-bgm 换 BGM。”

## First-use Prompt

After the repository-local skills are installed for the first time, surface this concise reminder to the user or developer:

```text
现在有三种使用方式：

1. 自然语言直接说
例如：“帮我把这个草稿换个更适合的 BGM，花字不要挡字幕。”
我会按 auto-cut 总入口去判断该走哪个子 skill。

2. 点名总 skill
例如：“用 auto-cut 分析一下这个需求该怎么做。”
总 skill 会负责路由：该用音频评估/修复、BGM、修订、音量峰值、红框安全区，还是多个组合。

3. 点名单独 skill
例如：“用 auto-cut-music-library-bgm 换 BGM。”
这种适合你已经知道要用哪个能力时精准调用。
```

## Routing Rules

Read [references/skill-catalog.md](references/skill-catalog.md) when selecting among the repository skills or when the user asks what skills exist.

Default routing:

- Explicit “高中历史 Auto-Cut 精简版”, “精简版 Auto-Cut”, or `workflow_mode=lite`:
  `auto-cut-high-school-history-lite`
- New short oral-video draft or “基础口播视频”: `auto-cut-basic-oral-video`
- Editable ad/oral-video revision with BGM, subtitles, flower text, overlap, color, or retention: `auto-cut-editable-ad-revision`
- Review-driven draft revision where edit traceability matters: `auto-cut-revision-draft`
- Explicit standalone restoration or evidence-backed audio repair routes to `auto-cut-audio-restoration`. The proposed automatic `clean` / `repair_needed` / `review_needed` quality gate is not yet wired into ordinary review jobs and must not be claimed without a saved assessment report.
- Standalone spoken-word restoration, breath/mouth-noise cleanup, denoise, pause-residue or dropout repair, spectrogram review, final WAV/MP3 delivery, or integrated runtime diagnostics: `auto-cut-audio-restoration`
- Feishu/Lark review-document audio deletion, precise spoken-word repair, ASR alignment, post-delete pause timing, visual-aware pause validation, or serial upload back to the document: `auto-cut-review-audio-precision`
- Red-box/safe-zone text placement, sticker placement, or flower intro/outro animation fixes: `auto-cut-text-safezone-animation-revision`
- Review-document PNG/JPG local replacement for formulas, labels, text blocks, icons, or another precise region, including alpha-visible sizing, original text/formula scale matching, protected-region clearance, and editable cleanup/overlay layers: `auto-cut-local-image-overlay-revision`
- PPT/course animation timing changes such as speed-up, page-turn advance, whole-section advance, full-screen state overlay, stable-frame detection, overlay release points, or collisions with original entrance/next animation: `auto-cut-animation-timing-revision`
- Explicit subject-library create/update/supplement/view/bind/rebind commands, even when the stage or subject is omitted: `auto-cut-subject-pointer-onboarding`; ask only for the missing stage or subject
- Every ordinary course pointer request, including hands, arrows, circles, magnifiers, screenshot-calibrated pointing, and pointer duration decisions: route to `auto-cut-pointer-targeting` first; when its exact binding/profile gate fails, open a same-task onboarding handoff to `auto-cut-subject-pointer-onboarding` before draft writes, then return through the fresh pointer gate
- Final acceptance, "is the draft really done?", complete/migratable project delivery, full Auto-Cut delivery, source review item coverage, audio/visual/pause validation, or delivery gate checks: `auto-cut-final-acceptance`
- BGM chosen from the JianYing music library: `auto-cut-music-library-bgm`
- Requests like “人声峰值到 -3dB” or waveform peak targeting: `auto-cut-audio-peak-target`
- Favorite flower text or text template assets: `auto-cut-favorite-text-assets`
- Keep only recent drafts or clean old drafts: `auto-cut-draft-retention`

For mixed tasks, combine skills in this order when relevant:

1. Style, timeline contract, and immutable source manifest
2. Asset source rules
3. Explicit or evidence-backed audio restoration with `auto-cut-audio-restoration`
4. Review-document audio precision, ASR alignment, and must-keep protection
5. Visual-aware pause timing when deletions affect picture rhythm
6. Audio peak semantics over the kept timeline
7. Revision/safe-zone details
8. Local document-image replacement, visible-bound scaling, protected-region checks, and cleanup evidence with `auto-cut-local-image-overlay-revision`
9. Pointer target/duration and animation collision evidence with `auto-cut-pointer-targeting`
10. Explicit subject-library operation, or a same-task onboarding handoff after a failed pointer gate, with `auto-cut-subject-pointer-onboarding`
11. Draft retention
12. Final validation with `auto-cut-final-acceptance`

For mixed tasks, route ordinary pointer work to `auto-cut-pointer-targeting` first. Include `auto-cut-subject-pointer-onboarding` for an explicit library operation or when the failed pointer gate opens a same-task onboarding handoff; never infer stage or subject from content. Non-pointer edits do not read project-bindings.json. Ordinary local image overlays do not read or require subject pointer bindings.

## Repository Contract

- Treat `skills/` as the source of truth for repository skills.
- Keep `workflow_mode=full` as the default. Lite mode must be explicit and must never silently
  alter or weaken the full workflow.
- Keep the total router small; put detailed domain rules in focused `auto-cut-*` subskills.
- Do not duplicate conflicting rules across skills. Update the smallest responsible skill.
- Prefer repository tools such as `scripts/jy_wrapper.py`, `scripts/draft_inspector.py`, and maintained project scripts over ad hoc JSON edits.
- Preserve editable JianYing draft structure unless the user explicitly asks for a baked export.
- Route review-driven jobs through canonical source-ledger recovery, verbatim marker validation, and the routed final-acceptance profile.
- For natural-language source-document jobs, run the maintained `review-document-run` command by
  default. It owns compilation, strict cache identity, ASR, editable draft writing, acceptance,
  packaging, and phase recovery, then routes evidence through the focused skills.
- Keep `review-job-compile` and `revision-run` only as low-level compatibility commands for callers
  that already have canonical intermediate files.
- Legacy direct scripts remain compatible, but cannot claim optimized/source-document final acceptance without evidence from equivalent checkpoints, cache entries, receipts, and gates.

## Complete Project Delivery

Trigger this contract when the user requests a complete project, migratable project, formal delivery, full Auto-Cut delivery, or a project that can move to another computer and remain editable. Intermediate drafts are exempt unless delivery is explicitly requested.

For an explicit `workflow_mode=lite` full-flow delivery, use the lite skill's final ZIP boundary
instead of the native draft mirror below. The ZIP step remains offline and does not open JianYing;
the native mirror rules continue to apply to full-workflow deliveries. For lite, do not continue
with the numbered native-mirror steps after the ZIP has passed its receipt and tree-hash checks.

1. Pass the ordinary editable draft acceptance first; the fixed path mirror uses the same absolute path and never replaces source coverage, visible cut structure, separated/source audio, material, marker, or root/active-timeline gates.
   The editable timeline retains source video, source audio, and local materials under `Resources/local` or `Resources/audioAlg`.
2. Resolve the source root from JianYing's explicit `currentCustomDraftPath`. On a new computer run `python scripts/jy_wrapper.py draft-root-check --expected-root "<source_currentCustomDraftPath>" --json`. If the path is absent, missing, ambiguous, or different, stop and show `请在对方电脑创建与源电脑相同的剪映草稿路径：<path>`; never silently select a conventional default.
3. Keep every local video, audio, and image used by the generated draft under `Resources/local` or `Resources/audioAlg` before writing the material reference. The editable timeline and source media remain inspectable.
4. Deliver with `python scripts/jy_wrapper.py draft-mirror-deliver --name "<DraftName>" --drafts-root "<source_currentCustomDraftPath>" --desktop-root "<Desktop>" --receipt-json "<receipt.json>" --job-input-digest "<sha256>" --json`. This copies the complete tree byte-for-byte, including empty directories; it does not open JianYing and does not rewrite draft JSON.
5. Require receipt fields `schema_version`, `source_tree_sha256`, `target_tree_sha256`, `file_count`, `directory_count`, and `byte_size`; the flags `json_rewritten`, `ui_invoked`, and `portable_package_invoked` must all be `false`. Refuse overwrite and refuse a destination already exists (`destination already exists`), reparse points, source/target overlap, source changes during copy, and any tree-hash mismatch. The receipt binds the source tree and target tree by SHA-256.
6. The old `portable_project_tool` package/import path is retired and is not final delivery evidence. It may remain only as historical/internal compatibility code; do not route complete-project delivery through it. The fixed absolute path and source tree/target tree receipt are the only delivery evidence.
7. Keep `target_open_verified` reserved for a real second computer that opened the mirrored draft, left the loading state, previewed it, and confirmed it remained editable. Cloud-only effects and fonts remain explicit non-file dependencies.

## Blocking User Actions

Read [references/user-action-required.md](references/user-action-required.md) whenever an Auto-Cut operation is genuinely blocked on user evidence, approval, authorization, or a high-risk confirmation. Persist the durable wait, or post the full interactive-only question in the originating Codex task, before invoking the optional notifier. Keep the full question and its only valid resolution channel in that same Codex task; Feishu is notification-only.

Directly invoked `auto-cut-*` skills inherit this shared contract. Ordinary clarification, progress updates, and nonblocking status messages do not invoke the notifier.

## Feishu Document Identity

For every Feishu/Lark document read, use the current operator's own user identity. The canonical
read command is `lark-cli docs +fetch --as user`; do not read review documents with an app/bot
identity or permit an automatic identity fallback. On each target computer, recommend the one-time
setup `lark-cli config default-as user` and `lark-cli config strict-mode user`. The login token is
machine-local, must be refreshed by the user when required, and must never be copied into a plugin,
ZIP package, repository, or another user's machine. Optional Feishu notifications are separate from
document reads and must not be described as the document authentication mechanism.

## Maintenance Rule

When adding a new skill:

1. Name the folder and frontmatter as `auto-cut-<purpose>`.
2. Add a concise Chinese explanation to `docs/skill-catalog.md`.
3. Add the routing entry to `skills/auto-cut/references/skill-catalog.md`.
4. Ensure `scripts/install_repo_skills.py --list` lists the new skill.

## Repository-local Installation

Install or refresh the repository-local skill copy under `.codex/skills`:

```powershell
python scripts/install_repo_skills.py
```

List without copying:

```powershell
python scripts/install_repo_skills.py --list
```

Only copy outside the repository when an explicit destination is intended:

```powershell
python scripts/install_repo_skills.py --dest C:\path\to\skills
```
