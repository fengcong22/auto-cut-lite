# Auto-Cut Lite 新手一键部署说明

## 先看结论

正常安装只有四步：

1. 对 ZIP 使用“全部解压”。
2. 打开解压后的 `Auto-cut-lite`，双击 `START-AUTO-CUT-LITE.cmd`。
3. 在弹窗中选择你希望长期使用的 `Auto-cut-lite` 工作区位置。
4. 部署成功后，用 Codex 打开窗口里显示的 `workspace_root`，并新建线程。

不需要先打开 PowerShell，不需要输入命令，也不需要自己替换路径。启动器默认使用国内
pip/npm 镜像，并自动校验解压后的每个受管文件。

## 一、首次安装

### 1. 准备最低环境

部署前只需要：

- Windows 10/11 x64；
- 64 位 Python 3.11，安装 Python 时勾选 `Add Python to PATH`；
- Codex Desktop 能正常打开。

剪映、FFmpeg、飞书登录和 ASR 密钥可以部署成功后再让 Codex 逐项引导配置。若部署器提示找
不到可用的 Codex CLI，再安装 Node.js LTS，然后重新双击启动器。

### 2. 全部解压

不要在压缩包预览窗口里直接双击文件。请右键 ZIP，选择“全部解压”。临时解压到下载目录也
可以，因为下一步可以另选长期工作区；若想让解压目录直接成为工作区，就把 ZIP 解压到准备长期
保存的位置。

### 3. 双击并选择工作区

进入解压后的 `Auto-cut-lite` 文件夹，双击：

```text
START-AUTO-CUT-LITE.cmd
```

首次部署会弹出文件夹选择框。你可以：

- 直接选择一个已经叫 `Auto-cut-lite` 的文件夹；
- 选择其他父文件夹，部署器会自动在里面创建 `Auto-cut-lite`。

例如 Codex 已经为你准备了某个工作区位置，就选择那个 `Auto-cut-lite` 文件夹或它的上一级目录。
部署器会智能识别，避免生成 `Auto-cut-lite\Auto-cut-lite`。

最终选定的 `Auto-cut-lite` 会同时保存完整包文件、`AGENTS.md` 和 `.codex\skills`，也是以后 Codex
应该打开的稳定工作区。运行时单独安装到 `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite`。

Windows 如果询问是否允许运行，请确认文件来自你收到的 Auto-Cut Lite 包后再继续。部署过程会
自动完成：

- 校验 `PACKAGE-MANIFEST.json` 和包内文件哈希；
- 安装或覆盖升级独立运行时；
- 注册 `Auto-Cut Lite` 命名市场并清理旧的 Personal 重复项；
- 写入工作区 `AGENTS.md` 和全部 `.codex\skills`；
- 安装独立音频环境；
- 失败时执行回滚；
- 输出并复制最终 `workspace_root`。

安装依赖可能需要较长时间，不要关闭黑色窗口。默认已经启用国内镜像，不需要再输入
`-UseChinaMirrors`。

## 二、部署成功后

窗口看到下面这些内容才算部署成功：

```text
deployment_status=installed
workspace_root=某个绝对路径\Auto-cut-lite
workspace_scope=repo
workspace_label=Auto-cut-lite
```

启动器会把工作区路径复制到剪贴板并打开资源管理器。接下来：

1. 打开 Codex，选择“打开文件夹”。
2. 粘贴 `workspace_root` 路径并打开。
3. 在这个工作区新建线程，旧线程不会自动刷新技能。
4. 打开工作区里的 `CODEX_NEXT_STEPS.md`，把其中的话发给 Codex。

技能列表中的 `auto-cut-*` 应显示为 `scope=repo`，右侧标签为 `Auto-cut-lite`，不应再有
Auto-Cut 的 `Personal` 重复项。

`readiness=pending_user_configuration` 通常不代表部署失败，只表示剪映、FFmpeg、飞书用户授权或
ASR 本机凭据还需要在新线程中配置。

部署成功并确认 Codex 能打开稳定工作区后，如果最初是在另一个位置临时解压，那个临时解压目录、
下载的 ZIP 和 `.zip.receipt.json` 都可以删除。不要删除 `workspace_root`，也不要删除
`%LOCALAPPDATA%\Auto-Cut\auto-cut-lite`。

## 三、以后升级

1. 把新 ZIP 解压到任意临时文件夹，不要直接覆盖旧的稳定工作区。
2. 在新解压包中双击 `START-AUTO-CUT-LITE.cmd`。
3. 检测到旧工作区后，选择“是”即可沿用并升级；选择“否”可以迁移到新位置。
4. 成功后确认输出的 `workspace_root` 正确，再删除这次升级使用的临时解压目录和 ZIP。
5. 用 Codex 重新打开 `workspace_root` 并新建线程。

迁移时部署器会先校验旧工作区，并使用已有的备份与回滚机制，不需要手工移动 `.codex` 文件。

## 四、失败时怎么做

不要反复移动文件或手工复制 `.codex\skills`。保持部署窗口打开，把窗口中的完整错误文字发给
Codex；若窗口给出了部署报告，也把下面这个文件告诉 Codex：

```text
%LOCALAPPDATA%\Auto-Cut\auto-cut-lite\deployment-report.json
```

部署器会保留失败原因并尽量回滚到升级前状态。

## 五、高级手动入口

只有排错或需要使用官方网络时才需要 PowerShell。在解压后的 `Auto-cut-lite` 文件夹地址栏输入
`powershell` 并回车，然后运行：

```powershell
# 默认国内镜像，并弹窗选择工作区
.\installer\one_click_deploy.ps1

# 改用官方 pip/npm 网络
.\installer\one_click_deploy.ps1 -OfficialNetwork

# 仅直接调用底层部署器
.\deploy-to-codex.ps1 -UseChinaMirrors
```

正常新手安装不需要执行这一节。
