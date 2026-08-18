# Subject Pointer Same-Task Handoff Contract

Use this contract when `auto-cut-pointer-targeting` cannot validate the current project's exact binding and ready subject profile. The handoff continues the original pointer task through `auto-cut-subject-pointer-onboarding`; it is not a terminal instruction to start another request.

## Closed Wait And Sidecar

Open the handoff before overlays or draft writes. For a resumable review job, write the complete Task 2 request envelope below and pass that file to `review-job-wait-open`; do not pass a stored `external_wait` projection or a bare sidecar.

### Initial Wait Request

This is the complete initial `wait-request.json`. Its outer `item_ids` and `input_digest` exactly match the sidecar item IDs and `job_input_digest`.

```json
{
  "schema_version": 1,
  "item_ids": ["source-item-id"],
  "input_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "artifact_payload": {
    "schema_version": 1,
    "job_input_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "project": {
      "project_key": "history-lesson-01",
      "draft_path": "<draft-root>/history-lesson-01",
      "draft_root": "<draft-root>"
    },
    "items": [
      {
        "item_id": "source-item-id",
        "source_ledger_id": "ledger-001",
        "requested_asset_roles": ["hand"],
        "current_layout_names": ["full-frame-map"]
      }
    ],
    "explicit_identity": {
      "stage_name": null,
      "subject_name": null,
      "stage_id": null,
      "subject_id": null
    },
    "profile_check": null,
    "user_action": {
      "action_code": "subject_identity",
      "prompt_revision": 1
    }
  },
  "replaces": null
}
```

The command validates that closed envelope, canonicalizes `artifact_payload`, writes it as `subject_pointer_onboarding.json`, and derives the stored wait. The stored projection has the same `item_ids` and `input_digest`; it is output, not input:

```json
{
  "code": "awaiting_subject_profile",
  "phase": "subject_pointer_profile_gate",
  "item_ids": ["source-item-id"],
  "input_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "artifact": "subject_pointer_onboarding.json",
  "artifact_sha256": "bad3b7fcf31805028947ddd1a9bce02fa48ab297c498f635dcbf89471d6ef279",
  "started_at": "2026-07-22T12:00:00Z"
}
```

### Changed Project-Binding Request

Every materially changed blocking question is another complete request envelope. Set `replaces.input_digest` and `replaces.artifact_sha256` to the values returned for the currently active wait, preserve its item IDs, and increment `prompt_revision` by exactly one. This example moves the initial identity question to a ready-profile `project_binding` question:

```json
{
  "schema_version": 1,
  "item_ids": ["source-item-id"],
  "input_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "artifact_payload": {
    "schema_version": 1,
    "job_input_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "project": {
      "project_key": "history-lesson-01",
      "draft_path": "<draft-root>/history-lesson-01",
      "draft_root": "<draft-root>"
    },
    "items": [
      {
        "item_id": "source-item-id",
        "source_ledger_id": "ledger-001",
        "requested_asset_roles": ["hand"],
        "current_layout_names": ["full-frame-map"]
      }
    ],
    "explicit_identity": {
      "stage_name": "高中",
      "subject_name": "历史",
      "stage_id": "senior-high",
      "subject_id": "history"
    },
    "profile_check": {
      "status": "ready",
      "missing_items": [],
      "problems": []
    },
    "user_action": {
      "action_code": "project_binding",
      "prompt_revision": 2
    }
  },
  "replaces": {
    "input_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "artifact_sha256": "bad3b7fcf31805028947ddd1a9bce02fa48ab297c498f635dcbf89471d6ef279"
  }
}
```

An unchanged question reuses the byte-identical request and preserves its revision. Never send `replaces` with only one digest, a digest from an older wait, or a changed question whose revision was preserved or advanced by more than one.

Copy every original pointer `item_id` and `source_ledger_id` unchanged. Multiple internal edits for one item stay under that item; do not replace, summarize, merge, reorder, or regenerate source-ledger IDs. The sidecar contains no source text, inferred identity, full profile, or fallback profile.

For a resumable source-document job, write the closed wait request and run:

```powershell
python scripts/jy_wrapper.py review-job-wait-open --state-json "<job-dir>/job_state.json" --wait-json "<job-dir>/wait-request.json" --json
```

Interactive work keeps the same fields in the current task even when no persisted review-job wait exists.

## Identity And Action Mapping

Never infer stage or subject from title, filename, OCR, ASR, course content, layout, another profile, or any media. Ask only for the missing stage or subject; retain an identity value the user already supplied.

Map the exact current state to one of four action codes:

