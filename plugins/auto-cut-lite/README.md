# Auto-Cut 精简通用版

这是一个通用的 Codex 插件包。它将审阅文档转换为可二次编辑的剪映工程，保留源视频、分离音频、替换素材、切点和逐条审阅标记。

在 Windows x64 目标电脑上解压完整 ZIP，然后在解压出的 `auto-cut-lite` 目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy-to-codex.ps1
```

脚本会校验包内容，安装独立 Python 运行环境，把插件注册到当前用户的 Codex 个人市场，并写出 `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite\deployment-report.json`。更新旧版本时会保留可恢复备份；安装完成后请新建 Codex 对话再使用。

插件不包含剪映软件、飞书登录状态、用户 token、ASR 密钥、用户收藏素材或任何本机项目绑定。首次使用时，操作者应在自己的电脑上配置飞书用户身份：

```powershell
lark-cli config default-as user
lark-cli config strict-mode user
lark-cli docs +fetch --as user --doc <document-url> --json
```

部署脚本只会设置严格用户身份，不会代替用户登录或伪造授权。目标机仍需安装 64 位 Python 3.10-3.12、Codex CLI、剪映专业版、FFmpeg/FFprobe 和 `lark-cli`；ASR 服务凭据也必须由操作者在本机配置。缺少这些本机配置时，插件会显示 `deployment_status=installed` 与 `readiness=pending_user_configuration`，不会误报为全部就绪。

运行代码位于 `runtime/`，依赖安装在插件自己的 `.runtime-venv` 中。通用版不携带任何预置学科指向物库；图片、箭头、手势和其他视觉素材按每个项目单独提供。
