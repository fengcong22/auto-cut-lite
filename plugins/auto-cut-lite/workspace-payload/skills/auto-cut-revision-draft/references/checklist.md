# Revision Checklist

Run this checklist before saying the JianYing revision draft is done.

For the packaged workspace's required `workflow_mode=lite`, every request to add, extend, shorten,
or otherwise adjust a pause is label-only at a unique ASR point or review-time fallback. The Lite
rules below replace any inherited full-workflow pause-mutation gate.

## Resumable Job Evidence

- Natural-language source-document work uses `review-document-run` and keeps its canonical
  outputs, checkpoints, cache, and receipts under ignored `tmp/` storage.
- The ten ordered public-runner phases in the Lite input spec have valid input/output digests in
  atomic `job_state.json` checkpoints; corrupt or mismatched phases and their dependents were rerun.
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
- Post-delete pause requests are recorded separately from delete-window accuracy as non-executing
  source-text-only labels at unique ASR points or review-time fallbacks.
- No pause shortening, extension, addition, `semantic_pause_adjustment`, visual-hold repair, or
  other pause mutation is executed in Lite.
- No pause request creates a hold, still-frame segment, hidden mute, audio gap, duration change, or
  offset to a later track, asset, or label.

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
- For `workflow_mode=lite`, the named grouped exception is accepted instead:
  `Review Marker Delete/Visual/Animation N` families are independently allocated and must pass
  the lite saved-layout checks for left alignment, 4–5 font size, both stage edges, and forced
  safe-width wrapping for long source text. Delete/Visual/Animation backgrounds stay
  red/green/amber respectively.
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
- Pause-label validation reports every pause item as `execution_required=false`, with its stable item
  ID, exact `source_text`, selected ASR or review-time source, and marker receipt.
- Every pause item attempts current source word/character ASR. A unique ASR point binds its identity
  and timing; an ASR failure or non-unique result binds `review_timestamp_fallback` instead.
- The request and saved draft contain no executable `semantic_pause_adjustment` or
  `pause_adjustments` row and no pause-generated media segment.
- Final duration equals source duration, and all later targets retain their original source-aligned
  times. Any pause-derived duration extension or offset fails Lite acceptance.

## Safety

- UI modes are explicit: `offline` is the default, `opened_verify` is conditional compositor verification, and `export` is UI-required export only.
- Offline work must not construct JianyingController or foreground JianYing. Same-name safety reads the target `.locked` file and writes one fallback draft when locked.
- No automatic UI action was used to force JianYing back to home during overwrite.
- Unsafe overwrite states were never forced.
- When overwrite was unsafe, the workflow still produced one writable revision draft.
- For an externally written new draft, returning to the draft home page is not a refresh. Only when the target draft is not visible, protect unsaved work, perform one complete JianYing process exit and relaunch, then confirm the exact draft name and timeline ID. Once the target draft is open, do not exit it merely to refresh or repeat evidence capture.
- Retention was deferred until after validation and final acceptance, so cleanup did not hide an unresolved failure or remove the last accepted fallback.

## Lite Visual Revisions

- A resolved supplied pointer/image has one attributable local material, editable segment, requested
  start, default geometry, and no keyframes.
- A missing hand/pointer attachment is reused only from the unique row with the same normalized
  modification name. Multiple candidates return structured `user_action_required`.
- Existing-hand cleanup, animation/picture timing, and text movement/animation items are label-only.
- No pointer profile, target point, hotspot, scale reference, cleanup layer, stable-frame evidence,
  whole-section advance, crop overlay, or opened-editor evidence is present or required.
