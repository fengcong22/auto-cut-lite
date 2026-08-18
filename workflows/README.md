# Workflow Boundary

This directory documents and hosts project-level Auto-Cut workflows. Reusable JianYing behavior
belongs under `scripts/core/` or `scripts/utils/`; project orchestration belongs under
`workflows/projects/`.

Root `make_tmall_*.py` and `edit_*.py` files are compatibility entrypoints. They remain callable
while project code is migrated incrementally, but new shared behavior should not be added there.

Workflow requirements:

- Configuration: keep local media paths, draft names, and project IDs in explicit project config.
- Optional dependencies: import project-only adapters lazily and fail only when that workflow runs.
- Runtime output: write generated reports, previews, and scratch media under ignored `tmp/` paths.
- Shared code: extract reusable timeline, validation, and draft-writing behavior into `scripts/core/`
  or `scripts/utils/` with focused tests.

See [projects/README.md](projects/README.md) for the compatibility and migration contract.
