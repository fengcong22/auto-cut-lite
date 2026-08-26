# Auto-Cut Lite Skill Catalog

All skills in this package run under
[the Lite execution contract](../../auto-cut-lite/references/lite-execution-contract.md).
That contract overrides any focused capability name.

| Skill | Lite role |
| --- | --- |
| `auto-cut` | Natural-language and mixed-request router |
| `auto-cut-lite` | Canonical compact editable workflow |
| `auto-cut-revision-draft` | Review-ledger compilation and editable draft execution |
| `auto-cut-final-acceptance` | Lite behavior, saved-draft, and package validation |
| `auto-cut-review-audio-precision` | ASR-aligned spoken review edits |
| `auto-cut-audio-restoration` | Standalone spoken-audio restoration |
| `auto-cut-audio-peak-target` | Final waveform peak targeting |
| `auto-cut-music-library-bgm` | JianYing music-library BGM |
| `auto-cut-basic-oral-video` | Basic oral-video style, subject to Lite visual limits |
| `auto-cut-editable-ad-revision` | Ad/oral revision, subject to Lite visual limits |
| `auto-cut-favorite-text-assets` | Favorite text asset lookup |
| `auto-cut-draft-retention` | Project-family draft retention |
| `auto-cut-animation-timing-revision` | Animation and picture-timing labels only |
| `auto-cut-pointer-targeting` | Supplied pointer default-geometry insertion; cleanup labels only |
| `auto-cut-local-image-overlay-revision` | Supplied image default-geometry insertion |
| `auto-cut-text-safezone-animation-revision` | Text/safe-zone/animation labels only |
| `auto-cut-profile-onboarding` | Explicit profile administration only; never an edit gate |

Natural-language requests route through `auto-cut`. Directly naming a focused skill does not
enable full-workflow position, scale, animation, cleanup, profile, or opened-editor behavior.
