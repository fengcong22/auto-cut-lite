---
name: auto-cut-audio-restoration
description: Use when spoken-word audio explicitly needs restoration for breathing, mouth noise, background noise, pause residue, dropouts, clipping, spectral cleanup, loudness, standalone WAV/MP3 delivery, or integrated audio runtime diagnostics.
---

# Auto-Cut Audio Restoration

这是 `auto-cut` 路由器下的音频修复子 skill。目标不是“跑一遍脚本就算完成”，而是用仓库内置音频引擎稳定交付已验证的中文口播成品。

`auto-cut` 是仓库唯一稳定总入口。本 skill 负责显式请求的音频修复、独立音频成品和运行时诊断；审核文档驱动的删词与必留词保护仍由 `auto-cut-review-audio-precision` 负责，剪映时间线上的峰值语义仍由 `auto-cut-audio-peak-target` 负责。

先读 `references/workflow.md`。  
需要具体命令时读 `references/commands.md`。  
需要最终验收标准时读 `references/acceptance.md`。

如果任务涉及删除口播、删词、删句、边界残留、接缝杂音、卡顿或突然续接，必须额外阅读 `references/spoken-segment-boundaries.md`；该流程优先于普通降噪节点精修，不得只靠加长交叉淡化处理。

## 默认入口

当前已迁入的是显式调用的独立修复与诊断能力。设计文档中的自动 `clean` / `repair_needed` / `review_needed` 质量门尚未接入 review job；没有保存的评估报告时，不得声称普通口播作业已自动运行该质量门。

当用户明确要独立修复音频，或上游流程已经提供可归因的修复证据并显式选择本 skill 时，目标是最终可直接使用的严格成品，而不是普通预清理。独立成品模式按以下规则选择：

- 用户明确要求严格独立成品时默认走 `reference-legacy`
- 只有用户明确提出其他目的时才切换：
  - 要更自然、更克制：`reference-style`
  - 要平衡交付：`final`
  - 要同文件噪声窗口隔离：`voice-isolate`
  - 要审查标记和复核报告：`review`

除非用户明确要求快速预览，否则不要把 `scripts/audio/audio_cleanup.py clean` 当成最终交付入口。最终交付优先用 `scripts/audio/audio_skill_workflow.py run ...`。

## 删词与接缝的特殊入口

当独立音频/视频任务要物理删除一句话、口头禅或片段时，使用 `scripts/audio/remove_spoken_segments.py`，不是 `scripts/audio/audio_cleanup.py clean`。先按 `references/spoken-segment-boundaries.md` 定位真实局部时间和语义边界，再运行删词脚本。若删除来自飞书/Lark 审核文档或要写入可编辑剪映草稿，改由 `auto-cut-review-audio-precision` 主导；除非有明确修复请求或证据，不要额外运行全轨修复。

- 默认使用 `--boundary-search-ms 80` 与 `--crossfade-ms 12`。
- 若用户反馈“后面卡一下”或“下一句/一个字突然接上”，改用 `--seam-pause-ms 60–100`，中文讲解默认从 `80` 开始。
- 不要用更长的交叉淡化掩盖残留或语义续接；它会混叠不同音素。
- 对视频必须验证音频与 MP4 时长差小于一帧；语音安全接缝会同步冻结上一帧。

## 严格执行顺序

1. 先确认运行环境：
   - 运行 `doctor`
   - 明确 `ffmpeg`、`ffprobe`、`DeepFilterNet`、Respiro-en 是否可用
2. 再跑主流程：
   - 默认严格成品标准用 `reference-legacy`
   - 主链必须保持明确：`Respiro-en -> SpectraMini 风格清理 -> DeepFilterNet`
3. 主流程完成后必须做频谱和响度复核：
   - 输出整体频谱图
   - 必要时输出问题节点的局部频谱图
   - 在聊天窗口中展示频谱图
4. 如果还有残留呼吸、气口、细丝、口水音：
   - 不要整段重做
   - 先局部巡检，再用精确时间窗做节点精修
5. 节点精修后必须再次导出 MP3、再次复测响度，并给出最终交付文件路径

## 铁律

- 只保留一个仓库总路由入口：`auto-cut`；本 skill 是聚焦的音频修复子技能
- 对最终成品，优先使用仓库 workflow，不用临时拼 shell 管道代替
- 在这个仓库里，“处理音频”默认就是做最终版，不默认保留轻处理思路
- 频谱图是过程证据；严格清理时，聊天窗口里要出现频谱图
- 最终用户只看 `output/修音成品/`；不要让用户从 `audio_preprocess/` 的中间文件里挑最终版
- 中文原文件名必须保留，最终命名固定为 `修音版_<原音频文件名>.wav|mp3`；同名已存在时递增为 `_01`、`_02`
- 中间 wav/mp3 默认清理掉；只有排查阶段问题时才加 `--keep-intermediate-audio` 保留
- 用户给了具体时间点时，只修确认过的窄窗口，避免吞字
- 涉及删词时，时间码只是候选范围；必须检查报告中的 `requested_cuts`、`cuts`、`crossfade_frames`、`seam_pause_frames` 和局部频谱/波形
- 用户没给时间点但说“不干净”时，先做局部频谱/能量巡检，再精修
- 不能口头宣称“Respiro-en 已调用”；必须以 `doctor` 或主流程报告中的实际状态为准
- 如果实际走了 fallback，而不是真正的 Respiro-en，必须明确说出来
- 节点精修后，整体响度不应被带偏；要复核和基底成品是否基本一致
- DeepFilterNet 防吞字恢复只允许修复夹在连续语音中的短缺口，不能恢复开头静音区或停顿区里的孤立杂音

## 对外说明口径

当用户问“这个 skill 现在到底会怎么做”时，按下面这个口径回答：

1. 先检查本地运行环境和 Respiro-en 是否真实可用
2. 再按仓库认可顺序处理：
   - Respiro-en 检测
   - SpectraMini 风格呼吸/口水音处理
   - DeepFilterNet 降噪
   - 仓库后处理清理停顿、细丝和残留
3. 然后做频谱图和响度复核
4. 如果仍有少量具体节点不干净，再做精确时间窗补修
5. 最终交付到 `output/修音成品/`，文件名为 `修音版_原名.wav/mp3`，如果已有同名则自动加 `_01/_02`

## 本 skill 不该做的事

- 不要默认走过于保守、只做基础清理的旧路径
- 不要把“脚本执行成功”当成“成品达标”
- 不要为了清理呼吸音，误吞后面起字
- 不要把“删词脚本成功执行”当成“删词接缝自然”；残留、突然归零或突然接字都必须继续修
- 不要把用户已经认可的响度重新推高
- 不要输出只有英文短名、看不出来源的文件名
- 不要擅自理解成“先随便出一版试试”；默认就是直接朝最终版做
