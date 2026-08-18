# Adapter Surfaces

This repository now exposes the existing `jy_wrapper.py` editing surface through lightweight HTTP and MCP adapters.

The adapters are intentionally thin:

- they reuse the existing `cmd_*` write paths
- they keep `JyProject + patch mode + asset search` as the only draft engine
- they do not introduce a second protocol builder or in-memory draft runtime

## HTTP Adapter

Entry point:

- `scripts/jy_http_server.py`

Start it:

```bash
python scripts/jy_http_server.py --host 127.0.0.1 --port 8765
```

Routes:

- `GET /health`
- `GET /tools`
- `POST /invoke`
- `POST /tools/<tool_name>`

Examples:

```bash
curl http://127.0.0.1:8765/tools
```

```bash
curl -X POST http://127.0.0.1:8765/invoke ^
  -H "Content-Type: application/json" ^
  -d "{\"tool\":\"search_assets\",\"arguments\":{\"query\":\"circle\",\"category\":\"mask\"}}"
```

```bash
curl -X POST http://127.0.0.1:8765/tools/add_image ^
  -H "Content-Type: application/json" ^
  -d "{\"name\":\"HttpImage\",\"drafts_root\":\"S:/drafts\",\"image_path\":\"C:/assets/cover.png\",\"background_blur\":2}"
```

## MCP Adapter

Entry point:

- `scripts/jy_mcp_server.py`

Start it over stdio:

```bash
python scripts/jy_mcp_server.py
```

Supported MCP methods:

- `initialize`
- `tools/list`
- `tools/call`

Example MCP client config:

```json
{
  "mcpServers": {
    "capcut-mate": {
      "command": "python",
      "args": ["scripts/jy_mcp_server.py"],
      "cwd": "/path/to/Auto-Cut"
    }
  }
}
```

## Tool Surface

Shared tool registry lives in:

- `scripts/jy_adapter_core.py`

Current tools:

- `detect_env`
- `smoke_test`
- `self_check`
- `search_assets`
- `resolve_asset`
- `list_segments`
- `draft_info`
- `list_materials`
- `show_material`
- `list_tracks`
- `list_texts`
- `show_segment`
- `add_sticker`
- `add_mask`
- `add_rich_text`
- `import_srt`
- `export_srt`
- `text_ranges`
- `set_text`
- `shift_segment`
- `trim_segment`
- `set_speed`
- `set_volume`
- `set_opacity`
- `shift_all`
- `batch_edit`
- `cut_timeline`
- `add_keyframes`
- `add_filter`
- `add_image`
- `add_effect`
- `add_video_effect`
- `add_face_effect`
- `add_audio_effect`
- `add_audio_fade`
- `attach_material`
- `add_complex_text`
- `add_green_screen`
- `add_text_template`
- `save_template`
- `apply_template`
- `apply_zoom`

## External Agent Usage

Recommended integration choices:

- **Codex / MCP-native agents**: use the MCP adapter
- **Coze / Dify / N8N / generic workflow tools**: use the HTTP adapter
- **Local Python automation**: keep calling `JyProject` directly

Recommended first call on a new machine:

```bash
curl -X POST http://127.0.0.1:8765/tools/self_check ^
  -H "Content-Type: application/json" ^
  -d "{\"cleanup\":true}"
```

Recommended adapter flow:

1. `self_check`
2. `search_assets` / `resolve_asset`
3. one of the write tools such as `add_rich_text`, `add_filter`, `add_video_effect`, `add_face_effect`, `attach_material`, `add_complex_text`, or `add_green_screen`
4. use `add_text_template` when the user wants the official text-template resource surface rather than saved-template reuse

If `self_check` returns `usable=false`, do not start larger edit tasks yet.

## Notes

- The adapter returns the same `make_result()` envelope shape used by the CLI.
- Tool additions should be registered once in `jy_adapter_core.py` so both HTTP and MCP expose the same behavior.
- The adapter does not yet add auth, queueing, or remote asset download orchestration.
- `add_text_template` accepts the same payload as the CLI surface: `template_id` / `template_query`, `text` or `texts_json`, `start_time`, `duration`, optional `template_payload_path`.
- If `JY_TEXT_TEMPLATE_ADAPTER` is not configured on the machine, adapter callers must provide `template_payload_path` for `add_text_template`.
- `set_text` also accepts `texts_json` for multi-slot text-template segments.
