# Auto-Cut 精简通用版

这是一个通用的 Codex 插件包。它将审阅文档转换为可二次编辑的剪映工程，保留源视频、分离音频、替换素材、切点和逐条审阅标记。

插件不包含剪映软件、飞书登录状态、用户 token、ASR 密钥、用户收藏素材或任何本机项目绑定。首次使用时，操作者应在自己的电脑上配置飞书用户身份：

```powershell
lark-cli config default-as user
lark-cli config strict-mode user
lark-cli docs +fetch --as user --doc <document-url> --json
```

运行代码位于 `runtime/`。Windows 目标机应安装 64 位 Python 3.10-3.12、剪映专业版和 FFmpeg；ASR 服务凭据也必须由操作者在本机配置。
