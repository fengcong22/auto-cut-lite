# Review Audio Precision Workflow

## 0. Compile Or Resume The Canonical Job

For a source-document job, run `review-document-run` and resume valid phases from
`job_state.json`. Use `review-job-compile` only as a low-level compatibility step when a maintained
caller owns the rest of the phase DAG. Reuse source ASR only when source audio SHA256,
preprocessing, provider/model/resource ID, and adapter version all match. Reuse reverse ASR only
for the identical complete final candidate audio SHA256, normalized plan SHA-256, and ASR
identity; any spoken-audio change still needs the final full-candidate gate. Its real attributable
result rows must match each item's strategy, delete/must-keep phrases, source-aligned logical cut windows,
plan-derived join times, local transcript, delete/keep hits, and semantic-join status; aggregate
status counts or a generic item-level pass cannot substitute for rows. Candidate duration must
equal the source timeline. Cache hits never waive `delete`, `must_keep`, semantic-join, or final
acceptance evidence.

The local transcript must contain alphanumeric content, and transcript aliases must agree after normalization. Cross-check each row's hit fields against its normalized local transcript: the transcript must not contain the delete phrase and must support every `must_keep` phrase. A retained same-word occurrence requires exactly one positive `delete_hit` and structured `delete_hit_adjudication` with `classification=kept_recurrence`, `occurrence_role`, `phrase`, `local_context`, `context_anchor`, and `reason`; the local context must occur in the transcript, the context anchor is not a substring of the delete phrase, and that anchor remains after removing the delete phrase from the local context. Multiple positive `delete_hits` or multiple local transcript delete occurrences, including overlapping occurrences, require per-hit adjudication and fail until one-to-many receipts are supported.

Every spoken-delete row is checked against a non-empty item contract containing strategy and delete plus an explicit `must_keep` field. Each positive `delete_hit` must match the item delete phrase. The candidate SHA-256 participates in the duration cache key so same-path media replacement cannot reuse stale timing.

The latest canonical doc item kind determines whether the spoken-delete contract applies. In the
packaged Lite workspace, every pause addition, extension, shortening, or adjustment is validated
independently as an ASR-first, review-time-fallback label-only item and never creates executable pause evidence. A
forbidden semantic-join phrase is waived only by `pass_adjudicated` with a non-empty reason,
`final_gap` between 0 and 0.2 seconds, and `no_extra_deletion_contract=pass`.

## 1. Read And Normalize The Review Document

1. Read the Feishu/Lark document with the Lark document skill/tooling.
2. Identify each audio or video attachment and the review text that belongs to it.
3. Build one structured item per requested edit:

```text
clip_id:
source:
rough_time:
delete:
must_keep:
strategy:
accepted_tradeoffs:
validation:
```

If the review text only says "delete this" near a timestamp, transcribe the local sentence first, then fill `delete` and `must_keep`.

## 2. Choose The Strategy

- Use `precision_first` when a word must not disappear, when the reviewer specified an exact phrase, or when a previous attempt swallowed speech.
- Use `hybrid` for most normal review edits: ASR boundary first, then listening-based edge tuning.
- Use `listening_first` only when the user wants the cleanest spoken flow and accepts dropping short fillers, particles, or function words.

Do not hide the strategy. Put it in internal working notes and the report; the visible JianYing label remains exactly the item's `source_text`.

## 3. Get Alignment Evidence

Prefer a configured Chinese word/character ASR provider whose result records provider, model/resource ID, adapter version, parameters, and candidate SHA-256. Check the integrated runtime first:

```powershell
python scripts/audio/audio_cleanup.py doctor
python scripts/portable/volc_setup.py status --json
```

Volcengine credentials and authorization are target-local and are not bundled. If status reports missing authorization, configure that target machine and check status again before alignment:

```powershell
python scripts/portable/volc_setup.py config
python scripts/portable/volc_setup.py status --json
.\.venv\Scripts\python.exe scripts/audio/volc_word_align.py "<audio-or-video>" --output "<alignment.json>" --phrase "<delete>" --anchor "START,END"
```

Accept a Volcengine adapter run only when the normalized alignment records `input_sha256`, `service_job_id`, `service_result_sha256`, the request-bound `resource_id`, `adapter_version`, and a non-empty `words` list whose timing values are finite, whose intervals have positive duration, and whose rows are monotonic. If authorization is absent, the required state is `status=pending`, `code=requires_user_authorization`, and `service_probe=not_run`; do not execute spoken deletion. Use the review-comment time only for non-executing items, keep `must_keep` intact, and do not claim the service or adapter ran.

