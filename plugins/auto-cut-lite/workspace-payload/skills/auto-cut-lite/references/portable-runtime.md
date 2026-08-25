# Portable Runtime Contract

This plugin is self-contained under the installed plugin root. On Windows, resolve it without relying on a source checkout:

```powershell
$autoCutLiteReport = Join-Path $env:LOCALAPPDATA 'Auto-Cut\auto-cut-lite\deployment-report.json'
$autoCutLiteDeployment = Get-Content -Raw -Encoding UTF8 $autoCutLiteReport | ConvertFrom-Json
$autoCutLiteRoot = $autoCutLiteDeployment.target_root
$autoCutLiteRuntime = Join-Path $autoCutLiteRoot 'runtime'
$autoCutLitePython = Join-Path $autoCutLiteRoot '.runtime-venv\Scripts\python.exe'
$autoCutLiteAudioPython = Join-Path $autoCutLiteRoot 'runtime\.venv-audio\Scripts\python.exe'
$autoCutProfileRoot = Join-Path $env:LOCALAPPDATA 'Auto-Cut\auto-cut-lite\pointer-profiles.local'
```

Before execution, require the plugin manifest, runtime directory, isolated main Python executable, and deployment report with `deployment_status=installed`. Audio restoration additionally requires the isolated audio Python executable and `components.audio_runtime.status=installed`. Stop if any required component is missing. Do not fall back to a source repository, another Auto-Cut checkout, system Python, or files copied from the source computer.

Run commands documented as `python scripts/<name>.py ...` from `$autoCutLiteRuntime`, using `$autoCutLitePython`. Commands under `scripts/audio/` must instead use `$autoCutLiteAudioPython`; never install or import the audio lock into the main environment. Paths documented as `data/...`, `schemas/...`, `presets/...`, or `tools/...` are relative to `$autoCutLiteRuntime`. Skill-local scripts are resolved under `$autoCutLiteRoot\workspace-payload\skills\<skill-name>\scripts` and run with `$autoCutLitePython` unless that skill explicitly requires the audio runtime.

This distribution always uses `workflow_mode=lite`, including when a focused skill is invoked directly. The focused skill keeps its decision and evidence rules, while the lite entrypoint remains authoritative for non-destructive track layout, duration preservation, review markers, replacement-local timebase mapping, and final delivery.

Feishu documents are read only as the current operator with `lark-cli docs +fetch --as user`. Credentials, login state, JianYing account state, project bindings, profile assets, and absolute source-machine paths are target-local and must never be copied into the plugin or delivery ZIP.

Pointer profiles are optional target-local user data under `$autoCutProfileRoot`. The package contains the generic onboarding mechanism and neutral guide images, but no preloaded profile, subject asset, approved preview, project binding, or private media.
