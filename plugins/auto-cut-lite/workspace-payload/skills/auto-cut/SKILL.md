---
name: auto-cut
description: Use when the user asks in natural language to make, revise, validate, or deliver a JianYing/CapCut project, needs a complete or migratable project, does not know which Auto-Cut skill fits, or combines multiple repository editing capabilities.
---

# Auto-Cut Lite Router

Use this as the stable natural-language and mixed-request entrypoint for the packaged Auto-Cut
Lite workspace. Every request in this distribution uses `workflow_mode=lite`.

Before routing or execution, read:

1. [Lite execution contract](../auto-cut-lite/references/lite-execution-contract.md)
2. [Portable runtime contract](../auto-cut-lite/references/portable-runtime.md)
3. [Packaged skill catalog](references/skill-catalog.md) when skill selection is needed

The Lite execution contract has priority over every selected subskill, even when the user names
that subskill directly.

## Invocation Modes

- Natural language: infer the request and route it without requiring a skill name.
- Router: use `$auto-cut` for mixed work or when the user asks which capability applies.
- Focused: use a named `$auto-cut-*` skill for one capability; keep the Lite contract active.

## Routing

- Generic compact editing and complete Lite workflow: `auto-cut-lite`
- Review-document editable draft: `auto-cut-revision-draft`
- Spoken deletion and precise review audio: `auto-cut-review-audio-precision`
- Standalone restoration: `auto-cut-audio-restoration`
- Waveform peak target: `auto-cut-audio-peak-target`
- JianYing-library BGM: `auto-cut-music-library-bgm`
- Basic oral-video style: `auto-cut-basic-oral-video`
- Ad/oral-video revision: `auto-cut-editable-ad-revision`
- Favorite text assets: `auto-cut-favorite-text-assets`
- Draft cleanup/retention: `auto-cut-draft-retention`
- Final validation or portable delivery: `auto-cut-final-acceptance`
- Animation/page-turn/reveal timing comments: `auto-cut-animation-timing-revision`, label-only
- Supplied hands/arrows/pointers: `auto-cut-pointer-targeting`, default-geometry insertion only
- Existing-hand occlusion/cleanup: `auto-cut-pointer-targeting`, label-only
- Supplied local PNG/JPG: `auto-cut-local-image-overlay-revision`, default-geometry insertion only
- Text/sticker position or flower animation comments:
  `auto-cut-text-safezone-animation-revision`, label-only
- Explicit profile-library administration only: `auto-cut-profile-onboarding`; never route an
  ordinary Lite edit there automatically

For natural-language or mixed review-document work, use `review-document-run` as the public
entrypoint. It compiles one canonical source ledger, applies strict cached ASR and media evidence,
writes the editable draft, validates it, publishes the requested ZIP, and resumes valid phases
from `job_state.json`. Use `review-job-compile` and `revision-run` only as low-level compatibility
commands when a maintained caller already owns canonical intermediate files and equivalent phase
evidence. Never generate a temporary Python runner.

Then apply audio operations, simple supplied-asset insertion, verbatim labels, saved-draft
validation, and requested packaging.
Never route a Lite visual item into full animation retiming, pointer targeting/calibration,
safe-zone movement, cleanup layers, or opened-editor evidence.

Route timing by evidence source before execution. Attempt authoritative word/character ASR for
anything locatable through speech or audio. Only a uniquely ASR-located spoken deletion may
execute; its edit and label use that boundary. Every non-executing item uses a unique ASR point
when available and otherwise the review-comment time. When the comment names a target after
`提前到`/`推迟到`/`移到`/`调到`, use that target. Never fall back to `0:00`.
For split-gap audio, require A2 to contain one independent clip per merged ASR delete window,
never a full-length continuous segment.

Use only the installed runtime from `deployment-report.json`. Never fall back to a source
checkout, another Auto-Cut installation, or system Python.
