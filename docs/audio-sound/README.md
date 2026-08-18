# Integrated Audio-sound Engine

Audio-sound is maintained in Auto-Cut as the first-party `audio_sound` package. The imported
engine keeps the seven standalone processing modes, spoken-segment removal, narrow-window repair,
spectrogram reporting, and Chinese delivery naming without depending on the external release
directory.

## Repository Layout

- `audio_sound/`: reusable engine and workflow code.
- `presets/audio_sound/`: the seven migrated JSON presets.
- `scripts/audio/`: direct and compatibility entrypoints.
- `skills/auto-cut-audio-restoration/`: repository source skill.
- `tests/audio_sound/`: the 102 upstream tests plus integration contracts.
- `requirements-audio.lock`: isolated Python 3.11 dependency lock.
- `docs/assets/audio-sound/`: public waveform and spectrum evidence.

## Commands

Run from the Auto-Cut repository root:

```powershell
python scripts/audio/audio_cleanup.py doctor
python scripts/audio/audio_cleanup.py list-presets
python scripts/audio/audio_skill_workflow.py describe-modes
python scripts/audio/audio_skill_workflow.py run "D:\media\voice.wav" --skip-deepfilternet
```

Physical spoken-word removal remains a separate timeline operation:

```powershell
python scripts/audio/remove_spoken_segments.py run "D:\media\lesson.mp4" --cut "0.62,1.82"
```

Target-local Volcengine word alignment uses the bundled maintained adapter:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-config
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-status --json
.\.venv\Scripts\python.exe scripts/audio/volc_word_align.py ".\media\voice.wav" --output "tmp\volc\voice.words.json"
```

`volc-config` stores the target user's values only in the ignored `.env`.
`volc-status --json` proves configuration presence, not a real service call; it
does not return the APP ID or access token. Without target-local authorization,
the adapter must fail and no successful alignment may be claimed.

This release supports two mutually exclusive authentication modes for the same
v3 API: a new-console `X-Api-Key`, or legacy-console `X-Api-App-Key` plus
`X-Api-Access-Key`. Use `volc-config` to select one; the local setup clears the
other fields and the adapter never mixes headers. Create and authorize the
application in the
[legacy speech console](https://console.volcengine.com/speech/app) when using
legacy credentials, or obtain the API key from the
[new console](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default).
Confirm the selected model against the
[HTTP API reference](https://www.volcengine.com/docs/6561/1354868): model 1.0 is
`volc.bigasr.auc`, while model 2.0 is `volc.seedasr.auc`. Both modes still send
that resource ID; the default does not prove entitlement. Service endpoints
must use HTTPS.

## Volcengine Alignment Evidence

Each successful normalized report is attributable to the exact input and
service response. It contains `input_sha256`, `service_job_id`,
`service_result_sha256`, the request-bound `resource_id`, and
`adapter_version` (`auto-cut-volc-asr-v3`). The `words` list must be non-empty.
Every word boundary must be finite and non-negative, every interval must have
positive duration (`end > start`), and starts and ends must be monotonic across
the complete result. Empty, missing, non-finite, zero/negative-duration, or
nonmonotonic timing fails normalization rather than producing weak evidence.

Configuration alone, package import, a provider acknowledgement without word
rows, or an authorization error is never reported as successful ASR. Store any
normalized or optional raw report only under ignored runtime paths such as
`tmp/`; reports and credentials are not release content.

## Runtime Boundary

Heavy packages are installed only in `.venv-audio`; the main Auto-Cut environment keeps its
existing NumPy and Python contract. Generated output, models, locally staged FFmpeg bundles,
`.env`, and runtime caches are ignored by Git. The formal offline dependency companion is built
separately from fresh wheels and contains the fixed licensed FFmpeg/FFprobe runtime; it never copies
`.venv-audio` or a pip cache. Automatic Respiro downloads are disabled until a verified asset
manifest with pinned revision, size, license, and SHA-256 is available. Existing verified local
assets may be configured through `.env`.

For the v1.7.0 full installer, the real doctor result is authoritative. Offline
setup installs the audio lock only with `--no-index` and the verified audio
wheelhouse. A
`degraded` result must remain degraded: SpectraMini-style local processing may be
available while DeepFilterNet and Respiro are `unavailable`. Model-backed stages
require a license, fixed version or revision, size, SHA-256, trusted adapter,
self-check, and execution report. Until those exist, use
`--skip-deepfilternet`; the release does not silently download a default model
or describe an installed package or cached DeepFilterNet wheel as a working
model chain. Respiro is not present in the companion.

`--skip-spectramini` is an execution guarantee: when selected, the stage does not modify audio and
the report records it as not applied.

## Provenance

`source-migration-manifest.json` accounts for all 50 files in
`Audio-sound-release-clean-v0.1.0`, including raw CRLF hashes and normalized Git
content identities. See `PROVENANCE.md` for the version-label mismatch and the
separately retained legacy license. Repository-control snapshots under
`upstream-control/` and `README.upstream.md` are provenance, not operating
instructions for the integrated repository.
