# Auto-Cut 精简通用版

这是一个通用的 Codex 插件包。它保留当前 Auto-Cut 的 17 个通用技能入口，将审阅文档转换为可二次编辑的剪映工程，并支持动画时序、音频、BGM、花字、局部图片、指向物配置和最终验收。

在 Windows x64 目标电脑上解压完整 ZIP，然后在解压出的 `auto-cut-lite` 目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy-to-codex.ps1
```

脚本会校验包内容，分别安装主 Python 与音频 Python 运行环境，把插件注册到显示名为 `Auto-Cut Lite` 的独立市场，并写出 `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite\deployment-report.json`。若机器上存在旧的 `auto-cut-lite@personal`，部署器会迁移安装来源并保留可恢复备份；安装完成后请新建 Codex 对话再使用。

插件不包含剪映软件、飞书登录状态、用户 token、ASR 密钥、用户收藏素材或任何本机项目绑定。首次使用时，操作者应在自己的电脑上配置飞书用户身份：

```powershell
lark-cli config default-as user
lark-cli config strict-mode user
lark-cli docs +fetch --as user --doc <document-url> --json
```

部署脚本只会设置严格用户身份，不会代替用户登录或伪造授权。目标机完整安装需要 64 位 Python 3.11、剪映专业版、FFmpeg/FFprobe 和 `lark-cli`；ASR 服务凭据也必须由操作者在本机配置。Codex CLI 必须可执行；若 Codex Desktop 自带入口受 Windows 限制但目标机装有 Node.js，部署器会通过固定版本的官方 `@openai/codex` npm 包执行插件注册。缺少这些本机配置时，插件会显示 `deployment_status=installed` 与 `readiness=pending_user_configuration`，不会误报为全部就绪。

运行代码位于 `runtime/`。主依赖安装在插件根目录的 `.runtime-venv`，音频修复依赖安装在独立的 `runtime\.venv-audio`，默认都会部署；只有显式传入 `-SkipAudio` 才会跳过音频环境并把部署标记为未完全就绪。ZIP 体积较小是因为固定版本依赖在目标电脑安装，完整部署后的实际占用会显著增加。通用版不携带任何预置学科、私有指向物库或本机项目绑定；用户可以在目标电脑通过 `auto-cut-profile-onboarding` 为自己的项目建立配置，该数据保存在 `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite\pointer-profiles.local`，不随插件升级被覆盖。
