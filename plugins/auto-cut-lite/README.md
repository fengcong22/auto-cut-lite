# Auto-Cut Lite

这是 Auto-Cut 的通用精简版 Codex 包，包含 17 个工作区技能。音频删除仍使用字/词级 ASR
确定真实切口，标签从真实切口开始，而不是直接采用修改意见里的粗略时间。动画、画面时序和原有
小手遮挡清理仅保留原文标签；用户提供的小手或局部图片按剪映默认几何贴入，不自动位移、缩放、
制作关键帧或校准落点。

本版采用“合并工作区 + 独立运行时”模式：

- 首次安装时，解压出的 `Auto-cut-lite` 文件夹就是要长期保留并用 Codex 打开的工作区。
- 工作区同时保存包文件、`AGENTS.md` 和 `.codex\skills`。
- 真正执行任务的插件和 Python 环境仍安装在 `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite`。
- 升级时把新 ZIP 解压到临时位置并运行新部署器；部署器按旧回执更新原工作区。
- 插件清单不注册 `skills`，包内也没有顶层 `skills`，因此技能应显示为仓库范围
  `Auto-cut-lite`，而不是重复的 `Personal`。

请从 [BEGINNER_DEPLOYMENT.md](BEGINNER_DEPLOYMENT.md) 开始。里面包含无需手工替换路径的
校验、首次安装、国内镜像、升级、验收和部署后配置步骤。
