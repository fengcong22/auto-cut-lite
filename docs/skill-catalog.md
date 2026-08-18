# Auto-Cut 技能目录

这个目录给人看，用来快速判断仓库里有哪些 skill、什么时候用、能解决什么问题。实际 AI 路由入口是 `skills/auto-cut/SKILL.md`。

## 首次使用提示

先从仓库根目录安装或刷新技能：

```powershell
python scripts/install_repo_skills.py
```

`skills/` 是源文件，`.codex/skills` 是可刷新的仓库本地运行时副本。只查看清单时运行
`python scripts/install_repo_skills.py --list`。

安装后有三种使用方式：

1. **自然语言直接说**
   - 例如：“帮我把这个草稿换个更适合的 BGM，花字不要挡字幕。”
   - AI 会按 `auto-cut` 总入口判断该走哪个子 skill。

2. **点名总 skill**
   - 例如：“用 `auto-cut` 分析一下这个需求该怎么做。”
   - 总 skill 会负责路由：该用音频评估/修复、BGM、修订、音量峰值、红框安全区，还是多个组合。

3. **点名单独 skill**
   - 例如：“用 `auto-cut-music-library-bgm` 换 BGM。”
   - 这种适合你已经知道要用哪个能力时精准调用。

关键信息：

- 当前默认安装目标是仓库本地 `.codex/skills`，不是全局 skills 目录。
- `skills/` 是源文件；`.codex/skills` 是运行时副本，可以用安装脚本刷新。
- 后续新增技能继续使用 `auto-cut-<purpose>` 命名，并同步更新本目录和 `skills/auto-cut/references/skill-catalog.md`。

## 技能清单

