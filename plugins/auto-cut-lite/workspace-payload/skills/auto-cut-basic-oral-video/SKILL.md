---
name: auto-cut-basic-oral-video
description: Apply the packaged generic basic oral-video style to editable JianYing drafts, including pacing, BGM matching, flower text, sound effects, safe-zone placement, animation semantics, font rules, and portrait color treatment.
---

# 基础口播视频

## Packaged Runtime

Before executing this skill from the distributed plugin, read the [portable runtime contract](../auto-cut-lite/references/portable-runtime.md). Use the installed isolated runtime and `workflow_mode=lite`; do not fall back to a source checkout or another Auto-Cut installation.

## Lite Review Override

For review-document work, first read the [Lite execution contract](../auto-cut-lite/references/lite-execution-contract.md). It overrides every position, scale, safe-zone, animation, sticker, and visual-validation rule below: review-requested text/flower animation or movement is label-only; supplied local visual assets use default geometry; pointer cleanup is label-only. The style rules below remain available only for a user-requested new oral-video build and must not be used to convert a Lite review label into a visual execution item.


Use this skill as a style guide for short oral-video ads that should feel brisk, benefit-dense, and clean rather than cinematic.

This is the plugin's default short oral-video style entrypoint. Its complete generic baseline is encoded below and does not depend on a source-machine draft.

When the work involves red-box safe-zone positioning, sticker/text placement, or flower-text animation changes, also apply the rules from `auto-cut-text-safezone-animation-revision`. In practice this means `基础口播视频` already includes that guardrail set; the separate skill remains the detailed reference.

## Style Snapshot

Treat the packaged `基础口播视频` baseline as this combination:

- oral video with dense but readable selling points
- fast segmented cuts, but not hyperactive montage
- funky cloud-music bed with modern retail/tech energy
- frequent light confirmation SFX on flower text, prices, and service points
- subtitles and flower text both anchored low, with overlap avoidance handled explicitly
- warmer source image corrected toward neutral-cool, then gently lifted brighter

## Rhythm

Keep the pacing close to the 通用基准 cut map:

- total spoken section around 30 to 40 seconds after rough-cut extraction
- shot units commonly around `0.5s` to `2.5s`
- use many short oral-video keep segments instead of long continuous talking-head sections
- reserve slightly longer holds for price, service guarantee, or final conversion lines

Editing heuristics:

- Cut on meaning units, not every breath.
- Use short burst segments for pain points and selling points.
- Let benefit clusters stack quickly, then give pricing and CTA slightly more hold time.
- Do not smooth everything into one continuous spoken clip; visible cut structure is part of the style.
- Do not keep adjacent repeated speech heads or tails after a cut. If two neighboring kept clips repeat the same spoken phrase or one cue starts with a carried-over fragment from the previous line, trim the overlap out of the later cue before saving.
- If transcript review shows one spoken line was accidentally kept twice in separated source windows, remove the duplicate keep window instead of masking it with subtitles or flower text.

## Voice Level

- default oral-voice peak target for this repository is `-3 dBFS`
- interpret requests like `人声音量调到 -3db` as the resulting spoken-audio peak target, not as directly setting a gain knob to `-3`
- when the source video already carries the intended voice, apply the peak-target gain directly to the segmented `Main Video` clips

## BGM

The v37 reference uses a cloud-music replacement with a funk/groove retail feel:

- target feel: `cloud funk`, `modern retail`, `light tech`, `shopping groove`
- avoid cinematic swells, heavy trap, emotional piano, or overly cute ukulele
- keep BGM as a separate editable music track
- BGM should support speech rhythm, not compete with it

Practical rules:

- Search and match BGM from the JianYing music library, not from arbitrary local music files, unless the user explicitly asks to override this source.
- If a requested BGM cannot be resolved from JianYing music-library metadata and its current preview URL, fail the build instead of silently substituting a local repo audio file.
- Prefer JianYing cloud music that feels bouncy, stylish, and slightly digital.
- Keep the track clean in the mids so speech remains forward.
- BGM loudness should sit clearly under voice; in the source build it is intentionally low and supportive.
- Use a short fade out near the tail instead of a dramatic ending.

If the user asks for “same BGM style” rather than the exact same track, search with themes such as:

- `funk`
- `groove`
- `shopping`
- `e-commerce`
- `tech`
- `corporate funk`

Selection rule:

