# 收藏花字与文字模板索引

本仓库现在支持把本机剪映里的常用文字模板、花字效果和本地缓存资产同步为可搜索索引。

## 目标

- 统一检索本机收藏/白名单草稿里的文字模板
- 统一检索本机花字效果缓存
- 让 `template_query` / `flower_query` 可以优先命中这些资产

## 同步命令

```powershell
python scripts/jy_wrapper.py sync-favorite-text-assets --json
```

同步结果会写到本机覆盖文件：

- `data/favorite_text_templates.local.csv`
- `data/favorite_flower_texts.local.csv`

这两份文件已被 `.gitignore` 忽略，不参与仓库提交。

## 基线样例

仓库内保留两份可跟踪基线样例：

- `data/favorite_text_templates.csv`
- `data/favorite_flower_texts.csv`

它们用于测试、示例和跨机器最小可用行为。

## 搜索行为

统一搜索入口会同时读取：

- 仓库基线样例 CSV
- 本机 `.local.csv` 覆盖文件
- 既有 `text_templates.csv`
- 既有 `cloud_text_styles.csv`

排序时会优先：

1. `favorite_flag=1` 的记录
2. `.local.csv` 本机记录
3. 本地草稿扫描 / 本地缓存来源

## 当前接入点

- CLI:
  - `search-assets`
  - `resolve-asset`
  - `sync-favorite-text-assets`
- adapter / MCP:
  - `search_assets`
  - `resolve_asset`
  - `sync_favorite_text_assets`

## 口播模板池

`make_tmall_c2608.py` 的本地模板池现在会先吸收收藏/白名单索引，再回退到草稿扫描和脚本内置模板池。

这一步目前只影响“允许使用模板”的花字 cue。
被 `force_plain_text=True` 固定为纯文本的 cue，暂时仍不会自动切成模板字。
