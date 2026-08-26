---
name: auto-cut-review-audio-precision
description: Use when processing Feishu/Lark review-document audio edits that require precise spoken-word deletion, Chinese word/character ASR alignment, must-keep phrase protection, listening-vs-literal strategy choice, post-delete pause timing, visual-aware pause validation, reverse validation, and serial upload of finished audio/video back to the review document.
---

# Auto-Cut Review Audio Precision

## Overview

Use this skill for review-document audio repair where the job is not generic denoise, but "read the comments, remove exactly the requested spoken words, preserve required neighboring words, validate the result, then append labeled deliverables back to the document."

Load `references/workflow.md` before executing a real review-document audio job.

## Scope

Use this skill when the request includes any of these signals:

- Feishu/Lark document review comments with audio or video attachments
- spoken-word deletion such as deleting filler words, repeated phrases, false starts, or tail phrases
- post-delete pause repair, such as shortening a long awkward gap, preserving a visual reading beat, or reviewing a too-short join
- review feedback such as "第二条", "第三条", "所以没有找回来", "语言被吞音"
- a need to upload processed audio/video back to the bottom of the same document
- a need to choose between literal precision and cleaner listening flow
- a need to transfer or reuse Volcengine/Doubao ASR word-position alignment from a local audio engine into this repository's JianYing review workflow

For ordinary JianYing draft revision, combine this with `auto-cut-revision-draft`. For peak/loudness-only requests, use `auto-cut-audio-peak-target`.

For delivery of an editable review draft, run `auto-cut-final-acceptance` after reverse ASR and pause-fit reports pass.

## Strategy Contract

Assign one strategy to every review item before cutting:

- `precision_first`: delete only the requested phrase. Protect `must_keep` words even if the join sounds slightly less clean.
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
- `gap_delete`: when the note says delete the pause between two anchors, keep both anchors in `must_keep` and delete only the gap/filler between them.
- `pause_timing_review`: when a spoken deletion leaves a questionable pause, decide `shorten`, `extend`, `keep`, `semantic_pause_adjustment`, or `visual_hold_review` separately from whether the delete itself succeeded.
- `tail_particle_delete`: for particles such as `吧`, `啊`, `哈`, `哎`, protect the following word onset and use a precise non-destructive duck/mute pass if the tail remains.

If the review document contains blue or red deletion text, inspect the document markup for the colored spans instead of relying on the plain-text rendering. Treat `text-color="rgb(36,91,219)"` as blue-span evidence and `text-color="rgb(216,57,49)"` as red-span evidence when present. Preserve every marked fragment independently and never absorb uncolored words between two marked spans.

## Alignment Rules

- Prefer configured Volcengine ASR word/character timing for Chinese spoken-word deletion.
- When the project has Volcengine/Doubao ASR connected, treat it as the primary timing alignment source. Feishu Minutes/Miaojì transcripts may be used only as semantic context or a search aid, not as proof of final cut boundaries.
- Whisper may be used as a fallback transcript aid, but do not treat it as authoritative for Chinese character-level cut boundaries when Volc ASR is available.
- If ASR credentials or resource ID are missing, say the workflow is degraded before cutting and rely on narrower manual listening windows.
- When importing ASR capability from another local repository, adapt it as an alignment dependency only. Keep this repository's review-item schema, must-keep contract, visual-aware pause gate, reverse validation, and editable-draft evidence rules as the controlling workflow.
- Do not infer the final cut window from the rough review timestamp alone. Use rough time only to find the local sentence.
- For repeated phrases, match by local context and adjacent words, not by phrase text alone.

## Resumable ASR Evidence

- Cache source alignment only by source audio SHA256 + preprocessing + provider/model/resource ID + adapter version. Missing identity versions, changed preprocessing, or a hash mismatch invalidate it.
- Cache reverse ASR only by complete final candidate audio SHA256 + provider/model/resource ID + adapter version. Every spoken-audio change or new hash still requires one final full-candidate reverse ASR for the delivered candidate.
- A cache hit reuses the matching evidence artifact; cache reuse never turns a required gate into skipped evidence.
- Keep local seam, `must_keep`, and visual-context checks around affected windows with configured context before/after. They supplement the full-candidate reverse-ASR pass and do not replace it.

## Cutting Rules

- In Lite mode, use source word/character ASR as the authoritative time source for every issue
  locatable through speech or audio, not only deletion. Semantic pauses, pronunciation, breath,
  mouth noise, and speech timing use the resolved ASR window or adjacent-utterance point for both
  execution and their verbatim label. The review timestamp remains a search hint. Fail before
  draft writing when the ASR identity or boundary evidence is missing.

