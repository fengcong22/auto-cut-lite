---
name: auto-cut-profile-onboarding
description: "Use when the user explicitly asks to create, update, supplement, view, bind, or rebind a target-local pointer profile, or when pointer targeting opens a same-task onboarding handoff."
---

# Auto-Cut Pointer Profile Onboarding
## Packaged Runtime

Before executing this skill from the distributed plugin, read the [portable runtime contract](../auto-cut-lite/references/portable-runtime.md). Use the installed isolated runtime and `workflow_mode=lite`; do not fall back to a source checkout or another Auto-Cut installation.


## Overview

Build or maintain one pointer-material profile for one user-supplied `阶段 + 内容类别`. Profiles live under `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite\pointer-profiles.local` so plugin upgrades do not remove user data. Direct entry runs only when the user explicitly requests a profile operation. An ordinary pointer request enters only through the same-task onboarding handoff opened by `auto-cut-pointer-targeting` after its exact binding/profile gate fails.

Read [references/handoff-contract.md](references/handoff-contract.md) for that handoff. Preserve its original pointer and source-ledger item IDs and project context through every question and return. Never infer stage or subject from title, filename, OCR, ASR, course content, layout, media, or another profile. If one identity value is already explicit, ask only for the missing stage or subject.

Read the shared [user-action-required contract](../auto-cut/references/user-action-required.md) before emitting `subject_identity`, `subject_evidence`, `preview_approval`, or `project_binding`. A resumable handoff persists its current wait before the optional notification. A direct interactive operation posts the complete blocking question in this Codex task before notification and accepts the answer only here; Feishu replies never approve onboarding or binding.

For a `subject_identity` handoff, use the reference's complete three-part user-facing response: say pointer draft writing is paused, say onboarding continues in this same task, ask only for the missing identity, and promise the fresh-gated return to the original pointer edit. A bare stage/subject question is not a complete handoff response.

## Explicit Command Gate

Accepted commands include:

- `为当前项目建立指向物配置档案`: create or complete the named profile, then bind the current project after it becomes ready.
- `建立指向物配置档案`: create or complete the profile without guessing a project binding.
- `更新指向物配置档案`: repair stale evidence or add a missing layout reference.
- `查看当前指向物配置档案`: list registered profiles.
- `查看指定的指向物配置档案`: inspect one exact named profile.
- `把当前项目绑定到指定配置档案`: bind an existing ready profile.
- `将当前项目改绑为另一个配置档案`: explicitly replace an existing project binding.

## Explicit Command Matrix

Classify the explicit command before asking for identity. `identity_policy` is a closed rule: `list-all` runs immediately without identity, while a profile-specific operation with incomplete identity asks only for the missing explicit field(s) and stops before evidence, profile, or binding work.

```json
{
  "list-all": {
    "example": "查看当前有哪些学科指向物素材库",
    "identity_policy": "none",
    "command": "python skills/auto-cut-profile-onboarding/scripts/profile_registry.py list --root <registry-root> --json"
  },
  "create": {
    "example": "建立阶段A类别A指向物素材库",
    "identity_policy": "ask_only_missing_explicit_identity_and_stop"
  },
  "update": {
    "example": "更新阶段A类别A指向物素材库",
    "identity_policy": "ask_only_missing_explicit_identity_and_stop"
  },
  "supplement": {
    "example": "补充阶段A类别A指向物素材库",
    "identity_policy": "ask_only_missing_explicit_identity_and_stop"
  },
  "view-profile": {
    "example": "查看阶段A类别A指向物素材库",
    "identity_policy": "ask_only_missing_explicit_identity_and_stop"
  },
  "bind": {
    "example": "把当前项目绑定到阶段A类别A素材库",
    "identity_policy": "ask_only_missing_explicit_identity_and_stop"
  },
  "rebind": {
    "example": "将当前项目改绑为阶段A类别B素材库",
    "identity_policy": "ask_only_missing_explicit_identity_and_stop"
  }
}
```

The user-supplied names are authoritative. 不得从标题、文件名、OCR、ASR、课程内容或版式选择学科. If the direct command or handoff omits the stage or subject, ask only for that missing value. Do not inspect the review document or media to fill it in.

The preceding missing-identity rule applies to profile-specific create, update, supplement, view-profile, bind, and rebind operations. It does not apply to `list-all`: run `list` with no stage or subject argument and return the registered profiles.

Pointer requests such as `这里加一个小手` or `奥地利这里加个指向物` still route to pointer targeting first. This skill handles them only after pointer targeting opens the same-task handoff. A generic request such as `按这个文档修改剪映草稿` follows the revision route instead. Non-pointer edits do not read `project-bindings.json` or enter onboarding. No profile is created and no project is bound until the user supplies the required input and confirmation.

## 1. Create Stable IDs

Generate internal IDs only from the explicit names:

```powershell
python skills/auto-cut-profile-onboarding/scripts/profile_registry.py identity --stage-name <stage_name> --subject-name <subject_name> --json
```

Store one display-name alias and one compatibility evidence row:

```json
{
  "aliases": ["阶段A类别A"],
  "detection_evidence": [
    {
      "source": "explicit_user_command",
      "value": "建立阶段A类别A指向物素材库",
      "confirmed": true
    }
  ]
}
```

These compatibility fields are audit metadata only and must never select a profile.

## 2. Check the Exact Profile

```powershell
python skills/auto-cut-profile-onboarding/scripts/profile_registry.py check --stage-id <stage_id> --subject-id <subject_id> --root <registry-root> --json
```

The lookup key is the exact lowercase ASCII `<stage_id>-<subject_id>` pair. 学段或学科不同 means a different profile; no nearest-subject, same-stage, or cross-stage fallback exists.

