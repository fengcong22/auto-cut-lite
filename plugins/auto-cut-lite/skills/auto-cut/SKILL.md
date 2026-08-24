---
name: auto-cut
description: Use when the user asks in natural language to make, revise, validate, or deliver a JianYing/CapCut project, needs a complete or migratable project, does not know which Auto-Cut skill fits, or combines multiple repository editing capabilities.
---

# Auto-Cut

## Packaged Runtime

Before executing this skill from the distributed plugin, read the [portable runtime contract](../auto-cut-lite/references/portable-runtime.md). Use the installed isolated runtime and `workflow_mode=lite`; do not fall back to a source checkout or another Auto-Cut installation.


Use this as the stable plugin entrypoint. It routes natural-language JianYing editing requests to the smallest useful set of bundled `auto-cut-*` subskills and executes them through the packaged runtime and validation rules.

`auto-cut-lite` is the generic workflow entrypoint. Every request in this distribution uses `workflow_mode=lite`; no stage, subject, project profile, or private asset is inferred or preloaded.

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

After the packaged skills are installed for the first time, surface this concise reminder to the user or developer:

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

- Explicit “Auto-Cut 精简版”, “精简版 Auto-Cut”, `lite`, `compact`, or any unspecified plugin request:
  `auto-cut-lite`
- New short oral-video draft or “基础口播视频”: `auto-cut-basic-oral-video`
- Editable ad/oral-video revision with BGM, subtitles, flower text, overlap, color, or retention: `auto-cut-editable-ad-revision`
- Review-driven draft revision where edit traceability matters: `auto-cut-revision-draft`
- Explicit standalone restoration or evidence-backed audio repair routes to `auto-cut-audio-restoration`. The proposed automatic `clean` / `repair_needed` / `review_needed` quality gate is not yet wired into ordinary review jobs and must not be claimed without a saved assessment report.
- Standalone spoken-word restoration, breath/mouth-noise cleanup, denoise, pause-residue or dropout repair, spectrogram review, final WAV/MP3 delivery, or integrated runtime diagnostics: `auto-cut-audio-restoration`
- Feishu/Lark review-document audio deletion, precise spoken-word repair, ASR alignment, post-delete pause timing, visual-aware pause validation, or serial upload back to the document: `auto-cut-review-audio-precision`
- Red-box/safe-zone text placement, sticker placement, or flower intro/outro animation fixes: `auto-cut-text-safezone-animation-revision`
- Review-document PNG/JPG local replacement for formulas, labels, text blocks, icons, or another precise region, including alpha-visible sizing, original text/formula scale matching, protected-region clearance, and editable cleanup/overlay layers: `auto-cut-local-image-overlay-revision`
- PPT/course animation timing changes such as speed-up, page-turn advance, whole-section advance, full-screen state overlay, stable-frame detection, overlay release points, or collisions with original entrance/next animation: `auto-cut-animation-timing-revision`
- Explicit subject-library create/update/supplement/view/bind/rebind commands, even when the stage or subject is omitted: `auto-cut-profile-onboarding`; ask only for the missing stage or subject
- Every ordinary course pointer request, including hands, arrows, circles, magnifiers, screenshot-calibrated pointing, and pointer duration decisions: route to `auto-cut-pointer-targeting` first; when its exact binding/profile gate fails, open a same-task onboarding handoff to `auto-cut-profile-onboarding` before draft writes, then return through the fresh pointer gate
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
10. Explicit subject-library operation, or a same-task onboarding handoff after a failed pointer gate, with `auto-cut-profile-onboarding`
11. Draft retention
12. Final validation with `auto-cut-final-acceptance`

For mixed tasks, route ordinary pointer work to `auto-cut-pointer-targeting` first. Include `auto-cut-profile-onboarding` for an explicit library operation or when the failed pointer gate opens a same-task onboarding handoff; never infer stage or subject from content. Non-pointer edits do not read project-bindings.json. Ordinary local image overlays do not read or require subject pointer bindings.

## Plugin Contract

- Treat this plugin's `skills/` as the skill source and its `runtime/` as the only execution source.
- Keep `workflow_mode=lite` for every route, including direct focused-skill invocation.
- Keep the total router small; put detailed domain rules in focused `auto-cut-*` subskills.
- Do not duplicate conflicting rules across skills. Update the smallest responsible skill.
- Prefer repository tools such as `scripts/jy_wrapper.py`, `scripts/draft_inspector.py`, and maintained project scripts over ad hoc JSON edits.
- Preserve editable JianYing draft structure unless the user explicitly asks for a baked export.
- Route review-driven jobs through canonical source-ledger recovery, verbatim marker validation, and the routed final-acceptance profile.
- For natural-language source-document jobs, run `review-job-compile` and the resumable job tooling by default, then route the compiled inputs through the focused revision and final-acceptance skills.
- Legacy direct scripts remain compatible, but cannot claim optimized/source-document final acceptance without evidence from equivalent checkpoints, cache entries, receipts, and gates.

## Complete Project Delivery

Trigger this contract when the user requests a complete project, migratable project, formal delivery, full Auto-Cut delivery, or a project that can move to another computer and remain editable. Intermediate drafts are exempt unless delivery is explicitly requested.

1. Pass editable-draft acceptance before packaging. Keep source video, source/separated/replacement audio, local materials, visible cut structure, verbatim markers, and root/active-timeline evidence.
2. Run the lite entrypoint's final ZIP packaging boundary. Require the external receipt's SHA-256, CRC, path-safety, extracted-tree hash, and material-reference checks.
3. Do not include credentials, login state, project profiles, private media, or source-machine absolute paths.
4. Keep `target_open_verified` reserved for a real second computer that opened the delivered draft, left loading state, previewed it, and confirmed it remained editable. Static package validation is not target-open evidence.

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
