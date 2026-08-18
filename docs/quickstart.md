# Quick Start

This is the shortest supported path for Auto-Cut v1.7.0 on a new Windows
10/11 x64 computer.

## 1. Run The Unified Installer

Install 64-bit Python 3.10-3.12 for the main runtime. Python 3.11 must also be
installed and discoverable for the isolated audio runtime. Then extract the
release, open PowerShell in the package root, and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

The formal offline path requires Windows x64 CPython 3.11 and the verified
companion ZIP:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 --offline-bundle <offline-dependency-zip> --json
```

This mode verifies every companion file and uses only `--no-index` and local
wheelhouses. Chromium revision 1169 and recording FFmpeg revision 1011 are
installed from the companion and verified with a launch plus a real
`record_video_dir` output. The browser store at
`tmp/offline-runtime/browsers` and FFmpeg/FFprobe at
`tmp/offline-runtime/tools/ffmpeg` are automatically discovered by normal
recording, pointer rendering, and audio commands. No dependency or browser
download fallback is allowed.

The companion is optional when the target is online: omit `--offline-bundle` to
use the normal network-backed package and browser installation, or keep the
flag to force the verified local wheelhouses. No AppContainer or physical
network disconnect is required for either choice.

The command creates `.venv`, installs `requirements.txt`, copies and verifies
all 16 Auto-Cut skill trees, installs and launch-tests Playwright Chromium,
verifies a recorded artifact, creates the isolated Python 3.11 `.venv-audio`,
checks FFmpeg/FFprobe, runs the real audio doctor, and runs the JianYing
self-check. The structured local report is `tmp/install/install-report.json`.

## 2. Read The Capability Result

Read the target-owned first-use status at any time:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py status --json
```

This first-use status does not replace `tmp/install/install-report.json`; use
the saved report for dependencies, Chromium, FFmpeg/FFprobe, the audio doctor,
and the JianYing smoke result.

`ready` means the capability was exercised successfully on this computer.
`degraded` is a real partial result, not full capability. `pending` means local
JianYing, authorization, software, or user assets are still required.
`unavailable` means this release cannot safely provide that optional capability.

The maintained audio runtime may be `degraded`: SpectraMini-style cleanup can be
ready while DeepFilterNet and Respiro remain unavailable until licensed,
fixed-version, SHA-256-verified model assets and trusted adapters exist. This
does not prevent editable JianYing work.

## 3. Initialize Machine-Owned Resources

Inspect all first-use requirements:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py guide --json
```

Then run only the actions needed on this target machine:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py favorites-sync --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py pointer-guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py feishu-status --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-config
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-status --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py tts-status --json
```

Favorite flower-text and template assets are resynchronized from this machine.
Subject pointers require user assets, reference images, profile approval, and
explicit binding confirmation. Feishu and Volcengine configuration and
authorization are local to this computer and are never copied from the release
builder. SAMI TTS uses only this computer's JianYing identity; Edge TTS still
needs a successful target-network request.

Configuration presence is not a Volcengine service success. After target-local
authorization, exercise the bundled word-level adapter against real media:

`volc-guide --json` provides both official console paths and protocol references.
The adapter supports two mutually exclusive authentication modes: a new-console
`X-Api-Key`, or legacy App ID plus Access Token. Select one mode in
`volc-config`; never configure or transmit both sets of headers.

```powershell
.\.venv\Scripts\python.exe scripts/audio/volc_word_align.py ".\media\voice.wav" --output "tmp\volc\voice.words.json"
```

Success requires `input_sha256`, `service_job_id`, `service_result_sha256`,
`resource_id`, `adapter_version`, and non-empty finite positive-duration
monotonic word timing rows. Without authorization or valid service evidence,
the capability remains pending or the command fails; it is never reported as
successful.

## 4. Verify JianYing Again

```powershell
.\.venv\Scripts\python.exe scripts\jy_wrapper.py self-check --cleanup --refresh --json
```

`data.usable=true` means editable draft writing and the create/cleanup smoke test
passed. Live window attachment can remain unavailable when JianYing is closed.

## 5. Start An Edit

Use Auto-Cut in one of three supported ways:

1. Natural language: describe the editing result and let `auto-cut` route it.
2. Router skill: explicitly ask `auto-cut` to select the required capabilities.
3. Targeted skill: name an exact `auto-cut-<purpose>` skill.

The release preserves source video, separated audio, replacement media, visible
cut boundaries, exact review markers, and final acceptance evidence in editable
JianYing drafts.

For fixed draft-root matching, private `senior-high-history` profile restore,
explicit target-project binding, and network calibration, follow
`TARGET_INSTALL.md` from the release root.

## Release Evidence

The release is accepted from a clean extraction without .git. Its versioned ZIP,
SHA-256 sidecar, JSON report, and Markdown report are generated together. See
`docs/release-full-install.md` for the 20-item capability table, verification
commands, privacy exclusions, and external requirements.