- Match the feel in the JianYing music library first.
- Reuse the exact same track when it is resolvable and still appropriate.
- If the exact track is unavailable, choose the nearest groove/texture match from the JianYing music library and keep it as a separate editable BGM track. Do not replace it with a local fallback song that changes the style intent.

## Sound Effects

Use SFX as micro-confirmation, not as spectacle.

Search source rule:

- Search sound effects from the user's JianYing favorites/collections first.
- Do not default to arbitrary generic library results when a fitting favorite exists.
- Keep SFX editable as separate material references rather than baking them into a mixed track.

Placement pattern from the reference:

- nearly every key flower-text beat gets one short SFX
- pain-point words use light impact/pop accents
- feature points use clean UI ding/click accents
- pricing reveal uses a more obvious pop or sparkle accent

Mix rules:

- keep SFX on a separate editable track
- short one-shot accents only
- SFX peak target is much lower than voice and should never dominate the line
- use tiny fades to prevent click edges
- do not drive every SFX with one fixed global gain; measure each selected SFX asset's own peak and compute per-cue gain so weak UI/service cues are still audible
- service confirmation cues such as `service_ui` should sit a little more forward than generic light accents; if they become effectively inaudible at a global default target, raise only that cue family rather than boosting all SFX blindly
- if a cue waveform exists but still sounds absent, inspect the exact `source_start` slice first; do not keep boosting a near-silent tail slice. Move to an audible hit window or swap the asset, and fail generation if the chosen SFX slice peak is still effectively silent
- if the cue plan defines `sfx_key`, the generator must create real editable SFX segments; a placeholder `_add_sfx(...)` implementation that returns `0` is invalid
- after saving, validate that the draft contains a real `SFX` track with the intended editable segments; do not pass a draft that planned SFX but saved none
- do not rely on “the project opens” as evidence that SFX exists; inspect saved track counts and fail the build when `SFX` is missing or materially under-counted

Sound categories that fit this style:

- `impact pop`
- `snap`
- `cute impact`
- `ui ding`
- `digital click`
- `service pop`
- `price reveal pop`

Avoid:

- booms
- cinematic risers
- exaggerated whooshes on every word
- comedy SFX with too much personality

## Subtitles And Flower Text

This style is text-dense but still controlled.

### Font and tone

- use fixed flower-text font `优设标题黑`
- prioritize thick strokes, high legibility, and strong retail emphasis
- flower text can be louder than subtitles, but not messy

Hard rule:

- All flower text must use `优设标题黑`.
- Do not swap flower text to similar bold fonts.
- If a text template is loaded from favorites or local cache, force every flower-text text material inside that template back to `优设标题黑` after insertion.
- If a template shell keeps visually overriding the intended `优设标题黑` look, switch the affected cue to plain editable flower text instead of keeping the template shell.
- After saving, validate both the saved flower-text font resource id and the saved text material fields such as `font_name` / `font_title`; do not accept a draft just because the input plan requested `优设标题黑`.
- For this baseline, if a saved `FlowerText` material still carries template `effectStyle` that changes the visible font personality away from `优设标题黑`, treat that as a build failure.
- The fixed `优设标题黑` requirement has priority over favorite/template flower shells. If `effectStyle` makes the visible font look different, remove that shell and keep plain editable flower text with the planned color, outline, timing, position, and animation.

### Subtitle rules

- subtitles stay lower center
- default subtitle position is the lower-center safe zone
- for 608x1088 vertical oral-video drafts, start subtitles around material font size `7.5` to `8.0`, segment scale `1.10` to `1.20`, dark stroke width at least `0.10`, and `clip.transform.y` around `-0.88`
- treat screenshot review where subtitles read like fine print as a build failure; increase material size first, then segment scale, and validate the saved draft values before reporting
- when subtitles overlap flower text, push the subtitle lower instead of moving the flower text away from its emphasis zone
- subtitle styling stays simpler than flower text: white text, strong dark outline, readable in one glance
- for this baseline, do not solve flower/subtitle overlap by deleting or carving away most subtitle windows; spoken subtitles must remain continuously reviewable across the whole kept oral-video timeline
- if a subtitle segment intersects any flower-text window, trim only the overlapping part and keep the non-overlapping part when it remains long enough to read
- never leave short subtitle fragments at the start or end of a flower-text window; these appear as stray extra captions in JianYing
- never delete a readable non-overlapping subtitle span just because another part of the same source subtitle overlaps flower text
- when one spoken sentence contains a late keyword flower such as a price reveal, keep the pre-keyword subtitle on screen until the keyword actually starts; do not cut the subtitle at an earlier generic flower boundary
- never split a trailing single Chinese character such as `值` into its own subtitle segment when it semantically belongs to the previous phrase
- if a subtitle split override would create a one-character tail segment for the final CTA or value line, rewrite that split before saving
- validate the saved subtitle segmentation for final conversion lines so the user does not see a stray one-character subtitle with the wrong timing at the tail
- after saving, fail the build if the subtitle track collapses to an implausibly low segment count for a multi-line oral video; one surviving subtitle segment is evidence of a broken subtitle generation path, not a valid overlap fix

