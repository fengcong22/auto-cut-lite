# Audio Restoration SOP

## 1. Inspect the runtime

```powershell
python scripts/audio/audio_cleanup.py doctor
```

Do not claim Respiro, DeepFilterNet, or any model-backed stage unless the report says it is
available and used. A missing `.venv-audio` is a prerequisite failure, not permission to install
heavy packages into the main environment.

## 2. Select the operation

- Whole-file restoration: `python scripts/audio/audio_skill_workflow.py run <input>`.
- Low-level preset processing: `python scripts/audio/audio_cleanup.py clean <input>`.
- Exact mute/duck repair: `python scripts/audio/exact_window_cleanup.py`.
- Narrow onset repair: `python scripts/audio/narrow_onset_cleanup.py`.
- Spoken deletion: `python scripts/audio/remove_spoken_segments.py`.

Use presets from `presets/audio_sound/`. Default final delivery remains `reference-legacy`; select a
different mode only for a documented reason.

## 3. Protect speech boundaries

Treat requested timecodes as search hints. Confirm phoneme boundaries with listening, waveform,
and spectrogram evidence. Use short crossfades only for click prevention. If a clean cut creates an
unnatural semantic join, use a reviewed 60-100 ms seam pause instead of widening the deleted range.

## 4. Validate the complete candidate

Review the complete beginning and ending, every changed window, peak/loudness, clipping, dropouts,
and output duration. After a correction, regenerate and recheck the complete candidate rather than
only the corrected window.

## 5. Preserve repository state

Runtime cleanup is scoped to `.venv-audio`, `tools/audio_sound_runtime/`, audio-owned scratch/cache,
and package bytecode. It must not delete `.worktrees`, the main `.venv`, `.omx`, user output, or
protected draft artifacts.
