# Auto-Cut Lite 新手部署说明

## 先理解三个位置

1. **下载位置**：刚收到的 ZIP 和 `.receipt.json` 校验回执所在的文件夹。部署成功后，这两个
   下载文件可以删除。
2. **稳定工作区**：首次安装时解压出来的 `Auto-cut-lite` 文件夹。它同时保存部署文件、
   `AGENTS.md` 和 `.codex\skills`，以后要用 Codex 打开它。这个文件夹不能删除。
3. **运行时位置**：部署器自动创建的 `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite`。真正执行插件的
   Python 环境在这里，不需要手工打开或移动。

首次安装可以把解压目录直接当工作区，而且本版默认就是这样做。升级时不要把新 ZIP 直接覆盖
解压到旧工作区；应把新包临时解压到别处，运行新部署器，让它根据安装回执更新原工作区。

## 一、首次安装

### 1. 准备最低环境

部署前只需要确保：

- Windows 10/11 x64；
- 64 位 Python 3.11，安装时勾选“Add Python to PATH”；
- Codex Desktop 可以正常打开。若部署器提示 Codex CLI 不可执行，再安装 Node.js LTS，部署器会
  使用官方 npm 入口。

剪映、FFmpeg、飞书登录和 ASR 密钥可以在部署成功后，让 Codex 再逐项帮助配置。

### 2. 在下载文件夹打开 PowerShell

把 ZIP 和名字以 `.zip.receipt.json` 结尾的回执放在同一个文件夹。用资源管理器打开这个文件夹，
点击顶部地址栏，输入 `powershell`，按回车。随后出现的 PowerShell 已经位于正确文件夹，不需要
自己输入或替换路径。

### 3. 自动校验 ZIP

整段复制到 PowerShell，按回车：

```powershell
$packages = @(Get-ChildItem -File -Filter 'auto-cut-lite-*-windows-x64.zip')
if ($packages.Count -ne 1) { throw '当前文件夹必须只有一个 Auto-Cut Lite ZIP，请移走旧版本后重试。' }
$zip = $packages[0]
$receiptFile = Get-Item -LiteralPath ($zip.FullName + '.receipt.json')
$receipt = Get-Content -LiteralPath $receiptFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
$actual = (Get-FileHash -LiteralPath $zip.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
$expected = ([string]$receipt.archive_sha256).ToLowerInvariant()
if ($actual -ne $expected) { throw "校验失败：实际 $actual，回执 $expected" }
Write-Host "校验成功：$actual"
```

看到“校验成功”才能继续。若提示找不到回执，说明 ZIP 对应的 `.receipt.json` 没放在同一文件夹。

### 4. 选择永久位置并解压

下面命令会弹出文件夹选择窗口。请选择一个准备长期保存 Auto-Cut Lite 的**父文件夹**，不要选择
现有的 `Auto-cut-lite` 文件夹。整段复制运行：

```powershell
$shell = New-Object -ComObject Shell.Application
$picked = $shell.BrowseForFolder(0, '选择 Auto-Cut Lite 的长期保存位置', 0)
if ($null -eq $picked) { throw '已取消选择。' }
$parent = [string]$picked.Self.Path
$workspace = Join-Path $parent 'Auto-cut-lite'
if (Test-Path -LiteralPath $workspace) { throw "目标已存在：$workspace。首次安装请选择其他父文件夹。" }
Expand-Archive -LiteralPath $zip.FullName -DestinationPath $parent
if (-not (Test-Path -LiteralPath (Join-Path $workspace 'deploy-to-codex.ps1'))) { throw '解压结果不完整。' }
Set-Location -LiteralPath $workspace
Write-Host "稳定工作区：$workspace"
```

这一步打印出的“稳定工作区”就是以后 Codex 要打开的目录。不要再移动或删除它。

### 5. 部署

国内网络建议直接使用镜像开关：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy-to-codex.ps1 -UseChinaMirrors
```

能稳定访问官方 pip/npm 时，也可以不用镜像：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy-to-codex.ps1
```

