# 动画时序精修参考流程

## Contact Sheets

For each review item, build two contact sheets:

1. Coarse sheet:
   - range: section start to section end
   - step: 1s
   - purpose: identify slide state changes, page turns, and neighboring animations
2. Fine sheet:
   - range: original animation time minus 1s to plus 2s
   - step: 0.1s, or exact frame step if frame notation is given
   - purpose: identify first_visible, stable_frame, release, and next_animation_start

Crop sheets are diagnostic only. Draft overlays should remain full-screen unless a real local effect requires otherwise.

## Scope Evidence Decision

Use the review comment as exact `source_text`, not as a complete scope specification. Pair it with the screenshot ROI and sampled frame events:

| Evidence result | Scope decision | Completion rule |
| --- | --- | --- |
| page turn, continuous title/layout, and first board item behave as one visual beat | `dependency_group` | all `required_elements` are stable for at least two samples before hold/release; no `forbidden_future_elements` appear |
| the screenshot or referenced text bounds one independent object | `roi_target` | only the positive in-canvas `screenshot_roi` target advances; surrounding elements retain source timing |

Each evidence object uses `confidence=high|medium|low`. Sample at least two frame events; each event records a nonempty `event` or `name` plus finite `time` or `source_time`. A dependency group has at least two distinct required elements, and every event names its `element` so the events cover the complete `required_elements` set.

Do not ask the user to rewrite the review wording. If the evidence cannot distinguish the two outcomes, set `confidence=low` and write at least two distinct `preview_receipts`, one for each scope, with distinct `preview_id` values. Every preview receipt has `status=pass` and one nonempty `preview_path`, `artifact_path`, or `segment_id` locator.

## Decision Table

| Situation | Preferred edit |
| --- | --- |
| same board content, target motion should be preserved | full-screen video segment, muted, speed/shift as needed |
| same board content before and after the local animation, and motion/scale must match source | whole-section advance or full-screen video advance |
| same board content, final state matters more than motion | full-screen 1920x1080 state overlay |
| moving live segment would reveal later text too early | full-screen state overlay |
| page turn should happen earlier but later text should keep original timing | full-screen blank/new-page state until original text entrance |
| target stable state collides with original entrance | hold overlay until stable_frame + buffer |
| next animation starts soon after target stabilizes | release before next_animation_start - 0.1s |
| stable_frame and next_animation_start overlap | mark low confidence and request confirmation |
| local crop would need scale or slide-position matching | avoid crop; prefer full-screen mode |

## Overlay Window Calculation

Use:

```text
in_point = requested_new_in
release = stable_frame + buffer
buffer = 0.2s to 0.3s
if next_animation_start exists:
    release = min(release, next_animation_start - 0.1s)
duration = release - in_point
```

Reject or review if:

```text
duration <= 0
release < stable_frame
next_animation_start - stable_frame < 0.15s
```

The `0.1s` gap is the preferred safety margin, not a validity requirement. End-exclusive overlays may use `release == next_animation_start`; reject only `release > next_animation_start`.

## Whole-Segment Advance Checks

Before advancing a whole segment:

- compare frames before the requested new in point and at the source segment start
- confirm no future text/label/arrow appears too early
- confirm audio should not be duplicated; mute overlay video
- keep source video underneath for editability

Use whole-section advance instead of local object crop when:

- the whole board state is effectively identical except for the target animation
- the target object moves/reveals as part of the full recording and would be hard to crop cleanly
- the user complained that a prior crop changed size, position, or alignment
- neighboring board content provides visual continuity that a crop would break

Reject whole-section advance when:

- it exposes future written content or labels before their intended explanation
- it hides a different animation that should remain at the original time
- it creates duplicate movement under the overlay that cannot be covered through the stable-frame release rule
- it would require moving speech unintentionally

## State Overlay Checks

Before using a full-screen state frame:

- confirm the state frame is 1920x1080
- keep `scale=1`, `transform=0`
- confirm the state does not hide a simultaneous source animation that should remain visible
- release only after the original animation has stabilized, not exactly at the original anchor time

## Draft Validation Checklist

