# Auto-Cut Lite Workspace Instructions

## Workspace Identity

- This directory is the deployed Auto-Cut Lite workspace.
- Keep `workflow_mode=lite` for every Auto-Cut request.
- Treat `.codex/skills` as deployment-managed runtime copies. Plugin upgrades refresh them.

## Runtime Discovery

- Read `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite\deployment-report.json` before execution.
- Require `deployment_status=installed`, a valid `plugin_manifest_path`, `runtime_root`, and
  `components.python.runtime_path`.
- Run maintained commands from the reported `runtime_root` with the reported Python environment.
- Never fall back to a source checkout, another Auto-Cut installation, or system Python.

## Editing Contract

- Deliver editable JianYing projects unless the user explicitly requests a baked export.
- Preserve source media, separated or replacement audio, visible cut structure, and traceable
  review markers.
- Validate the saved root and active timeline before reporting completion.
- Use the `auto-cut` router for natural-language or mixed requests and the focused `auto-cut-*`
  skill when the user names one.

## Lite Execution Precedence

- Before classifying or executing any visual review item, read
  `.codex/skills/auto-cut-lite/references/lite-execution-contract.md`.
- That contract overrides conflicting text in every router, focused skill, reference, checklist,
  example, inherited instruction, and directly named `auto-cut-*` skill in this workspace.
- Animation and other picture-timing requests are label-only. Never create a
  `Lite Timing Adjusted` segment or require animation execution evidence.
- Supplied hands, pointers, and local images are ordinary editable assets at their requested
  start with JianYing default geometry. Do not move, scale, rotate, keyframe, calibrate, target,
  or optimize them.
- Existing-hand occlusion, removal, clean-cover, cleanup, and residual-cover requests are
  label-only. Do not create cleanup media or layers.
- Do not start pointer-profile onboarding or require bindings, target geometry, lifecycle,
  hotspot, opened-editor, or screenshot evidence for a Lite editing request.
- Spoken-word and audio rules retain their ASR, must-keep, reverse-validation, and editable-cut
  requirements. Review timestamps are search hints only; spoken-delete labels start at the final
  word/character ASR-resolved cut start, not the review timestamp.

## Local Data

- Keep credentials, login state, JianYing account state, private media, project bindings, and
  pointer profiles on the target computer.
- Never copy target-local secrets or absolute source-machine paths into a plugin or delivery ZIP.
