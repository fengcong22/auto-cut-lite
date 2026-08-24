---
name: auto-cut-text-safezone-animation-revision
description: 当用户要修订剪映可编辑草稿里的红框或安全框文本位置、花字入场动画、花字出场/结束/消失动画、贴纸跟随花字位置，或说“位置还是不对，分析整个工程原因”“其他保持不变”时使用。适用于需要同时校验根 draft_content.json、Timelines/main_timeline_id/draft_content.json 和保存后 helper 覆写结果的场景。
---

# 剪映文本安全区与花字动画修订

## Packaged Runtime

Before executing this skill from the distributed plugin, read the [portable runtime contract](../auto-cut-lite/references/portable-runtime.md). Use the installed isolated runtime and `workflow_mode=lite`; do not fall back to a source checkout or another Auto-Cut installation.


这个 skill 用来处理一类容易反复出错、但又必须保持可编辑的剪映修订：

- 红框或安全框范围内的花字、逐字字幕、贴纸位置修订
- 花字 `anim_in` / `anim_out` 语义判定与单侧删除
- “脚本说改了，但打开工程位置还是不对”的保存后偏移排查
- 用户明确要求“其他保持不变”的局部修订

这是 `auto-cut-editable-ad-revision` 的聚焦版补充 skill。遇到这类文本、贴纸、动画问题时，优先调用这个 skill，再按需要组合其它 repo skill。

## 术语先判定

先把用户口径翻译成剪映字段，再动工程：

- `入场动画` / `开场动画` / `出现动画` -> `anim_in`
- `出场动画` / `结束动画` / `消失动画` / `退场动画` -> `anim_out`

硬规则：

- 用户说 `出场动画`，默认按 `anim_out` 处理，不要擅自理解成 `anim_in`
- 用户只说 `动画` 且上下文不够明确时，先用一句话复述你的理解，再开始修改
- 用户说 `其他保持不变` 时，只能动被点名的那一侧动画；另一侧必须保留
- 如果前一版已经误改了语义，下一版要显式纠正并 bump 新草稿版本，不能覆盖旧版

## 红框安全区修订流程

1. 从最新已验收的可编辑草稿开新版本。
2. 先确定本次受影响的轨道：通常是 `Flower*`、`Subtitles`、`PromoSticker` 或用户点名的贴纸轨。
3. 不只改草稿根目录；如果存在 `Timelines/main_timeline_id/draft_content.json`，必须同步 patch 和校验这份主时间线内容。
4. 如果共享 helper 会在保存后再次改花字字体、位置或模板，先同步 helper 的全局常量，再跑 helper，避免保存后被拉回旧默认值。
5. 位置修订完成后，验的是“保存后的真实 draft”，不是内存里的 cue plan。

## 动画修订流程

先改生成入口或维护脚本里的花字 cue 定义，再验保存结果。

当用户要删某一侧动画时：

- 删 `入场动画`：清空 `anim_in`，保留 `anim_out`
- 删 `出场动画`：清空 `anim_out`，保留 `anim_in`
- 不要把“删一侧动画”做成“两侧都删”

保存后必须检查花字段挂接的 `materials.material_animations`：

- 删 `anim_in` 时，`type == "in"` 必须为 `0`
- 删 `anim_out` 时，`type == "out"` 必须为 `0`
- 未被请求删除的那一侧动画，要继续保留

不要只看 `FLOWER_PLAN` 或脚本 diff 就宣称完成。

## 根因排查清单

用户说“位置还是不对”时，优先按这份顺序排查：

1. 是否只改了草稿根目录，漏掉了 `Timelines/<main_timeline_id>/draft_content.json`
2. 是否有共享 helper 在保存后再次覆写花字位置、字体或模板
3. 花字和字幕用的是比例坐标，贴纸可能是像素坐标；不要把两者当同一套 Y 值
4. 草稿是否已经变成运行态编码或不适合明文直接 patch；如果是，就回到受维护的生成脚本重建，不要盲改不透明内容
5. 用户要求的是全工程全局修订，还是只改截图那一帧；默认按全工程验，不按单帧通过

## “其他保持不变”约束

用户说 `其他保持不变` 时，默认锁住这些内容：

- 花字大小、颜色、字体、模板变体
- 文本位置以外的已验收布局
- 字幕内容与时序
- 贴纸时序与素材
- BGM、SFX、音量策略
- 主视频切口与分段结构

不要因为版本号变化而随机换花字样式；已验收 cue 的模板变体和 seed 要保持稳定。

## 显示漂移与坐标换算

当保存后或打开剪映后的真实画面与仓库自渲染预览不一致时，按显示漂移处理，不要继续只看预览图微调。

- 对 1920x1080 画布上的图片、贴纸、指针和文字位置修正，截图像素差要按半画布单位换算：`transform.x += dx / 960`，`transform.y += -dy / 540`。
- 除非具体 wrapper 明确说明传入的是像素或全画布比例，不要用 `1920`、`1080` 作为保存后 transform 的换算除数。
- 如果剪映已经把 `draft_content.json` 改成运行态编码或不透明内容，不要直接 patch；回到维护中的生成脚本、request JSON 或可读备份重建新版本。
- 如果独立可编辑贴纸/手指层在剪映打开后仍然偏移，而已验证渲染结果正确，改用修订草稿和指针技能里的局部 display-safe 烘焙窗口规则，不要反复做预览坐标猜测。

## 验收门槛

完成前至少确认这些点：

- 仍是可编辑 draft，没有退化成 `Final Video` / `Final Audio`
- `Main Video` 没塌成单段
- 被触达的文字或贴纸轨道数量与结构合理保留
- 根层和主时间线层都通过位置校验
- 花字动画通过保存后校验，删的是用户指定的那一侧
- 报告里明确写出“本次把哪一侧动画删了，哪一侧保持不变”

## 报告格式

返回结果时要明确说：

- 本次把 `出场动画` 理解为 `anim_out`，或把 `入场动画` 理解为 `anim_in`
- 改了哪些轨道和字段
- 哪些内容保持不变
- 新草稿路径
- 保留了哪 3 个版本、删除了哪些旧版本