- Source video remains on its own visible track.
- Full-screen video overlays have volume 0.
- Full-screen image overlays are 1920x1080, scale 1, transform 0.
- No local crop path such as `remaining_animation_assets` appears unless intentionally documented.
- Each executed item has one `修改` marker.
- Review-only uncertainty uses `校对` and is not reported as executed.
- Root `draft_content.json` and main `Timelines/<id>/draft_content.json` agree after save.
- Each animation timing item has evidence for edit mode, overlay or shifted segment id, `target_scope`, `completion_policy`, `target_anchor_state`, `confidence`, `scope_basis`, `required_elements`, `forbidden_future_elements`, `completion_receipt`, first_visible, stable_frame, release, and next_animation_start when applicable.
- `scope_basis` preserves exact `source_text`, the `screenshot_roi` key, and at least two valid sampled `frame_events`; the source wording is not treated as sufficient scope evidence by itself.
- A `dependency_group` has at least two distinct required elements and its frame-event `element` values cover all of them.
- `completion_receipt` passes with at least two stable samples, covers every required element, and reports no forbidden future element present.
- `animation_evidence_problems(evidence)` returns no problem codes.
- Animation and pointer events are exported as visual context for post-delete pause fitting, so audio pauses near these events are not shortened blindly.
- Whole-section advances have evidence that source/moved frames align at the canvas level and do not introduce future-content leakage.
- Collision checks include the original source entrance underneath the overlay and the next independent animation after release.

## Scope Examples

### ROI Target Example

Review comment:

```text
11:58，绿框中的动画，提前到11:52
```

The screenshot and text bound only the green-box object. Neighboring board content keeps its original timing, so this is `roi_target`, not a full-screen dependency group:

```json
{
  "target_scope": "roi_target",
  "completion_policy": "target_only",
  "target_anchor_state": "stable_complete",
  "confidence": "high",
  "scope_basis": {
    "source_text": "11:58，绿框中的动画，提前到11:52",
    "screenshot_roi": {
      "x": 238,
      "y": 164,
      "width": 812,
      "height": 448,
      "canvas_width": 1920,
      "canvas_height": 1080
    },
    "frame_events": [
      {"source_time": 718.4, "name": "green_box_first_visible"},
      {"time": 718.5, "event": "green_box_stable"}
    ]
  },
  "required_elements": ["green_box"],
  "forbidden_future_elements": ["next_label"],
  "first_visible": 718.4,
  "stable_frame": 718.5,
  "release": 718.8,
  "next_animation_start": 719.2,
  "completion_receipt": {
    "status": "pass",
    "stable_sample_count": 2,
    "required_elements_present": ["green_box"],
    "forbidden_future_elements_present": []
  }
}
```

Fine-frame reading and decision:

- 11:58.4: orange target is still entering
- 11:58.5: target is stable
- no unrelated next animation immediately at 11:58.5
- use full-screen state overlay from 11:52
- hold it until 11:58.8, not 11:58.5
- reason: avoid releasing exactly on the original entrance boundary

Validation checks 11:58.4 to 11:58.9 at frame or 0.1s granularity, confirms the source underneath is stable at release, and confirms the hold covers no next independent animation.

### Dependency Group Example

Review comment:

```text
03:06，翻页的动画，提前到02:53
```

Fine-frame reading shows that the page turn, continuous title/layout, and first board item are one visual beat. Holding only the first page-turn frame would expose a half-built page, so the evidence uses `dependency_group` and waits for the complete stable state:

```json
{
  "target_scope": "dependency_group",
  "completion_policy": "group_complete",
  "target_anchor_state": "stable_complete",
  "confidence": "high",
  "scope_basis": {
    "source_text": "03:06，翻页的动画，提前到02:53",
    "screenshot_roi": {
      "x": 0,
      "y": 0,
      "width": 1920,
      "height": 1080,
      "canvas_width": 1920,
      "canvas_height": 1080
    },
    "frame_events": [
      {
        "time": 185.8,
        "event": "page_turn_first_visible",
        "element": "page_turn"
      },
      {
        "time": 186.1,
        "event": "continuous_title_layout_visible",
        "element": "continuous_title_layout"
      },
      {
        "time": 186.4,
        "event": "first_board_item_stable",
        "element": "first_board_item"
      }
    ]
  },
  "required_elements": [
    "page_turn",
    "continuous_title_layout",
    "first_board_item"
  ],
  "forbidden_future_elements": ["second_board_item"],
  "first_visible": 185.8,
  "stable_frame": 186.4,
  "release": 186.7,
  "next_animation_start": 187.0,
  "completion_receipt": {
    "status": "pass",
    "stable_sample_count": 3,
    "required_elements_present": [
      "page_turn",
      "continuous_title_layout",
      "first_board_item"
    ],
    "forbidden_future_elements_present": []
  }
}
```

The completion receipt is valid only when all three required elements are stable in at least two sampled frames and the second board item is still absent. A half-built page fails even if the page-turn motion itself has ended.
