# Full Windows Distribution

Auto-Cut v1.7.0 is distributed as a source-inspectable ZIP for Windows 10/11
x64. The target computer needs 64-bit Python 3.10-3.12. Python 3.11 must also be
installed and discoverable when the isolated audio environment is installed.

## One-Command Setup

Extract the ZIP, open PowerShell in the package root, and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

The formally accepted offline installation uses official Windows x64 CPython
3.11 and the separately verified dependency companion:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 --offline-bundle <offline-dependency-zip> --json
```

The offline companion contains complete main, audio, bootstrap, and acceptance
wheelhouses; Playwright 1.52.0 Chromium revision 1169; recording FFmpeg
revision 1011; WinLDD revision 1007 under MIT and Apache-2.0 terms; and the
committed, reproducibly built minimal FFmpeg/FFprobe 8.1.2 (source tag
`n8.1.2`, static Windows x64 binaries, no external codec libraries). The
installer verifies release identity,
target ABI, manifest and payload hashes before use. Every pip command uses
`--no-index` with a bundle-local `--find-links`, and offline mode has no index,
proxy, browser-download, or native-tool fallback.

The verified browser store is persisted at `tmp/offline-runtime/browsers`, and
the general FFmpeg tools are persisted at `tmp/offline-runtime/tools/ffmpeg`.
Ordinary web recording, subject-pointer reference rendering, audio CLI, and
audio skill workflows discover those locations without long-lived manual
environment overrides.

The offline companion is optional on a networked target. Omit
`--offline-bundle` for the ordinary online install, or provide it to force all
dependency installation through the verified local wheelhouses. The choice is
explicit; neither mode requires Windows AppContainer or a physically
disconnected machine.

The installer performs and records these checks:

1. Windows x64 and Python 3.10-3.12 preflight.
2. Main `.venv`, `requirements.txt`, and all declared main dependency imports.
3. Exactly 16 source-identical repository-local Auto-Cut skills.
4. Playwright Chromium installation, a real headless launch, and a real
   `record_video_dir` output using recording FFmpeg revision 1011.
5. FFmpeg and FFprobe from the verified offline companion, or explicit local discovery during online setup.
6. Python 3.11 `.venv-audio` installation and the real audio doctor.
7. JianYing discovery, editable smoke-draft creation, and cleanup verification.
8. Capability-manifest integrity and target-local first-use status.

The target-specific JSON report is written below `tmp/install/`; it is ignored
and is never copied back into a release ZIP. Read target-owned first-use status
at any time with:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py status --json
```

This first-use status does not replace `tmp/install/install-report.json`, which
is authoritative for dependencies, Chromium, FFmpeg/FFprobe, the audio doctor,
and the JianYing smoke result.

`ready` means the target machine supplied real successful evidence. `degraded`
means a documented partial path, never full capability. `pending` means local
software, authorization, or user assets are still required. `unavailable`
means the release cannot safely enable the optional capability.

## First Use On The Target Machine

Start with the complete guide, then initialize only the machine-owned features
that the user needs:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py favorites-sync --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py pointer-guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py feishu-status --json
```

Favorite flower-text effects and text templates are rebuilt from the target
user's own JianYing favorites and caches. Subject pointer data is also
user-owned: the guide requests the exact stage and subject, role-specific PNG
assets, scale and layout screenshots, approved previews, reference images, and
a separate explicit binding confirmation. No default high-school-history
profile or build-computer binding is assumed.

Feishu notifications are optional and notification-only. Configuration,
authorization, identity selection, recipient approval, privacy approval, and
the dry-run preview all happen independently on the target machine. A Feishu
message cannot approve or resolve an operation in place of the originating
Codex task.

Volcengine word alignment and TTS have separate target-local checks:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-config
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-status --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py tts-status --json
```