The bundled adapter produces normalized alignment only. It does not replace the complete candidate's attributable full-candidate reverse-ASR report with rich per-item result rows, and it does not replace final acceptance. In this packaged Lite workflow, the repository records `delete`, `must_keep`, strategy, buffers, and a source-preserving logical split trace; it never physically removes source media or compresses the timeline. Lite pause-label routing and acceptance status remain separate gates.

## 4. Decide Cut Windows

For each phrase:

1. Locate the phrase in the ASR words list.
2. Check adjacent words and punctuation-insensitive phrase matches.
3. Protect every `must_keep` word.
4. If the request deletes a whole sentence/range, expand the matched ASR window to absorb tightly adjacent oral fillers and discourse particles that belong to that deleted range, even when the reviewer only wrote the core sentence text. Examples include `啊`, `嗯`, `呃`, `呢`, `哈`, `这个`, `就是`, `然后`, `那`, `对吧`, and short discourse phrases such as `坦率地讲哈`. Keep the next real content word outside the cut and record absorbed terms in `auto_absorbed_fillers`.
5. Keep detached-filler absorption narrow. Only absorb isolated oral residue or half-words unrelated to the kept sentence. Normal neighboring words are hard `must_keep` unless the user explicitly accepts a listening-first tradeoff.
6. Do not merge short delete windows into adjacent long sentence windows. A one-word delete such as `na` must keep its own source-time window.
7. Select a cut window by strategy:
   - `precision_first`: phrase start to phrase end as a logical source-time window, narrowed away from protected onsets if needed.
   - `hybrid`: ASR phrase boundary plus small 80 to 200 ms logical-window buffer, then adjust by listening.
   - `listening_first`: include accepted fillers/function words in the logical window only when they are listed in `accepted_tradeoffs`; this never authorizes physical removal in Lite.
8. Choose crossfade:
   - 8 to 15 ms when the boundary touches a character onset or vowel start.
   - 25 to 35 ms for a normal fluent join.

If repeated phrases exist, identify the occurrence by local context, not phrase text alone.

## 5. Record The Lite Split/Cut Trace

Lite never physically removes spoken segments. When a spoken deletion has one unique,
authoritative word/character-ASR boundary, record that source-time interval as a logical cut
window and keep the source media bytes complete. The editable writer represents the interval with
complementary source-aligned lanes: V1/A1 keep intervals and V2/A2 keep the matching delete
intervals at the same timeline times. This exposes the cut without shortening or offsetting the
project.

Do not run `remove_spoken_segments.py`, hard-mute, duck, trim, speed-change, hold, or still-frame
operations for a Lite review-document job. A reverse-ASR candidate may be rendered under ignored
`tmp/` storage solely as a source-time-preserving diagnostic probe; it is not a replacement or
delivery audio asset. Every non-executing or ASR-unresolved item receives only its exact label at
the ASR point or review-comment time.

## 6. Reverse Validate

For each Lite diagnostic candidate and saved split layout:

1. Re-run reverse ASR on the source-time-preserving diagnostic candidate when available.
2. Confirm the logical delete interval maps one-to-one to the V2/A2 source-aligned trace while the
   original source media remains present; do not require the phrase to be absent from the source.
3. Confirm every `must_keep` phrase remains in the audible source-aligned material.
4. Listen from at least 300 ms before to 500 ms after each recorded boundary without changing any
   later target time.
5. If a boundary would swallow an unrequested word, narrow the logical window and revalidate the
   trace; never widen it with physical removal or cleanup.
6. Confirm the saved project duration equals the source duration and no duck/mute, gap, hold,
   still-frame, speed, or offset was introduced.

Typical failure checks:

- "语言" sounds clipped: the logical window or crossfade touched the onset. Move the boundary
  away and shorten the trace crossfade; do not alter the source media.
- "所以" disappeared from an audible source-aligned lane: it was not listed as `must_keep`, or a
  broad logical window crossed into the next word. Add it to `must_keep` and re-resolve ASR.
- A diagnostic candidate may be silent inside a logical delete window, but that probe is never
  imported as delivery audio and does not change the source timeline.

## 7. Append Back To The Document

Append at the bottom in strict serial order:

```text
第N条 - Vx - strategy - 删除: ... - 保留: ... - 说明: ...
<uploaded file>
```

Upload one label and one file at a time. Do not launch parallel uploads.

## 8. Final Report

Report:

- document read status
- number of clips processed
- strategy used per clip
- ASR result and any review-time fallback used for non-executing items
- output paths
- validation result for each logical delete trace and protected `must_keep` phrase (source media is
  preserved in Lite)
- any accepted listening tradeoffs

## 9. V4 Precision Gate For JianYing Drafts

