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

## Local Data

- Keep credentials, login state, JianYing account state, private media, project bindings, and
  pointer profiles on the target computer.
- Never copy target-local secrets or absolute source-machine paths into a plugin or delivery ZIP.