### Flower-text rules

- flower text also lives in the lower-center torso/table safe zone
- if the user provides a red-frame or screenshot safe zone, all flower text, readable subtitles, and visually attached stickers must be placed inside that requested safe zone unless the user explicitly excludes one class of material
- keep flower text centered horizontally unless a specific layout reason exists
- prevent same-screen overlap by shifting later flower segments in time or by using separate non-overlapping flower tracks
- use visible segmentation; each selling point should remain independently editable
- main selling-point flower text in the torso zone must read as a large headline, not a small tag; size it closer to the `辅导没方向？` reference card than to a compact subtitle label
- for 608x1088 vertical oral-video drafts, start torso flower text around material font size `15.0` to `16.8`, segment scale `1.20` to `1.30`, stroke width at least `0.11`, and `clip.transform.y` around `-0.37`
- validate screen-fit from the saved draft, not only from the cue plan: inspect each text material's `size` and stroke fields plus the segment `clip.scale` and `clip.transform.y`
- if an opened JianYing draft has converted `draft_content.json` into runtime-encoded content instead of readable JSON, rebuild from the generator rather than applying an unsafe direct JSON patch
- 将“匹配画面大小”的花字理解为：在当前镜头里，花字要读成胸口主标题，而不是胸前一小块字幕贴片
- 对竖屏口播半身镜头，短花字（约 `4` 到 `8` 个汉字）默认应先按“大标题”处理：单行通常要吃到约 `28%` 到 `42%` 的画面宽度；两行块通常要有约 `10%` 到 `18%` 的画面高度
- 如果截图里花字看起来只占胸前很小一块、接近普通字幕大小、或者视觉上还没有领带结到胸口安全区宽度明显大，就判定为过小，必须继续放大
- 放大优先顺序是：先增大 `font_size`，再增大 `scale_x` / `scale_y`，最后才考虑微调位置；不要优先靠上移来伪装“变大了”
- 如果模板花字的默认壳子会把文字压成小标签，且放大请求没有真实生效，就切回 plain editable flower text，不要保留一个看起来还是小号模板字的结果
- if one flower text line cannot fit comfortably in the torso-safe zone, split it into two lines before insertion instead of shrinking it into one cramped line
- treat two-line wrapping as the default fix for long flower copy; do not preserve a single overlong line by reducing size, squeezing width, or changing unrelated style choices
- if a flower text already contains a manual line break but the two lines are still visually unbalanced, rewrap it into a better two-line split instead of preserving the old bad break
- rewrap by semantic phrases; never split protected Chinese phrases such as `同步课`, `语数英`, `语数培优课`, or `全科学习` across two lines
- when the user asks to change only one issue, preserve every unrelated flower-text cue's style, template choice, timing, and position exactly as-is
- when the user asks to align later flower text to the first flower-text position, treat the first accepted flower cue's `transform_y` as the canonical torso-safe-zone baseline and normalize the specified later cues back to that exact baseline
- do not keep legacy per-cue vertical offsets such as `-0.12`, `-0.08`, `-0.04`, or `-0.02` on later flower cues unless the user explicitly requests those exact deviations
- version bumps must not be used as style-randomization input for accepted flower-text cues; if a draft name changes from `vNN` to `vNN+1`, keep the accepted style seed or explicitly lock variants so unrelated flower styles do not change
- search flower-text style templates from the user's JianYing favorites/collections first
- prefer reusable text templates over rebuilding every flower style from scratch when a matching favorite template exists
- after applying a template, keep only the template's style shell; text content, timing, and final font must still obey this skill
- if a template flower's requested style or size change does not visibly take effect because the local favorite-template pool or template internals override the plan, switch only that requested cue to plain editable flower text; do not keep reporting a changed template candidate that JianYing still renders like the old style
- when a template-style cue has been accepted and is not part of the current request, lock its template variant instead of letting version-seed rotation change it indirectly
- do not let the whole draft collapse to only a few repeated flower queries when the cue plan covers multiple message types; pain point, benefit, service, and price cues must be diversified across the local favorite flower pool unless the user explicitly requests one unified style
- treat `flower_query` resolution as incomplete until the saved draft confirms the intended mode actually landed; for fixed-font baseline drafts, this means no saved `FlowerText` segment may keep a favorite/template `effectStyle`
- for price-reveal and other timing-sensitive words, prefer plain editable flower text over templates when templates shorten or mistime the reveal window
- timing-sensitive flower text must start near the spoken keyword and hold until the spoken phrase ends; do not let the flower appear early or disappear before the line finishes
- after saving, validate each expected flower cue for both start time and duration; if any cue collapses to a flash-like segment or exists only as a non-visible shell, fail the build
- if a sticker is visually attached to a flower text beat, drive the sticker from the same cue timing; never hard-code an independent time window for the sticker
- old-price, direct-discount, or savings-delta selling points such as `18626元`, `直降15000元`, `立减`, `省下` must be modeled as explicit flower-text cues, not left as subtitle-only coverage inside a longer sentence
- for these price-delta cues, validate both the source plan and the saved draft text materials so a future revision cannot accidentally drop the cue while changing unrelated timing, style, or version numbers
- validate flower text wrapping before saving so no planned torso-zone flower text stays as a single overlong line when it should have been shown as two lines
- for safe-zone revisions, patch and validate every active `draft_content.json`, including `Timelines/<main_timeline_id>/draft_content.json` when it exists; never claim success after changing only the root draft file
- if a save-time helper enforces flower fonts, templates, or positions, sync its global position constants first so the final helper pass does not snap accepted flower text back to an older location
- when the user says `其他保持不变`, preserve unrelated flower style, size, timing, template variant, subtitles, stickers, BGM, SFX, and main-video cuts

