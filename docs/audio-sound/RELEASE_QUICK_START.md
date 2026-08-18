# Integrated Audio Quick Start

Run commands from the extracted Auto-Cut package root.

```powershell
python scripts/audio/audio_cleanup.py doctor
python scripts/audio/audio_cleanup.py list-presets
python scripts/audio/audio_skill_workflow.py run "D:\media\voice.wav" --skip-deepfilternet
```

Optional isolated runtime setup:

```powershell
scripts\audio\setup.cmd
scripts\audio\doctor.cmd
```

Setup targets `.venv-audio` and reads `requirements-audio.lock`; it never installs the heavy stack
into the main Auto-Cut interpreter. Respiro auto-download is intentionally disabled until verified
asset hashes are checked in. Configure already verified assets using `.env.example` as the template.

For explicit deletion rather than restoration:

```powershell
python scripts/audio/remove_spoken_segments.py run "D:\media\lesson.mp4" --cut "0.62,1.82" --seam-pause-ms 80
```

See `README.md`, `reference-sop.md`, and `skills/auto-cut-audio-restoration/` for current behavior.
The source release's original wording is retained only in `README.upstream.md` for provenance.
