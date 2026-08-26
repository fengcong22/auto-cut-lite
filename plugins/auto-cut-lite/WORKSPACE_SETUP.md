# Auto-Cut Lite combined workspace

Auto-Cut Lite uses a combined deployment model:

- The stable Codex workspace contains the extracted package files, `AGENTS.md`, and `.codex/skills`.
- The executable plugin runtime and Python environments remain under
  `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite`.
- `deployment-report.json` and `workspace-install-receipt.json` connect the workspace to that
  runtime.

For the beginner flow, double-click `START-AUTO-CUT-LITE.cmd`. On a first install it opens a folder
picker: selecting an existing directory named exactly `Auto-cut-lite` uses that directory, while
selecting a parent creates or uses its `Auto-cut-lite` child. The complete managed package is
synchronized into the selected workspace, so a temporary extraction and the stable workspace may
be different without splitting the deployed package. The direct PowerShell deployer still defaults
to the current extracted package root when no path or prior receipt exists. On an upgrade, the new
package is extracted separately and its deployer follows this precedence:

```text
explicit -WorkspaceRoot -> existing installation receipt -> current extracted package root
```

The one-click launcher uses China pip/npm mirrors by default. The workspace installer verifies
`PACKAGE-MANIFEST.json`, synchronizes package files, refreshes
`AGENTS.md` and all `auto-cut*` repository skills, removes stale managed package files, and records
hashes in the receipt. Unrelated files are preserved. An unmanaged target file is adopted only when
its content exactly matches the incoming file; a different collision or a modified previously
managed file stops the operation. Package files, skills, and `AGENTS.md` share one rollback.

Do not extract a new ZIP directly over an existing stable workspace. Extract it separately, run the
new deployer, and delete that temporary extraction only after success. Passing a different absolute
`-WorkspaceRoot` whose final folder name is `Auto-cut-lite` performs a verified relocation with
rollback coverage.

The plugin manifest intentionally omits `skills`, and the package has no top-level `skills`
directory. Repository skills are installed only under `<workspace>\.codex\skills`, so Codex should
show scope `repo` and label `Auto-cut-lite` rather than duplicate `Personal` entries.

See `BEGINNER_DEPLOYMENT.md` for copyable first-install and upgrade instructions.