### Visual language by message type

Map message type to color mood:

- pain points: red/pink family with darker outline
- functional benefits: yellow/gold family
- AI/tech/service points: cyan/light-blue family
- old price/reference price: beige-gold family
- final hand price/current price: strong red family

Use bubble/background support selectively:

- pain points and price points can use bubble/background emphasis
- benefit points can stay cleaner with flower/effect emphasis
- service points can mix bubble plus friendly accent color

Animation rules:

- prefer short, playful, readable text intros
- intro/outro should feel reactive, not flashy
- common acceptable motions: shake in, click in, character reveal, blink, gentle zoom
- keep text animation windows short so oral pacing stays tight
- interpret `入场动画` / `开场动画` / `出现动画` as `anim_in`
- interpret `出场动画` / `结束动画` / `消失动画` / `退场动画` as `anim_out`; do not treat `出场动画` as `anim_in`
- when deleting one side of a flower-text animation, delete only that side and keep the other side unchanged
- after saving, validate `materials.material_animations` on flower segments: deleting `anim_in` means no flower animation item with `type == "in"` remains; deleting `anim_out` means no flower animation item with `type == "out"` remains

## Color Style

The v37 color comes from the v36 native adjustment base plus cloud-funk BGM replacement. Preserve this grading intent:

- remove yellow cast first
- then gently lift brightness
- stay natural on skin
- keep the room clean and airy

Operationally:

- move temperature slightly cooler
- apply a gentle RGB/luma curve lift rather than a heavy filter stack
- target is neutral-clean portrait commerce, not teal-orange, not film look, not beauty-filter plastic

Visual cues from the reference:

- white shirt remains clean without turning blue
- skin is brighter and cleaner, not over-whitened
- wood tabletop keeps some warmth
- overall room feels brighter and more modern than the original warm cast

## Editable Draft Requirements

When applying this style in this repo:

- keep `BGM` and `SFX` as separate editable tracks
- when the source video already contains the intended oral voice, do not add a second audible `Source Audio` timeline track that duplicates the same speech; preserve one clear voice path on the timeline instead of showing duplicate human-voice tracks
- in that case, keep the segmented `Main Video` clips audible and apply any requested voice peak target directly to the embedded video-audio segments instead of rebuilding a second human-voice track
- keep visible cut points on the main timeline
- keep subtitle and flower-text segments independently editable
- do not flatten music or SFX into final mixed audio
- do not replace the whole timeline with one baked full-length segment
- never write fake or placeholder media paths such as `cloud_music_<id>.mp3` into a saved draft; if a JianYing music-library BGM cannot be resolved, fail the build rather than swapping in an unrelated repo-local audio file
- validate every saved video and audio material path exists on disk before reporting completion, especially the BGM track, so JianYing does not show a media-missing dialog on open
- after bumping a draft version, actually run the generator and verify the exact `DRAFT_NAME` folder exists in the JianYing drafts root before reporting completion
- never report a new draft version as available based only on code changes; the saved folder, `draft_content.json`, and `draft_meta_info.json` must all exist under the exact new draft name

## Flower Timing Guardrails

When a flower-text cue uses a target-timeline override, keep every timing field in the same timeline basis before calling JianYing helper functions.

Hard rules:

- Do not mix source-video time and final target-timeline time inside one flower-text item.
- If `target_start_override` / `target_end_override` is used, pass those target values into all helper fields used for segment duration, including `src_start`, `src_end`, and `hold_to`.
- Never pass source-time `hold_to` with a target-time start; helpers may clamp the result to a tiny fallback duration, making the flower text appear missing or incorrectly rendered.
- After saving the draft, validate each `FlowerText` / `FlowerTemplate` segment against the intended plan by start time and duration. Fail the build if any expected flower segment collapses to a short flash, especially around `0.3s`.

Example:

- expected flower window `16.74s` to `21.28s`
- helper item timing must use `src_start=16.74`, `src_end=21.28`, `hold_to=21.28`
- do not use the original source clip time such as `src_start=24.18`, `hold_to=24.15` for that same target-window flower

## Chinese Text Encoding Guardrails

Chinese copy must stay valid Chinese from source plan to saved JianYing draft.

Hard rules:

- Treat mojibake such as `澶╀笓灞`, `姪杈呭`, `锟`, or `�` as a build failure, not a cosmetic issue.
- Do not write GBK/UTF-8 mis-decoded strings into cue plans, flower text, subtitles, template replacements, or draft text materials.
- Keep repo files and generated JSON in UTF-8.
- When inspecting JianYing runtime drafts on Windows, avoid passing Chinese paths through shell snippets that can convert them to `??`; enumerate project directories or use literal paths from PowerShell.
- Before saving and after saving, validate all planned text and all saved text materials for suspicious mojibake markers.

## How To Use This Skill

When the user asks for this style:

1. Start from the latest editable draft baseline.
2. Preserve segmented oral-video cuts.
3. Search flower-text templates from JianYing favorites first, then apply matching templates while forcing all flower-text fonts to `优设标题黑`.
4. Search SFX from JianYing favorites first and place light one-shot accents on a separate SFX track.
5. Search BGM from the JianYing music library and keep or replace it with a cloud-funk/groove equivalent.
6. Place subtitles and flower text in the lower-center safe zone with explicit overlap handling.
7. Apply the cooler-then-brighter portrait grade.
8. Validate that every flower-text cue appears at the intended target time with a non-collapsed duration.
9. Validate that torso-zone flower text is visually large enough for the current frame; fail the build if it still reads like a small subtitle tag instead of a headline.
10. Validate that no subtitle segment overlaps a flower-text window, no short leftover subtitle fragment exists, readable non-overlapping subtitle spans are preserved, late-keyword flowers do not cut subtitles early, and cue-bound stickers stay aligned with their flower text.
11. Validate that no source text or saved text material contains mojibake.
12. Validate that the draft remains editable and that cut structure is still visible.

## Required Post-Save Validation Command

After generating or revising a draft under this skill, run:

```powershell
python scripts/jy_wrapper.py validate-oral-video-draft `
  --name "<draft_name>" `
  --style basic-oral-video `
  --json
```

Do not report completion until this command returns `ok: true`.

This validator is the enforced gate for the baseline oral-video workflow. It must reject drafts that, after save:

- lost editable `SFX`, `BGM`, subtitle, or flower-text structure
- collapsed `Main Video` into one segment
- reintroduced preview-style `Final Video` / `Final Audio` tracks
- lost the locked flower font
- still carry forbidden flower template `effectStyle`
- contain missing local media paths
- contain mojibake in saved text materials

## Baseline Stability

The rules in this skill are the portable source of truth. Do not look for an unbundled reference draft or source-machine temporary script. If a future version intentionally changes this style, version the rule change explicitly rather than silently deriving behavior from a local project.
