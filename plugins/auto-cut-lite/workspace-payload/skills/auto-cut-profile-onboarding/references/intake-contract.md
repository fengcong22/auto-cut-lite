# Subject Pointer Intake Contract

Use this contract after the user explicitly names the stage and subject in a create/update/supplement command or in a same-task onboarding handoff. Never infer or inspect titles, filenames, OCR, ASR, course content, media, or layout to choose the identity. Ask only for the missing stage or subject and retain any identity value already supplied. When file evidence is needed, resolve both bundled guide files to absolute paths and display both images together in the same message.

For registry compatibility, store one confirmed display-name `aliases` entry and one `detection_evidence` row whose source is `explicit_user_command`. These fields are audit metadata and 不得用于自动选择档案. 不得从标题、文件名、OCR、ASR、课程内容或版式选择学科.

Use 增量 intake 合并 when the exact profile already contains valid evidence. A corrected pointer or scale screenshot must reuse its stable `asset_id` or `reference_id`. Put explicitly obsolete aliases in `remove_aliases`. List and supply only missing items; 不要求重新提交已满足项.

## Incremental Response Shape Matrix

Select exactly one response shape from this closed matrix. `request_classes` is exhaustive for that response: do not add a class from another row. The `_file_evidence` variants apply only when the named `missing_items` or `problems` entries require the user to upload or replace a PNG/screenshot; otherwise use the corresponding `_non_file` variant.

```json
{
  "subject_identity": {
    "request_classes": ["missing_explicit_identity_fields"],
    "file_evidence_requested": false,
    "guide_images": []
  },
  "missing": {
    "request_classes": [
      "pointer_png",
      "scale_references",
      "preview_after_registration",
      "optional_motion_reference"
    ],
    "file_evidence_requested": true,
    "guide_images": [
      "pointer-material-reference.png",
      "scale-reference-screenshot.png"
    ]
  },
  "incomplete_file_evidence": {
    "request_classes": ["profile_check.missing_items"],
    "file_evidence_requested": true,
    "guide_images": [
      "pointer-material-reference.png",
      "scale-reference-screenshot.png"
    ]
  },
  "incomplete_non_file": {
    "request_classes": ["profile_check.missing_items"],
    "file_evidence_requested": false,
    "guide_images": []
  },
  "stale_file_evidence": {
    "request_classes": ["profile_check.problems"],
    "file_evidence_requested": true,
    "guide_images": [
      "pointer-material-reference.png",
      "scale-reference-screenshot.png"
    ]
  },
  "stale_non_file": {
    "request_classes": ["profile_check.problems"],
    "file_evidence_requested": false,
    "guide_images": []
  },
  "scale_confirmation": {
    "request_classes": ["confirmed_scale_references"],
    "file_evidence_requested": false,
    "guide_images": []
  },
  "preview_approval": {
    "request_classes": ["approved_previews"],
    "file_evidence_requested": false,
    "guide_images": []
  },
  "project_binding": {
    "request_classes": ["project_binding_confirmation"],
    "file_evidence_requested": false,
    "guide_images": []
  }
}
```

`subject_identity` asks only for the explicit stage/subject fields that are null and asks for no files. `missing` may use the complete first-time packet below. `incomplete` copies only the classes named by `profile_check.missing_items`; `stale` copies only the missing or hash-changed classes named by `profile_check.problems`. `scale_confirmation` asks only whether the already supplied scale references are confirmed. `preview_approval` asks only for approval of the shown final preview. `project_binding` asks only the applicable bind/rebind question from `handoff-contract.md` and requests no evidence.

The guide pair is all-or-nothing. Resolve both files to absolute paths and display both images in the same message only for a row with `file_evidence_requested=true`; a non-file row displays neither guide.

For `missing` only, send this complete first-time packet shape. `确认预览` describes the later preview step; it does not ask the user to approve a preview that has not been generated yet.

```text
用户手动指定的学段与学科：<stage_name + subject_name；仅记录用户明确输入；aliases 使用该中文显示名；detection_evidence.source 固定为 explicit_user_command>
Profile 状态：missing
缺少项：<完整首次建档材料包>
学科指向物 PNG：请提供透明背景、完整边缘的 PNG；一种素材一个文件；保留原始分辨率；不要提供截图裁切出的素材；逐个注明素材角色（如 hand / arrow / circle / magnifier / highlight / custom）和归一化热点坐标 [x, y]。hand hotspot = fingertip；arrow hotspot = arrow head。
大小比例参考截图：请提供 2至3张完整 16:9 画面，指向物和目标必须同时可见，并分别标明课节、时间点、画面类型；确认后记录 full_frame: true；不要只截局部预览区域。
确认预览：素材和比例登记后，我会生成或展示最终预览；请明确确认，只有确认结果写入 approved_previews 且 approved: true 后档案才可进入 ready。
可选移动参考：如指向物需要跟随讲解移动，可补充一段同课节短视频或连续截图，并标明起止时间和目标顺序。
绝对路径展示参考图：
- 学科指向物 PNG 提供样式：<pointer-material-reference.png 的绝对路径，并在消息中展示图片>
- 大小比例参考截图样式：<scale-reference-screenshot.png 的绝对路径，并在消息中展示图片>
```

Do not add a neutral fallback offer, another subject's example, or a preset scale. Do not offer binding while evidence is incomplete. After the exact profile is ready, follow `handoff-contract.md` and ask for explicit binding confirmation before binding the current project and continuing the original edit.
