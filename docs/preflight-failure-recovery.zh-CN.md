# Auto-Cut Lite 预检失败与恢复修复

候选版本：`1.6.9+codex.20260915222824`。本文用于源码评审、候选包复核及后续 Taskboard 验证。
本轮不部署目标机运行时，不修改 AppData 中的已部署文件，不重跑已完成的 FEI-29。
插件内嵌核心仍声明为 `auto-cut 1.7.0`；Lite 插件与内嵌核心版本独立演进。

## 根因及证据边界

2026-09-15 的 FEI-29 使用 Lite 1.6.8，运行沙箱拒绝访问用户 npm 目录中的 `lark-cli`。
旧版 `run_preflight()` 捕获 CLI 异常后，先调用 `invalidate_lark_readiness()`，随后才记录原始错误。
该函数通过 `tempfile.mkstemp()` 创建同目录临时文件，最后原子替换 readiness；readiness 目录同样不可写时，诊断路径本身再次失败。

Windows Python 3.11 的 `tempfile._mkstemp_inner()` 在特定 `PermissionError` 情况下会判断父目录是否存在且看似可写，并继续尝试新文件名。
沙箱拒绝创建文件但目录探测仍返回可写时，会走大量重复创建尝试。此时同步阶段回调没有返回，`preflight=running` 便持续保留，原始 CLI 错误也尚未交给阶段执行器。
现有阶段执行器记录耗时，但没有抢占同步回调的阶段截止机制；耗时记录不能中断这个循环。

另一个可控案例确认，Windows 的 `subprocess.run(capture_output=True, timeout=...)` 在超时后仍会无期限排空输出管道；若 CLI 后代继承管道，单加 `timeout` 仍不能保证返回。修复改用排他创建的临时输出文件和显式 `Popen.wait()`，不依赖管道 EOF；超时回收仅针对本次启动的进程及可观测后代，共享 0.5 秒等待预算。不可观测、另行脱离或无权终止的后代无法保证被回收，已知回收失败以 `process_cleanup=incomplete` 返回，不能因此继续无期限等待或覆盖超时错误。

用户提供的原任务记录显示：必要访问权限恢复后，预检 1.375 秒，全流程约 152 秒。
该记录用于定位原故障，不作为本次代码性能测试，也不需要再次执行原任务。
本次以可控故障注入分别覆盖 CLI 拒绝访问、临时文件创建持续拒绝访问和诊断状态不可写；复现数据及回归结果随候选包交付。

## 修复范围

| 位置 | 行为 |
| --- | --- |
| `scripts/utils/review_document_intake.py` | CLI 版本及用户身份探测各默认 10 秒子进程超时，其他 JSON 命令默认 120 秒；readiness 写入使用有界原子文件助手。 |
| `scripts/utils/atomic_io.py` | 在目标文件同目录使用随机名和排他创建，真实文件名碰撞最多尝试 8 次；权限或其他 I/O 错误直接返回；成功后原子替换，失败不截断旧文件。 |
| `scripts/utils/review_document_runner.py` | 先保留原始 CLI/身份错误，再尝试更新 readiness；次生持久化错误不替换主错误；终态结果使用安全错误信息。 |
| `scripts/utils/review_job_pipeline.py` | 每把状态锁的等待上限为 5 秒，失败终态最多尝试持久化 2 次；阶段状态和计时文件使用有界原子持久化，保留主错误、事务回滚及恢复；正常恢复继续校验输入摘要和阶段回执。 |

这里的时间边界针对 CLI 子进程、单次锁等待与应用层文件创建重试。阶段执行器仍只记录耗时，不能抢占任意 Python 回调或操作系统文件调用；这些局部边界不是整个阶段的硬实时截止时间。
如果状态目录完全不可写，程序无法保证把磁盘中的旧 `running` 改成 `failed`；必须有界退出并把主错误与安全的持久化诊断返回调用方，不能宣称写入成功。
恢复权限后再次运行，正常阶段恢复逻辑重新处理未完成阶段，不能把旧 `running` 当作完成缓存。

## 回归验收

回归用例应覆盖以下行为；最终测试命令、数量及耗时以候选交付记录为准，不把未执行的目标机验证写成通过。

| 场景 | 验收结果要求 |
| --- | --- |
| CLI 路径不可访问 | 及时返回安全的 CLI 错误，不输出原始路径、命令内容或凭据；可写阶段状态为失败。 |
| CLI 子进程持续运行 | 由指定子进程超时终止等待，保留明确的超时错误。 |
| CLI 后代继承输出句柄 | 不等待后代关闭管道；受控案例中后代被终止，调用在命令超时与有限回收预算内返回。 |
| CLI 失败且 readiness 不可写 | 主错误仍是 CLI 错误，readiness 失败作为次生诊断；不进入长时间重试。 |
| Windows 临时文件持续 `PermissionError` | 新助手首次创建失败后返回；旧实现故障注入能证明其会继续尝试。 |
| 临时文件重名 | 重试次数有上限，超过上限有界失败；已有目标文件内容不损坏。 |
| 写入或原子替换失败 | 旧 JSON 保持完整，临时文件按安全范围清理；不报告写入成功。 |
| 状态锁被占用 | 每把锁在有限等待后返回错误，不无限阻塞阶段错误报告。 |
| 阶段状态文件部分不可写 | 不把阶段状态和计时文件强行写成不一致版本；保留事务恢复能力，向调用方返回主错误及持久化失败诊断。 |
| 旧诊断产物、回执或状态不可读 | 返回结构化原始 CLI 错误；次级诊断不触发递归报告失败。 |
| 正常预检及身份不符 | 正常身份可通过；身份不符继续拒绝，不能因容错绕过用户检查。 |
| 修复权限后的恢复 | 失败或未完成阶段重新执行；有效已完成阶段继续受摘要与回执验证保护。 |

