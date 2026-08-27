# Lite Pointer Asset Association

The packaged Lite workflow does not use project bindings, subject registries, profile receipts,
scale references, hotspots, or target adapters. Use only review-document structural attachment
associations produced by the maintained intake compiler.

For a row with an attachment, bind that exact asset. For a hand/pointer row without one, reuse is
allowed only from another row whose compiler-normalized modification name is identical and whose
candidate attachment is unique. Do not broaden the match with filename similarity, OCR, ASR,
repository search, or inferred subject identity.

The reuse receipt records the missing row ID, normalized modification name digest, source row ID,
asset ID, local path, and SHA-256. Multiple candidates return structured `user_action_required`
with safe candidate IDs. Zero candidates leaves the item label-only and reports the missing
insertion.
