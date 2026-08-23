---
name: auto-cut-final-acceptance
description: Use when validating or delivering a generic editable lite JianYing revision.
---

# Final Acceptance

Run the saved-draft validators before packaging. Required checks are unchanged project duration, editable segmented tracks, valid source and replacement material references, mapped replacement-local timelines, ASR boundary receipts for spoken deletes, reverse-audio evidence, verbatim marker text and safe marker timing.

For delivery, run `revision-run --workflow-mode lite --strict --package-zip <zip> --json`. Verify the external receipt's ZIP SHA-256, CRC result, extracted tree hash and absence of unsafe paths. Do not claim delivery when any gate is unresolved.
