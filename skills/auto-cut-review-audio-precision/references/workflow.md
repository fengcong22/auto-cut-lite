# Review Audio Precision Workflow

## 0. Compile Or Resume The Canonical Job

For a source-document job, run `review-document-run` and resume valid phases from `job_state.json`.
Use `review-job-compile` only as a low-level compatibility step when a maintained caller already
owns the rest of the phase DAG. Reuse source ASR only when source audio SHA256, preprocessing,
provider/model/resource ID, and adapter version all match. Reuse reverse ASR only for the
identical complete final candidate audio SHA256, normalized audio plan, and ASR identity; any
spoken-audio change still needs the final full-candidate gate. Every segmented reverse report
also binds the normalized plan SHA-256. Its real attributable result rows must match each item's
strategy, delete/must-keep phrases, physical cut windows, plan-derived join times, local
transcript, delete/keep hits, and semantic-join status; aggregate status counts or a generic
item-level pass cannot substitute for rows. Candidate duration must equal the source timeline.
Validation hashes and fully decodes the actual candidate. Cache hits never waive `delete`,
`must_keep`, semantic-join, or final-acceptance evidence.

The local transcript must contain alphanumeric content, transcript aliases must agree after normalization, and the transcript must prove every `must_keep` phrase. Cross-check each row's hit fields against its normalized local transcript: every retained occurrence is one positive `delete_hit` paired with exactly one `delete_hit_adjudications` receipt. Each receipt contains `classification=kept_recurrence`, the correct `occurrence_role`, `phrase`, independent `local_context`, `context_anchor`, `reason`, and an exact `hit` object with `phrase`, `text`, `start`, `end`, and `match_method`. The hit must match one reported hit exactly, use an allowed item-contract delete phrase, and lie outside every target cut window. Missing, duplicate, tampered, overlapping, or unreported occurrences fail. A single legacy `delete_hit_adjudication` remains valid only for exactly one positive hit. For `colored_span_delete`, use the ordered item-contract `delete_phrases`, not the aggregate display phrase, to determine allowed hits.

Every spoken-delete row is checked against a non-empty item contract containing strategy and delete plus an explicit `must_keep` field. Each positive `delete_hit` must match the item delete phrase. The candidate SHA-256 participates in the duration cache key so same-path media replacement cannot reuse stale timing.

The latest canonical doc item kind determines whether the spoken contract applies. Spoken-delete and semantic-pause evidence are validated independently, even under the same item ID. A forbidden semantic-join phrase is waived only by `pass_adjudicated` with a non-empty reason, `final_gap` between 0 and 0.2 seconds, and `no_extra_deletion_contract=pass`.

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

Do not hide the strategy. Put it in the working notes and final label.

## 3. Get Alignment Evidence

Prefer a configured Chinese word/character ASR provider whose result records provider, model/resource ID, adapter version, parameters, and candidate SHA-256. Check the integrated runtime first:

```powershell
python scripts/audio/audio_cleanup.py doctor
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-status --json
```

