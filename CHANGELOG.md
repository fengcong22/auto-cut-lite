# Changelog

## v1.7.0 - 2026-08-16
- Recorded the safely inspected prior release baseline:
  `Auto-Cut-v1.6.1-windows-x64.zip`, `7,372,704` bytes, ZIP SHA-256
  `66a88f25476a206c0bf83162b9d4996b4868b830bbbe38edb1c6cecbdb5d0251`,
  source commit `6269f1f76ed6f153d44f3dacfb6d8722a5e6e0b5`, and inventory
  SHA-256 `1a9e2e75f65f95ada32f3506c48bce29eb1f4baaec4c72e8a8f864f72348bcad`.
  No archive-bound acceptance report was found, so formal acceptance is unproven.
- Added a reproducible Windows x64 CPython 3.11 offline dependency companion
  with complete main/audio/bootstrap/test wheelhouses, per-file source,
  version, platform, license and SHA-256 records, Playwright 1.52.0 Chromium
  revision 1169 launch evidence, recording FFmpeg revision 1011, a real
  `record_video_dir` probe, WinLDD revision 1007 under MIT and Apache-2.0
  terms, and a fixed, reproducibly built minimal FFmpeg/FFprobe 8.1.2 runtime
  (source tag `n8.1.2`, static Windows x64 binaries, no external codec
  libraries) with committed source, build evidence, license closure, and
  SHA-256 identities.
- Extended the full Windows installer and clean-extraction acceptance to bind
  the offline dependency archive, use fail-closed `--no-index` installs, reject
  network or browser-download fallback, and preserve a truthful
  `capability-manifest.json` result.
- Clean-extraction acceptance now uses a temporary bundle-local execution
  guard; Windows AppContainer, firewall changes, and physical disconnection
  are not required or claimed. Reports distinguish
  `offline_bundle_no_network_fallback_verified` from physical network
  isolation.
- Constrained non-AppContainer acceptance executables, working directories,
  and temporary files to a fresh workspace, kept the schema-v1
  `offline_network_isolation` compatibility field, and documented that the
  fixed-command guard is not a general direct-socket security sandbox.
- The installer keeps both explicit modes: omit `--offline-bundle` for a
  network-backed install, or pass the companion to force local wheelhouses;
  neither mode silently changes into the other.
- Persisted verified browser and FFmpeg runtimes under `tmp/offline-runtime`
  and added automatic discovery for ordinary web recording, pointer-reference
  rendering, audio CLI, and audio skill workflows.
- Kept recording receipts byte-reproducible by persisting the verified result
  rather than a run-dependent media size, and made program/offline archive
  promotion remove final-looking outputs when hashing, self-verification, or
  SHA-256 sidecar creation fails.
- Added a separate private `senior-high-history` migration archive containing
  only approved user-owned pointer evidence and minimized registration data;
  restore requires a new empty isolated registry, validation, and explicit
  target-project binding.
- Fixed the runtime capability audit failure caused by a machine-shaped path
  example in runtime source without weakening the privacy scanner.
- Added `TARGET_INSTALL.md` for hash verification, fixed JianYing draft-root
  checks, private restore, authorization onboarding, and end-to-end calibration.

## v1.6.1 - 2026-07-23
- Added a fail-closed runtime capability contract and release audit covering all
  20 `capability-manifest.json` entries, their files, dependencies, external
  tools and service hosts, and clean extraction without Git metadata.
- Bundled the maintained Volcengine word-alignment adapter and required
  attributable `input_sha256`, service job/result identity, bound resource and
  adapter version, plus non-empty finite positive-duration monotonic word timing
  before ASR can be reported as successful.
- Added mutually exclusive legacy App ID/Access Token and new-console API-key
  authentication, HTTPS-only service endpoints, and queued-job polling for the
  Volcengine v3 adapter.
- Expanded the full Windows installer, first-use, API, audio, and privacy
  documentation with target-local Volcengine/TTS setup and truthful bundled,
  first-run, local JianYing, authorization, user-asset, and unavailable states.

## v1.6.0 - 2026-07-23
- Added the full Windows installer (`setup.ps1`) for Python 3.10-3.12, main
  dependencies, all 16 skills, Playwright Chromium, isolated audio setup, and
  JianYing environment checks.
- Added `capability-manifest.json`, first-use guidance, truthful audio component
  reporting, and fail-closed model execution.
- Added deterministic privacy-checked release ZIP construction and clean
  extraction acceptance without Git metadata.
- Preserved editable draft source media, separated audio, visible cuts, review
  markers, and final acceptance contracts.

## v1.5.0 - 2026-07-22
- Security hardening:
  - sanitized draft project names and blocked path traversal/out-of-root delete.
  - restored TLS verification for SAMI TTS by default.
  - added cloud download URL/header/size guards.
- API/CLI standardization:
  - unified machine-readable `--json` output for key scripts.
  - added strict mode for validator (`--strict`).
  - centralized runtime config (`scripts/utils/config.py`).
- Quality engineering:
  - expanded unit tests for security guards.
  - added repo hygiene and data schema checks.
  - added CI lint/format/test/schema pipeline.
- Repo organization:
  - removed tracked runtime artifacts and cache binaries.
  - added compatibility wrappers and common logger utility.
