---
name: core
description: Core JyProject operations including saving and exporting.
metadata:
  tags: core, save, export, save_project
---

# Core Operations

All operations are performed through the `JyProject` instance.

## 创建项目 (Project Creation)

在初始化 `JyProject` 时，请务必根据主视频素材的比例设置分辨率。**默认值为横屏 (1920x1080)**。

```python
# 默认：横屏 (16:9)
project = JyProject("Horizontal_Project") 

# 竖屏 (9:16)：必须在初始时指定，否则会有黑边
project = JyProject("Portrait_Project", width=1080, height=1920)
```


## Saving

You **MUST** call `project.save()` at the end of your script. 
This operation not only saves the JSON changes but also triggers a refresh in the Jianying UI (if applicable) or ensures the filesystem is synced.

```python
# Save changes and refresh draft state
project.save()
```

## Template-Based Production (Mass Creation)

For heavy-duty automation scenarios (e.g., creating 100 personalized ads from 1 template), follow the **clone-first** strategy:

1. Copy the source draft folder to a new draft name outside JianYing while the draft is closed.
2. Open the copied draft with `JyProject("Target_Customer_A", overwrite=False)`.
3. Use saved-draft patch commands (`set-text`, `replace`-style scripts, `import-srt`, `add-*`) instead of editing the shared template.

不要使用不存在的 template constructor API；它不是当前 wrapper 契约的一部分。可复用片段用 `save-template` / `apply-template`，官方文字模板用 `add-text-template`。

## Automated Exporting

You can trigger a headless export (using `uiautomation`) without manual clicking:

```bash
# Using the CLI tool
python <SKILL_ROOT>/scripts/auto_exporter.py "ProjectName" "custom_output.mp4" --res 1080 --fps 60
```

## Constraints

- **Draft Recognition**: The wrapper automatically handles `DraftFolder` structure. Do not manually manipulate `draft_content.json` unless you know exactly what you are doing.
- **Exporting Requirements**: Auto-exporting only works on **Windows** with **Jianying v5.9 or lower**. It relies on `uiautomation` to interact with the UI.
- **UI Refresh**: After the script runs, if Jianying is open, the user may need to exit and re-enter the draft to see changes.

## Quick Edit Execution Template (Standard)

For generic requests like "来个剪辑", follow `docs/agent-playbook.md` as the canonical execution sequence.

For revision-driven requests such as "修改意见", "按意见调整", "删除/前移/调序/静帧/过渡", default to:

- add review-marker text tracks with `project.add_review_markers(...)`
- export a matching markdown manifest with `project.export_review_markers_manifest(...)`
- keep the project editable; do not collapse editable audio into a single baked master track unless the user explicitly asks for that

## Acceptance Checks (Standard)

Use the mandatory Acceptance Checklist in `docs/agent-playbook.md`. Keep this rule focused on lifecycle constraints: correct resolution at project creation, `project.save()` at the end, and no manual JSON edits unless the wrapper/patch surface cannot express the operation.
