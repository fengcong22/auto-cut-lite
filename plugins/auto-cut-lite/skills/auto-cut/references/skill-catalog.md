# Auto-Cut Skill Catalog

Use this catalog to choose the focused packaged skill for a user request. Prefer the smallest skill set that covers the task.

`auto-cut-lite` is the generic entrypoint. It sets `workflow_mode=lite` and reuses the canonical router with a non-destructive fixed-track draft contract. Unspecified plugin requests also remain in lite mode.

## Invocation

The three supported invocation modes are:

1. **自然语言直接说**: describe the editing request and let `auto-cut` route it.
2. **点名总 skill**: name `auto-cut` when capability analysis is needed.
3. **点名单独 skill**: name an exact `auto-cut-<purpose>` skill when the capability is known.

| Skill | 中文说明 | Use when |
| --- | --- | --- |
| `auto-cut` | 总入口和路由器。用户不知道该用哪个 skill、请求混合多个剪映能力，或要求完整工程/可迁移工程时使用。 | Natural-language routing, skill selection, packaged lite workflow, complete project delivery |
| `auto-cut-lite` | 通用精简版。保持原视频时长，以固定 V1-V4/A1-A2 轨道记录切点、素材和时序复用；删除意见只切不删，标签原文置顶 2 秒。 | “Auto-Cut 精简版”、“精简版 Auto-Cut”、“只切不删” |
| `auto-cut-basic-oral-video` | 基础口播视频风格。规定节奏、BGM 气质、花字、字幕、音效、调色和默认人声音量目标。 | “基础口播视频”, standard oral-video style, similar pacing/style |
| `auto-cut-editable-ad-revision` | 可编辑广告/口播草稿修订。用于换 BGM、移动花字/字幕、去黄提亮、避免重叠，同时保持可编辑。 | BGM/text/color revisions on editable ad drafts |
| `auto-cut-revision-draft` | 审片意见驱动的可追踪修订。强调保留素材、切口、分离音频和独立修改标记；当用户要求从新素材/干净基线重做时，禁止继承旧标注。 | Review comments, second-stage manual editing, edit traceability, clean source baseline |
| `auto-cut-audio-restoration` | 显式口播修复与诊断。用于独立降噪、呼吸/口水音/停顿残留清理、频谱验收、WAV/MP3 交付和隔离运行时检查；普通 review job 尚未自动接入质量门。 | Explicit spoken-word restoration, breath/noise/pause cleanup, standalone final audio, runtime diagnostics |
| `auto-cut-review-audio-precision` | 审核文档精准修音。读取飞书/ Lark 审核意见，按 ASR 字词对齐删词，保护必留词，并检查删词后的停顿、衔接太快和画面节奏。 | Feishu/Lark review docs, precise spoken-word deletion, post-delete pause timing, semantic pause repair, visual-aware pause validation |
| `auto-cut-text-safezone-animation-revision` | 文本安全区和花字动画修订。处理红框位置、贴纸跟随、入场/出场动画语义和保存后偏移排查。 | Red-box safe zone, text/sticker placement, `anim_in`/`anim_out` fixes |
| `auto-cut-local-image-overlay-revision` | 局部图片覆盖修订。从修改意见文档读取 PNG/JPG，按非透明可见边界和原文字/公式比例精准定位，检查保护边界，并保留独立可编辑覆盖与清理轨道。 | Review-document local formula/image replacement, visible-content scaling, protected-region clearance, editable overlays |
| `auto-cut-animation-timing-revision` | 动画时序精修。处理动画提前、加速、翻页、整段提前、整屏状态覆盖，以及稳定帧/撤层出点/下一动画边界精读。优先判断整段提前或全屏覆盖，避免局部裁切尺寸错位。 | Animation timing revision, speed-up, page turn advance, whole-section advance, full-screen state overlay, stable-frame release boundary |
| `auto-cut-profile-onboarding` | 课程指向物学科建档。响应用户明确的建立、更新、补充、查看、绑定或改绑命令，也接收普通指向物门禁失败后的 same-task onboarding handoff；不从内容推断身份。 | Explicit subject-library commands or a pointer-targeting same-task handoff |
| `auto-cut-pointer-targeting` | 指向物定位。先读取 `project-bindings.json` 并 fresh-check 已绑定的 `status=ready` profile，再按登记素材 SHA-256 和 current-layout 比例定位；门禁失败时在写草稿前打开 same-task onboarding handoff，绑定后重新校验；原视频/截图参考须先登记。 | Pointer assets, exact profile gate, same-task onboarding handoff, current-layout evidence, duration/out point |
| `auto-cut-final-acceptance` | 最终验收。检查编辑证据，并为完整工程封包、素材命名和隔离导入执行便携交付门禁；静态封包/隔离导入不等于真实目标机已打开。 | Final delivery gate, editable draft acceptance, portable package and target-open evidence |
| `auto-cut-music-library-bgm` | 剪映音乐库 BGM。按项目画面、节奏和口播密度选曲，禁止随便用本地音频兜底。 | Choose/replace/validate JianYing music-library BGM |
| `auto-cut-audio-peak-target` | 音频峰值目标。把 “-3dB” 等请求解释成最终波形峰值目标，而不是直接设置增益旋钮。 | Voice/BGM/SFX peak target ambiguity |
| `auto-cut-favorite-text-assets` | 剪映收藏花字和文字模板。先同步收藏，再区分花字效果可直接落草稿与官方模板 adapter 限制。 | Favorite flower text, favorite text template, `flower_query` |
| `auto-cut-draft-retention` | 草稿保留策略。按项目族只保留最近 3 个草稿，最多保留 1 个 fallback；修订草稿验收通过后再清理，失败 fallback 只清理自己。 | Clean old drafts, keep latest 3, limit fallback clutter without lowering validation |