部署会安装主运行时和独立音频运行时，可能需要较长时间。不要关闭窗口。仅想先检查包和最低环境
时，可把 `-ValidateOnly` 加到命令末尾；检查成功后仍要再运行一次不带 `-ValidateOnly` 的正式部署。

## 二、部署成功后的操作

成功输出中会出现：

```text
deployment_status=installed
workspace_root=某个绝对路径\Auto-cut-lite
workspace_scope=repo
workspace_label=Auto-cut-lite
workspace_mode=combined_package_workspace
```

`readiness=pending_user_configuration` 通常不是部署失败，只表示剪映、FFmpeg、飞书用户授权或 ASR
密钥仍有一项需要配置。

1. 复制 `workspace_root=` 后面的路径。
2. 在 Codex 中选择“打开文件夹”，打开这个路径。
3. 在该工作区新建一个线程。旧线程不会自动刷新技能。
4. 查看技能列表：所有 `auto-cut-*` 应为 `scope=repo`，右侧标签为 `Auto-cut-lite`，不应再有
   Auto-Cut 的 `Personal` 重复项。

然后在新线程发送下面这段话：

```text
请读取当前 Auto-Cut Lite 工作区的 AGENTS.md，并检查
%LOCALAPPDATA%\Auto-Cut\auto-cut-lite\deployment-report.json。
请逐项帮助我完成并验证：剪映专业版及草稿目录、FFmpeg/FFprobe、lark-cli 当前用户登录与
default-as user/strict-mode user、火山 ASR 本机凭据和一次真实字词对齐测试。
不要让我提供或发送任何 token；需要我登录、授权或填写本机密钥时，请一步一步提示我操作。
最后重新检查 readiness，并明确列出仍未完成的项目。
```

可以发给其他人的部署成功话术：

```text
Auto-Cut Lite 已部署完成。请用 Codex 打开部署输出中的 workspace_root，并在该目录新建线程。
技能列表中的 auto-cut-* 应显示为仓库技能，标签为 Auto-cut-lite，不应显示 Personal 重复项。
如果 readiness=pending_user_configuration，请在新线程让 Codex 继续配置剪映、FFmpeg、飞书用户
身份和 ASR；这不代表部署失败。
```

部署成功并确认稳定工作区存在后，下载目录里的 ZIP 和 `.zip.receipt.json` 可以删除。不要删除
稳定工作区，也不要删除 `%LOCALAPPDATA%\Auto-Cut\auto-cut-lite`。

## 三、以后升级

升级时仍先在新 ZIP 所在的下载文件夹打开 PowerShell，并按“自动校验 ZIP”的命令完成校验。
然后整段运行：

```powershell
$upgradeParent = Join-Path ([System.IO.Path]::GetTempPath()) ('auto-cut-lite-upgrade-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $upgradeParent | Out-Null
Expand-Archive -LiteralPath $zip.FullName -DestinationPath $upgradeParent
$upgradePackage = Join-Path $upgradeParent 'Auto-cut-lite'
Set-Location -LiteralPath $upgradePackage
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy-to-codex.ps1 -UseChinaMirrors
```

部署器会按“命令参数 -> 旧安装回执 -> 当前临时解压目录”的顺序选择工作区。正常升级没有传
`-WorkspaceRoot`，所以它会自动更新原来的稳定工作区，不会把临时目录变成新工作区。

成功后检查输出的 `workspace_root` 仍是原工作区，再关闭 PowerShell，用资源管理器删除刚创建的
`auto-cut-lite-upgrade-...` 临时文件夹。新 ZIP 和回执也可以删除。随后重新用 Codex 打开原
`workspace_root` 并新建线程。

不要把新 ZIP 直接解压覆盖旧工作区。那样会在部署器校验和创建回滚备份之前改写文件。

## 四、主动更换工作区位置

只有确实要迁移时才使用 `-WorkspaceRoot`。目标必须是绝对路径，最后一级文件夹名必须完全等于
`Auto-cut-lite`。部署器会先验证旧工作区未被手工修改，再把受管文件迁移到新位置；目标中的
不同名用户文件会保留，同名且内容不同的文件会使迁移停止。