| Result | Incremental action for a direct command or handoff |
| --- | --- |
| `status=ready` | Verify stored SHA-256 values, then request explicit binding confirmation when the current pointer handoff has project context. |
| `missing` | Request the complete first-time material packet. |
| `incomplete` | 只请求缺少项 from `missing_items`. |
| `needs_confirmation` | Request only unconfirmed reference or preview approval. |
| `stale` | Request only missing or hash-changed evidence from `problems`. |

A scale reference qualifies only when it is a confirmed full 16:9 frame with lesson, time, layout, positive canvas, and an in-canvas visible bounding box.

When an explicit update/check finds a legacy `hand` asset whose only missing requirement is `media_contract`, migrate from the already stored evidence before requesting any original intake file:

```powershell
python skills/auto-cut-profile-onboarding/scripts/profile_registry.py migrate-hand-media --stage-id <stage_id> --subject-id <subject_id> --root <registry-root> --json
```

Migration is valid only when every stored evidence path remains inside the exact profile, every current file matches its stored SHA-256, and the hand decodes as an RGBA PNG. A migration failure leaves the profile and catalogs unchanged; report the invalid stored evidence instead of silently dropping it.

## 3. Request Missing Evidence

Read [references/intake-contract.md](references/intake-contract.md) before requesting evidence. When file evidence is needed, resolve and display both bundled guide images together in the same message. Read [references/profile-schema.md](references/profile-schema.md) before writing intake JSON. Request only evidence missing, stale, unconfirmed, or absent for a requested role/current layout; do not ask for evidence already valid in the exact profile.

Use 增量 intake 合并 for an existing readable exact profile. Preserve valid evidence, replace the same logical `asset_id` or `reference_id`, apply explicit `remove_aliases`, and 不要求重新提交已满足项.

## 4. Register and Validate

```powershell
python skills/auto-cut-profile-onboarding/scripts/profile_registry.py register --input <intake.json> --root <registry-root> --json
python skills/auto-cut-profile-onboarding/scripts/profile_registry.py check --stage-id <stage_id> --subject-id <subject_id> --root <registry-root> --json
python skills/auto-cut-profile-onboarding/scripts/profile_registry.py list --root <registry-root> --json
python skills/auto-cut-profile-onboarding/scripts/profile_registry.py validate --root <registry-root> --json
```

Continue only after the exact check returns `status=ready` and every copied evidence file still matches its stored SHA-256.

## 5. Bind Only After Explicit Confirmation

For `为当前项目建立...`, `把当前项目绑定到...`, or a same-task pointer handoff, wait until the exact profile is ready. Read the current project binding and select the bind/rebind row before persisting the question, then follow the matrix in `references/handoff-contract.md`.

Only when an active resumable review-job wait exists, persist the selected `project_binding` state using that wait's two current replacement digests and the next valid prompt revision. Direct interactive operations without an active wait do not invent replacement digests or call `review-job-wait-open`; they keep the question in the current task.

When the project has no different existing binding, ask exactly:

```text
Bind this ready profile to the current project and continue the original edit?
```

This question is the explicit binding confirmation for a new binding and keeps `allow_rebind=False`. Bind only after an affirmative response. If the project is already bound to a different profile, do not use or persist this generic question: select the matrix's distinct question first, disclose both `<current_profile_key>` and `<new_profile_key>`, and keep `allow_rebind=False` until the user explicitly confirms replacement.

Resolve `<project_key>` from `project.project_key` in the current revision request, or use the repository's existing `infer_project_family` helper on the current draft name. Use the absolute current draft directory as `<project_path>` and its configured drafts root as `<project_root>`. If this current-project context is unavailable, create/update the profile only and do not guess a binding from course content.

```powershell
python skills/auto-cut-profile-onboarding/scripts/project_bindings.py bind --project-key <project_key> --project-path <project_path> --project-root <project_root> --stage-id <stage_id> --subject-id <subject_id> --root <registry-root> --json
```

For an explicitly confirmed rebind, repeat the same command with `--rebind`; the function-level equivalent is `allow_rebind=True`. A plain `建立...配置档案` command without current-project binding wording creates the profile only. Project bindings are stored under the target-local profile root in `project-bindings.json`.

After a ready profile is explicitly bound, return the preserved original items to `auto-cut-pointer-targeting`. Pointer targeting must reload the binding, run a fresh registry `check`, recalculate the asset, scale-reference, and approved-preview hashes, and verify the requested role plus every current layout before it writes a pointer. No stale handoff receipt on return. This skill does not calculate target positions, keyframes, duration, or landing proof.

## Non-Negotiable Guardrails

- 禁止跨学科: never reuse another subject's graphic, hotspot, ratio, or preview.
- 不得复用其他学段: the same subject at another stage is a different profile.
- 不得使用中性内置兜底: no generic hand, arrow, circle, or placeholder substitutes for a missing profile.
- 不得使用居中 0.22 or another fixed-scale fallback.
- Never create or update a profile without explicit user-supplied identity/evidence, and never bind or rebind without explicit binding confirmation.
- Never bind an `incomplete`, `needs_confirmation`, or `stale` profile.

## Quick Reference

| Need | Resource |
| --- | --- |
| Exact evidence request | `references/intake-contract.md` |
| Intake/stored fields | `references/profile-schema.md` |
| Profile operations | `scripts/profile_registry.py --help` |
| Legacy hand media migration | `scripts/profile_registry.py migrate-hand-media --help` |
| Project bindings | `scripts/project_bindings.py --help` |
| Material guide | `assets/pointer-material-reference.png` |
| Scale guide | `assets/scale-reference-screenshot.png` |
