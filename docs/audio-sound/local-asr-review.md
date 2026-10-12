# 本地 ASR 人工边界参考

`scripts/audio/local_asr_feedback.py` 将已经在原聊天确认的人工试听范围导入可重复校验的收据。它支持现有 `local-asr-refined-boundary-feedback-v1` 记录，分别保存模型候选与人工参考，不修改转写、时间戳、音频或正式剪辑配置。

```powershell
python scripts/audio/local_asr_feedback.py import --feedback "<refined-boundary-feedback.json>" --output "tmp/<job>/human-reference.json"
python scripts/audio/local_asr_feedback.py verify --receipt "tmp/<job>/human-reference.json"
```

输入须已有 `human_confirmation_in_chat=true`、`user_judgment=refined_boundary_suitable` 及四项完整性判断。当前导入适配器使用历史 Famous Works 反馈的判断字段 `target_onset_F`、`target_final_s`、`preceding_word`、`following_word_or_quote`。该适配器不从截图推断帧、不补写人工确认，也不读取浏览器听音状态。其他目标/格式需独立适配，不能改字段冒充这份反馈。

校验内容：

- 反馈、manifest、源 WAV、上下文 WAV 以及已有前序反馈的实际文件 SHA-256。
- 16 kHz 单声道 PCM16 格式；上下文逐字节等于源音频的指定帧切片。
- 候选 ID、目标、上下文范围、人工源帧/相对帧/秒数一致。
- manifest 提供模型结果时，验证结果文件、原始 worker 输出哈希、输入音频身份、转写与原生单位一致，并重新检索唯一候选。
- 已有收据从原始输入重新计算后必须完全一致；输入改变或收据篡改均失败。

收据保留原始反馈文件绑定，不把旧的“起音吞音”覆盖成“原模型通过”。`difference_ms` 为人工范围减模型范围；负起点表示人工选择更早。文件校验不等于独立听音、当前安装模型验证或自动边界验收。缺少模型结果时，只保存人工参考，`model_evidence.status=not_supplied`。

输出位于忽略的 `tmp/`，原子创建且拒绝覆盖任何输入、绑定证据或不同内容的既有收据。相同内容重跑可复用。所有生产门禁保持关闭，`authoritative_cut_boundary=false`、`execution_required=false`、`executable_cuts=[]`。

正式 `resolve_lite_audio_items()` 拒绝本地诊断 schema、候选/人工证据、本地诊断 backend 或显式关闭的权威门禁；给候选复制 `words` 或强行改成 `true` 不会绕过已知类型检查。这只是误用保护，不代替正式来源身份验证。

下一接入阶段仍须建立本地 provider 的源识别、全候选反向识别、模型与运行时身份、缓存及恢复接口，并用独立样本证明边界与相邻词完整性。人工参考不能直接写成正式 `asr_alignment`，也不能据此宣布全学科精切通过。
