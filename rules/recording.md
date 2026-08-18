# 录屏与智能缩放 (Recording & Smart Zoom)

Carnac 是目标电脑上的可选软件。发布包只检测已经安装的 `Carnac.exe` 或
用户明确设置的 `CARNAC_EXE`，不会下载未经固定版本、许可证和 SHA-256
验证的可执行文件。没有 Carnac 时仍可录屏，但不会显示按键叠加。

如果你需要录制屏幕内容并在剪映中自动应用“智能缩放”（根据点击位置自动放大画面），请遵循以下流程。

## 1. 启动录制器
运行内置的图形化录制工具：
```bash
# 默认保存到用户目录（Windows: %LOCALAPPDATA%\JianyingEditor\recordings），不落在项目内
python <SKILL_ROOT>/tools/recording/recorder.py
# 若需指定目录：python <SKILL_ROOT>/tools/recording/recorder.py "D:\MyRecordings"
```

### 录制器功能：
- **画面录制**: 捕获全屏或指定显示器内容。
- **音频录制**: 支持捕获系统声音（需在脚本中配置正确的设备 ID）。
- **事件捕获**: 实时记录点击（Click）、按键（Keypress）和光标移动（Move）。
- **小圆点模式**: 录制过程中会变为红色小圆点，点击即可停止。
- **Carnac（可选）**: 勾选「录制时启动 Carnac」后尝试启动目标电脑上已有的 Carnac。由助手启动的实例会在结束录制或关闭窗口时退出；未安装时请先独立安装或设置 `CARNAC_EXE`。

## 2. 自动生成草稿
录制结束后，录制器会弹出选单，选择 **“✨ 自动生成智能草稿”**。

这会后台调用 `scripts/jy_wrapper.py apply-zoom` 命令：
1. **导入视频**: 将刚录制的 MP4 导入剪映。
2. **分析事件**: 读取配套的 `_events.json`。
3. **应用关键帧**: 在每个点击位置点，自动插入“放大-停留-恢复”的缩放关键帧，实现无需手动剪辑的导览视频效果。

## 3. 手动调用智能缩放
如果你已经有了录屏文件和对应的 JSON 事件文件，也可以手动执行：
```bash
python <SKILL_ROOT>/scripts/jy_wrapper.py apply-zoom --name "我的演示项目" --video "recording.mp4" --json "recording_events.json" --scale 150
```

## 注意事项 (Constraints)
- **环境**: 录制功能仅支持 Windows。
- **依赖**: 必须安装 `ffmpeg` (已加入系统路径) 以及 Python 库 `pynput` 和 `tkinter`。
- **坐标**: 录制器会自动处理不同分辨率下的坐标归一化，确保缩放位置精准。
- **稳定性**: 键鼠事件写入带互斥锁；关闭助手窗口时若正在录制会静默结束；停止 FFmpeg 时先发 `q`，超时则结束进程树。
