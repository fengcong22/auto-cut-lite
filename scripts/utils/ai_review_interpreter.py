"""Claude-assisted locating of spoken deletions that deterministic matching missed.

The model only *proposes* which ASR words a review comment refers to.  It never
supplies a time: the proposal is a pair of indexes into the authoritative
source-ASR word list, and ``review_audio_precision`` re-verifies every proposal
with deterministic gates (continuity, review-time proximity, text agreement)
before it may become an editable cut.  Anything that fails stays label-only.

Proposals are cached by a content digest so a resumed job reuses the same
answer instead of asking again.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

AI_INTERPRETER_VERSION = "lite-ai-review-interpreter-v1"
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
CONTEXT_BEFORE_SECONDS = 20.0
CONTEXT_AFTER_SECONDS = 40.0
_ENABLE_KEYS = ("AUTO_CUT_AI_REVIEW", "AUTO_CUT_AI_REVIEW_INTERPRETER")
_MODEL_KEY = "AUTO_CUT_AI_REVIEW_MODEL"

SYSTEM_PROMPT = """\
你在协助一个视频剪辑工具定位老师的口播删除意见。

你会收到一条审核意见（老师写的原文、评论时间）和这段时间附近的语音识别（ASR）逐字结果。
每个字带有编号和时间。请判断老师要删除的口播对应 ASR 中的哪一段连续文字。

判断时注意：
- 老师的写法常常是概括：可能省略语气词（啊、呃、嗯）、口头连接词（然后、那个、的），
  也可能用“……”省略中间整段。ASR 也可能把字识别错（同音字、形近字）。
- 如果意见只给了时间段、没写原话（例如“00:00-00:08，删除”），意思是删除该时间段内的口播；
  请选出这段时间内的完整语句，不要切断词语。
- 只选老师要删的内容，不要把前后要保留的字带进来。
- 如果附近同样的话出现了多次，选离评论时间最近、和上下文最吻合的那一处。
- 如果这条意见不是删除口播（例如删除停顿、调整动画、移动小手），或者你无法确定位置，
  返回 action = "not_applicable"，不要猜。

