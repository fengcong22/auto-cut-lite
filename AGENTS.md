# Repository Instructions

## First-use Skill Installation

- Before first using this package on a machine or after cloning/updating the repository, install the bundled repository skills locally:
  ```powershell
  python scripts/install_repo_skills.py
  ```
- This copies `skills/auto-cut*` into the repository-local `.codex/skills` directory, not the global Codex skills directory.
- Do this before relying on `$auto-cut` or any `auto-cut-*` skill name; otherwise the current runtime may not discover the package-specific skills.
- Use `python scripts/install_repo_skills.py --list` to inspect the bundled skill set without copying.
- After installation, tell the user/developer the three supported usage modes:
  1. Natural language: “帮我把这个草稿换个更适合的 BGM，花字不要挡字幕。”
     - Route through the `auto-cut` entrypoint and choose the right child skill automatically.
  2. Router skill: “用 `auto-cut` 分析一下这个需求该怎么做。”
     - Use the router to decide whether BGM, revision, audio peak, safe-zone, or multiple skills are needed.
  3. Targeted skill: “用 `auto-cut-music-library-bgm` 换 BGM。”
     - Use this when the exact capability is already known.
- Treat `skills/` as the source of truth. Installed copies under `.codex/skills` are refreshable runtime copies.
- When adding a new skill later, keep the `auto-cut-<purpose>` name prefix, update both skill catalogs, and rerun `python scripts/install_repo_skills.py` so the router and natural-language usage keep working.

## Blocking User Actions And Optional Feishu Alerts

- Read `skills/auto-cut/references/user-action-required.md` whenever Auto-Cut is genuinely blocked on user evidence, approval, authorization, or high-risk confirmation.
- Follow this order:
  1. Persist the durable job wait, or post the full blocking question in the originating Codex task for an interactive-only action.
  2. Classify the blocker with one of the six closed action codes.
  3. Invoke the optional notification helper.
  4. Keep the originating Codex task waiting on the already-visible question.
  5. Accept resolution only in the originating Codex task.
- Feishu is notification-only. Replies, reactions, and messages never approve or resolve an operation.
- Notification failure never changes the persisted wait or the editing workflow. Do not expose raw provider output, destination IDs, project keys, item IDs, absolute paths, credentials, source text, or media content in notification reports.
- Subject-pointer actions use their current verified durable sidecar. `authorization` and `high_risk_confirmation` are interactive-only and never create or reuse the subject-pointer `external_wait`.

## Iron Law for JianYing Revision Work

- Always deliver a JianYing editable project, not a baked final export disguised as a draft.
- For screenshot-driven or review-driven revisions, the user must be able to inspect the timeline and continue secondary editing without rebuilding the structure.
- The draft must preserve:
  - source video material
  - separated or source audio material
  - replacement audio material when used
  - visible cut points and split boundaries
  - one in-draft marker or label per requested source item
- Never replace a revision project with only one full-length flattened video segment plus one full-length flattened audio segment unless the user explicitly asks for a baked export.
- If a special effect cannot be represented directly, bake only that local window. The rest of the timeline must remain segmented and editable.
- For revision jobs, also output an in-draft edit trace or review labeling layer so the user can see what was changed and where.

## Validation Gate

Before claiming completion, inspect the saved draft and fail the task if any of the following is true:

- the main video timeline for a multi-edit job collapsed into one full-length segment
- the draft only contains preview-style `Final Video` / `Final Audio` tracks
- required source or replacement materials are missing from draft metadata
- review markers are missing, merged, or not independently traceable
- the draft no longer exposes the edit process well enough for second-stage manual editing

## Reporting

When returning a revision result, report:

- the editable draft name and path
- which requirements were changed
- where the review markers or edit-trace lanes were written
- any local windows that had to be baked for effect reasons

## Revision Marker Semantics

- Distinguish strictly between `修改` and `校对` items in revision-driven drafts.
- `修改...` means the request was executed as a real timeline edit such as delete, replace, trim, split, or another operation that must leave visible cut structure on the main editable tracks.
- `校对...` means the request is preserved as a review/check item for manual verification or second-stage adjustment and does not, by itself, imply a visible cut on the main video or audio tracks.
- Do not present `校对` markers as evidence that a cut was already executed.
- If a request is preserved only as `校对`, state that clearly in the draft report and keep it independently traceable on marker tracks.
- If the user expects every marked item to create a visible cut, treat that as an explicit execution requirement rather than a marker-only review requirement.

## Source-ledger Marker Fidelity

- For a source-ledger item, the visible marker text must equal `source_text` code-point-for-code-point, including timestamps, punctuation, whitespace, line breaks, and literal question marks.
- Never replace source-ledger text with a summary, prefix, item ID, translation, or truncation unless that content already exists in `source_text`.
- Keep `execution_status`, `label_only_unresolved`, `verbatim_status`, item IDs, warnings, and
  diagnostic wording only in internal request metadata, marker receipts, validation output, and
  reports. Never concatenate any of them into `source_text` or a visible JianYing label.