- Use physical segment removal for spoken-word deletion, not denoise or hard mute, unless the target is pure breath/noise.
- When a reviewer marks a whole sentence/range for deletion, automatically include tightly adjacent oral fillers and discourse particles that belong to that removed range even if the written note did not spell them out. Examples include `啊`, `嗯`, `呃`, `呢`, `哈`, `这个`, `就是`, `然后`, `那`, `对吧`, and short phrases such as `坦率地讲哈` when they sit between the deleted sentence anchors. Record these as `auto_absorbed_fillers` in evidence.
- Detached filler absorption is narrow. Only absorb oral residue that is not part of the kept sentence, such as an isolated `a/en/e/dui-ba` tail or a half-word left directly against the deleted range. Do not absorb normal neighboring content words such as `na`, `ye`, `da`, `ta`, `jin`, `tong`, `shang`, or `hou-mu` unless the user explicitly accepts that tradeoff.
- Treat normal words on both sides of a cut as hard `must_keep` boundary words. If a wider physical cut would swallow a kept onset or tail, back the cut away and use a short post-splice exact duck/mute cleanup only for the proven residue.
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

Separate timeline deletion from local audio cleanup:

- physical deletion must be used when video and audio should shorten together
- exact duck/mute cleanup may be added after the splice for tail residue when further physical deletion would swallow a protected next word
- document the post-splice cleanup as an exact window with the source reason, target phrase, gain, and fade

Separate pause fitting from deletion correctness:

- run pause fitting only after the delete window has passed the spoken-word accuracy check
- for every `semantic_pause_adjustment`, the rough timestamp is a search hint, not an insertion point; resolve it from a real source ASR path whose bytes match the recorded source ASR SHA-256
- bind that ASR to the current source audio SHA-256, source video SHA-256, exact alignment-audio path/hash, and complete ASR identity including provider, model/resource ID, adapter version, and preprocessing; `preprocessing=none` requires source and alignment audio to be byte-identical, while transformed audio requires a structured receipt binding both hashes, tool/version, and parameters; self-hashed unrelated ASR is invalid
- prefer the adjacent utterance gap and place the hold at its interior midpoint, never directly on the ASR-reported previous tail or next onset; recompute positive guard time from the nearest real word end/start as well as the utterance envelope, and reject a resolved source time inside a spoken word, too close to either word edge, or inside a physical delete window; preserve the requested source time and resolved source time separately
- utterance-only ASR is insufficient: each semantic pause requires real word or character timing, and its semantic pause edit and `pause_adjustments` entry must correspond one-to-one before draft generation
- require the still frame's frame source time to equal the resolved source time, bind its SHA-256, successfully seek and decode the current source video within one frame interval plus a small tolerance, and confirm the frame matches source video at pixel level; a frame sampled at the rough timestamp, decoded from a wrong seek position, or copied from another source is invalid
- compile the segmented audio delivery plan after pause alignment, split source/reference segments at the resolved boundary, and keep every audible segment outside the hold
- bind every segmented final reverse-ASR report to its normalized plan with audio delivery plan SHA-256 through `revision-bind-audio-report`; one stable item ID may carry only one semantic pause until one-to-many receipts are supported
- do not decide pause shortening from audio silence alone; inspect nearby animation overlays, page turns, pointer assets, full-screen state holds, and key visual information
- if a join sounds too fast after a valid deletion, judge the semantic boundary before adding time: sentence/range deletions, question-to-answer transitions, section changes, and replacement-video bridges often need a short breath; one-word fillers and within-clause repairs usually should stay tight
- when a too-fast semantic join needs more breath, insert a short silent still-frame hold from the current source/replacement frame instead of widening the delete window; keep the hold as its own video segment with no audio so the original cut points remain visible
- if a candidate pause compression overlaps a visual event's source-time guard window, keep the beat and label it `visual_hold_review` instead of auto-shortening
- visual events include pointer assets with inferred duration/out points and animation timing overlays with stable-frame release windows
- use conservative defaults when no project-specific values exist: about 2.0 seconds before and 1.0 second after a visual event or visual hold window
- report every adjusted pause and every visual-hold skip with source time, draft timeline time, duration, reason, frame/still source when used, and affected review item

## Validation Rules

After processing every clip:

- Any spoken-audio change retains one final full-candidate reverse ASR over the delivered candidate.
- The candidate duration must cover the normalized segmented delivery timeline; only a two-sided fade at an exactly adjacent audible-segment boundary counts as crossfade overlap, while isolated fades do not shorten the required duration.
- Hash and fully decode the actual candidate bytes. Non-WAV candidates require a complete FFmpeg audio-stream decode, not only container-duration probing.
- Every spoken-delete result row contains attributable local transcript, strategy, source cut windows, mapped join times, explicit `delete_hits`, `keep_hits`, and passing semantic-join validation, all matching that item's request contract and segmented-plan mapping. A generic item-level `status=pass` is invalid.
- The local transcript must contain alphanumeric content, and transcript aliases must agree after normalization. Cross-check hit fields against the normalized local transcript: it must not contain the delete phrase and must support every `must_keep` phrase. A retained same-word occurrence passes only with exactly one positive `delete_hit` and structured `delete_hit_adjudication` fields `classification=kept_recurrence`, `occurrence_role`, `phrase`, `local_context`, `context_anchor`, and `reason`; the local context must occur in the transcript, the context anchor is not a substring of the delete phrase, and that anchor remains after removing the delete phrase from the local context. Multiple positive `delete_hits` or multiple local transcript delete occurrences, including overlapping occurrences, require per-hit adjudication and fail until one-to-many receipts are supported.
- Every spoken-delete row is checked against a non-empty item contract containing strategy and delete plus an explicit `must_keep` field. Each positive `delete_hit` must match the item delete phrase. The candidate SHA-256 participates in the duration cache key so same-path media replacement cannot reuse stale timing.
- The latest canonical doc item kind determines whether the spoken contract applies. Spoken-delete and semantic-pause evidence are validated independently, even under the same item ID. A forbidden semantic-join phrase is waived only by `pass_adjudicated` with a non-empty reason, `final_gap` between 0 and 0.2 seconds, and `no_extra_deletion_contract=pass`.
- Local seam, `must_keep`, and visual-context windows remain targeted; they supplement the full-candidate pass instead of expanding every check to the whole file.
- Re-run ASR or do a manual transcript check on the output.
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
- validate pause timing separately from delete accuracy. A `visual_hold_review` can be intentional for picture rhythm even when the measured audio gap is longer than the audio-only target range.
- validate `semantic_pause_adjustment` rows separately from delete accuracy. A short silent still-frame hold is acceptable when it repairs a too-fast semantic join and does not hide deletion residue, swallow protected words, or flatten the editable timeline.
- a semantic-pause row passes only when its source ASR path/hash/identity equal the immutable request configuration and current bytes, source audio SHA-256 and source video SHA-256 match the current media, alignment-audio preprocessing has byte-identity or a complete transformation receipt, its requested source time and resolved source time bind the midpoint of a real utterance gap outside every spoken word with the configured guard from the nearest real word end/start, its frame source time equals that midpoint and the decoded frame time/pixels match source video, its saved audio delivery plan was compiled after pause alignment and bound by audio delivery plan SHA-256, and full-candidate reverse ASR confirms both the preceding sentence tail and following sentence onset remain complete

Do not claim completion while reverse ASR still has unresolved `fail` items.

The repair callback must not mutate the live project and must not write saved draft files directly; it must return a scoped `RevisionRequest` for the raw and prepared scope checks.

For Feishu/Lark review-document drafts, also run `python scripts/jy_wrapper.py revision-validate --request-json "<request.json>" --doc-items-json "<doc_items.json>" --strict --json`. If audio rows lack delete/must-keep validation evidence, the draft is not complete.

Then hand the saved draft and reports to `auto-cut-final-acceptance` so audio pass/fail, visual evidence, pause timing, and editable structure are judged together.

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
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-status --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-config
.\.venv\Scripts\python.exe scripts/audio/volc_word_align.py "<audio-or-video>" --output "<alignment.json>"
```

Treat the bundled adapter as having run successfully only when its normalized alignment contains `input_sha256`, `service_job_id`, `service_result_sha256`, the request-bound `resource_id`, `adapter_version`, and a non-empty `words` list with finite, positive-duration, monotonically ordered timing rows. When authorization is absent, `volc-status` remains `status=pending`, `code=requires_user_authorization`, and `service_probe=not_run`; keep the workflow degraded and the item unresolved, and do not claim Volcengine or the adapter ran.

The adapter produces normalized word-level alignment only. It does not replace the attributable rich rows required from full-candidate reverse ASR, and it does not replace final acceptance.

Do not claim Respiro-en, DeepFilterNet, Volc ASR, or Whisper were used unless the command output or report shows they actually ran.
