# Revision Checklist

Run this checklist before saying the JianYing revision draft is done.

## Resumable Job Evidence

- Natural-language source-document work has canonical `review-job-compile` outputs under ignored `tmp/` storage.
- The seven ordered phases in `docs/revision-input-spec.md` have valid input/output digests in atomic `job_state.json` checkpoints; corrupt or mismatched phases alone were rerun.
- Source ASR, reverse ASR, visual frames, local renders, and document snapshots use the complete cache identities from the input spec.
- Independent read-only preparation is bounded; JianYing writes, saved-draft inspection, ordered Feishu writes, and overlapping timeline repairs obey their serialization rules.
- `job_timing.json` identifies phase time, wait, cache status, retries, workers where available, item IDs, digests, and unresolved items without secrets.

## Structure

- Draft opens without "content damaged" errors.
- Draft still contains the intended working project name.
- Draft was created from the requested source baseline. If the user said to use a fresh source folder/video, no unrelated older review markers or overlays remain.
- If the task was "continue on the same draft", the requested draft name was used when safe.
- If same-name overwrite was unsafe because JianYing was busy in another edit/export session, exactly one fallback revision draft was created instead of stopping with no output.
- Failed fallback revision drafts are cleaned after validation failure; previously accepted drafts/fallbacks are not deleted before the new draft passes acceptance.
- After acceptance passes, retention leaves at most one fallback draft for the project family.

## Material Bin

- Original video material is present.
- Separated original audio is present.
- Replacement audio is present.
- Newly imported support assets are present when used.

## Timeline

- Video track contains the expected segment count.
- Separated-audio track contains the expected segment count.
- Replacement audio track exists if replacement audio was requested.
- No accidental duplicate stacked video layer exists.
- Main video track is not a single full-length baked preview for a multi-edit revision job.
- Preview-style `Final Video` / `Final Audio` replacement tracks were not used as the final deliverable structure.

For precise spoken-word revision:

- Physical delete windows are reflected in the main video segmentation.
- The original separated audio remains available, usually muted as a reference track.
- The processed audible narration is on a distinct replacement-audio track.
- Exact duck/mute tail-cleanup windows, if used, are recorded in the audio report.
- Post-delete pause timing decisions are recorded separately from delete-window accuracy.
- Any pause shortening near animations, pointers, page turns, or key visual holds has passed a visual-aware guard check.
- Any `semantic_pause_adjustment` that repairs a too-fast join is a separate no-audio still-frame segment, not a widened delete window or hidden mute.

## Cut Visibility

- Zero-second trims or opening deletions still leave a visible first cut boundary.
- Split points introduced by review edits are still visible.
- Adjacent segments were not merged in a way that hides edit evidence.
- If the user asked to see the editing process, trace lanes or equivalent in-draft edit evidence exist.

## Reviewability

- The latest `doc_items` read is canonical for source item identity, kind, and text.
- A missing internal `source_text` triggers automatic document re-read/recovery before marker rendering.
- If the source cannot be re-read, the most complete unmodified candidate is retained with `verbatim_status=unverified_source_unavailable`; the workflow emits a warning and does not claim exactness.
- Active source-text recovery remains nonblocking: it does not stop editing, remove an item, or prevent draft generation.
- Every source-ledger marker equals canonical `source_text` code-point-for-code-point, including timestamps, punctuation, whitespace, line breaks, and literal question marks.
- No source-ledger marker adds a summary, prefix, ID, translation, or truncation unless those characters are present in the source.
- Each stable source item ID has exactly one marker. Multiple internal edits share it; duplicate IDs fail; identical text under different IDs remains separate.
- Marker tracks are actual dynamically allocated `Review Marker N` text tracks.
- Dynamic marker labels occupy one horizontal top safe band on centerline `y=0.8`; concurrent lanes divide the width instead of adding a middle-picture row.
- Caller-supplied `x_positions` cannot override automatic or explicitly named dynamic tracks; custom positioning is limited to explicitly non-dynamic track groups.
- Caller-supplied lane hints are ignored for dynamic layouts, and mixed dynamic/custom track-name groups are invalid.
- Every saved dynamic marker segment has a finite `clip.transform.y` inside normalized `0.7..0.9` in the root and active timeline copies; legacy/manual marker names remain compatible.
- `review_marker_top_layout_problems(content)` receives one saved draft-content object per call; strict validation invokes it separately for root and every declared active timeline copy.
- Every marker receipt contains `item_id`, `source_text`, `verbatim_status`, `segment_id`, `material_id`, `track_name`, `start_time`, and `duration`.
- Root `draft_content.json` and active `Timelines/<main_timeline_id>/draft_content.json` agree on exact marker text, count, receipt mapping, and mapped timeline start.
- An explicit empty source ledger rejects old or otherwise extra marker segments.
- If a manifest exists, it matches the in-draft marker set and source-ledger receipts.
- The user can tell what was cut, replaced, shifted, inserted, or delayed without reconstructing the timeline by hand.
- Markers from older unrelated tasks are absent unless the user explicitly asked to preserve them.

