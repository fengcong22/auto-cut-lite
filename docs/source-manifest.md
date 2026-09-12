# Taskboard source-manifest handoff

This document describes the implemented Taskboard-to-Auto-Cut Lite source and
artifact handoff. It is a closed, structured path for one Taskboard task and one
run. It is not activated by task descriptions, comments, prompts, or filenames.

## Invocation

Taskboard creates the run-owned JSON files and invokes the repository entrypoint
with explicit paths:

```powershell
python scripts/jy_wrapper.py review-document-run `
  --source-manifest "<absolute-run-root>\source-manifest.json" `
  --execution-input "<absolute-run-root>\execution-input.json" `
  --job-root "<absolute-run-root>\job" `
  --drafts-root "<configured-jianying-draft-root>" `
  --package-zip "<configured-stage-output-root>\placeholder.zip" `
  --result-path "<absolute-run-root>\taskboard-result.json" `
  --json
```

All six paths are selected by Taskboard or its fixed Auto-Cut package
configuration. Values read from Feishu cells are data only. They are not used as
paths, commands, executables, shell fragments, or prompts.

`--package-zip` must name a `.zip` path, but its basename is only a destination
placeholder. The runner uses its parent directory and writes
`<resolved-draft-name>.zip`. It never enumerates that directory to find a ZIP.

The execution input has exactly two fields:

```json
{
  "schema_version": 1,
  "artifact_name": "Course Name - Initial"
}
```

`artifact_name` is normalized to a safe Windows path component and becomes both
the JianYing draft name and final ZIP basename. Unknown execution-input fields,
an empty name, a symlink, or invalid JSON block the run.

## Run binding

The source manifest is an absolute, regular, non-symlink JSON file. Its
`binding` is compared with the Taskboard-owned execution context:

| Manifest field | Execution binding |
| --- | --- |
| `task_id` | `CODEX_AUTOCUT_TASK_ID` |
| `run_id` | `CODEX_AUTOCUT_RUN_ID` |
| `subject_key` | `CODEX_AUTOCUT_SUBJECT_KEY` |
| `config_version` | `CODEX_AUTOCUT_CONFIG_VERSION` |
| `stage_id` | `CODEX_AUTOCUT_STAGE_ID` |
| `event_id` | `CODEX_AUTOCUT_EVENT_ID` |

The canonical SHA-256 of the normalized manifest must also equal
`CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256`. Object keys are sorted for the canonical
hash; array order is retained. Supported stage IDs are `initial`,
`first_review`, and `final_review`. A missing or different binding or digest is
rejected before source processing.

The manifest contains only identifiers, one Feishu `/docx/<token>` or
`/wiki/<token>` URL, and source descriptors. This example uses placeholders
rather than a real tenant, token, record, or field. The URL path contains
exactly that one token segment and has no query string or fragment:

```json
{
  "schema_version": 1,
  "binding": {
    "task_id": "task-example",
    "run_id": "run-example",
    "subject_key": "base-example:table-example",
    "config_version": 1,
    "stage_id": "initial",
    "event_id": "event-example"
  },
  "record": {
    "base_token": "base-example",
    "table_id": "table-example",
    "record_id": "record-example"
  },
  "document": {
    "field_id": "document-field-example",
    "url": "https://tenant.feishu.cn/docx/docx-token-placeholder"
  },
  "sources": {
    "video": {
      "kind": "docx_section",
      "anchor_text": "Video"
    },
    "review": {
      "kind": "docx_section",
      "anchor_text": "Review Notes"
    },
    "audio": {
      "mode": "video_original"
    }
  }
}
```

Unknown manifest fields are rejected. Keys that attempt to carry a path,
command, prompt, shell, or executable are also rejected.

## Feishu identity

Document and Base reads use the current operator's Feishu/Lark user identity.
The local CLI must be configured with both settings before Taskboard starts a
run:

```powershell
lark-cli config default-as user
lark-cli config strict-mode user
```

The runner checks `lark-cli whoami`; availability, current identity, and default
identity must all report `user`. Docx fetch and Docx media download commands pass
the user identity explicitly; Base reads run only after the verified user
default. There is no bot/application fallback.

## Source selection

### Docx sections

A `docx_section` descriptor contains one manually configured `anchor_text`.
Only structural heading blocks are eligible anchors; paragraph text, checkbox
content, attachment captions, and other body blocks are ignored even when their
text is identical. Matching first uses the complete, case-sensitive heading
text after trimming leading and trailing whitespace. A unique exact match
always wins. If there is no exact match, the selector may remove at most one
controlled leading section number from each side and then compare the complete
remaining text. Supported forms include Chinese enumeration (`二、`), Arabic
enumeration (`2.`), parenthesized enumeration (`（二）`, `(2)`), and hierarchical
numbering (`3.1`). The fallback does not use substring, case folding,
transliteration, edit distance, or other fuzzy matching. Zero fallback matches
raise `docx_anchor_missing`; multiple fallback matches raise
`docx_anchor_ambiguous`. The configured `anchor_text` remains unchanged in the
manifest and selection receipt.

Hierarchical numbers use whitespace or an enumeration separator before the
title body. An unseparated version-like title such as `2.0时代` is ordinary
title text and is not stripped as an automatic section number.

The selected range begins after the anchor. Nested headings and their content
remain in the range. Selection stops at the first of:

- another configured structural heading under the same exact/one-number
  fallback rule, when it is at the same or a higher level; or
- the next structural heading at the same or a higher level, whether or not its
  text is a configured anchor.

A configured heading nested below the selected heading does not close the
section. Body text and checkboxes never close a section merely because they
equal a configured anchor. If structural heading metadata omits a usable level,
the next structural heading closes the range rather than allowing the range to
leak into a later section.

Every attachment in a selected media range is downloaded in document order.
Original filenames are retained; a local collision receives `_2`, `_3`, and so
on. MIME type or extension classifies downloaded attachments; a video source
uses only video attachments and an audio source uses only audio attachments.
Unrelated attachment types remain downloaded but are not treated as media. A
range with no attachment of the configured media type blocks the run. When a
review range contains checkbox blocks, those checkboxes are the review items;
surrounding plain-text labels are not. Docx block presentation types are removed
before the review compiler infers editing semantics from the original text.

The runner does not search outside the configured ranges, score approximate
headings, match filenames, or guess which attachment is intended.

### Base attachment fields

Video or replacement audio may instead use a `base_attachment` descriptor:

```json
{
  "kind": "base_attachment",
  "field_id": "attachment-field-example"
}
```

`base_token`, `table_id`, and `record_id` default to the manifest's top-level
`record` binding. The configured field must contain exactly one attachment. Zero
or multiple attachments block the run; Auto-Cut does not choose one by name,
size, time, or order. Review notes remain a Docx section.

## Ordering and audio

Downloaded videos retain Feishu document order. In `replace_original` mode,
downloaded audio files also retain document order and are paired one-to-one by
position. Counts must match. There is no filename pairing, duration sorting, or
other inference.

The implemented audio modes are:

- `video_original`: use each video's original audio. The manifest must not
  provide an audio source or duration tolerance. A video without an audio
  stream blocks the run.
- `replace_original`: mute the video-original audio and use the paired external
  audio. `sources.audio.source` is required. The default
  `duration_tolerance_seconds` is `3`; a video/audio difference greater than
  the configured tolerance blocks the run.

For ordered pairs, word-level ASR is performed independently for each pair and
then mapped onto a cumulative timeline in that same order. Pair order, source
hash, ASR input hash, provider identity, and pair-local timing mismatches are
hard integrity failures rather than label-only ASR fallbacks.

## Terminal result

`--result-path` is the only Taskboard terminal-result location. A successful
run writes this schema atomically after the editable draft and ZIP pass final
validation:

```json
{
  "schema_version": 1,
  "binding": {
    "task_id": "task-example",
    "run_id": "run-example",
    "subject_key": "base-example:table-example",
    "config_version": 1,
    "stage_id": "initial",
    "event_id": "event-example"
  },
  "manifest_sha256": "<normalized-source-manifest-sha256>",
  "status": "pass",
  "package_zip": "<absolute-final-zip-path>",
  "archive_sha256": "<final-zip-sha256>",
  "draft_name": "Course Name - Initial"
}
```

Taskboard must verify the exact binding, `manifest_sha256`, absolute
`package_zip`, and the ZIP bytes against `archive_sha256`. It must not replace
`package_zip` with a directory search or a filename guess.

The validated ZIP has a separate sibling package receipt named
`<final-name>.zip.receipt.json`. That receipt uses
`source_manifest_sha256` for the manifest digest and also contains the binding,
absolute package path, archive hash, ordered source pairs, and the existing Lite
package validation evidence. The field-name difference is intentional:
Taskboard terminal result uses `manifest_sha256`; package receipt uses
`source_manifest_sha256`.

## Blocking behavior

A failure after the manifest is bound writes a terminal result with
`status: "blocked"`, the same binding and `manifest_sha256`, and a sanitized
`error` object containing `code`, `message`, and `details`. If manifest parsing
or binding validation fails before a manifest can be loaded, the runner uses
only the complete Taskboard-injected binding and digest to write that same
blocked receipt; it never copies binding fields from the invalid manifest.
A blocked result does not contain `package_zip`, `archive_sha256`, or
`draft_name`, and cannot be reported as a successful artifact.

Blocking conditions include a missing or ambiguous anchor, an empty configured
range, unavailable user identity, unreadable document or Base record, attachment
count/type/download failures, video/audio count or duration mismatch, media or
ASR identity mismatch, invalid draft output, and failed ZIP validation. The job
remains resumable under its explicit `--job-root`; no different task, run,
stage, source, or artifact is inferred to continue it.
