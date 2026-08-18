# Project Workflows

The repository currently keeps root `make_tmall_*.py` and `edit_*.py` scripts as compatibility
project entrypoints. Existing automation may continue invoking them by filename.

For new work or incremental migration:

1. Put project configuration and orchestration under `workflows/projects/<project>/`.
2. Keep optional dependencies project-scoped and lazily loaded.
3. Store generated output under ignored `tmp/` directories, never beside reusable source modules.
4. Move reusable logic to `scripts/core/` or `scripts/utils/`, preserving a thin root entrypoint.
5. Add tests for the extracted shared behavior before switching the compatibility entrypoint.

This boundary avoids treating hard-coded material paths and project-specific adapters as general
Auto-Cut APIs while preserving current command lines during migration.
