---
name: auto-cut-pointer-targeting
description: Use in Auto-Cut Lite for supplied hands, arrows, circles, magnifiers, highlights, or pointer comments. Lite inserts supplied assets at default geometry; existing-hand occlusion or cleanup remains label-only.
---

# Lite Pointer Asset Placement

Read and follow the
[Lite execution contract](../auto-cut-lite/references/lite-execution-contract.md). Do not apply
full-workflow pointer targeting, profile, lifecycle, or cleanup rules.

## Supplied Asset

When the user supplies a pointer-like local file and asks to add or replace a pointer:

- set `execution_required=true`;
- insert an ordinary editable segment on `Lite Visual Assets` at the requested start;
- use the requested duration, clamped at the unchanged project end;
- keep default geometry: scale 1, transform 0/0, rotation 0, alpha 1;
- add no motion, position, scale, opacity, or other keyframes;
- keep one verbatim label on `Review Marker Visual N`.

Do not calibrate size, move the asset, locate a target, infer a hotspot, optimize fingertip
landing, or adjust the out point from speech/visual events.

## Cleanup Or Missing Asset

Existing-hand occlusion, removal, clean-cover, cleanup, and residual-cover comments are
`execution_required=false` and label-only. Do not create cover media, clean layers, baked
windows, or source-hand removal.

If no usable local asset was supplied for an insertion request, keep the label and report that
the asset was not inserted. Do not start profile onboarding, choose a substitute, or synthesize
one.

Lite acceptance requires only the local material, editable segment, requested start, default
geometry, and absence of keyframes for an executed insertion. It never requires a profile,
binding, target point, hotspot, lifecycle, opened JianYing screenshot, or cleanup proof.
