# Audio Tuning Guide

The maintained presets are JSON files under `presets/audio_sound/`:

- `fast`: light preview-oriented processing.
- `safe`: conservative spoken-word cleanup.
- `review`: cleanup plus review evidence.
- `repair-soft`: restrained repair.
- `repair`: stronger developer-selected repair.
- `final`: balanced final delivery.
- `voice-isolate`: same-file noise-window workflow.

Inspect a preset without editing it:

```powershell
python scripts/audio/audio_cleanup.py describe-preset safe
```

Override only the parameter requested by the user:

```powershell
python scripts/audio/audio_cleanup.py clean "D:\media\voice.wav" --preset safe --target-lufs -20
```

`--skip-spectramini` and `--skip-deepfilternet` disable execution, not only reporting. Exact repair
windows should be as narrow as evidence permits; do not use broad de-click, denoise, or long
crossfades to hide residual speech.

The main Auto-Cut environment must not receive Torch, Torchaudio, DeepFilterNet, librosa, or the
audio runtime's NumPy pin. Change those versions only in `requirements-audio.lock`.
