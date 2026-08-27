# Minimal Command SOP

Use this SOP for simple editing requests to minimize noisy exploration.

## Goal

- Produce one runnable script
- Run once
- Validate result with the shared playbook checklist

## Source-document Review Jobs

This single-script SOP is only for simple jobs without a source review document. For
natural-language Feishu/Lark source-document work, use the maintained resumable runner and keep
all runtime artifacts under an ignored `tmp/` job directory:

```powershell
python scripts/jy_wrapper.py review-document-run `
  --snapshot-json "tmp/review-job/document_snapshot.json" `
  --project-json "tmp/review-job/project.json" `
  --job-root "tmp/review-job" `
  --drafts-root "<JianYing draft root>" `
  --package-zip "<output>\Auto-Cut-Lite.zip" `
  --json
```

After the phase executor has created `job_state.json`, inspect state or one cache entry without changing either:

```powershell
python scripts/jy_wrapper.py review-job-status --state-json "tmp/review-job/job_state.json" --json
python scripts/jy_wrapper.py review-job-cache-inspect --cache-root "tmp/review-job/cache" --namespace "source_asr" --digest "<sha256>" --json
```

`review-document-run` writes canonical inputs, `job_state.json`, `job_timing.json`, phase receipts,
the editable draft, and the validated delivery ZIP. Re-running the same command resumes only
valid completed phases. `review-job-status` is read-only and does not mutate the checkpoint or
draft. `review-job-cache-inspect` is also read-only: it validates one cache entry and does not
mutate the cache, expose identity inputs, or return secrets. Legacy compile/direct commands remain
available for compatibility, but must produce the same resumable evidence before claiming
optimized/source-document final acceptance.

## Step 1: Minimal Environment Check (2 commands max)

```powershell
python --version
Test-Path (Join-Path $env:LOCALAPPDATA "JianyingPro\User Data\Projects\com.lveditor.draft")
```

If the draft path differs, pass `drafts_root=...` to `JyProject(...)` or `--drafts-root ...` to `scripts/jy_wrapper.py`.

## Step 2: Asset Check (1 command)

```powershell
Get-ChildItem .agent\skills\Auto-Cut\assets -File
```

Do not recursively scan the whole workspace unless asset lookup fails.

## Step 3: Script Generation

- Create exactly one script in workspace root, e.g. `simple_edit.py`.
- Use deterministic APIs only:
  - `JyProject(...)`
  - `add_media_safe(...)`
  - `add_cloud_media(...)` or `add_cloud_music(...)`
  - `add_text_simple(...)` / `add_rich_text(...)` / `add_complex_text(...)`
  - `add_review_markers(...)` for revision-driven edit requests
  - `save()`

## Step 4: Single Execution

```powershell
python simple_edit.py
```

If failed, patch script once based on the concrete error, then rerun once.

## Step 5: Acceptance Validation (Mandatory)

Use the Acceptance Checklist in `docs/agent-playbook.md`. For revision-driven tasks, verify review-marker tracks and the marker manifest, or explicitly explain why markers were skipped.

## Error Handling Rules

- `SegmentOverlap`: move clip start to track end or separate track.
- Missing asset: switch to known local asset or valid cloud ID.
- Import/path failure: set `JY_SKILL_ROOT`, rerun bootstrap.

## Anti-Patterns (Avoid)

- Repeated `Get-ChildItem -Recurse` over full workspace
- Temporary introspection files (`*_dir.txt`, `output.txt`) unless debugging is explicitly requested
- Multiple style/effect searches before first successful draft generation
