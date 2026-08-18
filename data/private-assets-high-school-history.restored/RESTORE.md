# Private High-School History Pointer Assets

This private archive is paired with Auto-Cut v1.7.0. Verify the ZIP
SHA-256 before extraction. Run these commands from the extracted Auto-Cut
program directory after extracting this private archive to a separate folder.
Use a new empty registry. Do not merge this intake into an existing registry or
reuse any existing project bindings.

```powershell
$privateRegistry = Join-Path "data" "subject-pointer-profiles.restored-history"
if (Test-Path -LiteralPath $privateRegistry) { throw "new empty registry required" }
New-Item -ItemType Directory -Path $privateRegistry | Out-Null
.\.venv\Scripts\python.exe skills/auto-cut-subject-pointer-onboarding/scripts/profile_registry.py register --input <private-assets>\intake.json --root $privateRegistry --json
.\.venv\Scripts\python.exe skills/auto-cut-subject-pointer-onboarding/scripts/profile_registry.py check --stage-id senior-high --subject-id history --root $privateRegistry --json
.\.venv\Scripts\python.exe skills/auto-cut-subject-pointer-onboarding/scripts/profile_registry.py validate --root $privateRegistry --json
```

Continue only when the exact check returns `status=ready` and validation has no
problems. This archive contains no project binding. Ask the target user for an
explicit confirmation before binding or rebinding any target project.
