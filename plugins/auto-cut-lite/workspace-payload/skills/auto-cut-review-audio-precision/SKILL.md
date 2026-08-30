---
name: auto-cut-review-audio-precision
description: Use when processing Feishu/Lark review-document audio edits that require precise spoken-word deletion, Chinese word/character ASR alignment, must-keep phrase protection, ASR-first review-time-fallback labels for non-executing audio items, reverse validation, and serial upload of finished audio/video back to the review document.
---

# Auto-Cut Review Audio Precision

## Packaged Runtime

Before executing this skill from the distributed plugin, read the [portable runtime contract](../auto-cut-lite/references/portable-runtime.md). Use the installed isolated runtime and `workflow_mode=lite`; do not fall back to a source checkout or another Auto-Cut installation.


## Overview

Use this skill for review-document audio alignment where the job is not generic denoise, but
"read the comments, resolve exactly the requested spoken words, preserve required neighboring
words, write a non-destructive editable split/cut trace, validate source preservation, then append
labeled deliverables back to the document." In Lite, source media is never physically removed.

Load `references/workflow.md` before executing a real review-document audio job.

## Scope

Use this skill when the request includes any of these signals:

- Feishu/Lark document review comments with audio or video attachments
- spoken-word deletion requests such as filler words, repeated phrases, false starts, or tail
  phrases; in Lite these become ASR-resolved logical cut boundaries only
- post-delete pause requests that need an ASR-first, review-time-fallback source-text-only label
  without changing the timeline
- review feedback such as "第二条", "第三条", "所以没有找回来", "语言被吞音"
- a need to upload processed audio/video back to the bottom of the same document
- a need to choose between literal precision and cleaner listening flow
- a need to transfer or reuse Volcengine/Doubao ASR word-position alignment from a local audio engine into this repository's JianYing review workflow

For ordinary JianYing draft revision, combine this with `auto-cut-revision-draft`. For peak/loudness-only requests, use `auto-cut-audio-peak-target`.

For delivery of an editable review draft, run `auto-cut-final-acceptance` after reverse ASR and
Lite pause-label checks pass.

## Strategy Contract

Assign one strategy to every review item before cutting:

- `precision_first`: resolve only the requested phrase's source window. Protect `must_keep` words
  even if the trace boundary is less convenient; Lite never physically removes the window.
- `hybrid`: use ASR boundaries plus small buffers, then tune by listening. This is the default when the review note is clear but not enough to justify removing extra words.
- `listening_first`: optimize the spoken flow. This may remove short fillers, particles, or function words only when the review note or user preference allows that tradeoff.

Never silently switch from `precision_first` to `listening_first`. If deleting an unrequested word sounds better, label it as a proposed tradeoff and keep a literal version or ask when the risk is high.

## Required Review Item Shape

Convert every comment into a structured item before processing:

```text
clip_id:
source:
rough_time:
delete:
must_keep:
strategy: precision_first | hybrid | listening_first
accepted_tradeoffs:
validation:
```

`must_keep` is mandatory when the user complains that a word disappeared, when a phrase is adjacent to the deletion target, or when the instruction says "do not delete X".

## Review Semantics

Classify each review item before matching ASR. Do not flatten every item into one phrase-delete operation.

- `phrase_delete`: delete the exact requested spoken phrase.
- `ellipsis_range_delete`: when the reviewer writes `...` or `……`, delete the full spoken range between the matched prefix and suffix, not only the suffix phrase.
- `colored_span_delete`: when the Feishu/Lark document uses colored spans, preserve span boundaries and create one delete window per colored fragment. Do not join uncolored words between colored spans into the deletion.
- `gap_delete`: in Lite, when the note says delete the pause between two anchors, keep both anchors
  as ASR context when available but create only the source-text label; do not delete the gap or
  filler. Fall back to the review-comment time when ASR fails or is non-unique.
- `pause_timing_review`: in Lite, attempt the adjacent-utterance point with ASR and fall back to the
  review-comment time. Keep only a source-text label; never choose or execute shortening,
  extension, `semantic_pause_adjustment`, or visual-hold repair.
- `tail_particle_delete`: for particles such as `吧`, `啊`, `哈`, `哎`, protect the following word onset and use a precise non-destructive duck/mute pass if the tail remains.

If the review document contains blue or red deletion text, inspect the document markup for the colored spans instead of relying on the plain-text rendering. Treat `text-color="rgb(36,91,219)"` as blue-span evidence and `text-color="rgb(216,57,49)"` as red-span evidence when present. Preserve every marked fragment independently and never absorb uncolored words between two marked spans.

## Alignment Rules

