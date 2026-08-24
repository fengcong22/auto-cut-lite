---
name: auto-cut-lite
description: Use for the generic compact Auto-Cut workflow when the result must remain editable in JianYing/CapCut and review-document edits must be traceable.
---

# Auto-Cut 精简通用版

这是通用精简版入口。用户说“精简版”“lite”“compact”或明确要求可编辑剪映草稿时使用本 skill，并将 `workflow_mode=lite` 写入每个请求。

## Runtime

插件根目录下的 `runtime/` 是实际运行代码。不要从源码仓库或另一台电脑复制运行状态。常用命令形式为：

```powershell
$autoCutLiteRoot = Join-Path $env:USERPROFILE 'plugins\auto-cut-lite'
$autoCutLitePython = Join-Path $autoCutLiteRoot '.runtime-venv\Scripts\python.exe'
& $autoCutLitePython (Join-Path $autoCutLiteRoot 'runtime\scripts\jy_wrapper.py') review-job-compile --snapshot-json <snapshot> --project-json <project> --output-dir <job-dir> --json
& $autoCutLitePython (Join-Path $autoCutLiteRoot 'runtime\scripts\jy_wrapper.py') revision-run --request-json <request> --doc-items-json <items> --workflow-mode lite --strict --package-zip <output.zip> --json
```

若 `.runtime-venv` 不存在或部署报告不是 `deployment_status=installed`，停止剪辑并让用户在完整插件包目录重新运行 `deploy-to-codex.ps1`。不要回退到源码仓库、全量版目录或系统 Python。

## Feishu/Lark identity

审阅文档必须使用当前操作者自己的飞书用户身份读取。首次使用时在目标电脑执行：

```powershell
lark-cli config default-as user
lark-cli config strict-mode user
lark-cli auth login --scope <document-read-scopes> --no-wait --json
lark-cli docs +fetch --as user --doc <document-url> --json
```

严格模式禁止应用或机器人身份回退。token、登录缓存和授权结果只保留在目标电脑，不得写入请求 JSON、草稿包或插件 ZIP。

## Review-document timebase

文档中的时间首先只作为搜索提示。每条审阅项必须保存 `source_role`、`source_time_range` 和 `timeline_time_range`：

- 主视频项使用全片时间线。
- 补录或替换视频项使用补录片段自己的局部时间线；不能把局部 `00:05` 直接当成全片 `00:05`。
- 补录段必须由文档中的替换声明、锚点或明确回到主视频的时间建立映射；没有唯一锚点时标记为 unresolved 并停止执行该项。
- 编译器会把补录局部时间转换成全片时间，同时保留原始局部区间作为审计证据。修改意见只能绑定转换后的 `timeline_time_range`。

这条规则适用于删除、替换、图片覆盖、字幕、花字和校对标记，避免跨文档重复出现补录时间误判。

## Editable lite contract

- 默认使用 `lite_cut_layout=split_gap`。
- V1 `Original Video` 和 A1 `Separated Source Audio` 保留非删除区间；V2 `Lite Cut Segments` 和 A2 `Lite Reused Audio` 保存被切出的源区间。
- 工程总时长不改变，根 draft 和 active timeline 都写入 `config.maintrack_adsorb=false`。
- 删除窗口必须有 ASR 字词边界和反向校验；文档时间不能直接成为最终切点。
- 每条源审阅项保留一条原文标记，标记起点与已解析的编辑时间对齐，默认显示 2 秒。
- 图片、视频、音频、字幕和本地覆盖素材都保留为可编辑素材；不把工程压平成最终视频。
- 动画时间意见在精简模式下保留为可追踪标记，不擅自改写原始动画；局部素材替换必须保存为独立可编辑片段。

## Generic visual handling

本通用包不读取任何预置学科、阶段或私有指向物库。需要手、箭头、圈选或其他视觉素材时，只使用请求中提供的本地素材或明确指定的公共素材，并把它保存到可编辑轨道；没有素材时保留校对标记，不猜测素材身份。

## Delivery gate

严格模式下先运行草稿验收，再运行精简版 ZIP 打包。ZIP 必须经过 CRC、路径安全、解压树哈希和素材引用检查；收据放在 ZIP 外部。没有通过验收不能报告“已交付”。
