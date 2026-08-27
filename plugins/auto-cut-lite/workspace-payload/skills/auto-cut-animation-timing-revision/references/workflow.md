# Lite Animation Timing Reference

Read the
[Lite execution contract](../../auto-cut-lite/references/lite-execution-contract.md). This packaged
workspace does not execute animation, page-turn, reveal, release, whole-section shift, speed,
hold, or other picture-timing changes.

For every such review item:

- keep `execution_required=false`;
- preserve exactly one visible label equal only to `source_text`;
- place it on `Review Marker Animation N` at the requested review-comment start or explicit target;
- use two seconds, clamped at the unchanged project end;
- leave `Lite Timing Adjusted` empty.

Do not sample frames to derive an executable boundary, retime or split source clips, change speed,
create an overlay or hold, shift a section, add keyframes, or request animation/opened-editor
evidence. These operations can change duration or picture timing and are outside the Lite executor
allowlist.

Acceptance requires the exact label, unchanged source duration, an empty `Lite Timing Adjusted`
track, and absence of animation-generated segments or keyframes. A newly encountered animation
instruction remains label-only even when no existing classifier name matches it.
