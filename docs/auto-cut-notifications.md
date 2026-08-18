# Optional Feishu user-action notifications

Auto-Cut can send a short Feishu alert when an operation is already waiting for a
decision in Codex. This integration is optional and notification-only. It never
resolves an operation. Return to the originating Codex task to inspect the full
question and approve or reject it. Feishu replies, reactions, or messages do not
approve anything.

## Prerequisites and approval

Complete the local `lark-cli` configuration and authentication before setup. The
operator must obtain explicit Feishu recipient approval, content approval, and
identity approval (`user` or `bot`) for this computer. Setup also requires a
successful `--dry-run`; it does not infer a recipient or reuse a recipient from
another computer.

The fixed alert template sends only a project fingerprint, a closed action code
and label, the number of waiting items, an event ID, and an instruction to return
to Codex. The setup preview explicitly lists fields that must never be sent:

- `review_document_text`
- `source_ledger_text`
- `media_contents`
- `local_absolute_paths`
- `access_tokens`
- `app_credentials`
- `authorization_urls`
- `device_codes`
- `full_profile_evidence`
- `raw_destination_id`
- `raw_project_key`
- `raw_item_ids`
- `input_digest`
- `provider_stdout`
- `provider_stderr`

## Enable on one computer

First preview the exact identity, destination fingerprint, template, and privacy
contract. `setup-preview` executes the normal send argument array with
`--dry-run`; it does not send a message or change local configuration.

```powershell
$previewJson = python scripts/auto_cut_notifications.py setup-preview --as bot --chat-id oc_example_notification_chat --json
$preview = $previewJson | ConvertFrom-Json
if (-not $preview.ok) { throw "notification setup preview failed" }
$previewDigest = $preview.data.preview_digest
```

Review the preview locally. Then enable only those exact choices with both closed
approval literals:

```powershell
python scripts/auto_cut_notifications.py setup-enable --as bot --chat-id oc_example_notification_chat --preview-digest $previewDigest --confirm-template auto_cut_user_action_required_v1 --confirm-privacy auto_cut_notification_privacy_v1 --json
```

Use exactly one of `--chat-id` or `--user-id`. Changing the recipient, identity,
template, or privacy contract invalidates the preview digest and requires a new
preview.

## Status and disable

`status` performs only a local command-availability check. It does not launch
`lark-cli`, expose the raw destination ID, or inspect Feishu messages.

```powershell
python scripts/auto_cut_notifications.py status --json
python scripts/auto_cut_notifications.py disable --json
```

`disable` is idempotent. It keeps the valid local recipient configuration with
`enabled=false`; it does not delete `lark-cli` authentication or delivery
receipts.

## Emit a closed alert

Auto-Cut integrations call `notify` only after the complete blocking question is
visible and durable in the originating Codex task:

```powershell
python scripts/auto_cut_notifications.py notify --input-digest aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --project-key history-lesson-01 --action-code subject_identity --item-id item-001 --prompt-revision 1 --json
```

Provider failure is reported in the sanitized receipt but exits successfully for
this command because the local Codex wait remains valid. Invalid local input exits
with code 2; setup or runtime failure exits with code 1.

The local configuration and receipts are private runtime state under `data/` and
are excluded from source archives.
