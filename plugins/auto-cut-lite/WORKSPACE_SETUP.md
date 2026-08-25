# Auto-Cut Lite workspace

Deployment keeps the isolated runtime under `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite`. By default it
installs the skill surface under:

```text
%USERPROFILE%\Documents\Codex\Auto-cut-lite\.codex\skills
```

To use another parent directory, deploy with an absolute path whose final folder name remains
`Auto-cut-lite`:

```powershell
.\deploy-to-codex.ps1 -WorkspaceRoot "D:\CodexWorkspaces\Auto-cut-lite"
```

Open the reported `workspace_root` as the Codex workspace and start a new thread. The Skills page
will then discover the Auto-Cut skills with repository scope and display the workspace label
`Auto-cut-lite` instead of `Personal`. On upgrade, omitting `-WorkspaceRoot` reuses the installed
workspace path from the previous receipt before falling back to the default Documents path. Passing
a different explicit path performs a verified relocation: the old managed skills and `AGENTS.md`
are moved only after their hashes match the active receipt, and deployment rollback restores the
previous workspace.

The workspace installer refreshes only `AGENTS.md` and skill directories whose names are
`auto-cut` or begin with `auto-cut-`. Other workspace files are preserved. Upgrade backups and the
workspace installation receipt are kept under `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite`.

The plugin stores its deployable skill payload under `workspace-payload/skills`. It intentionally
has no top-level `skills` directory, preventing Codex from auto-discovering the same skills at
user scope and displaying duplicate `Personal` entries.
