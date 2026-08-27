# Lite Pointer Workflow

Read the
[Lite execution contract](../../auto-cut-lite/references/lite-execution-contract.md). The packaged
Lite workflow inserts attributable local pointer assets only; it does not run profile targeting,
hotspot calibration, lifecycle analysis, cleanup, or opened-editor validation.

## Associate The Asset

1. Prefer the attachment structurally associated with the current review row.
2. If a hand/pointer row omitted its attachment, compare only other rows with the same compiler-
   normalized modification name.
3. Reuse the asset when exactly one candidate exists. Record its source row ID, asset ID, local
   path, and SHA-256 in the intake/execution receipt.
4. When multiple candidates exist, stop that association with structured
   `user_action_required`; return safe candidate IDs and never guess from filenames or source code.
5. With no candidate, retain the exact review label and report the insertion missing. Do not open
   profile onboarding or synthesize an asset.

## Insert Or Label

- A resolved supplied asset is `execution_required=true`: add one ordinary editable segment to
  `Lite Visual Assets` at the requested review-comment start.
- Keep JianYing default geometry: scale 1, transform 0/0, rotation 0, alpha 1, and no keyframes.
- Clamp only its requested duration to the unchanged project end.
- Existing-hand occlusion, removal, cleanup, clean-cover, and residual-cover requests are
  `execution_required=false` and label-only.
- Every row retains exactly one visible label equal only to `source_text`.

Acceptance checks association evidence, saved local material, segment start, default geometry,
absence of keyframes, and unchanged project duration. It does not require a profile, target point,
hotspot, pointer lifecycle, cleanup media, screenshots, or JianYing UI access.