- Prefer configured Volcengine ASR word/character timing for Chinese spoken-word deletion.
- When the project has Volcengine/Doubao ASR connected, treat it as the primary timing alignment source. Feishu Minutes/Miaojì transcripts may be used only as semantic context or a search aid, not as proof of final cut boundaries.
- Whisper may be used as a fallback transcript aid, but do not treat it as authoritative for Chinese character-level cut boundaries when Volc ASR is available.
- If ASR credentials or resource ID are missing, do not execute a spoken deletion. Non-executing
  audio items use their review-comment time; manual listening may supplement review but cannot
  authorize a duration-changing cut.
- When importing ASR capability from another local repository, adapt it as an alignment dependency
  only. Keep this repository's review-item schema, must-keep contract, Lite pause-label gate,
  reverse validation, and editable-draft evidence rules as the controlling workflow.
- Do not infer the final cut window from the rough review timestamp alone. Use rough time only to find the local sentence.
- For repeated phrases, match by local context and adjacent words, not by phrase text alone.
- In Lite, permit a time-anchored high-confidence fuzzy match only when it selects one continuous
  ASR word span, retains at least 80% ordered keyword coverage, and wins uniquely by review-time
  distance and text confidence. Permit one non-tail omitted character for short phrases and two
  for phrases of at least 16 normalized characters; a detached optional discourse tail such as
  `对吧` may also be absent when it belongs to another utterance. Cut only the ASR-recognized
  remainder, record omitted review characters, and never skip intervening ASR words. An inter-word
  gap over 1.5 seconds breaks continuity; a provider utterance boundary alone does not.

## Resumable ASR Evidence

- Cache source alignment only by source audio SHA256 + preprocessing + provider/model/resource ID + adapter version. Missing identity versions, changed preprocessing, or a hash mismatch invalidate it.
- Cache reverse ASR only by complete final candidate audio SHA256 + provider/model/resource ID + adapter version. Every spoken-audio change or new hash still requires one final full-candidate reverse ASR for the delivered candidate.
- A cache hit reuses the matching evidence artifact; cache reuse never turns a required gate into skipped evidence.
- Keep local seam, `must_keep`, and visual-context checks around affected windows with configured context before/after. They supplement the full-candidate reverse-ASR pass and do not replace it.

## Cutting Rules

- In Lite mode, attempt source word/character ASR for every issue locatable through speech or
  audio. Only a uniquely ASR-located precise spoken deletion may execute as a non-destructive
  editable split/cut trace. Semantic pauses,
  pronunciation, breath, mouth noise, speech timing, and every pause addition, extension,
  shortening, or adjustment are non-executing: use the unique ASR point when available and
  otherwise the review-comment time. Fail only when neither source supplies a valid time.

- In Lite, do not use physical segment removal, denoise, or hard mute to implement a spoken-word
  deletion. Preserve the source media and write the ASR-resolved logical split/cut trace instead.
  Physical segment removal is a full-workflow-only operation and is outside this packaged Lite
  contract.
- When a reviewer marks a whole sentence/range for deletion, automatically include tightly adjacent oral fillers and discourse particles that belong to that removed range even if the written note did not spell them out. Examples include `啊`, `嗯`, `呃`, `呢`, `哈`, `这个`, `就是`, `然后`, `那`, `对吧`, and short phrases such as `坦率地讲哈` when they sit between the deleted sentence anchors. Record these as `auto_absorbed_fillers` in evidence.
- Detached filler absorption is narrow. Only absorb oral residue that is not part of the kept sentence, such as an isolated `a/en/e/dui-ba` tail or a half-word left directly against the deleted range. Do not absorb normal neighboring content words such as `na`, `ye`, `da`, `ta`, `jin`, `tong`, `shang`, or `hou-mu` unless the user explicitly accepts that tradeoff.
- Treat normal words on both sides of a logical boundary as hard `must_keep` context. If a wider
  boundary would cross a kept onset or tail, narrow the trace; do not widen it and do not create a
  physical cleanup to simulate deletion.
- Short delete items must keep independent windows. Do not let a one-word or particle delete inherit a longer merged sentence window from an adjacent review item.
- In Lite `split_gap`, write one independent A2 clip per merged authoritative delete window.
  Never replace these clips with one full-length/continuous A2 segment, and never accept a
  pending or empty segmented plan as a writable draft.
- Keep cut windows narrow. Add only the buffer allowed by strategy:
  - `precision_first`: usually no extra buffer, or 0 to 60 ms if it does not touch `must_keep`.
  - `hybrid`: about 80 to 200 ms around ASR boundaries when it improves the join.
  - `listening_first`: may include nearby filler/function words only when listed in `accepted_tradeoffs`.
