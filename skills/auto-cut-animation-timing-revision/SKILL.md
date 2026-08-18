---
name: auto-cut-animation-timing-revision
description: "Use when revising PPT/JianYing course animation timing in editable drafts: advancing or speeding up animations, page turns, line/box/fill reveals, whole-section timing shifts, full-screen state overlays, and frame-level boundary checks. Trigger for review comments like “动画提前到...”, “速度加快”, “翻页动画提前”, “整段提前”, “出点不精确”, “提前后撞上原动画入场”, or “别盖住下一条动画”."
---

# 动画时序精修技能

## Overview

Use this repository-local skill for course/PPT recording review comments that ask to change visual animation timing while preserving an editable JianYing draft. It covers the whole family of animation timing edits: speed-up, full-segment advance, full-screen state overlay, page-turn advance, and anti-collision boundary reading.

Always combine with:

- `auto-cut-revision-draft` for editable draft delivery, source preservation, and one marker per review item.
- `auto-cut-text-safezone-animation-revision` when the edit touches actual JianYing `anim_in` / `anim_out` fields, text/sticker placement, or saved-draft drift.
- `auto-cut-final-acceptance` before delivery to verify timing evidence, overlay structure, collision checks, and source review item coverage.

## Scope

Use this skill for:

- `02:51-02:53，普鲁士的动画，速度加快`
- `03:06，翻页的动画，提前到02:53`
- `09:02，直线的动画，提前到08:52`
- `09:14，铺色的动画，提前到09:03`
- `11:58，绿框中的动画，提前到11:52`
- comments where a local animation is early/late but the pre/post board content is identical enough to shift a whole section
- any follow-up like “提前后又和原动画入场撞上”

Do not use local crop patches as the default solution for PPT animation timing. Local crops are fragile because scale, slide position, anti-aliasing, and surrounding board content can drift.

## Workflow

1. Normalize review comments into timing tasks:
   - original anchor time or range
   - requested new in point
   - target animation object
   - animation class: speed-up, page turn, line, box, fill, label, highlight, or unknown
   - exact `source_text`, screenshot-bounded `screenshot_roi`, and sampled `frame_events`
   - `target_scope`, `completion_policy`, and `target_anchor_state`
   - Do not ask the user to rewrite an ambiguous review comment. Resolve scope from the combined evidence.
2. Build a coarse visual timeline:
   - Extract 1-second full-frame contact sheets around each section.
   - Identify whether board content before/after is the same.
   - Identify neighboring independent animations.
   - Identify whether the whole source segment can move earlier without revealing future labels or hiding current content.
3. Choose the edit mode:
   - **Full-screen video advance**: use when the same board content can move earlier and the original motion should remain visible.
   - **Whole-section advance**: use when the front/back board states match and moving the entire section preserves the intended timing better than cropping a local animation.
   - **Full-screen state overlay**: use when moving the live segment would reveal later text or labels too early.
   - **Page-turn state hold**: use when the page should appear earlier but later page text should keep original timing.
   - **Review-only marker**: use only when fine-frame reading proves the requested state is already visible at the requested time.
4. Run fine boundary reading for risky in/out points:
   - Extract `original_time - 1.0s` to `original_time + 2.0s`.
   - Use 0.1s steps by default.
   - If the user gives frame-like notation such as `11:58:15`, convert it using fps and verify with frames.
   - Crop for diagnosis only; write full-screen overlays into the draft unless a local effect is explicitly required.
5. Compute overlay windows:
   - In point = requested new in point.
   - Out point is not automatically the original anchor time.
   - For static state overlays, use the stable-frame release rule below.
   - For full-screen video advances, mute overlay video and keep source video underneath.
6. Write the editable draft:
   - Keep source video on its own visible track.
   - Put each timing edit on a separate track or clearly traceable segment.
   - Add one `修改` marker per executed item.
   - Write `review_items[*].evidence` for each timing item with edit mode, overlay/shifted segment ids, track name, scope evidence, completion receipt, first_visible, stable_frame, release, and next_animation_start when available.
   - Keep full-screen image overlays at 1920x1080, `scale=1`, `transform=0`.
   - Mute full-screen video overlays.
7. Validate the saved draft:
   - Inspect root `draft_content.json` and the main timeline content.
   - Confirm no `Final Video` / `Final Audio` replacement.
   - Confirm no unrelated old markers.
   - Confirm overlay assets are full-screen where expected.
   - Confirm boundary release does not collide with original entrance or cover the next independent animation.
   - Confirm whole-section advances do not duplicate speech, reveal future board content too early, or desync pointer/audio beats.
   - Run `animation_evidence_problems` for every executed animation item and reject any returned problem code.
   - Run the revision strict gate when the source came from a review document.
   - Run the final acceptance checklist before reporting the draft as complete.

## Scope and Completion Evidence

Review wording is not the scope decision. Record the exact source text, the screenshot ROI, and sampled frame events, then select one scope:

| Scope | Use when | Required policy |
| --- | --- | --- |
| `dependency_group` | A page turn and its continuous title/layout plus first board item form one visual dependency group. The group must be complete and stable before the hold begins. | `completion_policy=group_complete`, `target_anchor_state=stable_complete` |
| `roi_target` | Only the screenshot/text-bounded target should advance; neighboring layout or later board elements retain their original timing. | `completion_policy=target_only`, positive in-canvas `screenshot_roi` |

