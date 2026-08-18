# Auto-Cut | AI 全自动用你的剪映替你剪辑
![封面图](assets/cover.png)

## Full Windows Install (v1.7.0)

On Windows 10/11 x64, install 64-bit Python 3.10-3.12 for the main runtime.
Python 3.11 must also be installed and discoverable so the installer can create
the isolated audio runtime. Then extract the release and run one command from
its root:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

For the formal offline release, install 64-bit CPython 3.11 and keep the
verified dependency companion next to the extracted program package:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 --offline-bundle <offline-dependency-zip> --json
```

Offline mode verifies the companion manifest and every SHA-256, uses only
`--no-index` plus bundle-local wheelhouses, installs Playwright 1.52.0 Chromium
revision 1169 plus recording FFmpeg revision 1011 without a browser download,
and verifies both a real launch and a `record_video_dir` output. The installed
browser store at `tmp/offline-runtime/browsers` and licensed FFmpeg/FFprobe at
`tmp/offline-runtime/tools/ffmpeg` are auto-discovered by ordinary recording,
pointer rendering, and audio entrypoints. It never falls back to an index or proxy. See
[`TARGET_INSTALL.md`](TARGET_INSTALL.md) for private profile restoration,
fixed JianYing draft-root checks, authorization boundaries, and calibration.

This is an optional installation mode. When the target has network access, the
same command without `--offline-bundle` performs the normal online dependency
and browser setup; the user may still choose the verified bundle instead. The
two modes are selected explicitly and do not require AppContainer or a
physically disconnected machine.

The installer creates `.venv`, installs `requirements.txt`, installs and
hash-verifies all 17 repository-local Auto-Cut skills, installs and launches
Playwright Chromium, verifies recording output, creates the isolated Python
3.11 `.venv-audio`, runs the real audio doctor, checks FFmpeg/FFprobe, and runs
the JianYing editable-draft self-check. Results are written to
`tmp/install/install-report.json` and printed as a capability table. Read the
target-owned first-use status with:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py status --json
```

This first-use status does not replace `tmp/install/install-report.json`, which
remains authoritative for dependencies, Chromium, FFmpeg/FFprobe, the audio
doctor, and the JianYing smoke check. `ready`, `degraded`, `pending`, and
`unavailable` are distinct results; partial capability is never called full.

Machine-owned setup remains explicit on the target machine:

```powershell
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py favorites-sync --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py pointer-guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py feishu-status --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-guide --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-config
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py volc-status --json
.\.venv\Scripts\python.exe scripts/auto_cut_first_run.py tts-status --json
```

Favorite resources are resynchronized locally. Subject pointer profiles require
the user's stage/subject names, assets, reference images, approval, and explicit
binding confirmation. Feishu notification configuration and authorization are
completed independently on the target machine. Volcengine credentials are also
target-local: the bundled `scripts/audio/volc_word_align.py` adapter reports
success only when a real service result binds `input_sha256`, `service_job_id`,
`service_result_sha256`, `resource_id`, and `adapter_version` to non-empty,
finite, positive-duration, monotonic word timing. Configuration presence or
missing authorization is not success. The guide supports two mutually exclusive
authentication modes: a new-console `X-Api-Key`, or legacy App ID plus Access
Token. The adapter never mixes their headers. See
[`docs/release-full-install.md`](docs/release-full-install.md) for capability
status, all 20 machine-readable capability IDs, and troubleshooting.

The portable package preserves the editable draft contract: source video,
separated audio, replacement media, visible cut boundaries, one traceable review
marker per source item, and final acceptance evidence remain available for
secondary editing. Release acceptance runs from a clean extraction without .git
and binds the result to the ZIP SHA-256. The ZIP excludes `.git`, `.codex`, both
virtual environments, `.env`, credentials, local user profiles and assets,
absolute project bindings, output, temporary files, caches, and logs.


这是一个能实现自动剪辑的 skill 项目。剪辑师只需用自然语言告诉 AI 你想做什么视频，它就能帮你完成从**写文案、配音、加字幕、选音乐、上特效到最终导出**的整套流程。



### 能做什么