- Use short crossfades to avoid clicks:
  - fragile character boundary: 8 to 15 ms
  - normal spoken join: 25 to 35 ms
- If a boundary risks swallowing an onset such as "语言", back the start/end window away and use a shorter crossfade.

Separate logical cut tracing from local audio cleanup:

- Lite never shortens video or audio for a spoken deletion. Record the ASR-resolved source window as
  a non-destructive split/cut trace; a pause timing request remains label-only and never authorizes
  a physical operation.
- Do not add a duck/mute cleanup to imitate physical deletion in Lite. Any independently requested
  non-duration audio cleanup follows its own maintained routing and must not alter project duration.

Route every Lite pause request separately from deletion correctness:

- cover every request to add, extend, shorten, or otherwise adjust a pause, including input named
  `semantic_pause_adjustment`;
- attempt the adjacent-utterance point from word/character ASR bound to the current source bytes
  and provider/model identity;
- when ASR yields a unique point, require it to fall in the attributable utterance gap, outside
  spoken words and logical source-aligned delete windows; utterance-only or transcript-only ASR is insufficient
  for claiming an ASR-resolved point;
- set effective `execution_required=false` and place exactly one visible label equal only to
  `source_text` at the unique ASR point, or at the review-comment time when ASR fails or is
  non-unique;
- do not emit `pause_adjustments`, split audio for a pause, create a hold or still frame, add an
  audio gap, alter project duration, or offset any later track, asset, or label;
- fail before draft open/write only when neither ASR nor the review comment supplies a valid time;
  never use `0:00`.

## Validation Rules

After processing every clip:

- Any spoken-audio trace retains one final source-boundary/reverse-ASR report over the delivered
  candidate or source-aligned segments; the report verifies source preservation and boundary
  mapping, not physical phrase removal.
- The candidate duration must cover the normalized segmented delivery timeline; only a two-sided fade at an exactly adjacent audible-segment boundary counts as crossfade overlap, while isolated fades do not shorten the required duration.
- Hash and fully decode the actual candidate bytes. Non-WAV candidates require a complete FFmpeg audio-stream decode, not only container-duration probing.
- Every spoken-delete result row contains attributable local transcript, strategy, source cut windows, mapped join times, explicit `delete_hits`, `keep_hits`, and passing semantic-join validation, all matching that item's request contract and segmented-plan mapping. A generic item-level `status=pass` is invalid.
- The local transcript must contain alphanumeric content, and transcript aliases must agree after normalization. Cross-check hit fields against the normalized local transcript: it must not contain the delete phrase and must support every `must_keep` phrase. A retained same-word occurrence passes only with exactly one positive `delete_hit` and structured `delete_hit_adjudication` fields `classification=kept_recurrence`, `occurrence_role`, `phrase`, `local_context`, `context_anchor`, and `reason`; the local context must occur in the transcript, the context anchor is not a substring of the delete phrase, and that anchor remains after removing the delete phrase from the local context. Multiple positive `delete_hits` or multiple local transcript delete occurrences, including overlapping occurrences, require per-hit adjudication and fail until one-to-many receipts are supported.
- Every spoken-delete row is checked against a non-empty item contract containing strategy and delete plus an explicit `must_keep` field. Each positive `delete_hit` must match the item delete phrase. The candidate SHA-256 participates in the duration cache key so same-path media replacement cannot reuse stale timing.
- The latest canonical doc item kind determines whether the spoken-delete contract applies. Pause
  requests are validated independently as ASR-first, review-time-fallback label-only items and must not produce
  executable pause evidence. A forbidden semantic-join phrase is waived only by
  `pass_adjudicated` with a non-empty reason, `final_gap` between 0 and 0.2 seconds, and
  `no_extra_deletion_contract=pass`.
- Local seam, `must_keep`, and visual-context windows remain targeted; they supplement the full-candidate pass instead of expanding every check to the whole file.
- Re-run ASR on the output; manual transcript review may supplement but cannot replace that gate.
- Confirm each `delete` phrase is gone.
- Confirm every `must_keep` phrase is still present and audible.
- Run a semantic-join scan on the local reverse-ASR text around every join. Delete-absent is not enough. Fail and iterate when the joined text contains residual fragments, duplicated characters, dangling predicates, or broken noun phrases such as `阶段性。成就`, `发发明`, `禅让制。于`, `它说明。`, or `夏朝。立的`.
- Treat any false `must_keep` hit in the reverse-ASR row as unresolved unless a written adjudication proves the phrase is present in nearby audible context. Do not mark the row pass only because the delete phrase is absent.
- Gate protected boundary words separately from delete residue. A clean delete is not complete if the next or previous kept word sounds clipped, and a conservative acoustic warning may be recorded as `boundary_watch` only after reverse ASR and the protected-boundary check pass.
- Listen across each join, not just the removed phrase.
- If the result follows `listening_first` and removed an extra particle/filler, state that in the version label or rationale.
- Keep separate versions when comparing literal precision with cleaner listening flow.