## Routing Examples

- “这个需求该用哪个 skill？” -> `auto-cut`
- “按基础口播视频风格做一版” -> `auto-cut-basic-oral-video`
- “BGM 换成更适合电商口播的剪映曲库音乐” -> `auto-cut-music-library-bgm` + `auto-cut-editable-ad-revision`
- “花字移到红框里，其他不变，出场动画删掉” -> `auto-cut-text-safezone-animation-revision`
- “修改意见文档里说式子有误，用下面的 PNG 替换；比例和原字号一致，边界不要挡住旁边内容” -> `auto-cut-local-image-overlay-revision` + `auto-cut-revision-draft`
- “动画提前/加速/翻页提前，出点还要避开原动画入场和下一条动画，能整段提前就不要裁局部” -> `auto-cut-animation-timing-revision` + `auto-cut-revision-draft`
- “为当前项目建立指向物配置档案” -> `auto-cut-profile-onboarding`
- “加小手/箭头/圈选指向老师标注的位置，位置要精准，大小按原视频参考素材，只给入点但要判断停留多久消失” -> `auto-cut-pointer-targeting` -> exact gate fails only: `auto-cut-profile-onboarding` -> fresh pointer gate + `auto-cut-revision-draft`
- “从 PPT定稿+翻录 下拉新素材重做，不要之前草稿里的旧删除标注” -> `auto-cut-revision-draft` + `auto-cut-final-acceptance`
- “按审片意见继续改，保留切口和素材” -> `auto-cut-revision-draft`
- “检查这段口播是否需要修复，清掉呼吸音、口水音和停顿残留，输出最终 WAV/MP3” -> `auto-cut-audio-restoration`
- “重新读取飞书审核文档，按意见精确删词修音，处理好后贴回文档最下方” -> `auto-cut-review-audio-precision`
- “删词后有些停顿不合适/衔接太快，靠近动画/小手/翻页的地方要保留画面节奏” -> `auto-cut-review-audio-precision` + `auto-cut-revision-draft`
- “检查最终成片/草稿是否都改到了，音频衔接是否和谐，画面修改是否完整” -> `auto-cut-final-acceptance`
- “交付完整工程，搬到其他电脑后还能打开、预览并换电脑继续编辑” -> `auto-cut` + `auto-cut-final-acceptance`；真实目标机打开前只报告静态状态
- “人声峰值调到 -3dB” -> `auto-cut-audio-peak-target`
- “清理旧草稿，只保留最近 3 个” -> `auto-cut-draft-retention`
