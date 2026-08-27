# User Action Required

Use this shared contract when Auto-Cut cannot continue without a user decision or user-supplied evidence. A `user_action_required` alert is notification-only. The full blocking question stays in the originating Codex task, and Auto-Cut must accept resolution only in the originating Codex task.

In ordinary Lite editing, pointer profiles and onboarding waits are disabled. A missing hand/pointer
attachment with multiple same-normalized-modification-name candidates uses the interactive-only
`high_risk_confirmation` action with safe candidate IDs. A unique candidate is reused without a
question; zero candidates keeps the item label-only and reports the missing insertion.

Ordinary clarification, a progress update, and nonblocking status text do not notify. A question qualifies only when the operation is genuinely blocked and the user has a concrete action to take.

## Closed Action Codes

Classify every qualifying blocker as exactly one of these codes:

| Action code | Use |
| --- | --- |
| `subject_identity` | The explicit stage, subject, or both are missing. |
| `subject_evidence` | Required pointer-profile evidence is missing, incomplete, stale, or awaiting scale-reference confirmation. |
| `preview_approval` | A final pointer-profile preview needs explicit approval. |
| `project_binding` | A ready subject profile needs explicit bind or rebind confirmation. |
| `authorization` | A local or external authorization step blocks the operation. |
| `high_risk_confirmation` | A high-risk operation needs explicit confirmation. |

Do not invent another action code and do not use a pointer action for an unrelated blocker.

## Required Order

1. Persist the durable wait before notification delivery. A resumable pointer-profile job does this with `review-job-wait-open`. An interactive-only blocker first posts the full blocking question in the originating Codex task.
2. Read the current persisted and verified context, then classify it with one closed action code.
3. Invoke the optional helper `deliver_user_action_required`, or the `notify` CLI for an interactive-only blocker.
4. Keep the originating Codex task waiting on the already-visible question. Delivery failure does not alter the wait.
5. Accept resolution only in the originating Codex task.

Never send first and persist later. Never rebuild a persisted pointer event from an earlier in-memory request. For a resumable pointer wait, the event inputs come from one locked read of the current verified sidecar and current wait snapshot:

- `job_input_digest`
- `project.project_key`
- `items[*].item_id`
- `user_action.action_code`
- `user_action.prompt_revision`

The project key is only a hash input. The notification contains a project fingerprint, action code, waiting-item count, and event ID; it does not contain source text, media content, item IDs, absolute paths, credentials, or the full question.

## Resolution Boundary

Feishu replies, reactions, or messages do not approve, reject, resume, resolve, or otherwise mutate an Auto-Cut operation. No notification command reads Feishu responses. The user returns to the same originating Codex task and answers the full blocking question there.

Notification failure never clears, retries, widens, or fails the editing wait. A valid terminal `sent`, `failed`, or `disabled` receipt is summarized with privacy-safe fields. An `unavailable` summary is a safe status projection, not a receipt. Its `attempt_count` is `null` because attempts are unknown; it must not claim that zero provider attempts occurred.

## Pointer-Profile Waits

The four pointer-profile actions use the durable `pointer_profile_onboarding.json` sidecar:

- incomplete explicit identity -> `subject_identity`
- `missing`, `incomplete`, `stale`, or scale-reference confirmation -> `subject_evidence`
- final preview confirmation -> `preview_approval`
- ready profile awaiting bind or rebind -> `project_binding`

Within `review-job-wait-open`, only after `begin_external_wait` persistence succeeds, the handler reads the current verified context and calls `deliver_user_action_required`. The caller must not send the notification again. A same-event terminal receipt is reusable and must not trigger another provider send.

## Interactive-Only Actions

`authorization` and `high_risk_confirmation` are interactive-only in this release. They never create or reuse the pointer-profile `external_wait`.

Before invoking `notify`:

- Show the full question in the current Codex task.
- Use synthetic item ID `operation:authorization` for `authorization`.
- Use synthetic item ID `operation:high-risk` for `high_risk_confirmation`.
- Derive the input digest from the local operation plus the project fingerprint.
- Start at prompt revision 1 and increment it exactly once when the material question changes.

### Authorization

After the authorization question is visible in the originating Codex task, use this exact command shape:

```powershell
python scripts/auto_cut_notifications.py notify `
  --input-digest <operation-plus-project-sha256> `
  --project-key <project-key> `
  --action-code authorization `
  --item-id operation:authorization `
  --prompt-revision <revision> `
  --json
```

### High-Risk Confirmation

After the high-risk question is visible in the originating Codex task, use this exact command shape:

```powershell
python scripts/auto_cut_notifications.py notify `
  --input-digest <operation-plus-project-sha256> `
  --project-key <project-key> `
  --action-code high_risk_confirmation `
  --item-id operation:high-risk `
  --prompt-revision <revision> `
  --json
```

The CLI receives only the digest, project key hash input, action code, synthetic item ID, and prompt revision. It remains a one-way optional alert and never becomes an approval channel.
