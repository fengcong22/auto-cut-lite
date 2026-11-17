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

AI_INTERPRETER_VERSION = "lite-ai-review-interpreter-v3-schema-checked"
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


def context_window(
    rough_start: float | None, rough_end: float | None
) -> tuple[float, float] | None:
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
                    self.entries = {
                        str(k): dict(v) for k, v in entries.items() if isinstance(v, dict)
                    }

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


_JSON_INSTRUCTION = (
    "\n\n只输出一个 JSON 对象，不要输出其他文字。字段：action（delete_speech 或 "
    "not_applicable）、word_start_index（整数）、word_end_index（整数）、confidence（high、"
    "medium 或 low）、rationale（字符串）。"
)


def _request_variants(model: str, prompt: str) -> list[tuple[str, dict[str, Any]]]:
    """Requests from richest to most compatible.

    API relays often reject newer parameters with a 400; each later variant
    drops one of them so a relay that speaks the basic Messages API still works.
    """

    # The field list is always in the prompt: some relays accept but ignore
    # ``output_config.format``, and every reply is schema-checked anyway.
    base = {
        "model": model,
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt + _JSON_INSTRUCTION}],
    }
    structured = {
        "effort": DEFAULT_EFFORT,
        "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
    }
    return [
        (
            "beta_fallback_structured",
            {
                **base,
                "betas": ["server-side-fallback-2026-07-01"],
                "fallbacks": "default",
                "output_config": structured,
            },
        ),
        ("structured", {**base, "output_config": structured}),
        ("plain_json", base),
    ]


def _is_unsupported_request(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) in {400, 404, 422}


def _extract_json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        value = None
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                value = json.loads(text[start : end + 1])
            except ValueError:
                value = None
    return value if isinstance(value, dict) else None


def _schema_valid(data: Mapping[str, Any]) -> bool:
    if set(data) - set(OUTPUT_SCHEMA["properties"]) or not set(OUTPUT_SCHEMA["required"]) <= set(
        data
    ):
        return False
    if data.get("action") not in {"delete_speech", "not_applicable"}:
        return False
    if data.get("confidence") not in {"high", "medium", "low"}:
        return False
    for key in ("word_start_index", "word_end_index"):
        if not isinstance(data.get(key), int) or isinstance(data.get(key), bool):
            return False
    return isinstance(data.get("rationale"), str)


def _call_claude(
    client: Any, model: str, prompt: str, *, first_variant: int = 0
) -> tuple[dict[str, Any], int]:
    """Ask once per variant until a schema-valid answer arrives.

    Returns the proposal and the index of the variant that produced it, so the
    caller can start there next time instead of re-paying for ignored options.
    """

    variants = _request_variants(model, prompt)
    last_error: Exception | None = None
    last_status = "invalid_json"
    for index in range(first_variant, len(variants)):
        variant, request = variants[index]
        try:
            if "betas" in request:
                response = client.beta.messages.create(**request)
            else:
                response = client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001 - downgrade only on request-shape errors
            if _is_unsupported_request(exc):
                last_error = exc
                continue
            raise
        if response.stop_reason == "refusal":
            return {
                "action": "not_applicable",
                "status": "refused",
                "request_variant": variant,
            }, index
        if response.stop_reason == "max_tokens":
            return {
                "action": "not_applicable",
                "status": "truncated",
                "request_variant": variant,
            }, index
        text = "".join(
            str(getattr(block, "text", "") or "")
            for block in response.content
            if getattr(block, "type", "") == "text"
        )
        data = _extract_json_object(text)
        if data is None or not _schema_valid(data):
            last_status = "invalid_json" if data is None else "schema_mismatch"
            continue
        data["status"] = "answered"
        data["request_variant"] = variant
        data["model"] = str(getattr(response, "model", model) or model)
        data["request_id"] = str(getattr(response, "_request_id", "") or "")
        return data, index
    if last_error is not None and last_status == "invalid_json":
        raise last_error
    return {"action": "not_applicable", "status": last_status}, first_variant


def make_resolver(
    client: Any,
    *,
    model: str = DEFAULT_MODEL,
    cache: ProposalCache | None = None,
) -> AiResolver:
    """Return a resolver that asks Claude once per unique request."""

    cache = cache or ProposalCache(None)
    state = {"first_variant": 0}

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
            proposal, used_variant = _call_claude(
                client, model, prompt, first_variant=state["first_variant"]
            )
            if proposal.get("status") == "answered":
                state["first_variant"] = used_variant
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
    # The Auto-Cut config file wins over the process environment, so a job
    # never silently picks up another tool's provider settings.
    merged = {
        **{k: v for k, v in os.environ.items() if k.startswith(("AUTO_CUT_", "ANTHROPIC_"))},
        **values,
    }
    if not any(_truthy(merged.get(key)) for key in _ENABLE_KEYS):
        return None
    try:
        import anthropic  # noqa: PLC0415 - optional dependency
    except ImportError:
        return None
    options: dict[str, Any] = {}
    if merged.get("ANTHROPIC_API_KEY"):
        options["api_key"] = merged["ANTHROPIC_API_KEY"]
    if merged.get("ANTHROPIC_AUTH_TOKEN"):
        # Some relays expect ``Authorization: Bearer`` instead of ``x-api-key``.
        options["auth_token"] = merged["ANTHROPIC_AUTH_TOKEN"]
    if merged.get("ANTHROPIC_BASE_URL"):
        options["base_url"] = merged["ANTHROPIC_BASE_URL"].strip()
    try:
        client = anthropic.Anthropic(**options)
    except Exception:  # noqa: BLE001 - missing credentials disable the feature
        return None
    model = str(merged.get(_MODEL_KEY) or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return make_resolver(client, model=model, cache=ProposalCache(cache_path))


def identity_from_environment(env_values: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Secret-free identity of the AI configuration, for phase cache keys."""

    values = dict(env_values or {})
    merged = {**{k: v for k, v in os.environ.items() if k.startswith("AUTO_CUT_")}, **values}
    enabled = any(_truthy(merged.get(key)) for key in _ENABLE_KEYS)
    model = str(merged.get(_MODEL_KEY) or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    from urllib.parse import urlparse  # noqa: PLC0415

    base_url = str(
        values.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or ""
    ).strip()
    endpoint = urlparse(base_url).netloc if base_url else "api.anthropic.com"
    return {
        "enabled": enabled,
        "model": model if enabled else "",
        "endpoint_host": endpoint if enabled else "",
        "interpreter_version": AI_INTERPRETER_VERSION if enabled else "",
    }