Auto-Cut v1.7.0 supports two mutually exclusive authentication modes for the
same Volcengine v3 submit/query API. New-console users provide one `X-Api-Key`
from the
[API-key console](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default).
Legacy-console users provide App ID plus Access Token, which the adapter sends
as `X-Api-App-Key` plus `X-Api-Access-Key`. Register, complete real-name
verification, create an application, and enable the required recording-file ASR
model in the [legacy speech console](https://console.volcengine.com/speech/app)
when using legacy credentials; the
[legacy quickstart](https://www.volcengine.com/docs/6561/163043) and
[credential FAQ](https://www.volcengine.com/docs/6561/196768) describe service
activation and where those values become visible. `volc-config` asks which mode
to use, clears the other mode's local fields, and never mixes authentication
headers. Charges, trial state, and entitlement belong to the target user's
account.

The current [HTTP API reference](https://www.volcengine.com/docs/6561/1354868)
maps model 1.0 to `volc.bigasr.auc` and model 2.0 to
`volc.seedasr.auc`. The default `volc.bigasr.auc` follows the full Auto-Cut recording path;
application has model 2.0 trial or paid entitlement. Both authentication modes
still require `X-Api-Resource-Id`; a configured credential or default resource
ID is not entitlement evidence.

`volc-status --json` reports whether target-local configuration is present but
does not claim a service request succeeded. After authorization, a real
word-alignment request can be run with the bundled adapter:

```powershell
.\.venv\Scripts\python.exe scripts/audio/volc_word_align.py ".\media\voice.wav" --output "tmp\volc\voice.words.json"
```

A successful normalized result must bind `input_sha256`, `service_job_id`,
`service_result_sha256`, `resource_id`, and `adapter_version` to non-empty,
finite, positive-duration, monotonic word timing rows. Missing authorization,
configuration presence alone, an empty word list, or invalid timing is not
reported as success.

No credential, authorization URL, device code, raw recipient ID, JianYing
identity, or prior computer's notification state is bundled.

## Capability Status

The six Boolean columns below reproduce the classifications in
`capability-manifest.json`. They describe what the release owns; the actual
`ready`, `degraded`, `pending`, or `unavailable` result is measured again on the
target computer.

| Capability ID | Bundled | First run | Local JianYing | User auth | User assets | Unavailable | Initial boundary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `editable_draft_contract` | true | false | true | false | false | false | Bundled contract; real editing needs compatible local JianYing. |
| `auto_cut_skills` | true | true | false | false | false | false | All 16 source skill trees are bundled and hash-verified during installation. |
| `main_python_dependencies` | false | true | false | false | false | false | Installed online or from the verified CPython 3.11 wheelhouse. |
| `playwright_chromium` | false | true | false | false | false | false | Chromium revision 1169 and recording FFmpeg revision 1011 are installed, launch-tested, and verified with `record_video_dir`. |
| `jianying_smoke_draft` | true | false | true | false | false | false | Pending until local JianYing can create and clean an editable smoke draft. |
| `favorite_text_assets` | true | false | true | false | true | false | Code is bundled; the target user's favorites must be resynchronized. |
| `subject_pointer_profiles` | true | false | false | false | true | false | Create locally or restore the private exact profile into a new empty isolated registry, validate it, then explicitly bind. |
| `feishu_notifications` | true | false | false | true | false | false | Notification code is bundled; target-local authorization and approval remain pending. |
| `ffmpeg` | false | true | false | false | false | false | Committed minimal 8.1.2 build (source tag `n8.1.2`, license closure and SHA-256 pinned) persisted under `tmp/offline-runtime/tools/ffmpeg`; otherwise explicit local software. |
| `ffprobe` | false | true | false | false | false | false | Committed minimal 8.1.2 build (source tag `n8.1.2`, license closure and SHA-256 pinned) persisted under `tmp/offline-runtime/tools/ffmpeg`; otherwise explicit local software. |
| `audio_runtime` | true | true | false | false | false | false | Installer creates `.venv-audio`; the real doctor decides readiness. |
| `spectramini_cleanup` | true | true | false | false | false | false | Maintained local stages are installed, then truthfully doctor-tested. |
| `deepfilternet` | false | true | false | false | false | true | Its wheel may be cached, but no verified model/adapter chain is available. |
| `respiro` | false | false | false | false | false | true | No licensed, fixed, hash-verified model manifest and trusted adapter are bundled. |
| `volc_asr_alignment` | true | false | false | true | false | false | Adapter is bundled; each target user authorizes and proves a real request. |
| `sami_tts` | true | false | true | false | false | false | Uses only the target computer's JianYing identity and verified network request. |
| `edge_tts` | false | true | false | false | false | false | Client installs on first run; network voice execution remains target-dependent. |
| `cloud_materials` | true | false | false | false | false | false | Static index is bundled; each remote asset is validated on request, so initial status is degraded. |
| `subtitle_material_matching` | true | false | false | false | false | false | Deterministic fallback is bundled; no external AI matcher is claimed, so status is degraded. |
| `carnac_overlay` | false | false | false | false | false | false | Optional target-installed software; recording works without it. |

Each manifest row also carries its own verification command and machine-readable
actual result. Build and clean-extraction acceptance, not target setup, run the
runtime-contract and privacy gates. The release-time capability audit rejects
missing entrypoints, undeclared dependencies or service hosts, missing release
files, and mismatches between the runtime contract and the manifest.

## Audio Capability Boundary

The complete audio doctor may be `degraded`, not full: SpectraMini-style local
processing can be available while DeepFilterNet and Respiro remain
`unavailable`. A model-backed stage can be enabled only with a license, fixed
version or revision, byte size, SHA-256, trusted adapter identity, successful
self-check, and saved run report. Until then, workflows use
`--skip-deepfilternet`; no implicit model download is allowed, and package
presence is never described as a working model chain.

## Editable Draft Contract

Portability does not flatten revision work. A delivered revision still exposes
source video, separated audio, replacement media, visible cut and split
boundaries, exact source-ledger review marker text, one independently traceable
marker per source item, and final acceptance evidence. A local effect may bake
only its own window; the remaining timeline stays editable.

## Privacy And Release Acceptance

The final ZIP is unpacked into a clean extraction without .git. Acceptance
verifies inventory hashes, all 20 runtime capability contracts, privacy policy,
16 skill hashes, main imports, Chromium launch and `record_video_dir` output,
JianYing smoke cleanup, the real audio doctor, no-Git repository contracts, and
data schemas. External JSON and Markdown reports bind those results to the
source commit and ZIP SHA-256.

The archive excludes `.git`, `.codex`, `.venv`, `.venv-audio`, `.env`,
credentials, Feishu or Volcengine authorization, local CSV overlays,
`data/subject-pointer-profiles.local`, project bindings and absolute paths,
JianYing identity, notification state, user assets, output, temporary files,
caches, and logs. These are recreated or authorized on the target machine.

The private high-school-history archive is intentionally outside the universal
program ZIP. It contains only hash-bound user-owned evidence plus sanitized
registration input, never project bindings or source-computer paths. Follow
`TARGET_INSTALL.md` for hash verification, exact `register`/`check`/`validate`,
fixed JianYing version and draft-root checks, explicit binding, and the full
target calibration sequence.
