---
name: auto-cut-audio-peak-target
description: Align spoken-audio loudness in JianYing draft workflows by targeting the kept timeline's waveform peak near a requested dBFS value instead of blindly setting the segment volume knob to the same numeric dB value. Use when the user says audio should be "raised to -3dB", "peak at -3", "waveform peak should sit near -3", or when there is ambiguity between gain-knob dB and final signal peak level.
---

# JianYing Audio Peak Target

## Packaged Runtime

Before executing this skill from the distributed plugin, read the [portable runtime contract](../auto-cut-lite/references/portable-runtime.md). Use the installed isolated runtime and `workflow_mode=lite`; do not fall back to a source checkout or another Auto-Cut installation.


When the user gives a target like `-3dB`, first determine whether they mean:

- the clip gain control value shown in JianYing, or
- the resulting waveform peak / meter peak near `-3 dBFS`

For this repository, default to **resulting kept-audio peak near the requested dBFS target** when the user talks about waveform position, meter peak, loudness result, or shows a screenshot of the peak scale.
For this repository's oral-video workflow, when the user gives no different target, default the spoken-audio peak target to `-3 dBFS`.

## Rule

Do not assume `volume=-3dB` means multiplying audio by `10^(-3/20)`.

That interpretation reduces level by 5 dB and is wrong when the user's intent is:

- "raise the audio so the peak sits around -3"
- "make the waveform peak hit -3"
- "让峰值到 -3 的位置"

Instead:

1. Build the list of kept source windows that survive the final editable timeline.
2. Measure the current audio peak across those kept windows only.
3. Compute gain delta:
   - `gain_db = target_peak_dbfs - current_peak_dbfs`
4. Convert to linear multiplier:
   - `multiplier = 10^(gain_db / 20)`
5. Apply that multiplier uniformly to all kept audio segments in the draft.

## Important distinction

- `target_peak_dbfs = -3` means the resulting signal peak should land near `-3 dBFS`.
- It does **not** mean "reduce the clip by 5 dB".

If the current kept-audio peak is already `-3.2 dBFS`, only a small boost is needed.
If the current kept-audio peak is `-14 dBFS`, a much larger boost is needed.

## JianYing draft workflow

In this repository's editable draft workflows:

1. Keep the editable segmented audio track.
2. Measure peak from the kept source windows, not from deleted windows.
3. Apply one consistent multiplier to the audio segments that remain in the timeline.
4. Preserve the cut structure; do not flatten the project to normalize audio.

## ffmpeg measurement pattern

Use `ffmpeg` with `atrim + concat + volumedetect` over the kept windows, then read:

- `max_volume: ... dB`

Treat that as `current_peak_dbfs`.

## Reporting

When returning the result, report:

- requested target peak
- measured current peak before adjustment
- applied multiplier
- whether the result reflects peak targeting or knob-value targeting

## Default behavior for this repo

When the user is ambiguous, but references waveform peak position or meter screenshots, interpret the request as:

- **target resulting peak near the requested dBFS value**

Only use direct knob-value semantics when the user explicitly says they want the JianYing gain control itself set to a specific dB value regardless of resulting peak.