For multi-edit JianYing review jobs, use the V4-style validation gate:

- reverse ASR the processed audio and check every delete item near its mapped join time
- write each spoken-delete `review_items[*].evidence` with the ASR source, cut window, delete phrase, must-keep phrase, strategy, and reverse validation status
- fail the output if a deleted phrase remains near the join and is not explained by a different kept occurrence
- fail the output if `semantic_join_anomalies` is non-empty, including duplicated-character artifacts, residual single-character tails, dangling half-sentences, or broken phrase continuity
- treat same-word recurrence as an adjudication, not an automatic failure, only when local context proves the ASR hit belongs to a later kept phrase such as `这个区域`
- iterate with a new version when reverse ASR exposes residue, especially ultra-short colored-span words such as `它`
- record `pass`, `review`, `fail`, and any `pass_adjudicated` items in the report
- validate every pause request separately from delete accuracy as `execution_required=false`, with
  a current unique source-ASR point or `review_timestamp_fallback`, exact `source_text`, and one
  marker receipt;
- reject any executable `semantic_pause_adjustment`, `pause_adjustments` row, pause-generated media,
  duration change, or later-target offset;
- require final duration to equal source duration and keep all later target times source-aligned.

Do not claim completion while reverse ASR still has unresolved `fail` items.

The repair callback must not mutate the live project and must not write saved draft files directly; it must return a scoped `RevisionRequest` for the raw and prepared scope checks.

For Feishu/Lark review-document drafts, also run `python scripts/jy_wrapper.py revision-validate --request-json "<request.json>" --doc-items-json "<doc_items.json>" --strict --json`. If audio rows lack delete/must-keep validation evidence, the draft is not complete.

Then hand the saved draft and reports to `auto-cut-final-acceptance` so audio pass/fail, visual
evidence, pause-label compliance, and editable structure are judged together.

## Feishu/Lark Return Rules

When the source is a Feishu/Lark document:

- Read the document through the Lark document tooling, not browser scraping.
- Bind and use the current operator's own Feishu user identity. Fetch with
  `lark-cli docs +fetch --as user`; do not use an app/bot identity or silently fall back to one.
  On a new computer, run `lark-cli config default-as user` and
  `lark-cli config strict-mode user` after the operator completes the one-time user login.
  Tokens and login state are target-local and must never be copied from another computer or
  included in a plugin/package.
- Append results at the bottom unless the user asks for another location.
- Upload strictly serially in this order: text label, file, text label, file.
- Do not parallel-upload attachments because document order can become confusing.
- Each label must include clip number, strategy, removed phrase, kept phrase if relevant, and version intent.
- Do not expose `.env` secrets, ASR tokens, app IDs, or access tokens in the document.

## Integrated Audio Engine

Use the maintained repository entrypoints for runtime inspection and physical segment removal:

```powershell
python scripts/audio/audio_cleanup.py doctor
python scripts/audio/remove_spoken_segments.py run "<audio-or-video>" --cut "START,END" --removed-phrase "<delete>"
```

Inspect target-local Volcengine authorization before alignment. Credentials are never bundled; run `volc-config` on the target machine when status requests authorization:

```powershell
python scripts/portable/volc_setup.py status --json
python scripts/portable/volc_setup.py config
.\.venv\Scripts\python.exe scripts/audio/volc_word_align.py "<audio-or-video>" --output "<alignment.json>"
```

Treat the bundled adapter as having run successfully only when its normalized alignment contains `input_sha256`, `service_job_id`, `service_result_sha256`, the request-bound `resource_id`, `adapter_version`, and a non-empty `words` list with finite, positive-duration, monotonically ordered timing rows. When authorization is absent, `volc-status` remains `status=pending`, `code=requires_user_authorization`, and `service_probe=not_run`; keep the workflow degraded and the item unresolved, and do not claim Volcengine or the adapter ran.

The adapter produces normalized word-level alignment only. It does not replace the attributable rich rows required from full-candidate reverse ASR, and it does not replace final acceptance.

Do not claim Respiro-en, DeepFilterNet, Volc ASR, or Whisper were used unless the command output or report shows they actually ran.