Use this gate when Feishu/Lark review-document audio edits are delivered as an editable JianYing draft.

### Preserve Review Semantics

- `colored_span_delete`: inspect document markup and keep colored fragments separate. If only `你看。那个`, `它`, and `是不是` are blue, create three delete windows and preserve the uncolored words between them.
- `gap_delete`: in Lite, keep both anchors as ASR context and create only the exact source-text
  label at the unique gap point, or at the review-comment time when ASR fails or is non-unique. Do
  not delete the gap or filler.
- `ellipsis_range_delete`: record the full spoken range between prefix and suffix as one logical
  source-time window. Do not match only the suffix phrase, and do not physically remove the range
  in Lite.
- `tail_particle_delete`: protect the next word onset. If the particle tail remains, narrow the
  logical trace and keep the source media; do not add a post-splice duck/mute cleanup in Lite.

### Keep Timeline And Audio Cleanup Separate

- Use source-time logical cut windows for uniquely ASR-located spoken deletions. In Lite these are
  source-preserving split boundaries only; they never shorten video or audio together.
- Keep the complete source-aligned diagnostic candidate under `tmp/` when reverse ASR needs it.
  Mark it validation-only and never import it as a replacement or delivery track.
- For editable delivery, write V1/A1 complement segments and one independent V2/A2 source-aligned
  clip per logical delete window. Keep all lanes at their original source times and preserve the
  complete source media.
- Do not map an `exact_window_cleanup.py` duck/mute window for a Lite spoken deletion. Independently
  requested non-duration cleanup must follow its own maintained route and still preserve total
  duration.

### Lite Pause Label Gate

Run this gate independently from delete accuracy for every request to add, extend, shorten, or
otherwise adjust a pause, including input named `semantic_pause_adjustment`.

1. Attempt authoritative word/character ASR bound to the current source audio bytes,
   provider/model or resource identity, adapter version, and ordered timing rows.
2. If ASR yields one unique adjacent-utterance point, require it to be attributable to the current
   source and outside spoken words and logical delete windows. Utterance-only or transcript-only
   ASR is insufficient for claiming an ASR-resolved point.
3. Set effective `execution_required=false`. Place exactly one visible label equal only to the
   source item's `source_text` at the unique ASR point, or at the review-comment time when ASR fails
   or is non-unique.
4. Do not create executable `pause_adjustments`, split audio for a pause, create a hold or still
   frame, add an audio gap, change project duration, or offset any later video, audio, asset, or
   label target.
5. Require final duration to equal source duration and all later targets to retain their original
   source-aligned times. Any pause-derived media, duration extension, or offset fails Lite
   acceptance.
6. Fail before draft open/write only when neither ASR nor the review comment supplies a valid time.
   Never use `0:00`.

### Reverse-ASR Validation

1. Reverse-ASR the final processed audio.
2. For every delete item, map its source cut start to the processed timeline and inspect a local window around the join.
3. Run semantic-join validation on that local window before accepting the row. Flag duplicated characters, orphan particles, residual half-words, dangling predicates, and broken phrase continuity. Known regression patterns include `阶段性。成就`, `发发明`, `禅让制。于`, `它说明。`, and `夏朝。立的`.
4. Treat `keep_hits` false as unresolved unless a written adjudication proves the must-keep phrase remains audible in adjacent context.
5. Mark each row as:
   - `pass`: deleted phrase absent and must-keep context is present or not applicable
   - `review`: must-keep is not confirmed or there is large partial overlap
   - `fail`: deleted phrase is still present near the join
   - `pass_adjudicated`: a same-word hit is proven to be a later kept occurrence by local context
6. Iterate to a new version when there is any unresolved `fail`, `review`, false `must_keep`, or semantic-join anomaly.
7. Use `pass_adjudicated` only with local evidence. Example: deleting an earlier `这个` can still leave the later kept phrase `这个区域`; that is not a deletion failure.

Do not claim a precise audio revision complete until the reverse-ASR report has zero unresolved `fail` rows.

Allow at most one automatic targeted repair. Invoke it on a deep copy, validate the raw callback result against the authorized scope before normalization or media access, then normalize/preflight and run the same scope check again before saving. In Lite a repair may only narrow a logical source-aligned trace or fix metadata; it must not physically remove, mute, widen the delete window across `must_keep`, alter unrelated review items, or skip the global acceptance gates. Restore the pre-repair files if the callback aborts, scope validation fails, or the attempted save is structurally invalid. When a structurally valid repair still fails media acceptance, preserve it and report the draft path plus unresolved item IDs for manual handling.

The repair callback must not mutate the live project and must not write saved draft files directly; it must return a scoped `RevisionRequest` for those raw and prepared scope checks.