- Create exactly one marker per stable source item ID. Multiple internal edits for that item share the marker; duplicate IDs fail validation, while identical text under different IDs remains separate.
- Treat the latest `doc_items` document read as canonical. If an internal row lacks source text, automatically re-read the document; if recovery is unavailable, use the most complete unmodified candidate with `verbatim_status=unverified_source_unavailable` and a warning.
- Source-text recovery is nonblocking: it must not stop editing, remove an item, prevent draft generation, or silently claim exactness.
- Validate marker text, count, receipt mapping, and mapped timeline start in both the saved root and active timeline draft content. An explicit empty ledger rejects old extra markers.
- When a newly encountered issue has a reliable authoritative start but no safe maintained
  implementation, do not improvise a fix. Keep one visible label equal only to its `source_text`,
  record `execution_status=label_only_unresolved` only in internal metadata/receipts/reports, and
  continue independent items. Treat that item as non-executing until a maintained solution exists.
- In Lite, every newly encountered or unrecognized instruction is label-only by default. A new
  executable category requires an explicit maintained allowlist and tests; never infer execution
  from generic words such as “修改”, “添加”, or “调整”.
- That downgrade never authorizes guessed timing. Attempt speech/audio ASR first. If ASR cannot
  uniquely locate a non-executing item, use the time written in the review comment. Fail before
  draft writing only when neither ASR nor the review comment supplies a valid time; never use `0:00`.

## Review Document Audio Precision

- For Feishu/Lark review documents that ask for spoken-word deletion or audio repair, use `skills/auto-cut-review-audio-precision/SKILL.md` before cutting.
- Convert every review comment into `clip_id`, `source`, `rough_time`, `delete`, `must_keep`, `strategy`, `accepted_tradeoffs`, and `validation`.
- In Lite mode, every issue that can be located from speech or audio must attempt configured Chinese
  word/character ASR before execution. This includes spoken deletion,
  pause addition/extension/shortening/adjustment, pronunciation, breath, mouth noise, and speech
  timing. A precisely requested spoken deletion uses a unique ASR-resolved boundary for a
  non-destructive editable split/cut trace and label; source media remains complete and the
  timeline is unchanged. Every pause or other non-executing item uses a unique ASR point when
  available, otherwise the review-comment time, and is never executed in Lite.
- Review-only and ASR-unresolved items use the timestamp written in the review comment.
  For target wording such as `07:14 ... 提前到 07:12`, use the requested target `07:12`.
  A point timestamp is sufficient for a two-second label; never map missing timing to `0:00`.
- Protect `must_keep` phrases explicitly. Do not widen the logical source-aligned boundary across
  adjacent words unless the item is marked `listening_first` or the user accepts that tradeoff.
- Validate the output by confirming the ASR boundary, source-preserving split ranges, and
  `must_keep` context. Lite does not require the requested phrase to be absent because no source
  media is physically removed.
- When returning results to a document, append label then file, label then file, strictly serially so attachments stay in the intended order.

## Lite A1/A2 Audio Layout

- In Lite `split_gap`, A1 contains exactly the complement of the authoritative ASR delete windows.
- A2 contains exactly one independent, audible, source-aligned clip for each logical delete window
  on one `Lite Reused Audio` track. Overlapping windows may merge only when they belong to the same
  stable source item. Adjacent windows from different items remain separate; true overlap between
  different items fails before draft writing and requires disambiguation.
- A full-length, continuous, cross-item-merged, muted, or otherwise mismatched A2 segment is invalid.
- A pending or empty segmented audio plan must fail before draft creation; it must never bypass
  split-gap construction or be treated as a successful audio delivery.

## Lite Timeline Duration And Layout

- Lite deletion is always non-destructive: a uniquely ASR-proved spoken deletion creates only a
  source-aligned editable split/cut trace. It never removes media, compresses the timeline, or
  offsets later material. Lite never executes any other request that can change duration,
  including pause, speed, hold, or still-frame changes. Final project duration therefore equals
  source duration even though V1/A1 keep windows and V2/A2 delete windows remain segmented.
- Treat every pause request, including `+1s`, `-1s`, and `semantic_pause_adjustment`, as
  label-only. Place exactly one visible label equal only to `source_text` at a unique ASR point or,
  when ASR cannot locate it, at the review-comment time.
- Do not create a pause edit, `pause_adjustments` row, hold, still-frame segment, audio gap, or
  time offset. Do not change total duration or move any later V1/V2/A1/A2 segment, visual asset,
  or review label because of a pause request.
- If a hand/pointer row omitted its material, reuse only a unique material from another row with
  the same normalized modification name. Multiple candidates require structured user selection.
