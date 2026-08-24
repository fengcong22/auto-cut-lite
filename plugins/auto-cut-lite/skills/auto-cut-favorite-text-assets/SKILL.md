---
name: auto-cut-favorite-text-assets
description: 使用剪映收藏夹里的花字效果、花字模板、文字模板时调用。适用于用户明确要求“直接调用我收藏夹里的花字/模板”“优先用我收藏的花字效果”“先从收藏夹找花字模板/文字模板”的场景。该 skill 负责先同步本机收藏与缓存资产，再区分两条能力边界：收藏夹花字效果可直接通过 `flower_query` 调用；官方文字模板 `template_query` 只有在本机存在 `JY_TEXT_TEMPLATE_ADAPTER` 时才能直接落草稿，否则只能解析资源或要求提供 `template_payload(_path)`。
---

# 剪映收藏花字与模板

## Packaged Runtime

Before executing this skill from the distributed plugin, read the [portable runtime contract](../auto-cut-lite/references/portable-runtime.md). Use the installed isolated runtime and `workflow_mode=lite`; do not fall back to a source checkout or another Auto-Cut installation.


用这条 skill 处理“直接调用我收藏夹里的花字/模板”的请求，不要把“搜得到”和“真正能落到草稿里”混为一谈。

## 核心结论

- 收藏夹花字效果：可以直接调用。
- 官方文字模板：默认不能直接调用，除非本机已配置 `JY_TEXT_TEMPLATE_ADAPTER`。

当前仓库已验证过的直接调用结论：

1. 先执行 `sync-favorite-text-assets`，把本机剪映收藏、草稿扫描结果和本地缓存同步到 `data/*.local.csv`。
2. `flower_query` 会优先命中这些本机同步记录，可以直接生成可编辑草稿。
3. `template_query` 虽然也能命中收藏/缓存索引，但真正写入官方 `text_template` 材质时仍依赖 adapter；没有 adapter 就会报错。

## 标准流程

1. 先同步收藏资产：

```powershell
python scripts/jy_wrapper.py sync-favorite-text-assets --json
```

2. 再区分用户要的是哪一类：

- 花字效果：走 `add-complex-text --flower-query`
- 官方文字模板：走 `add-text-template --template-query`

3. 必须做一次真实调用验证，不要只停在 `search-assets` 或 `resolve-asset`。

## 花字效果调用

当用户要“收藏夹花字”“我收藏的花字效果”“红色花字1 这种花字”时，优先走这条路径：

```powershell
python scripts/jy_wrapper.py add-complex-text `
  --name __probe_favorite_flower `
  --drafts-root tmp `
  --text 测试花字 `
  --start-time 0s `
  --duration 2s `
  --flower-query "红色花字1" `
  --json
```

判定成功的最低标准：

- 命令返回 `ok: true`
- `resolved_assets.flower` 来自 `favorite_flower_texts.local.csv`、`favorite_flower_texts.csv`、本机草稿扫描或本机 artistEffect 缓存之一
- 生成了可编辑草稿目录

## 官方文字模板调用

当用户要“收藏夹文字模板”“官方 text template”“template_query 直接套模板”时，先按下面方式测试：

```powershell
python scripts/jy_wrapper.py add-text-template `
  --name __probe_favorite_template `
  --drafts-root tmp `
  --text 测试模板 `
  --start-time 0s `
  --duration 2s `
  --template-query "收藏夹模板白名单草稿" `
  --json
```

判定规则：

- 如果成功，说明当前机器可直接调用收藏夹官方文字模板。
- 如果报错 `text template adapter not available; provide template_payload or template_payload_path`，说明当前机器只能搜到模板，不能直接落草稿。

## 报告口径

向用户汇报时，必须明确区分：

- 已经能直接调用收藏夹花字效果
- 收藏夹官方文字模板是否也能直接调用
- 本次实际测试命令是否成功
- 生成的草稿路径

不要笼统说“收藏夹模板可以用了”，除非你已经分别验证过对应路径。

## 本仓库已确认的现状

在当前仓库环境下，已确认：

- `sync-favorite-text-assets` 可同步本机收藏与缓存资产
- 收藏夹花字效果可通过 `flower_query` 直接调用
- 收藏夹官方文字模板在没有 `JY_TEXT_TEMPLATE_ADAPTER` 时不能仅靠 `template_query` 直接调用

如果未来补上 adapter，更新这条 skill，而不是只把结论留在聊天里。
