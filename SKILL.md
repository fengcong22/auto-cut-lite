# Auto-Cut Compatibility Entry

The repository skill source of truth is [`skills/auto-cut/SKILL.md`](skills/auto-cut/SKILL.md).
This root file remains only for tools and links that still look for `SKILL.md` at the repository root.

Install or refresh all bundled skills in the repository-local runtime directory:

```powershell
python scripts/install_repo_skills.py
```

List the bundled skills without copying them:

```powershell
python scripts/install_repo_skills.py --list
```

Use Auto-Cut in one of three ways:

1. Describe the editing request in natural language and let `auto-cut` route it.
2. Name `auto-cut` when the request needs capability analysis.
3. Name an exact `auto-cut-<purpose>` skill when the capability is already known.

Edit files under `skills/`, then rerun the installer. Treat `.codex/skills` as a refreshable runtime copy.