- `lite_cut_layout=copy` is historical read/validation compatibility only. Reject it for every new
  Lite execution; new tasks must use `split_gap`.

## Packaged Runtime Integrity

- Before a non-mock task uses an installed Auto-Cut Lite runtime, validate the deployment report,
  package-manifest anchor, and installed runtime inventory.
- Any missing, changed, extra, or hash-mismatched managed runtime file is deployment drift. Stop
  before task execution and require redeployment from a verified package. Never hot-patch the
  installed runtime, accept the drift, or fall back to another checkout.
- Target-local asset indexes (`data/*.local.csv`, `data/jy_cached_audio.csv`, the packaged cloud
  index refreshes, and `assets/jy_sync/**`) are intentional mutable caches; they are not code
  changes and are excluded from the immutable runtime inventory check.

## Resumable Review Pipeline

- Natural-language source-document jobs use `review-document-run` by default. That maintained
  command owns compilation, strict content-addressed cache reuse, source ASR, full-candidate
  reverse ASR, editable draft writing, final acceptance, ZIP publication, and phase recovery.
- `review-job-compile` and `revision-run` remain low-level compatibility commands for callers that
  already have canonical intermediate files. They cannot claim optimized/source-document final
  acceptance without the same saved evidence and completed phase receipts.
- Keep runtime cache, checkpoints, compiled inputs, and timing artifacts under ignored `tmp/` paths.
- Each resumable job writes `job_state.json` and `job_timing.json`. Phase reporting includes `started_at`, `finished_at`, `elapsed_seconds`, `active_seconds`, `wait_seconds`, cache hit/miss through `cache_hit`, `retry_count`, `worker_count` where available, `item_ids`, `input_digest`, `output_digest`, and `unresolved_item_ids`. Reports contain no secrets.
- The final user report separates active compute, external API wait, and application/user blocking. The first optimized real job establishes the baseline; compare later runs by media duration and item/gate mix, not wall clock alone.
- Optimization cannot remove evidence, flatten the draft, import full QA narration, weaken exact markers, `must_keep`, visual, pointer, or animation gates, or make missing source text block editing.
- Final spoken-audio acceptance requires a real full-candidate reverse-ASR report whose candidate SHA-256 matches the candidate bytes and whose provider/model/adapter identity and result rows are present; aggregate status counts and a generic item-level `pass` are not sufficient.
- Automatic media repair is limited to one attributable attempt, reruns the affected and global gates, does not widen logical source-aligned audio windows or alter unrelated items, and restores the pre-repair draft if the callback aborts, scope validation fails, or the attempted save is structurally invalid. A structurally valid unresolved draft remains available with its path and unresolved item IDs in the failure result.

## Integrated Audio Engine

- The maintained audio engine lives in `audio_sound/`, with presets under `presets/audio_sound/` and thin entrypoints under `scripts/audio/`.
- Use `skills/auto-cut-audio-restoration/SKILL.md` for standalone restoration and local audio diagnostics. Spoken deletion in review-document jobs remains owned by `auto-cut-review-audio-precision`.
- Heavy audio dependencies are isolated in `.venv-audio` and declared only by `requirements-audio.lock`. Never merge Torch, Torchaudio, DeepFilterNet, librosa, intervaltree, or the audio NumPy pin into the main requirements.
- Never install audio dependencies into the main Auto-Cut interpreter, and never let audio cleanup remove `.worktrees`, the main `.venv`, `.omx`, user output, or protected draft artifacts.
- Do not claim Respiro, DeepFilterNet, SpectraMini-style cleanup, or another model-backed stage ran unless the saved report proves actual availability and execution. Unverified floating asset downloads remain disabled.
- `--skip-spectramini` and other skip controls must prevent execution as well as report the stage as skipped.
- Standalone restoration output is rendered audio; it does not replace the editable JianYing contract. A revision draft must still preserve source/reference material, segmented audio, cut structure, and traceable markers.
- `docs/audio-sound/source-migration-manifest.json` is the canonical clean-source migration receipt. It records 50 raw source files plus normalized Git identities and a separate retained legacy license. Provenance files under `docs/audio-sound/upstream-control/` are not current operating instructions.

## Repository Hygiene

- After a task is verified complete, do not leave the repository in a casually dirty state.
- Close each finished task with a clean `git status` unless the user explicitly asked to keep an uncommitted diff or an external blocker prevents cleanup; report that blocker plainly.
- Keep generated runtime outputs under ignored locations such as `tmp/`.
- If the task introduced intended repository changes, stage and commit them before finishing when it is safe to do so.
- If a clean commit is blocked, report the blocker explicitly instead of leaving unexplained modified or untracked repository files behind.
- Do not treat a successful edit or draft generation as complete until repository hygiene has also been handled.