Every evidence object contains:

- `scope_basis` with nonempty `source_text`, a `screenshot_roi` key, and at least two sampled frame events
- every frame event has a nonempty `event` or `name` and a finite numeric `time` or `source_time`
- nonempty `required_elements` and an explicit `forbidden_future_elements` list
- numeric `first_visible`, `stable_frame`, and `release`; numeric `next_animation_start` when known
- `completion_receipt.status=pass`, `stable_sample_count>=2`, all required elements in `required_elements_present`, and an empty `forbidden_future_elements_present`
- `confidence` is required and must be `high`, `medium`, or `low`

For `dependency_group`, provide at least two distinct `required_elements`. Every sampled frame event also has a nonempty `element`, and the event set must cover every `required_elements` entry. A page-turn event alone cannot prove that its continuous title/layout and first board item are complete.

The accepted boundary order is:

```text
first_visible <= stable_frame <= release
if next_animation_start exists:
    release <= next_animation_start
```

An end-exclusive overlay may release exactly at `next_animation_start`. Only a release after that boundary is a collision.

Validate with the repository utility before accepting the item:

```python
from utils.animation_validation import animation_evidence_problems

problems = animation_evidence_problems(evidence)
```

When `confidence=low`, create at least two distinct preview receipts under `preview_receipts`: one `dependency_group` and one `roi_target`. Each receipt has a distinct `preview_id`, `status=pass`, and at least one nonempty traceable locator: `preview_path`, `artifact_path`, or `segment_id`. Keep both previews traceable so the user can compare them without rewriting the source comment.

## Edit Mode Rules

Prefer full-screen video advance when:

- the same board content surrounds the animation
- the motion itself should be preserved, just faster or earlier
- moving the segment does not reveal future annotations

Prefer whole-section advance when:

- the local animation belongs to a short continuous visual beat rather than an isolated object
- frames before the requested new in point and frames after the original animation share the same board content
- moving a full-screen source segment preserves scale, position, anti-aliasing, and the original motion better than a local crop
- audio can stay coherent because the advanced segment is muted as visual overlay, or the whole audio/video section is intentionally shifted with matching edit evidence

Prefer full-screen state overlay when:

- later text would be revealed too early by moving a full segment
- the review intent is “make the final state visible earlier”
- the source animation is a short entrance/reveal where motion is less important than state timing

Prefer page-turn state hold when:

- the page turn should happen earlier
- the next page’s internal text/label animations should remain at their original times

Avoid local crop overlays unless:

- a full-screen overlay would hide unrelated active content
- a real JianYing/PPT effect cannot be represented otherwise
- the crop size, target transform, and visual continuity have been validated with screenshot/frame comparison

## Stable-Frame Release Rule

Do not use the teacher's original animation time as the overlay out point by default.

Do not release exactly on the original entrance frame. A too-early release can expose the source animation's original entrance underneath and create a double entrance or jump.

Find:

- `first_visible`: first frame where the target begins appearing
- `stable_frame`: first frame where target position, opacity, and size match final state for at least 2-3 sampled frames
- `next_animation_start`: first frame after `stable_frame` where an unrelated object starts entering, moving, revealing, or scrolling

Calculate:

```text
release = stable_frame + safety_buffer
safety_buffer = 0.2s to 0.3s
if next_animation_start exists:
    release = min(release, next_animation_start - 0.1s)
```

If `release < stable_frame` or the stable frame overlaps the next independent animation, mark the item low confidence and explain the tradeoff.

## Collision Reading Rule

For every advanced animation, inspect the source layer underneath around the planned release window:

- if the source original entrance is still visible, extend the overlay until the source is stable
- if the next independent animation starts before the target stabilizes, choose the least harmful tradeoff and mark it low confidence
- if the new overlay covers another requested edit, split the timing task or change edit mode
- if a pointer or audio pause depends on the same visual beat, export that event as visual context for `auto-cut-review-audio-precision`

## Acceptance Rule

Animation timing requests are execution items unless fine-frame reading proves the requested state is already visible at the requested time. A marker saying "动画提前/推迟/加快" is not enough. The draft must contain one of:

- a full-screen video/image overlay segment with matching evidence
- a whole-section advanced segment with matching evidence
- a shifted or sped segment with matching evidence
- a documented local baked window for an effect that cannot remain editable
- a review-only decision with frame evidence proving no edit was needed

Before delivery, run:

```powershell
python scripts/jy_wrapper.py revision-validate --request-json "<request.json>" --doc-items-json "<doc_items.json>" --strict --json
```

## Time Notation

Interpret time carefully:

- `11:58` means 11 minutes 58 seconds as a rough event time.
- `11:58:15` may mean frame 15 at 11:58, not 11 hours. At 30fps this is about `11:58.5`.
- Always verify frame-like notation by extracted frames before changing the draft.

## Reporting

Report:

- draft name and path
- each timing item and chosen edit mode
- for boundary-sensitive items: first_visible, stable_frame, release, and next_animation_start
- marker track names
- validation result and low-confidence items