2026-09-15 最终功能回归已执行：

```powershell
python -m pytest -q tests/test_atomic_io.py tests/test_preflight_failures.py tests/test_review_document_intake.py tests/test_review_document_runner.py tests/test_review_document_runner_source_pairs.py tests/test_review_document_cli.py tests/test_review_job_pipeline.py tests/test_lite_plugin_deployer.py tests/test_validate_lite_plugin_package.py tests/test_runtime_integrity.py --basetemp tmp/pytest-preflight-release --junitxml tmp/preflight-release-tests.xml
```

结果为 **184 passed in 78.87s**，零失败、零跳过。覆盖上表所有场景、正常文档流程与阶段恢复、严格身份/完整性检查，以及版本同步、组合工作区 ZIP 构建和脏工作区拒绝。测试不调用真实飞书/Taskboard。

Windows Python 3.11 上的有界复现将标准库 `TMP_MAX` 仅在测试中降为 16，持续权限拒绝确实触发 16 次创建；新助手只调用一次 `os.open`。另一次真实父子进程探测中，父子各休眠 30 秒，设置命令超时 0.1 秒后，实测 0.126788 秒返回，子进程已终止、`process_cleanup=terminated`。自动回归也包含该继承句柄场景并校验退出上限，不能把单次探测数字当作目标机性能保证。

本次四个功能源码文件和四个测试文件通过 Ruff、Black；`git diff --check`、仓库卫生检查及数据结构检查通过。贡献指南要求的全仓检查仍存在基线限制：Ruff 的 12 项问题全部位于未改动文件；Black 报 37 个文件需要格式化，其中构建器本轮只修改版本号，`main` 上同一文件也不通过 Black。指南指定的 `tests/test_wrapper.py` 在 `main` 中不存在，命令未执行任何测试。这些结果已记录在 `tmp/preflight-ruff-full.txt`、`tmp/preflight-black-full.txt`、`tmp/preflight-black-builder-baseline.txt`、`tmp/preflight-wrapper-tests.xml`；不能宣称全仓检查全绿。

候选 ZIP 的提交号、SHA-256、离线验包结果以相邻构建回执和交付报告为准；源码提交后再构建，避免脏树候选。

## 构建与离线验包

从已验证、已提交且干净的源码分支构建；最终 `source_git_commit` 与 `source_git_clean=true` 必须来自构建回执。
构建与离线验包使用 Python 标准库，不安装依赖、不访问真实飞书或 Taskboard、不写入已部署运行时。
以下命令均从仓库根目录执行，验包解压目录必须尚不存在：

```powershell
python scripts/assert_lite_workspace.py
python scripts/release/build_lite_plugin.py --output "tmp/releases/auto-cut-lite-1.6.9+codex.20260915222824-windows-x64.zip" --json
python -m scripts.release.validate_lite_plugin_package --archive "tmp/releases/auto-cut-lite-1.6.9+codex.20260915222824-windows-x64.zip" --receipt "tmp/releases/auto-cut-lite-1.6.9+codex.20260915222824-windows-x64.zip.receipt.json" --extract-to "tmp/releases/verify-1.6.9"
```

交付 ZIP、相邻 `.zip.receipt.json`、验包报告和测试报告。离线验包须通过 ZIP CRC、路径安全、完整文件树、大小和 SHA-256、隐私扫描、能力闭包以及插件/内嵌核心版本身份校验。
离线验包只证明候选 ZIP 安装闭包，不等同于离线依赖伴随包、实际安装、真实账号或 Taskboard 联合验收。

## 后续目标机部署与 Taskboard 验证

1. 评审候选源码、测试结果、ZIP SHA-256 和 `source_git_commit`，确认部署使用这一个候选包。
2. 将 ZIP 全部解压到临时目录，在 `Auto-cut-lite` 内运行 `一键安装或升级-Auto-Cut-Lite.cmd`，选择已有稳定工作区完成常规升级。不要直接覆盖稳定工作区或手工替换 AppData 中的 Python 文件。
3. 按部署报告确认 `deployment_status=installed`、`plugin_version=1.6.9+codex.20260915222824`、`runtime_root` 和主 Python 环境；运行前继续验证包清单锚点及受管文件库存。
4. 用 Codex 打开报告中的稳定工作区并新建线程；为测试任务配置独立、可丢弃的 job 目录和 readiness 路径。保持用户身份授权及已有运行时完整性检查。
5. 新建专用 Taskboard 验证任务，分别测试 CLI 访问受限、readiness 不可写、正常权限以及失败后的权限恢复，保存安全终态结果、`job_state.json`、`job_timing.json` 和测得的退出时间。不要重跑 FEI-29。
6. 确认 CLI 错误不被 readiness 错误覆盖、没有持续高 CPU 循环、可写阶段状态退出 `running`、拒写状态得到明确诊断，并且权限恢复后能够重新执行失败阶段。

目标机最低环境遵循包内新手部署说明：Windows 10/11 x64、64 位 Python 3.11、Codex Desktop；部署器提示时补充 Node.js LTS。
安装器按正常流程维护独立主运行环境和音频环境，pip/npm 获取依赖需要相应网络访问。
真实预检还需要当前用户身份的 `lark-cli` 可执行访问、readiness/job 目录写权限及任务所需的剪映、FFmpeg、ASR 本机配置；本修复不代替这些访问授权或配置。
CLI 输出捕获还需运行账户的系统临时目录可写；拒写时只尝试一次并返回安全错误，绝不转入标准库的大量临时文件重试。测试注入用的 `lark_runner` 是受信任同步替身，其调用者负责遵守超时约定；生产入口使用上述受控子进程实现。
