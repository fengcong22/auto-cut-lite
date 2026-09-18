# Taskboard ZIP 生成目录声明协议

Lite 包的 `PACKAGE-MANIFEST.json` 包含：

```json
{
  "interface": {
    "zipOutput": {
      "relativeDirectory": "output"
    }
  }
}
```

这只是目录声明，不包含当前电脑的绝对路径，也不触发 Lite 创建目录。

## Taskboard 侧同步要求

1. 验证完整包后，读取工作区中的 `PACKAGE-MANIFEST.json`。
2. 校验 `relativeDirectory` 是非空安全相对路径。接受 `/` 或 `\` 分隔的目录及中文名称；
   拒绝盘符路径（包括 `C:output`）、UNC、设备路径、以 `/` 或 `\` 起始的路径、
   `.`、`..`、空路径段、控制字符、Windows 非法字符、尾部点/空格和 Windows 设备名称。
   解析结果必须仍在工作区内；现有链接或目录联接不能使实际路径逃逸工作区。
3. 解析为 `<workspacePath>\<relativeDirectory>`。“验证并读取”只显示这个将使用的路径，
   不创建、修改目录或配置。
4. 用户保存或启用包时重新验证并自动创建缺失目录；普通文件占位或不可创建时明确失败。
5. 每次受控运行生成并冻结一个精确的绝对 ZIP 文件路径，创建它的父目录，设置
   `CODEX_AUTOCUT_PACKAGE_ZIP_PATH`，并把同一路径传给 `--package-zip`。
   可以保留现有的 run 隔离子目录，避免不同运行互相覆盖。
6. `execution-input.json` 中的 `artifact_name`、草稿目录名、ZIP 内根目录名和冻结的 ZIP
   文件名继续按现有协议一致。Lite 遇到冲突直接失败，不生成另一个名字的文件。
7. 仅当 `result.json` 为 `pass` 且 result、相邻 `<ZIP>.receipt.json`、SHA-256、运行绑定
   均通过时，通过现有 `taskctl artifact report` 上报这个精确 ZIP。上报路径必须等于注入路径；
   不能通过扫描目录、选择最新 ZIP、标题匹配或文件名匹配获取其他产物。
8. NAS 上传由 Taskboard 的既有上传流程负责，Lite 不自行上传。

旧版清单无 `interface.zipOutput` 时，保留手工填写“ZIP 生成目录”；不得自动补成
`workspace\output`。声明存在但内容非法时应拒绝，不能退回猜测目录。

## Lite 已实现的边界

- 新包声明默认目录 `output`；构建器、离线验包器、部署预检和工作区安装器校验声明。
- `review-document-run` 在处理源素材前检查传入的路径与注入路径一致，以及父目录已存在。
- 已绑定路径的编译、打包、断点恢复和成功报告都保持同一个文件路径。
- 保留 `result.json` schema v1、相邻 package receipt schema v2、名称、哈希及运行绑定检查。
- 非 Taskboard 的独立交付保留现有命名方式；无注入路径时不会读取目录声明来猜测路径。

## 对接验收

| 场景 | 预期结果 | 验证归属 |
| --- | --- | --- |
| `output`、`output/课程 初稿`、`delivery\drafts` | 接受并解析在工作区内 | 两端 |
| `../output`、`C:output`、`C:\output`、UNC、`\output` 等 | 拒绝声明 | 两端 |
| “验证并读取”遇到目录不存在 | 只显示路径，磁盘不变 | Taskboard |
| 保存或启用包遇到目录不存在 | 验证后自动创建 | Taskboard |
| 无声明的旧包 | 手工配置，不猜测 `output` | Taskboard |
| `--package-zip` 与注入路径不同 | 处理素材前失败，不生成另一个 ZIP | Lite |
| 注入路径父目录未创建 | `package_directory_missing`，不另建目录 | Lite |
| 实际草稿名与冻结文件名不同 | 失败，不改名 | Lite |
| 正常完成 | 返回值、result、receipt 和上报文件为同一精确 ZIP | 两端 |
| 上报同一目录下另一个 ZIP，即使它较新 | 拒绝上报 | Taskboard |

Taskboard 侧的保存/启用、目录创建和上报接口测试由 Taskboard 仓库执行。Lite 仓库的测试
覆盖声明验证、路径不一致拒绝、目录缺失拒绝及成功产物与回执路径一致，不表示 Taskboard 已升级。
