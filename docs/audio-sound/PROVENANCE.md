# Audio-sound Source Provenance

The canonical integration source is the read-only source snapshot named
`Audio-sound-release-clean-v0.1.0`.

Its receipt is stored in `source-migration-manifest.json`: 50 files totaling
1,255,475 raw bytes. Forty-eight text files use CRLF in the source package and
are represented in Git with LF-normalized content. The manifest therefore
records both the raw source SHA-256 and the normalized content SHA-256/Git blob
identity. The JPG and WAV fixture require no normalization.

The directory name suggests version `0.1.0`, while its `pyproject.toml` declares
package version `0.2.0`. Both values are recorded; the integrated package is not
downgraded based only on the directory name.

The clean source omits `LICENSE`. The existing
`docs/audio-sound/LICENSE.audio-sound` is retained unchanged from
`Audio-sound-release-main` as legal and attribution evidence. It is listed under
`retained_legacy_artifacts`, not among the 50 clean-source items.

Files under `upstream-control/` and `README.upstream.md` are Git-normalized
content snapshots used for provenance. They are not raw CRLF copies and are not
current operating instructions.
