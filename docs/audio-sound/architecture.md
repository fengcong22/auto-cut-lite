# Audio Engine Architecture

## Boundaries

The maintained engine lives in `audio_sound/`. CLI files under `scripts/audio/` are adapters and
must remain runnable from any working directory. Presets live only under `presets/audio_sound/`.
The package must not import `scripts.*` modules; reusable narrow-window behavior therefore lives in
`audio_sound/narrow_cleanup.py`.

## Runtime

The main Auto-Cut interpreter starts setup and diagnostics but never receives the heavy audio
stack. `audio_sound.bootstrap` builds a random sibling staging environment from the sole lock at
`requirements-audio.lock`, keeps pip temp files and the Rust build cache inside that staging tree,
and promotes it to `.venv-audio` only after verification. Model or tool assets belong under
`tools/audio_sound_runtime/`.

Setup and cleanup share a repository lock, use random quarantine/staging paths, and refuse links or
Windows reparse points. These controls protect repository-owned and cooperating processes; they are
not an operating-system sandbox against a hostile process already running as the same user. Do not
run setup or cleanup concurrently with untrusted same-user code.

Unverified floating downloads are rejected. Respiro or model-backed execution may be reported as
available only when the interpreter, source checkout, weights, and required Python packages are
actually present.

## Processing Flow

1. Resolve a preset from `presets/audio_sound/`.
2. Probe and extract the input with FFmpeg/FFprobe.
3. Run only selected stages and record actual execution state.
4. Produce WAV/MP3, metrics, reports, and spectrogram evidence.
5. For explicit spoken deletion, use `audio_sound.segment_removal` and validate each seam.

Standalone audio output is necessarily rendered audio. When the result enters a JianYing revision,
the revision pipeline must still preserve source media, separated audio, visible cut structure,
markers, and segmented editable delivery.

## Source Traceability

The migration ledger at `docs/audio-sound/source-migration-manifest.json`
records raw source size and SHA-256, line-ending normalization, normalized
SHA-256 and Git blob ID, treatment, and maintained destinations for all 50 files
in the canonical clean source. The separately retained license is legal
provenance from the previous source package. Historical or source-control
documents are provenance only; current operating instructions are in this
directory and `skills/auto-cut-audio-restoration/`.
