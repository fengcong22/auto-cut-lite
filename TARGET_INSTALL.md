# Auto-Cut 1.7.0 Target Installation

This release is delivered as three independent archives. Keep them separate:

- `Auto-Cut-v1.7.0-windows-x64.zip`: universal program source.
- `Auto-Cut-v1.7.0-windows-x64-offline-deps.zip`: Windows x64, CPython 3.11 dependencies, Chromium, and licensed FFmpeg tools.
- `Auto-Cut-v1.7.0-private-assets-high-school-history.zip`: private, user-owned pointer-profile evidence. Do not redistribute it with the universal archives.

The target computer must be Windows 10/11 x64 and must have official 64-bit
CPython 3.11 installed and discoverable through `py -3.11` or `python`. No
source-computer virtual environment, pip cache, or browser cache is copied.
A new extraction creates new local environments; rerunning setup in the same
extraction may reuse only compatible repository-local environments and always
reinstalls the fixed hashed locks. Offline mode does not fall back to the network.

Formal release acceptance uses a fresh temporary extraction plus the same
bundle-local command and environment gates. It does not require Windows
AppContainer, a firewall change, or a physically disconnected machine. The
acceptance report explicitly records physical network isolation as
`not_required`; `offline_bundle_no_network_fallback_verified` means that
package indexes, browser downloads, proxy/index environment variables, and
URL fallbacks were rejected for the fixed, hash-verified release commands. It
is not a general network security sandbox and does not claim to block arbitrary
direct socket code; the privacy scan and runtime capability audit run before
any extracted release command.

## 1. Verify The Delivered Bytes

Compare every printed digest with the release report received through the
trusted transfer channel. The program ZIP must also match its `.sha256`
sidecar.

```powershell
Get-FileHash .\Auto-Cut-v1.7.0-windows-x64.zip -Algorithm SHA256
Get-Content .\Auto-Cut-v1.7.0-windows-x64.zip.sha256
Get-FileHash .\Auto-Cut-v1.7.0-windows-x64-offline-deps.zip -Algorithm SHA256
Get-FileHash .\Auto-Cut-v1.7.0-private-assets-high-school-history.zip -Algorithm SHA256
```

Stop if any filename, size, or digest differs. SHA-256 proves byte integrity;
the trusted delivery channel establishes publisher identity.

## 2. Perform The Offline Program Install

Extract only the universal program ZIP into a new directory. Keep the offline
dependency ZIP unchanged and pass it directly to setup:

```powershell
Set-Location <extracted-program-directory>
powershell -ExecutionPolicy Bypass -File .\setup.ps1 --offline-bundle <offline-dependency-zip> --json
```

Offline setup verifies the companion manifest, release version, source commit,
Windows x64 target, CPython 3.11 ABI, and every payload SHA-256 before use. All
pip operations use `--no-index` and bundle-local `--find-links`. Chromium is
installed from the verified browser store with recording FFmpeg revision 1011,
then launch-tested and exercised through a real `record_video_dir` output.
The persisted browser store at `tmp/offline-runtime/browsers` is automatically
discovered by ordinary recording and pointer-reference rendering. FFmpeg and
FFprobe are taken from the verified companion, persisted at
`tmp/offline-runtime/tools/ffmpeg`, and automatically discovered by ordinary
audio commands. The audio runtime remains in a separate `.venv-audio` and its
real doctor is executed.

