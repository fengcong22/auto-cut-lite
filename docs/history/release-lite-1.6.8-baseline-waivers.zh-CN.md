# Auto-Cut Lite 1.6.8 基线豁免与路径扫描说明

## 适用范围

本文仅用于 Auto-Cut Lite `1.6.8` 候选包发布复核。豁免不代表对应的完整源码发布或离线依赖伴随包已经通过，也不得用于替代目标机安装验证、真实飞书/Taskboard 联合验收或用户发布授权。

Lite ZIP 由 `scripts/release/build_lite_plugin.py` 的显式 allowlist 构建。构建器生成 `PACKAGE-MANIFEST.json`，并由离线验包器和部署器校验包内文件的路径、大小及 SHA-256。下列三项不属于该 ZIP 的安装运行闭包。

## 基线豁免

### 音频迁移历史 Git blob 不可达

- 基线失败：`docs/audio-sound/source-migration-manifest.json` 引用的历史 Git blob `a1055faa0c6b09ab37600098750a5954ab272e2a` 在当前独立 Lite 仓库提交历史中不可达。
- 豁免理由：该对象用于源码迁移 provenance 测试，不是 Lite ZIP 文件，也不被目标机运行时读取。
- 边界：只豁免该历史对象的仓库可达性；不豁免 Lite ZIP 内 `audio_sound/**` 文件的包清单、哈希或运行时完整性校验。

### FFmpeg build receipt 缺失

- 基线失败：源码树缺少 `scripts/release/ffmpeg_assets/build/build-receipt.json`。
- 豁免理由：`scripts/release/ffmpeg_assets/**` 是完整离线依赖伴随包的构建输入，并被通用源码发布策略显式排除；Lite ZIP 不打包或读取该回执。
- 边界：只豁免 Lite ZIP 候选复核；如发布完整离线 FFmpeg 伴随包，仍须补齐并验证该回执。

### FFmpeg companion manifest 缺失

- 基线失败：源码树缺少 `scripts/release/ffmpeg_assets/manifest.json`。
- 豁免理由：该文件属于完整离线依赖伴随包，不属于 Lite ZIP 显式运行时 allowlist，也不参与 Lite ZIP 安装闭包。
- 边界：只豁免 Lite ZIP 候选复核；如发布或声称包含离线 FFmpeg 伴随包，仍须补齐清单并通过对应测试。

## `lite_package.py` 路径扫描误报

`scripts/utils/lite_package.py` 中的 `\\resources\\local\\` 与 `\\resources\\audioalg\\` 是用于从剪映草稿素材路径中提取包内相对资源位置的匹配标记，不是 UNC 主机路径、构建机绝对路径或固定部署目录。解析结果仍必须落在当前草稿的 `Resources/local` 或 `Resources/audioAlg` 下，并通过目录包含关系、文件存在性及 reparse-point 检查。

发布扫描只对白名单中的“文件路径 + 两个精确字面量”认定为已审计误报；其他 UNC、盘符绝对路径和机器绑定仍保持 fail-closed，并由负向测试覆盖。
