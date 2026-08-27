# Auto-Cut Lite 部署后的 Codex 操作

部署器显示成功后，用 Codex 打开输出中的 `workspace_root`，并在该目录新建线程。不要继续使用
安装前已经打开的旧线程，因为旧线程不会自动刷新工作区技能。

在新线程发送：

```text
请读取当前 Auto-Cut Lite 工作区的 AGENTS.md，并检查
%LOCALAPPDATA%\Auto-Cut\auto-cut-lite\deployment-report.json。
请先确认所有 auto-cut-* 技能都是 scope=repo、标签为 Auto-cut-lite，并且没有 Auto-Cut 的
Personal 重复技能。然后逐项帮助我完成并验证：剪映专业版及草稿目录、FFmpeg/FFprobe、
lark-cli 当前用户登录与 default-as user/strict-mode user、火山 ASR 本机凭据和一次真实字词
对齐测试。不要让我发送任何 token；需要登录、授权或填写本机密钥时，请一步一步提示我。
最后重新检查 readiness，并明确列出仍未完成的项目。
```

安装后可以用三种方式调用技能：

1. 直接用自然语言描述剪辑任务，让 `auto-cut` 自动路由。
2. 明确说“使用 `auto-cut` 分析并执行这个任务”。
3. 已知具体能力时，直接指定对应的 `auto-cut-*` 技能。