The companion is optional when the target has network access. For the normal
online installation, omit `--offline-bundle`:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 --json
```

That uses the ordinary package index and browser download path. Supplying
`--offline-bundle` selects the verified local path above; the installer never
silently switches between the two modes.

Review `tmp/install/install-report.json`. Do not treat a failed, skipped, or
missing stage as a complete install. DeepFilterNet remains `unavailable` until
a fixed verified model and trusted adapter exist, even when its Python wheel is
present. Respiro is not bundled and remains `unavailable`.

## 3. Match JianYing And Its Fixed Draft Root

JianYing itself is not redistributed. Obtain the source project's verified
JianYing application version and its `currentCustomDraftPath` through the
trusted project handoff; neither value is copied from account caches. The
detected target version must equal the handed-off version exactly. Set the
expected value and stop immediately on a missing or different version:

```powershell
$expectedJianYingVersion = "<source-app-version>"
$detectedJianYing = .\.venv\Scripts\python.exe scripts\jy_wrapper.py detect-env --json | ConvertFrom-Json
$actualJianYingVersion = $detectedJianYing.data.environment.app_version
if ([string]::IsNullOrWhiteSpace($actualJianYingVersion) -or $actualJianYingVersion -cne $expectedJianYingVersion) {
    throw "JianYing version mismatch"
}
.\.venv\Scripts\python.exe scripts\jy_wrapper.py draft-root-check --expected-root <source-currentCustomDraftPath> --json
.\.venv\Scripts\python.exe scripts\jy_wrapper.py self-check --cleanup --refresh --json
```

Configure JianYing to use the exact agreed draft root and create that directory
before continuing. A missing or different path is a blocking result; never let
Auto-Cut silently substitute a conventional default. Confirm the self-check
created and removed an editable smoke draft and recorded the detected app
version.

## 4. Restore The Private History Profile

Extract the private archive into a separate target-owned directory only after
its outer ZIP hash is verified. Register the sanitized intake into a new empty registry;
never merge it into an existing registry or reuse existing project bindings. The
following path is dedicated to this restored profile and must not already exist:

```powershell
$privateRegistry = Join-Path "data" "subject-pointer-profiles.restored-history"
if (Test-Path -LiteralPath $privateRegistry) { throw "new empty registry required" }
New-Item -ItemType Directory -Path $privateRegistry | Out-Null
.\.venv\Scripts\python.exe skills/auto-cut-subject-pointer-onboarding/scripts/profile_registry.py register --input <private-assets-directory>\intake.json --root $privateRegistry --json
.\.venv\Scripts\python.exe skills/auto-cut-subject-pointer-onboarding/scripts/profile_registry.py check --stage-id senior-high --subject-id history --root $privateRegistry --json
.\.venv\Scripts\python.exe skills/auto-cut-subject-pointer-onboarding/scripts/profile_registry.py validate --root $privateRegistry --json
```

Continue only when the exact `senior-high-history` profile is `ready`, every
stored file hash is current, both scale references remain confirmed full-frame
evidence, and all previews remain approved. The archive contains no
`project-bindings.json`. After the user explicitly confirms the exact target
project, run the binding command described by the onboarding skill against the
same `$privateRegistry`. Use
`--rebind` only after a separate explicit replacement confirmation. Never
substitute a generic hand, another subject, or another school stage.

## 5. Complete Network And Authorization Onboarding

The following are intentionally not transferable offline capabilities:

- Volcengine ASR credentials, entitlement, service jobs, and reverse-ASR evidence.
- Edge TTS and JianYing SAMI TTS network execution.
- Codex account or authorization state.
- Feishu notification identity, destination approval, and authorization.
- JianYing cloud materials, favorites, templates, and account caches.

Configure only the capabilities required on this target:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py favorites-sync --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py pointer-guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py feishu-status --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-status --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py tts-status --json
```

Configuration presence is not proof that a cloud request succeeded. Preserve
the service-specific receipts required by the relevant Auto-Cut skill.

## 6. End-To-End Calibration

1. Confirm the install report, main imports, Chromium launch, FFmpeg,
   FFprobe, audio doctor, 16 skill hashes, and JianYing self-check.
2. Re-run the exact private-profile `check` immediately before pointer work.
3. Use a small target-owned sample to create an editable draft with source
   video, separated audio, at least one visible split, and one review marker.
4. Open that draft in the detected JianYing version, preview it, save it, and
   confirm the fixed draft root still matches.
5. Exercise each authorized network capability with non-sensitive test media
   and retain its attributable result receipt; leave unused capabilities
   pending or unavailable.
6. Inspect the final draft timeline. A flattened full-length video/audio pair,
   missing source material, missing cut structure, or missing trace markers is
   a failed calibration.

Use the natural-language `auto-cut` entrypoint for ordinary work, the
`auto-cut` router when capability selection is unclear, or an exact
`auto-cut-<purpose>` skill when the required workflow is already known.