| 功能                | 说明                                                   |
| ------------------- | ------------------------------------------------------ |
| **素材导入**        | 视频、音频、图片一句话丢进时间轴，自动排列             |
| **AI 配音**         | 输入文案自动生成语音，支持剪映原生音色和微软语音       |
| **字幕生成**        | 根据配音自动拆句、逐句对齐字幕，支持打字机等动画效果   |
| **自动配乐**        | 本地音乐或剪映素材库的云端音乐曲                       |
| **特效/转场/滤镜**  | 按名字搜索剪映自带的特效库，一句话应用                 |
| **网页动效转视频**  | 用 HTML/JS/Canvas 写动画，自动录屏变成视频素材导入剪映 |
| **录屏 + 智能变焦** | 录制屏幕操作，自动给鼠标点击位置加缩放和红圈标记       |
| **影视解说**        | AI 分析视频内容，自动生成分镜脚本并合成解说视频        |
| **自动导出**        | 剪完直接导出 MP4，支持 1080P 到 4K                     |
| **关键帧动画**      | 缩放、位移、透明度等关键帧，做出运镜效果               |
| **复合片段**        | 像嵌套工程一样，把多个子项目组合成一个完整视频         |

### 做不到什么

- **不是剪映的替代品** -- 最终的视频渲染、预览回放还是靠剪映本身完成的，这个工具负责的是"自动帮你把时间轴搭好"，帮你点击导出
- **不能用剪映的实时特效** -- 像智能抠图、美颜、语音识别字幕这些需要剪映 GPU 实时处理的功能，目前无法通过代码调用
- **不能操作剪映的全部 UI 按钮** -- "一键成片""图文成片"这类剪映内置的 AI 功能暂时没法自动触发
- **自动导出依赖老版本** -- 自动导出功能目前只支持 **剪映 5.9 及以下版本**（6.0+ 弹窗太多会干扰自动化脚本）
- **不支持手机端剪映** -- 只能配合 Windows/Mac 桌面版剪映专业版使用

## 🚀 快速开始 (Quick Start)

### 1. 安装 Skill (Install)
当前仓库默认按本地源码仓使用，不再在 README 中内置远程安装地址。

**本地仓库直接使用：**
```bash
python -m pip install -r requirements.txt
python scripts/install_repo_skills.py
playwright install chromium
```

开发和测试环境使用 Python `>=3.10,<3.13`：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

`skills/` 是技能源文件，安装脚本会把它们刷新到仓库本地 `.codex/skills`。可以用
`python scripts/install_repo_skills.py --list` 只查看清单。

### 三种 Skill 使用方式

1. **自然语言直接说**
   - 例如：“帮我把这个草稿换个更适合的 BGM，花字不要挡字幕。”
   - `auto-cut` 会自动选择一个或多个子 skill。
2. **点名总 skill**
   - 例如：“用 `auto-cut` 分析一下这个需求该怎么做。”
   - 适合不确定该用 BGM、修订、音量峰值、安全区还是组合能力时。
3. **点名单独 skill**
   - 例如：“用 `auto-cut-music-library-bgm` 换 BGM。”
   - 适合已经知道所需能力时精准调用。

## Complete JianYing Project Delivery (Repository-Local)

This repository-local capability delivers the complete editable JianYing draft by mirroring the directory named by `currentCustomDraftPath`. The editable timeline, source video, source audio, separated/replacement audio, markers, and local materials remain inspectable. It does not open JianYing, invoke UI automation, or rewrite draft JSON.

Before first use on the recipient computer, read `JianyingPro/User Data/Config/globalSetting` and verify the same fixed absolute `currentCustomDraftPath`:

```powershell
python scripts/jy_wrapper.py draft-root-check `
  --expected-root "<source_currentCustomDraftPath>" --json
```

If the configured path is absent, ambiguous, missing, or different, stop and show exactly: `请在对方电脑创建与源电脑相同的剪映草稿路径：<path>`. Never silently choose a conventional default.

Keep every local video, audio, and image used by the draft under `Resources/local` or `Resources/audioAlg` before writing material references. Mirror the saved directory byte-for-byte to the Desktop transfer folder and write the receipt outside both draft trees:

```powershell
python scripts/jy_wrapper.py draft-mirror-deliver `
  --name "<DraftName>" `
  --drafts-root "<source_currentCustomDraftPath>" `
  --desktop-root "<Desktop>\transfer" `
  --receipt-json "<receipt.json>" `
  --job-input-digest "<sha256>" --json
```

Delivery waits for the complete draft tree to stay unchanged for a continuous
quiet window before copying. The copy is made to a temporary sibling, checked,
and the source is observed for a second quiet window before atomic promotion.
If the source changes, only the temporary copy is removed and the operation
retries from a new stable snapshot; timeout or retry exhaustion leaves no final
target or success receipt. Defaults are 6 seconds quiet, 1 second polling,
120 seconds timeout, and 2 retries. Override with
`--quiet-window-seconds`, `--poll-interval-seconds`,
`--stability-timeout-seconds`, and `--stability-retries`.