| Exact state | `action_code` | Required next action |
| --- | --- | --- |
| `stage_name` or `subject_name` is missing | `subject_identity` | Ask only for the missing stage or subject. |
| `profile_check.status` is `missing`, `incomplete`, or `stale` | `subject_evidence` | Request only missing, invalid, or hash-changed evidence. |
| `status=needs_confirmation` and any `missing_items` row starts with `confirmed_scale_references:` | `subject_evidence` | Request scale-reference confirmation first. |
| `status=needs_confirmation` with exactly `approved_previews:0` remaining | `preview_approval` | Ask for explicit approval of the shown final preview. |
| `status=ready` and the exact profile is not the confirmed binding (bind or rebind) | `project_binding` | Ask the exact binding question below; replacing a different binding requires explicit rebind confirmation. |

For `subject_identity`, the user-facing response has exactly these three parts, in this order:

1. State that pointer overlay and draft writing are paused before any edit.
2. State that onboarding is continuing inside this same task, then ask only for the missing stage, subject, or both when both are absent.
3. State that after the user supplies them, this same task will check the exact profile, collect only missing evidence, request binding confirmation, rerun the fresh pointer gate, and continue the original pointer edit.

Do not reduce this response to a bare identity question. Do not request files, preview approval, or binding confirmation in the `subject_identity` response; those are later mapped states.

When scale-reference and preview confirmations are both pending, complete `subject_evidence` first, rerun the exact profile check, then open `preview_approval`. Reopening an unchanged question preserves `prompt_revision`; each materially changed blocking question increments it by exactly one.

When `subject_evidence` needs files, follow `intake-contract.md`: resolve and display `pointer-material-reference.png` and `scale-reference-screenshot.png` together in the same message. Do not request evidence already valid in the exact profile.

## Optional User-Action Notification

Read the shared [user-action-required contract](../../auto-cut/references/user-action-required.md). For a resumable handoff, within `review-job-wait-open`, only after `begin_external_wait` succeeds, the handler calls `deliver_user_action_required`. The caller must not send the same notification again. Delivery is optional and cannot change whether the wait is valid.

The integration reads these inputs from one locked read of the current verified sidecar and current wait snapshot:

- `job_input_digest`
- `project.project_key`
- `items[*].item_id`
- `user_action.action_code`
- `user_action.prompt_revision`

Never rebuild notification fields from the in-memory wait request. The same current wait snapshot supplies both the notification event and the wait metadata returned to the caller. If the current wait is absent or its sidecar cannot be verified, do not send; return only a privacy-safe `unavailable` status projection with unknown attempt count.

Keep the complete question visible here because only the originating Codex task can resolve the wait. Feishu replies, reactions, or messages do not approve any identity, evidence, preview, bind, or rebind action.

## Binding And Return

After the exact profile reaches `status=ready`, read the current project binding and select the `bind` or `rebind` row before opening or replacing a `project_binding` wait. The selected question must be known before persistence because the closed sidecar records `action_code=project_binding`, not the question variant.

When an active resumable review-job wait exists, persist one `project_binding` request using that wait's current replacement digests and the next valid `prompt_revision`, then show the already selected question. Interactive work without an active persisted wait does not invent `replaces` digests and does not call `review-job-wait-open`; keep the same action/revision semantics in the current task only. Do not open the generic bind row and then convert it to `rebind`: choose from the current binding first.

### Binding Confirmation Matrix

```json
{
  "bind": {
    "question": "Bind this ready profile to the current project and continue the original edit?",
    "allow_rebind_before_confirmation": false,
    "allow_rebind_after_affirmative": false
  },
  "rebind": {
    "question": "Replace the current project's existing binding `<current_profile_key>` with ready profile `<new_profile_key>` and continue the original edit?",
    "allow_rebind_before_confirmation": false,
    "allow_rebind_after_affirmative": true
  }
}
```

For a new binding, use the `bind` row; `allow_rebind` is false both while asking and after an affirmative answer. When the current project is already bound to a different profile, the generic bind question is insufficient: select the `rebind` row before persistence, substitute both profile keys so the replacement is disclosed, and use `allow_rebind_before_confirmation=false` while asking. Only that affirmative rebind answer changes the function argument to `allow_rebind_after_affirmative=true`. Do not increment a persisted revision merely to switch matrix rows; the row must already be selected when the single `project_binding` transition is opened.

Return to `auto-cut-pointer-targeting` only after it:

1. reloads the current project binding from `project-bindings.json`;
2. runs a fresh registry `check` for that exact `stage_id + subject_id`;
3. recalculates and validates current asset, scale-reference, and approved-preview hashes;
4. verifies the requested asset role and every current layout required by the preserved pointer items; and
5. resolves the external wait with its current input and artifact digests, then reruns `subject_pointer_profile_gate` from normal entry.

Never use the handoff's earlier `profile_check` as a ready receipt. Any changed project identity, stale hash, wrong role, missing layout, or changed handoff digest opens a new handoff and leaves pointer-dependent phases pending.

## Invariants

- No identity from content.
- No profile creation or binding without user input.
- No pointer draft write while waiting.
- No stale handoff receipt on return.
- No second request for evidence already valid in the exact profile.