Volcengine credentials and authorization are target-local and are not bundled. If status reports missing authorization, configure that target machine and check status again before alignment:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-config
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-status --json
.\.venv\Scripts\python.exe scripts/audio/volc_word_align.py "<audio-or-video>" --output "<alignment.json>" --phrase "<delete>" --anchor "START,END"
```

Accept a Volcengine adapter run only when the normalized alignment records `input_sha256`, `service_job_id`, `service_result_sha256`, the request-bound `resource_id`, `adapter_version`, and a non-empty `words` list whose timing values are finite, whose intervals have positive duration, and whose rows are monotonic. If authorization is absent, the required state is `status=pending`, `code=requires_user_authorization`, and `service_probe=not_run`; report degraded mode, keep the item unresolved for final acceptance, use conservative listening evidence without weakening `must_keep`, and do not claim the service or adapter ran.

The bundled adapter produces normalized alignment only. It does not replace the complete candidate's attributable full-candidate reverse-ASR report with rich per-item result rows, and it does not replace final acceptance. This repository still decides `delete`, `must_keep`, strategy, buffers, physical removal, visual-aware pause handling, and acceptance status.

## 4. Decide Cut Windows

For each phrase:

1. Locate the phrase in the ASR words list.
2. Check adjacent words and punctuation-insensitive phrase matches.
3. Protect every `must_keep` word.
4. If the request deletes a whole sentence/range, expand the matched ASR window to absorb tightly adjacent oral fillers and discourse particles that belong to that deleted range, even when the reviewer only wrote the core sentence text. Examples include `啊`, `嗯`, `呃`, `呢`, `哈`, `这个`, `就是`, `然后`, `那`, `对吧`, and short discourse phrases such as `坦率地讲哈`. Keep the next real content word outside the cut and record absorbed terms in `auto_absorbed_fillers`.
5. Keep detached-filler absorption narrow. Only absorb isolated oral residue or half-words unrelated to the kept sentence. Normal neighboring words are hard `must_keep` unless the user explicitly accepts a listening-first tradeoff.
6. Do not merge short delete windows into adjacent long sentence windows. A one-word delete such as `na` must keep its own source-time window.
7. Select a cut window by strategy:
   - `precision_first`: phrase start to phrase end, shortened away from protected onsets if needed.
   - `hybrid`: ASR phrase boundary plus small 80 to 200 ms buffer, then adjust by listening.
   - `listening_first`: include accepted fillers/function words only when they are listed in `accepted_tradeoffs`.
8. Choose crossfade:
   - 8 to 15 ms when the boundary touches a character onset or vowel start.
   - 25 to 35 ms for a normal fluent join.

If repeated phrases exist, identify the occurrence by local context, not phrase text alone.

## 5. Physically Remove Spoken Segments

Use the integrated segment removal script from the Auto-Cut repository root:

```powershell
python scripts/audio/remove_spoken_segments.py run "<audio-or-video>" --cut "START,END" --removed-phrase "<delete>"
```

For multiple cuts in one file, repeat `--cut` or use `run-batch` with a JSON jobs file. Use a blank end time only for intentional tail deletion.

Standalone compatibility outputs may be written under the ignored directory:

```text
output\修音成品\
```

For a JianYing revision job, treat those files as processing artifacts and write the accepted audio through the segmented editable-draft contract instead of importing one flattened full-track candidate.

## 6. Reverse Validate

For each output:

1. Re-run ASR on the processed file when possible.
2. Confirm `delete` is absent.
3. Confirm `must_keep` is present and audible.
4. Listen from at least 300 ms before to 500 ms after every join.
5. If a word was swallowed or an unrequested word disappeared, reject that version and revise the window.
6. If delete residue remains but widening the physical cut would touch a protected word, add an exact post-splice duck/mute window and validate it with reverse ASR plus an attenuation check.

Typical failure checks:

- "语言" sounds clipped: the cut window or crossfade touched the onset. Move the boundary away and shorten crossfade.
- "所以" disappeared: it was not listed as `must_keep`, or a broad buffer crossed into the next word. Add it to `must_keep` and re-cut from ASR boundaries.
- A cleaner version removed "它": only acceptable under `listening_first` or explicit accepted tradeoff.

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
- ASR source used, such as Volc ASR or degraded manual alignment
- output paths
- validation result for removed and kept phrases
- any accepted listening tradeoffs

## 9. V5 Precision Gate For JianYing Drafts

Use this gate when Feishu/Lark review-document audio edits are delivered as an editable JianYing draft.

### Preserve Review Semantics

- `colored_span_delete`: inspect document markup and keep colored fragments separate. If only `你看。那个`, `它`, and `是不是` are blue, create three delete windows and preserve the uncolored words between them. Add that inter-span text and its exact source-ASR window to automatic `must_keep` evidence.
- `gap_delete`: keep both anchors. For "delete the pause between 宰相 and 为核心", delete only the gap/filler and protect both `宰相` and `为核心`.
- `ellipsis_range_delete`: delete the full spoken range between prefix and suffix. Do not match only the suffix phrase.
- `tail_particle_delete`: protect the next word onset. If the particle tail remains but extending the physical cut would swallow the next word, add a post-splice exact duck/mute window instead.

### Keep Timeline And Audio Cleanup Separate

- Use source-time physical cut windows for spoken words that must shorten the video and audio together.
- Keep the complete processed narration as a validation-only artifact under `tmp/` unless the user explicitly requests whole-track replacement.
- For editable delivery, write explicit audible source, replacement-video, and local-repair segments, and preserve the approved separated source audio as a muted reference track.
- Map any `exact_window_cleanup.py` duck/mute window to the processed output timeline and record its source reason, target phrase, gain, and fade.

### Visual-Aware Pause Fit Gate

Run this gate after delete accuracy passes and before claiming the draft's listening rhythm is final.

Boundary evidence is mandatory: the rough timestamp is a search hint, never the
insertion point. Bind a real source ASR path and source ASR SHA-256, preserve the
requested source time and resolved source time, prefer an adjacent utterance gap,
resolve to the gap's interior midpoint instead of either ASR-reported edge, and
record positive recomputable guard time from the nearest real word end/start as
well as the utterance bounds. Reject a point inside a
spoken word, on an utterance edge, or inside a physical delete window. Extract
the still only after resolution and record frame source time equal to resolved
source time. Compile the segmented audio delivery plan after pause alignment, split
source/reference segments at the resolved boundary, and require zero audible
overlap with the hold.

Utterance-only ASR is insufficient: each semantic pause requires real word or character timing, and its semantic pause edit and `pause_adjustments` entry must correspond one-to-one before draft generation.

The request also binds the current source audio SHA-256, source video SHA-256,
exact alignment-audio path/SHA-256, and complete ASR identity with provider,
model/resource ID, adapter version, and preprocessing. `preprocessing=none`
requires byte-identical source/alignment audio; any transform requires a receipt
binding both hashes, tool/version, and parameters. Strict validation requires the
evidence ASR path/hash/identity to equal that request configuration, decodes the
current source video within one frame interval of frame source time, and requires
the still pixels to match source video. After compiling the normalized segmented plan, run
`revision-bind-audio-report` so the reverse-ASR report carries the matching audio
delivery plan SHA-256. Reject duplicate semantic pauses under one normalized source
item ID until receipts support one-to-many pause evidence.

1. Build pause candidates only from joins that passed reverse-ASR delete validation. Do not use pause fitting to hide a deletion failure.
2. For each candidate, record source cut window, output join time, previous word, next word, observed gap, and audio-only target class:
   - `same_clause`: short verbal particle or same-clause repair, usually near 0.22 seconds
   - `normal_phrase`: ordinary phrase deletion, usually near 0.35 seconds
   - `sentence_range`: larger sentence/range deletion, usually near 0.55 seconds
   - `too_fast_semantic_join`: a valid deletion where the next sentence, answer, section, or replacement bridge arrives too quickly and needs a short breath
3. Build the visual context list from the same draft plan used to write the JianYing project:
   - animation overlays and full-screen state holds
   - page turns and stable-frame replacement windows
   - pointer assets such as hands, arrows, circles, or magnifiers, including their inferred in/out duration
   - key visual information that needs a reading beat
4. Map visual events in source time and output timeline time. Use project-specific guard values when available; otherwise start with 2.0 seconds before and 1.0 second after the visual event or hold window.
5. If the candidate pause-compression source window overlaps a visual guard window, skip the compression and mark the row as `visual_hold_review`. Include the visual event label, source guard window, candidate source window, and the audio-only target that would have been used.
6. If there is no visual conflict, shorten only clearly long, ASR-safe pauses. Keep protected word onsets outside the cut.
7. If the join is too fast, add time only after judging the semantic boundary. Sentence/range deletions, question-to-answer transitions, new-section starts, and replacement-video bridges can receive a short silent still-frame hold; one-word filler deletions and within-clause repairs usually stay tight.
8. For a `semantic_pause_adjustment`, insert the hold as its own no-audio still-frame video segment sampled from the current source or replacement frame. Do not widen the delete window, do not mute live speech, and do not collapse adjacent editable segments.
9. Keep the pause report separate from delete validation. A row can be `visual_hold_review` for picture rhythm or `semantic_pause_adjustment` for listening rhythm while all delete rows remain `pass`.
10. Run full-candidate reverse ASR and require the exact preceding sentence tail and following sentence onset to remain. A zero-overlap hold is not sufficient when an ASR edge clipped the first or last protected syllable.

V6 precedent: when an audio-only pass wanted to shorten `修改22`, the candidate source window touched the guard for the `05:10 小手指向奥地利` visual event, so the workflow kept the longer pause and recorded it as `visual_hold_review`.

Lesson-1 precedent: after several review fixes left some joins too quick, the accepted repair inserted 16 short no-audio still-frame holds totaling 4.75 seconds. Each hold kept the source/replacement cut windows unchanged, shifted later segments through the same timeline plan, and recorded item id, source cut window, timeline start/end, duration, frame path, reason, and visual context.

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

Allow at most one automatic targeted repair. Invoke it on a deep copy, validate the raw callback result against the authorized scope before normalization or media access, then normalize/preflight and run the same scope check again before saving. It may narrow a physical delete or use a local non-destructive repair, but it must not widen the delete window across `must_keep`, alter unrelated review items, or skip the global acceptance gates. Restore the pre-repair files if the callback aborts, scope validation fails, or the attempted save is structurally invalid. When a structurally valid repair still fails media acceptance, preserve it and report the draft path plus unresolved item IDs for manual handling.

The repair callback must not mutate the live project and must not write saved draft files directly; it must return a scoped `RevisionRequest` for those raw and prepared scope checks.