The external receipt records `schema_version`, `source_tree_sha256`, `target_tree_sha256`, `tree_sha256`, `file_count`, `directory_count`, `byte_size`, the job-input digest, the stability policy, observation evidence, and source-stability booleans. It includes empty directories, source tree and target tree inventories, and requires identical hashes. The mirror refuses overwrite and an existing destination (`destination already exists`), reparse points, source/target overlap, source changes during copy, and any mismatch.

The receipt records `json_rewritten=false`, `ui_invoked=false`, `native_editor_invoked=false`, and `portable_package_invoked=false`. Use a trusted transfer channel for the mirrored directory and receipt; SHA-256 is integrity evidence, not publisher identity.

The old `portable_project_tool` package/import code and importer EXE may remain as historical/internal compatibility code, but they are not routed and are not final acceptance evidence. Cloud-only effects, fonts, and templates remain explicit non-file dependencies.

Only another physical computer that opens the mirrored draft at the same absolute path, leaves the loading state, previews it, and confirms it remained editable can be called `target_open_verified`.

可选的飞书阻塞提醒需要在每台电脑上单独审批和配置，操作步骤与隐私边界见
[`docs/auto-cut-notifications.md`](docs/auto-cut-notifications.md)。飞书仅发送提醒，确认仍须回到原 Codex 任务完成。

**手动安装到 Skill 目录：**

**🤖 Antigravity / Gemini Code Assist:**
```bash
git clone <your-repo-url> .agent/skills/Auto-Cut
```

**🚀 Trae IDE:**
```bash
git clone <your-repo-url> .trae/skills/Auto-Cut
```

**🧠 Claude Code:**
```bash
git clone <your-repo-url> .claude/skills/Auto-Cut
```

**💻 Cursor / VSCode / 通用:**
```bash
# 通用方式：安装到根目录 include 列表
git clone <your-repo-url> skills/Auto-Cut
```

### 2. 🛠️ 环境自检 (Recommended)
第一次在新机器上使用，先运行：

```bash
python scripts/jy_wrapper.py self-check --cleanup --json
```

这一步会自动探测：

- 本机剪映安装路径
- 草稿目录
- 资源目录
- 烟测草稿写入
- 当前运行中的剪映窗口

### 3. 🛠️ 资源下载与版本准备 (Essential Resources)
本仓库的草稿写入、草稿 patch、素材搜索、文本模板、滤镜/特效/贴纸等能力，不绑定某个固定剪映版本；只要当前版本的草稿结构和资源路径可被探测到，就可以正常工作。

需要单独注意的是：

- **自动导出** 仍然更适合低弹窗、低干扰环境，旧版本通常更稳。
- **UI 自动化链路** 会受剪映界面变化影响，版本越新，越要先跑 `self-check`。

### 4. 试试这样跟 AI 说

**随便剪一个试试**
> "帮我随便剪一个视频看看效果"

**做个 Vlog**
> "把 D:\旅行素材 这个文件夹里的视频和照片帮我剪成一个 Vlog，配个轻快的音乐，加上标题'周末露营记'"

**写文案 + 配音 + 出片**
> "帮我写一段关于'秋天的第一杯奶茶'的短视频文案，配上温柔女声旁白和字幕，再找个温馨的 BGM"

**影视解说**
> "这个视频 D:\电影片段.mp4 ，帮我做一个 60 秒的影视解说"

**录个软件教程**
> "我要录一段操作教程，帮我启动录屏，录完自动导入剪映"


**做个炫酷的片头动画**
> "帮我用网页写一个星空粒子的片头动画，5 秒钟，然后导入到剪映里"

**字幕配画面**
> "我有一段旁白录音 旁白.mp3，帮我识别出字幕，然后从 F:\素材库 里自动挑画面配上去"

**用剪映曲库的音乐**
> "我想用剪映里那首'阳光旅途'当背景音乐"（需要先在剪映里播放一次建立缓存）

## 📦 环境准备 (必读)

为了让 Skill 正常工作，您还需要告知 AI 再做一点工作：

### 1. 安装 Python 依赖
请在终端运行以下命令以确保所有自动化功能正常工作：
```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 初始化网页捕获环境 (Web-to-Video 功能必填)
playwright install chromium
```


### 2. 确认剪映安装位置
默认情况下，工具会自动探测剪映安装目录和草稿目录。

如果自动探测失败，再显式告诉 AI：
> "我的剪映草稿目录在 D:\JianyingPro\..."

或者直接设置：

```bash
set JY_DRAFTS_ROOT=D:\JianyingPro\Drafts
set JY_APP_ROOT=D:\JianyingPro
```

## 📂 文件夹说明

