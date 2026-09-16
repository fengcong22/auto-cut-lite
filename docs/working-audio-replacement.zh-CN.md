# Lite 修音工作音源修复

候选插件版本：`1.6.10+codex.20260916122000`，内嵌核心 `1.7.0`。
仅修改独立 Auto-Cut Lite 源码；不修改 Taskboard，不热补丁已安装运行时。

## 音源与时间轴

每个 `source_pairs` 配对独立选择音源。`replace_original` 选择修音文件；
`video_original` 选择原音。旧单视频入口未声明 `audio_mode` 且提供
`replacement_audio` 时兼容推导为替换模式，显式 `video_original` 始终优先。

原音、视频、修音路径与 SHA-256 分别留在 source material ledger、工作音源绑定和
本地素材库中。`source_audio` 不再被切分计划覆盖，`replacement_audio` 不再被清空。
ASR、逻辑边界、反向验证候选及 A1/A2 全部来自同一个工作源。
反向候选仅是诊断材料；不导入草稿。

`split_gap` 的 A1 是正常窗口，A2 是各逻辑删除窗口。两轨均正常音量、源时间与目标时间
相同且互补。不同审阅项的相邻窗口不合并。视频内置声音为零；原音保留在素材库，
不创建有声全长 Replacement Audio。时长、画面位置、原文标签及可编辑结构不变。

## 同步策略与限制

不能由旧 `duration_tolerance_seconds` 推断语音同步。真实替换文件必须通过
`preserve_timeline_envelope_v1`：

- 解码为 8 kHz 单声道，仅用于诊断；交付素材不重采样、不拉伸。
- 工作音频必须覆盖完整视频时间轴；缺少覆盖不补白、不回退原音。
- 允许最多 50 ms 的容器尾部余量；诊断 WAV 按每个视频时长裁去尾部，防止多视频
  反向候选产生累积偏移。交付片段仍引用完整工作文件。
- 10 ms 包络步长，连续最多 10 s 窗口（短音频尽量分为三段），搜索 ±500 ms 偏移。
- 每窗相关系数至少 0.85；相对离最佳位置超过 40 ms 的候选优势至少 0.05。
- 绝对偏移不超过 40 ms；各窗口偏移跨度不超过 40 ms。
- 静音、恒定音、强修复导致无法唯一关联、错误配对、偏移或局部漂移均拒绝交付，
  不将“测不出”当成“同步”。同字节文件可以用哈希相等作为精确身份凭据。

这是保守的能量包络检测，并非逐字口型证明。它可能拒绝正确但大幅重构的修音、
长静音或缺少明显能量变化的素材；小于阈值或小于分析窗口的局部变形可能无法区分。
目前没有自动校时或放宽检测的输入开关。失败时需提供真正保留原时间轴且可验证的修音文件。
合成真实媒体测试验证 WAV/MP4 解码、素材本地化、草稿和 ZIP；未调用真实付费 ASR、
未重跑生产任务、未在剪映 UI 中试听，不能将离线测试称为现场语音验收。

## 保存与缓存验收

根草稿和活动时间线均校验工作材料引用、路径与哈希、音量、轨道静音、播放速度、
源/目标范围、淡入淡出、音量关键帧、A1/A2 覆盖及原音保留。任何参考或额外音频轨有声、原音混入、A1/A2 静音、
错误素材路径或材料 ID 都不通过。

工作文件内容 SHA-256 参与材料/ASR/候选身份，同名文件内容变化不可复用旧缓存。
写稿前重新核对 source ledger 哈希，并将旧单视频的期望哈希带入本地化后的保存验收，
防止 ASR 完成后、素材复制期间同名文件变化而交付另一份音频。
runner 版本变为 `auto-cut-lite-review-document-run-v10-working-audio`，旧阶段回执失效。
package receipt 保持 v2，兼容扩展 `working_audio_writer_version=lite-working-audio-split-gap-v1`。
旧 ZIP 回执不能复用；保留现有拒绝覆盖交付物规则，升级后应使用新的交付名称或输出目录。
`result.json` 仍为 v1，现有 `review-document-run` 参数和 ZIP 布局不变。

## 测试状态隔离

pytest 每例使用临时 readiness；相关 unittest 测试类提供相同保护。
测试夹具显式传递临时路径并初始化 pending 状态，守卫拒绝测试目录以外的 readiness
读写。`_fake_asr` 仍返回 `test-adapter-v1`；不通过换版本号绕过污染。
测试前后只读核对正式 readiness 哈希，凭据、正式状态和自动执行开关均不修改。

## 源码验证记录

先补失败回归，再实现修复。写稿器首轮 6 项失败、管线首轮 4 项失败、readiness
首轮 2 项失败均在隔离状态下记录。覆盖替换有/无切分、混合多视频、旧单视频、
同名内容变化、阶段升级失效、污染拦截和合成真实媒体草稿/ZIP。

全库审查后结果为 1361 passed / 3 skipped / 3 failed；剩余失败是既有源码环境缺件，
本次没有修改相应测试或伪造凭据：

- `test_every_normalized_source_blob_is_reachable_in_git_history`：迁移清单记录的
  `a1055faa0c6b09ab37600098750a5954ab272e2a` 对象在当前 Git 对象库不可用。
- `test_ffmpeg_build_receipt_has_complete_toolchain_hashes`：缺少
  `scripts/release/ffmpeg_assets/build/build-receipt.json`。
- `test_ffmpeg_asset_manifest_self_hash_and_file_rows_are_complete`：缺少
  `scripts/release/ffmpeg_assets/manifest.json`。

这些是全量核心离线依赖包的历史/构建输入；本次 Lite 插件包使用自己的显式 runtime
allowlist、PACKAGE-MANIFEST 和离线解包校验，不包含或伪造上述缺失 FFmpeg 构建材料。
后续补跑和包证据保存在 `tmp/`，对外交付应连同 ZIP SHA-256 和独立解包验证回执。