## Audio Precision

- Every review-document audio item has `delete`, `must_keep`, `strategy`, and validation status.
- Colored-span deletes are traceable as separate fragments instead of one collapsed sentence.
- Gap-delete items preserve both anchor words.
- Reverse ASR has no unresolved `fail` rows.
- The full candidate is fully decoded, binds the normalized segmented plan digest, and covers its timeline after only exactly adjacent two-sided crossfades; isolated fades add no duration allowance, and every spoken-delete row's transcript, cut/join mapping, delete/keep hits, and semantic-join validation match the same item contract rather than a generic pass.
- Same-word recurrence is only accepted when an adjudication explains why the hit belongs to a later kept phrase.
- Pause-fit validation reports adjusted pause count, inserted semantic-pause count, skipped visual-context count, and any `semantic_pause_adjustment` or `visual_hold_review` rows.
- A `semantic_pause_adjustment` row records item id, source cut window, timeline start/end, duration, still-frame source or frame path, semantic reason, and confirms protected words are unchanged.
- Every semantic pause binds source ASR path and source ASR SHA-256 plus identity equal to the immutable request configuration, current source audio/video hashes, exact alignment-audio hash, and either byte identity for `preprocessing=none` or a receipt binding transformed hashes/tool/version/parameters; preserves requested source time and resolved source time; resolves to the utterance-gap midpoint with positive recomputable guard time from the nearest real word end/start; records frame source time at that midpoint and proves successful decode within one frame interval plus pixel agreement; uses an audio delivery plan compiled after pause alignment and bound by audio delivery plan SHA-256; and has full-candidate reverse-ASR proof that the preceding sentence tail and following sentence onset remain complete.
- Utterance-only ASR is insufficient: each semantic pause requires real word or character timing, and its semantic pause edit and `pause_adjustments` entry must correspond one-to-one before draft generation.
- Each stable source item ID has at most one semantic pause until one-to-many pause receipts are implemented.
- A `visual_hold_review` row is acceptable only when it names the conflicting visual event and source-time guard window; do not count it as a spoken-word deletion failure.

## Safety

- UI modes are explicit: `offline` is the default, `opened_verify` is conditional compositor verification, and `export` is UI-required export only.
- Offline work must not construct JianyingController or foreground JianYing. Same-name safety reads the target `.locked` file and writes one fallback draft when locked.
- No automatic UI action was used to force JianYing back to home during overwrite.
- Unsafe overwrite states were never forced.
- When overwrite was unsafe, the workflow still produced one writable revision draft.
- For an externally written new draft, returning to the draft home page is not a refresh. Only when the target draft is not visible, protect unsaved work, perform one complete JianYing process exit and relaunch, then confirm the exact draft name and timeline ID. Once the target draft is open, do not exit it merely to refresh or repeat evidence capture.
- Retention was deferred until after validation and final acceptance, so cleanup did not hide an unresolved failure or remove the last accepted fallback.

## Visual Revisions

- Pointer/highlight rows include overlay segment id, target point, anchor rule, scale rule, in point, out point, duration rule, and out reason.
- The checklist must reject pointer/highlight scale evidence unless `acceptance.require_subject_pointer_binding=true`, `review_items[*].evidence.subject_profile_receipt` is present, `project-bindings.json` contains the current `project_key`, stored `project_path`, and bound `profile_key`, and `auto-cut-pointer-targeting` provides a fresh exact `stage_id + subject_id` profile check with `status=ready`. The receipt's `project_key`, stored `project_path`, `profile_key`, `stage_id`, and `subject_id` must all match the same binding, and its `profile_path` must match that binding's fresh registry result. The flag is required audit metadata and cannot disable the inferred strict pointer gate when omitted or false.
- Receipt evidence binds the current asset SHA-256 and current bytes to that profile, and binds `scale_reference_path`, scale reference SHA-256, current-layout confirmed ratio, and approved preview evidence to current verified files.
- A `text_bottom` item records in-canvas `target_geometry`; `underline_layers` and `clean_layers` are explicit arrays even when empty; any display-safe fallback binds its artifact SHA-256, timeline ID, and exact window.
- Pointer/highlight out points were checked against spoken target beats, next visual change, next pointer target, page turn, and animation entrances.
- Animation timing rows include edit mode, first visible frame, stable frame, release, next independent animation, and collision result when applicable.
- Whole-section advances include evidence that board states align and future content is not revealed early.
- Local crop overlays are used only when full-screen or whole-section methods are invalid, and include a size/position comparison.