- `SKILL.md`: 给 AI 看的说明书。
- `references/`: 参考文档与示例资料（非运行时依赖）。
- `scripts/vendor/`: 运行时内置依赖（如 `pyJianYingDraft`）。
- `tools/recording/`: **录屏神器**，都在这里面。
- `assets/`: 演示用的测试视频和音乐。

## 集成音频修复引擎

仓库已内置首方 `audio_sound/` 音频引擎，包含七套预设、严格成品 workflow、窄窗口修复、
删词接缝和频谱验收。常用入口：

```powershell
python scripts/audio/audio_cleanup.py doctor
python scripts/audio/audio_skill_workflow.py run "D:\media\voice.wav" --skip-deepfilternet
```

重依赖只安装到 `.venv-audio`，版本由 `requirements-audio.lock` 单独管理，不会改变主环境的
NumPy/Python 约束。完整架构、命令、安全边界和 50 项来源清单见
[`docs/audio-sound/README.md`](docs/audio-sound/README.md)。精确能力可直接调用
`auto-cut-audio-restoration`，自然语言任务仍由 `auto-cut` 总入口路由。

## ⚠️ 常见问题 (FAQ)

1. **看不到新生成的草稿？**
   剪映软件不会实时刷新文件列表。生成草稿后，请**重启剪映**，或者随便点进一个旧草稿再退出来，就能看到新的了。

2. **自动导出失败？**
   自动导出脚本模拟了鼠标键盘操作。
   - 运行导出时，请**不要**动鼠标和键盘。
   - 目前仅支持 **剪映 5.9 或更早版本** (新版本弹窗太多容易干扰脚本)。

## 🔄 如何更新 (Update)

当有新功能发布时，您可以输入以下命令一键更新：

```bash
cd .agent/skills/Auto-Cut
git pull
```

## 📅 更新日志 (Changelog)

最新版本请直接查看 [CHANGELOG.md](CHANGELOG.md) 与 [VERSION](VERSION)。

### v1.4 (2026-02-09) - 全自动 AI 导演系统上线！
- **🧠 AI 语义素材匹配 (Semantic Footage Match)**:
  - **核心里程碑**：现在支持根据“视频画面内容、旁白音频、SRT 字幕”三位一体进行语义分析。AI 会自动理解每一句台词的含义，并从素材库中精准挑选最契合的画面进行剪辑（如说到“爆汁”自动对位流油特写）。


### v1.3 (2026-02-03) - 突破二次元壁！
- **✨ 网页转视频 (Web-to-Video)**:
  - 核心突破！现在支持直接将 **HTML/Javascript/Canvas/SVG** 编写的网页动效实时录制并无缝导入剪映主轨道。
  - 集成 **Playwright 智能录屏引擎**，支持自动等待动画结束信号 (`window.animationFinished`)，产出高清无损素材。
  - 真正实现“代码即特效”，让前端动效库（如 Three.js, GSAP, Lottie）成为你的剪接素材库。

## 🌟 核心特性 (V3 进化版)

- **顶级素材接入**:
  - **banana (Imagen 3)**: 正式接入，支持一行指令生成 4K 电影级神兽/场景贴纸。
  - **Grok 3 (Media)**: 视觉天花板级图生视频，让你的静态素材瞬间化身史诗大片。
- **多轨管理**：支持视频、音频、字幕、贴纸、特效无限叠加，像专业剪辑师一样操作。
- **全自动闭环**: 从 Claude 4.5 剧本创作到素材生成，再到剪映草稿合成，一键全自动。
- **智能变焦**: 独家的 Smart Zoom 功能，能把普通的录屏自动变成“带镜头感”的演示视频。
- **网页转视频 (Web-to-Video)**: 完美支持 Canvas/JS 动效实时捕捉，让 Web 的无限创意瞬间化身视频 VFX 素材。
- **自动导出**：内置自动化脚本，支持一键导出 1080P/4K 视频，彻底解放双手。

### v1.2 (2026-01-27) - 像变魔术一样！
- **✨ 智能变焦 (Smart Zoom)**:
  - 录制的教程视频太平淡？现在，它会自动帮你把镜头**推进特写**到鼠标点击的地方，就像电影镜头一样酷！
  - **自动红圈**：鼠标点哪里，那里就自动出现小红圈，观众一眼就能看到重点。
  - **丝滑跟随**：鼠标移动时，画面会像摄像机云台一样平滑跟随，再也不怕画面太小看不清了。
- **🎥 录屏神器大升级**:
  - 录完就能**一键生成草稿**！不用手动打开剪映，不用导入素材，点一下按钮，草稿就躺在你的剪映里了。
  - 终于支持连续录制了，一口气录十段素材也不用重启软件。
  - 录像文件会自动整理好，不再乱丢在桌面。

---