| Skill 名称 | 中文说明 | 典型触发语句 |
| --- | --- | --- |
| `auto-cut` | 总入口。负责分析自然语言需求，选择一个或多个 `auto-cut-*` 子 skill，并路由完整工程/可迁移工程交付。 | “不知道该用哪个 skill”、“按 Auto-Cut 全套规则处理”、“交付完整工程” |
| `auto-cut-basic-oral-video` | 基础口播视频风格。控制剪辑节奏、花字、字幕、安全区、BGM 风格、SFX、调色和默认人声音量。 | “基础口播视频”、“按之前口播风格做”、“节奏像 C2597 那版” |
| `auto-cut-editable-ad-revision` | 可编辑广告/口播草稿修订。适合换 BGM、移动花字/字幕、避免重叠、画面去黄提亮，并保留可编辑结构。 | “换个更合适的音乐”、“花字移到红框里”、“不要重叠”、“其他保持不变” |
| `auto-cut-revision-draft` | 审片意见驱动修订。强调保留素材、分离音频、切口、每条修改标记，方便二次人工编辑；用户要求新素材/干净基线时，禁止带入旧标注。 | “按审片意见继续改”、“保留切口”、“要能看出改了哪里”、“从PPT定稿+翻录重新处理，不要旧草稿” |
| `auto-cut-audio-restoration` | 显式口播修复与诊断。用于独立降噪、呼吸/口水音/停顿残留清理、频谱验收、WAV/MP3 交付和隔离运行时检查；普通 review job 尚未自动接入质量门。 | “清掉呼吸音和口水音”、“输出最终修音 WAV/MP3”、“检查音频运行环境” |
| `auto-cut-review-audio-precision` | 审核文档精准修音。读取飞书/ Lark 审核意见，按 ASR 字词对齐删词，保护必留词，并单独处理删词后的停顿节奏、衔接太快和画面阅读节奏。 | “重新读取飞书文档剪六条音频”、“第二条别吞语言”、“第三条所以要保留”、“衔接太快需要调整停顿”、“处理好贴回文档最下方” |
| `auto-cut-text-safezone-animation-revision` | 文本安全区和花字动画修订。处理红框位置、贴纸跟随、入场/出场动画语义、保存后位置偏移。 | “花字位置还是不对”、“删出场动画”、“入场动画保留”、“贴纸跟着花字” |
| `auto-cut-local-image-overlay-revision` | 局部图片覆盖修订。从修改意见文档提取 PNG/JPG，按非透明可见边界和原文字/公式比例精准定位，保护相邻内容边界，并保留独立可编辑覆盖与清理轨道。 | “式子有误，用下面图片替换”、“比例和原字号一致”、“PNG 不要挡住旁边内容”、“覆盖到换页前结束” |
| `auto-cut-animation-timing-revision` | 动画时序精修技能。处理动画提前、加速、翻页、整段提前、整屏状态覆盖，并精读稳定帧、撤层出点和下一动画边界；优先判断整段提前或全屏覆盖，避免局部裁切尺寸错位。 | “动画提前到02:53”、“速度加快”、“翻页动画提前”、“11:58:15 有入场动作”、“提前层别盖住下一条动画”、“能整段提前就不要裁局部” |
| `auto-cut-subject-pointer-onboarding` | 课程指向物学科建档。处理用户明确的建立、更新、查看、绑定或改绑命令，也处理普通指向物门禁失败后的同一任务 onboarding handoff；始终只用用户明确身份，不从课程内容自动识别。 | “为当前项目建立高中历史指向物素材库”、“更新高中历史素材库”、“查看学科素材库”、“改绑为高中地理” |
| `auto-cut-pointer-targeting` | 指向物定位技能。先读取 `project-bindings.json` 并 fresh-check 已绑定的 `status=ready` profile，再按登记素材 SHA-256 和 current-layout 比例定位；门禁失败时在写草稿前打开同一任务 onboarding handoff，绑定后重新校验再继续；原视频/截图参考须先登记。 | “加小手指向这里”、“手的大小不对”、“箭头指到老师标注的位置”、“按原视频参考大小”、“位置还是不对，看截图”、“只有入点，判断它停留多久消失” |
| `auto-cut-final-acceptance` | 最终验收。除编辑证据外，还检查完整工程封包、素材命名和隔离导入；静态封包/隔离导入不等于真实目标机已经打开。 | “最终验收这个草稿”、“交付可迁移完整工程”、“换电脑继续编辑” |
| `auto-cut-music-library-bgm` | 剪映音乐库 BGM。根据画面、节奏、产品和口播密度选曲，禁止随便用本地音频替代。 | “从剪映曲库换 BGM”、“找更适合电商口播的音乐”、“不要本地音频兜底” |
| `auto-cut-audio-peak-target` | 音频峰值目标。把 “-3dB” 等请求理解为最终波形峰值目标，而不是直接把增益旋钮设成 -3。 | “人声峰值到 -3dB”、“波形到 -3 那条线”、“BGM 到 -30dB” |
| `auto-cut-favorite-text-assets` | 剪映收藏花字/文字模板。先同步本机收藏，再判断花字效果能否直接落草稿、官方模板是否需要 adapter。 | “用我收藏的花字”、“先从收藏夹找模板”、“直接调用收藏花字” |
| `auto-cut-draft-retention` | 草稿保留。默认同一项目族只保留最近 3 个草稿，且最多保留 1 个 fallback；修订草稿通过验收后再清理，失败 fallback 只清理自己。 | “只保留最近 3 个草稿”、“清理旧草稿”、“删除早期 fallback 草稿”、“减少生成太多草稿但不影响正确率” |

## 新增技能维护规则

新增 skill 时按这个顺序做：

1. 在 `skills/` 下创建 `auto-cut-<purpose>/SKILL.md`。
2. `SKILL.md` frontmatter 的 `name` 必须等于目录名。
3. 在本文件补一行中文说明和典型触发语句。
4. 在 `skills/auto-cut/references/skill-catalog.md` 补路由说明。
5. 跑 `python scripts/install_repo_skills.py` 安装到仓库本地 `.codex/skills`；需要只看清单时用 `--list`。不要默认安装到全局 skill 目录。