只返回规定格式的 JSON。"""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["delete_speech", "not_applicable"]},
        "word_start_index": {"type": "integer"},
        "word_end_index": {"type": "integer"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "rationale": {"type": "string"},
    },
    "required": ["action", "word_start_index", "word_end_index", "confidence", "rationale"],
    "additionalProperties": False,
}

AiResolver = Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]], tuple[float, float]], dict]


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on", "claude"}


def context_window(rough_start: float | None, rough_end: float | None) -> tuple[float, float] | None:
    if rough_start is None:
        return None
    end = rough_end if rough_end is not None else rough_start
    return max(0.0, float(rough_start) - CONTEXT_BEFORE_SECONDS), float(end) + CONTEXT_AFTER_SECONDS


def build_user_prompt(
    item: Mapping[str, Any],
    words: Sequence[Mapping[str, Any]],
    window: tuple[float, float],
) -> str:
    lines = []
    for index, word in enumerate(words):
        try:
            start = float(word["start"])
            end = float(word["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < window[0] or start > window[1]:
            continue
        lines.append(f"{index}\t{start:.2f}\t{end:.2f}\t{word.get('text') or ''}")
    return (
        f"审核意见原文：{item.get('source_text') or ''}\n"
        f"审核意见类型：{item.get('kind') or ''}\n\n"
        "ASR 逐字结果（编号\t开始秒\t结束秒\t文字）：\n" + "\n".join(lines)
    )


def proposal_cache_key(model: str, item: Mapping[str, Any], prompt: str) -> str:
    payload = json.dumps(
        {
            "version": AI_INTERPRETER_VERSION,
            "model": model,
            "system": SYSTEM_PROMPT,
            "item_id": str(item.get("id") or item.get("item_id") or ""),
            "prompt": prompt,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ProposalCache:
    """JSON file cache keyed by the full request digest."""

    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path) if path else None
        self.entries: dict[str, dict[str, Any]] = {}
        if self.path and self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = {}
            if isinstance(loaded, dict) and loaded.get("version") == AI_INTERPRETER_VERSION:
                entries = loaded.get("entries")
                if isinstance(entries, dict):
                    self.entries = {str(k): dict(v) for k, v in entries.items() if isinstance(v, dict)}

    def get(self, key: str) -> dict[str, Any] | None:
        value = self.entries.get(key)
        return dict(value) if value is not None else None

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        self.entries[key] = dict(value)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {"version": AI_INTERPRETER_VERSION, "entries": self.entries},
                ensure_ascii=False,
                indent=1,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def _call_claude(client: Any, model: str, prompt: str) -> dict[str, Any]:
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        output_config={
            "effort": DEFAULT_EFFORT,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        return {"action": "not_applicable", "status": "refused"}
    if response.stop_reason == "max_tokens":
        return {"action": "not_applicable", "status": "truncated"}
    text = next((block.text for block in response.content if block.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        return {"action": "not_applicable", "status": "invalid_json"}
    if not isinstance(data, dict):
        return {"action": "not_applicable", "status": "invalid_json"}
    data["status"] = "answered"
    data["model"] = str(getattr(response, "model", model) or model)
    data["request_id"] = str(getattr(response, "_request_id", "") or "")
    return data


def make_resolver(
    client: Any,
    *,
    model: str = DEFAULT_MODEL,
    cache: ProposalCache | None = None,
) -> AiResolver:
    """Return a resolver that asks Claude once per unique request."""

    cache = cache or ProposalCache(None)

    def resolve(
        item: Mapping[str, Any],
        words: Sequence[Mapping[str, Any]],
        window: tuple[float, float],
    ) -> dict[str, Any]:
        prompt = build_user_prompt(item, words, window)
        key = proposal_cache_key(model, item, prompt)
        cached = cache.get(key)
        if cached is not None:
            cached["cache_hit"] = True
            return cached
        try:
            proposal = _call_claude(client, model, prompt)
        except Exception as exc:  # noqa: BLE001 - any API failure keeps the item label-only
            return {
                "action": "not_applicable",
                "status": "api_error",
                "error": type(exc).__name__,
                "cache_hit": False,
            }
        proposal["cache_key"] = key
        cache.put(key, proposal)
        proposal["cache_hit"] = False
        return proposal

    return resolve


def resolver_from_environment(
    env_values: Mapping[str, str] | None = None,
    *,
    cache_path: str | os.PathLike[str] | None = None,
) -> AiResolver | None:
    """Build a resolver when the operator enabled it and credentials exist.

    Enabling is explicit (``AUTO_CUT_AI_REVIEW=1`` in the process environment
    or the Auto-Cut config ``.env``) because review text and ASR text are sent
    to the model provider.  Returns ``None`` when disabled or unavailable.
    """

    values = dict(env_values or {})
    merged = {**values, **{k: v for k, v in os.environ.items() if k.startswith(("AUTO_CUT_", "ANTHROPIC_"))}}
    if not any(_truthy(merged.get(key)) for key in _ENABLE_KEYS):
        return None
    try:
        import anthropic  # noqa: PLC0415 - optional dependency
    except ImportError:
        return None
    api_key = merged.get("ANTHROPIC_API_KEY") or None
    try:
        client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    except Exception:  # noqa: BLE001 - missing credentials disable the feature
        return None
    model = str(merged.get(_MODEL_KEY) or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return make_resolver(client, model=model, cache=ProposalCache(cache_path))


def identity_from_environment(env_values: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Secret-free identity of the AI configuration, for phase cache keys."""

    values = dict(env_values or {})
    merged = {**values, **{k: v for k, v in os.environ.items() if k.startswith("AUTO_CUT_")}}
    enabled = any(_truthy(merged.get(key)) for key in _ENABLE_KEYS)
    model = str(merged.get(_MODEL_KEY) or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return {
        "enabled": enabled,
        "model": model if enabled else "",
        "interpreter_version": AI_INTERPRETER_VERSION if enabled else "",
    }
