# Preservation Rules

These are the hard rules for review-driven draft editing.

## Media-Bin Rules

1. Any timeline-used video must also exist in draft metadata.
2. Any timeline-used audio must also exist in draft metadata.
3. If the workflow separated original audio from video, both the video material and the separated-audio material must remain visible.
4. Replacement audio must not displace the original source materials from metadata.

## Track Rules

1. Video belongs on video tracks.
2. Separated source audio belongs on audio tracks.
3. Replacement audio should use its own track unless the user explicitly asks to replace-in-place behavior.
4. Do not place the same full-length video clip on multiple stacked tracks unless a real layered-video effect requires it.

## Segment Rules

1. Preserve splits created by meaningful edit actions.
2. Do not flatten adjacent segments after a delete/trim if that would hide the cut.
3. If a region is muted, replaced, shortened, or shifted, the segment boundaries around that change should still expose where the action occurred.
4. Never replace a revision project with one full-length baked video segment and one full-length baked audio segment unless the user explicitly asked for export-only output.
5. If a special effect truly requires pre-rendering, pre-render only that local window and keep the rest of the draft segmented.

## Review-Marker Rules

1. One issue, one marker.
2. Do not merge multiple fixes into one generic marker.
3. Marker naming should reflect the issue, not just "fixed".
4. When the user needs to inspect the剪辑过程, add review-marker lanes, trace lanes, or equivalent visible draft evidence.

## Overwrite Rules

1. Prefer same-draft continuation when technically safe.
2. Never force-close JianYing or switch away from another open editing page just to make overwrite succeed.
3. If the app is in an unsafe state, do not force overwrite on the requested draft.
4. If the requested draft cannot be safely overwritten, create exactly one fallback revision draft instead of returning no result.
5. Do not run old-draft retention before the new revision passes validation and final acceptance.
6. If a fallback revision fails, delete only that failed fallback and keep the last accepted draft/fallback available.
7. After a successful accepted revision, retain at most one fallback draft in the project family.
