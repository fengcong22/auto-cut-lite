from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import shutil
import string
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .config import PROJECT_ROOT, load_env_file

VOLC_ASR_ADAPTER_VERSION = "auto-cut-volc-asr-v3"
# The full Auto-Cut workflow uses the recording ASR resource, which accepts
# the locally extracted source audio directly. Keep lite mode on the same path.
DEFAULT_RESOURCE_ID = "volc.bigasr.auc"
DEFAULT_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
DEFAULT_QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
SUCCESS_STATUS = "20000000"
PROCESSING_STATUS_CODES = {"20000001", "20000002"}
MAX_AUDIO_BYTES = 50 * 1024 * 1024
_EVIDENCE_KEY = "_auto_cut_evidence"
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".ts"}


class VolcAsrError(RuntimeError):
    """Raised when configuration or the Volcengine ASR service is unusable."""


@dataclass(frozen=True)
class VolcAsrConfig:
    app_id: str = ""
    access_token: str = field(default="", repr=False)
    resource_id: str = DEFAULT_RESOURCE_ID
    submit_url: str = DEFAULT_SUBMIT_URL
    query_url: str = DEFAULT_QUERY_URL
    uid: str = "auto-cut"
    api_key: str = field(default="", repr=False)

    @property
    def authentication_mode(self) -> str:
        return "new_console_api_key" if self.api_key else "legacy_app_id_access_token"


def _config_value(env_values: dict[str, str], *names: str) -> str:
    for name in names:
        value = os.environ.get(name) or env_values.get(name)
        if value:
            cleaned = value.strip()
            if not cleaned or any(ord(character) < 32 for character in cleaned):
                continue
            return cleaned
    return ""


def _validate_endpoint(value: str, field_name: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise VolcAsrError(f"Invalid {field_name}; use an HTTPS endpoint")
    return value


def load_volc_asr_config(env_path: str | Path = PROJECT_ROOT / ".env") -> VolcAsrConfig:
    env_values = load_env_file(env_path)
    app_id = _config_value(env_values, "VOLC_ASR_APP_ID", "VOLC_SPEECH_APP_ID")
    access_token = _config_value(env_values, "VOLC_ASR_ACCESS_TOKEN", "VOLC_SPEECH_ACCESS_TOKEN")
    api_key = _config_value(env_values, "VOLC_ASR_API_KEY", "VOLC_SPEECH_API_KEY")
    resource_id = (
        _config_value(env_values, "VOLC_ASR_RESOURCE_ID", "VOLC_SPEECH_RESOURCE_ID")
        or DEFAULT_RESOURCE_ID
    )
    submit_url = _config_value(env_values, "VOLC_ASR_SUBMIT_URL") or DEFAULT_SUBMIT_URL
    query_url = _config_value(env_values, "VOLC_ASR_QUERY_URL") or DEFAULT_QUERY_URL
    uid = _config_value(env_values, "VOLC_ASR_UID") or "auto-cut"

    if api_key and (app_id or access_token):
        raise VolcAsrError("Configure either a new-console API key or legacy credentials, not both")
    if not api_key:
        missing = []
        if not app_id:
            missing.append("VOLC_ASR_APP_ID")
        if not access_token:
            missing.append("VOLC_ASR_ACCESS_TOKEN")
        if missing:
            raise VolcAsrError(f"Missing Volc ASR config: {', '.join(missing)}")

    return VolcAsrConfig(
        app_id=app_id,
        access_token=access_token,
        resource_id=resource_id,
        submit_url=_validate_endpoint(submit_url, "VOLC_ASR_SUBMIT_URL"),
        query_url=_validate_endpoint(query_url, "VOLC_ASR_QUERY_URL"),
        uid=uid,
        api_key=api_key,
    )


def _audio_format(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"wav", "mp3", "m4a", "aac", "ogg", "opus", "flac", "mp4", "mov", "m4v"}:
        return suffix
    return "wav"


def _redact(value: object, config: VolcAsrConfig | None = None) -> str:
    text = str(value)
    if config is not None:
        for secret in (
            getattr(config, "api_key", ""),
            getattr(config, "access_token", ""),
            getattr(config, "app_id", ""),
        ):
            if secret:
                text = text.replace(secret, "<redacted>")
    return text[:1000]


def _http_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> tuple[dict[str, Any], dict[str, str], int]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                raise VolcAsrError("Volc ASR returned invalid JSON") from exc
            if not isinstance(parsed, dict):
                raise VolcAsrError("Volc ASR returned a non-object JSON response")
            return parsed, dict(response.headers.items()), response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            parsed = {"message": raw[:1000]}
        if not isinstance(parsed, dict):
            parsed = {"message": "non-object error response"}
        return parsed, dict(exc.headers.items()), exc.code
    except urllib.error.URLError as exc:
        raise VolcAsrError(f"Volc ASR network error: {_redact(exc.reason)}") from exc


def _api_headers(
    config: VolcAsrConfig, request_id: str, *, sequence: str | None = None
) -> dict[str, str]:
    api_key = getattr(config, "api_key", "")
    app_id = getattr(config, "app_id", "")
    access_token = getattr(config, "access_token", "")
    if api_key and (app_id or access_token):
        raise VolcAsrError("Volc ASR authentication modes cannot be mixed")
    headers = {
        "X-Api-Resource-Id": config.resource_id,
        "X-Api-Request-Id": request_id,
    }
    if api_key:
        headers["X-Api-Key"] = api_key
    elif app_id and access_token:
        headers["X-Api-App-Key"] = app_id
        headers["X-Api-Access-Key"] = access_token
    else:
        raise VolcAsrError("Volc ASR authentication is not configured")
    if sequence is not None:
        headers["X-Api-Sequence"] = sequence
    return headers


def _header_value(headers: dict[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return str(value)
    return ""


def _read_audio_bytes(audio_path: Path) -> bytes:
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    if not audio_path.is_file():
        raise VolcAsrError("Audio input is not a regular file")
    size = audio_path.stat().st_size
    if size <= 0:
        raise VolcAsrError("Audio input is empty")
    if size > MAX_AUDIO_BYTES:
        raise VolcAsrError(f"Audio input exceeds {MAX_AUDIO_BYTES} bytes")
    return audio_path.read_bytes()


def extract_audio_from_video(
    video_path: Path,
    output_path: Path,
    *,
    ffmpeg_bin: str | None = None,
) -> Path:
    """Extract compact local audio for the shared ASR path without cloud storage."""

    if not video_path.is_file():
        raise FileNotFoundError(f"Video input not found: {video_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    binary = ffmpeg_bin or os.environ.get("AUDIO_SOUND_FFMPEG") or shutil.which("ffmpeg")
    if not binary:
        raise VolcAsrError("FFmpeg is required to extract audio from video")
    command = [
        str(binary),
        "-y",
        "-hide_banner",
        "-nostdin",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        str(output_path),
    ]
    completed = subprocess.run(command, capture_output=True)
    if completed.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        detail_bytes = completed.stderr or completed.stdout
        detail = detail_bytes.decode("utf-8", errors="replace").strip() if detail_bytes else "FFmpeg extraction failed"
        raise VolcAsrError(f"Unable to extract ASR audio from video: {detail[-1000:]}")
    return output_path


def submit_audio(
    audio_path: Path,
    *,
    config: VolcAsrConfig,
    request_id: str,
    timeout_seconds: float,
    audio_bytes: bytes | None = None,
) -> None:
    submitted_bytes = _read_audio_bytes(audio_path) if audio_bytes is None else audio_bytes

    payload = {
        "user": {"uid": config.uid},
        "audio": {
            "data": base64.b64encode(submitted_bytes).decode("ascii"),
            "format": _audio_format(audio_path),
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
        },
    }
    response, headers, http_status = _http_json(
        config.submit_url,
        headers=_api_headers(config, request_id, sequence="-1"),
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    status = _header_value(headers, "X-Api-Status-Code")
    if http_status != 200 or status != SUCCESS_STATUS:
        message = _header_value(headers, "X-Api-Message") or response.get("message")
        code = status or response.get("code") or response.get("header", {}).get("code")
        raise VolcAsrError(
            f"Volc ASR submit failed: http={http_status} code={_redact(code)} "
            f"message={_redact(message, config)}"
        )


def query_result(
    *,
    config: VolcAsrConfig,
    request_id: str,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str, str]:
    response, headers, http_status = _http_json(
        config.query_url,
        headers=_api_headers(config, request_id),
        payload={},
        timeout_seconds=timeout_seconds,
    )
    status = _header_value(headers, "X-Api-Status-Code")
    message = _header_value(headers, "X-Api-Message")
    if http_status != 200:
        raise VolcAsrError(
            f"Volc ASR query failed: http={http_status} code={_redact(status)} "
            f"message={_redact(message, config)}"
        )
    return response, status, message


def run_volc_asr(
    audio_path: Path,
    *,
    config: VolcAsrConfig,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 2.0,
    max_wait_seconds: float = 120.0,
) -> dict[str, Any]:
    if timeout_seconds <= 0 or poll_interval_seconds < 0 or max_wait_seconds <= 0:
        raise ValueError("ASR timeout values are invalid")
    audio_bytes = _read_audio_bytes(audio_path)
    input_sha256 = hashlib.sha256(audio_bytes).hexdigest()
    request_id = str(uuid.uuid4())
    submit_audio(
        audio_path,
        config=config,
        request_id=request_id,
        timeout_seconds=timeout_seconds,
        audio_bytes=audio_bytes,
    )
    deadline = time.monotonic() + max_wait_seconds
    last_status = ""
    last_message = ""
    first_poll = True
    while time.monotonic() <= deadline:
        if not first_poll and poll_interval_seconds:
            time.sleep(poll_interval_seconds)
        first_poll = False
        response, status, message = query_result(
            config=config,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )
        last_status, last_message = status, message
        if status == SUCCESS_STATUS and response:
            attributable_response = dict(response)
            attributable_response[_EVIDENCE_KEY] = {
                "input_sha256": input_sha256,
                "service_job_id": request_id,
                "resource_id": config.resource_id,
                "adapter_version": VOLC_ASR_ADAPTER_VERSION,
            }
            return attributable_response
        if status and status not in PROCESSING_STATUS_CODES:
            raise VolcAsrError(
                f"Volc ASR query failed: code={_redact(status)} "
                f"message={_redact(message, config)}"
            )
    raise TimeoutError(
        f"Timed out waiting for Volc ASR result: code={_redact(last_status)} "
        f"message={_redact(last_message, config)}"
    )


def _seconds(value_ms: Any) -> float:
    return round(float(value_ms) / 1000.0, 6)


def normalize_result(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload.get(_EVIDENCE_KEY)
    if not isinstance(evidence, dict):
        raise VolcAsrError("Volc ASR result evidence is missing")
    input_sha256 = str(evidence.get("input_sha256") or "").lower()
    service_job_id = str(evidence.get("service_job_id") or "")
    resource_id = str(evidence.get("resource_id") or "")
    adapter_version = str(evidence.get("adapter_version") or "")
    if (
        len(input_sha256) != 64
        or any(character not in "0123456789abcdef" for character in input_sha256)
        or not service_job_id
        or not resource_id
        or adapter_version != VOLC_ASR_ADAPTER_VERSION
    ):
        raise VolcAsrError("Volc ASR result evidence is invalid")
    service_payload = {key: value for key, value in payload.items() if key != _EVIDENCE_KEY}
    service_result_sha256 = hashlib.sha256(
        json.dumps(
            service_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    audio_info = payload.get("audio_info") if isinstance(payload.get("audio_info"), dict) else {}
    utterances_payload = (
        result.get("utterances") if isinstance(result.get("utterances"), list) else []
    )
    utterances: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    discarded_word_rows: list[dict[str, Any]] = []

    for utterance_index, utterance in enumerate(utterances_payload):
        if not isinstance(utterance, dict):
            continue
        utterance_words: list[dict[str, Any]] = []
        raw_words = utterance.get("words")
        if not isinstance(raw_words, list):
            raise VolcAsrError("Volc ASR word timing evidence is invalid")
        for word_index, word in enumerate(raw_words):
            if not isinstance(word, dict):
                raise VolcAsrError("Volc ASR word timing evidence is invalid")
            raw_start = word.get("start_time", word.get("start"))
            raw_end = word.get("end_time", word.get("end"))
            if raw_start is None or raw_end is None:
                raise VolcAsrError("Volc ASR word timing evidence is invalid")
            raw_text = str(word.get("text", ""))
            try:
                whitespace_sentinel = (
                    not raw_text.strip()
                    and float(raw_start) < 0
                    and float(raw_end) < 0
                )
            except (TypeError, ValueError, OverflowError):
                whitespace_sentinel = False
            if whitespace_sentinel:
                discarded_word_rows.append(
                    {
                        "utterance_index": utterance_index,
                        "word_index": word_index,
                        "text": raw_text,
                        "raw_start": raw_start,
                        "raw_end": raw_end,
                        "reason": "service_whitespace_sentinel",
                    }
                )
                continue
            try:
                start = _seconds(raw_start)
                end = _seconds(raw_end)
            except (TypeError, ValueError, OverflowError) as exc:
                raise VolcAsrError("Volc ASR word timing evidence is invalid") from exc
            item: dict[str, Any] = {
                "text": raw_text,
                "start": start,
                "end": end,
            }
            if "confidence" in word:
                item["confidence"] = word["confidence"]
            utterance_words.append(item)
            words.append(item)
        utterances.append(
            {
                "text": str(utterance.get("text", "")),
                "start": _seconds(utterance.get("start_time", utterance.get("start", 0))),
                "end": _seconds(utterance.get("end_time", utterance.get("end", 0))),
                "words": utterance_words,
            }
        )

    if not words:
        raise VolcAsrError("Volc ASR word timing evidence is empty")
    previous_start = -1.0
    previous_end = -1.0
    for word in words:
        start = float(word["start"])
        end = float(word["end"])
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or start < previous_start
            or end < previous_end
        ):
            raise VolcAsrError("Volc ASR word timing evidence is invalid or nonmonotonic")
        previous_start = start
        previous_end = end

    additions = result.get("additions") if isinstance(result.get("additions"), dict) else {}
    duration_ms = audio_info.get("duration") or additions.get("duration")
    duration_seconds = _seconds(duration_ms) if duration_ms not in (None, "") else None
    return {
        "provider": "volc_asr",
        "resource_id": resource_id,
        "adapter_version": adapter_version,
        "input_sha256": input_sha256,
        "service_job_id": service_job_id,
        "service_result_sha256": service_result_sha256,
        "text": str(result.get("text", "")),
        "audio_duration_seconds": duration_seconds,
        "utterances": utterances,
        "words": words,
        "word_timing_count": len(words),
        "discarded_word_rows": discarded_word_rows,
    }


_PUNCTUATION = set(string.punctuation) | set(
    "，。！？；：、‘’“”（）【】《》〈〉「」『』〔〕…—～·\t\r\n "
)


def _search_text(value: str) -> str:
    return "".join(char for char in value if char not in _PUNCTUATION)


def find_phrase_matches(
    words: Sequence[dict[str, Any]],
    phrase: str,
    *,
    anchor_start: float | None = None,
    anchor_end: float | None = None,
) -> list[dict[str, Any]]:
    target = _search_text(phrase)
    if not target:
        return []

    haystack_parts: list[str] = []
    char_to_word: list[int] = []
    for index, word in enumerate(words):
        text = _search_text(str(word.get("text", "")))
        for char in text:
            haystack_parts.append(char)
            char_to_word.append(index)
    haystack = "".join(haystack_parts)

    matches: list[dict[str, Any]] = []
    cursor = 0
    while True:
        found = haystack.find(target, cursor)
        if found < 0:
            break
        end_character = found + len(target) - 1
        if found >= len(char_to_word) or end_character >= len(char_to_word):
            break
        start_word_index = char_to_word[found]
        end_word_index = char_to_word[end_character]
        selected_words = list(words[start_word_index : end_word_index + 1])
        if not selected_words:
            cursor = found + 1
            continue
        start = float(selected_words[0].get("start", 0.0))
        end = float(selected_words[-1].get("end", 0.0))
        score = 0.0
        if anchor_start is not None and anchor_end is not None:
            overlap = max(0.0, min(end, anchor_end) - max(start, anchor_start))
            center = (start + end) / 2.0
            anchor_center = (anchor_start + anchor_end) / 2.0
            score = overlap - abs(center - anchor_center) * 0.01
        matches.append(
            {
                "phrase": phrase,
                "start": start,
                "end": end,
                "text": "".join(str(word.get("text", "")) for word in selected_words),
                "word_start_index": start_word_index,
                "word_end_index": end_word_index,
                "anchor_score": score,
            }
        )
        cursor = found + 1

    if anchor_start is not None and anchor_end is not None:
        matches.sort(key=lambda item: item["anchor_score"], reverse=True)
    return matches


def parse_anchor(value: str | None) -> tuple[float | None, float | None]:
    if not value:
        return None, None
    if "," not in value:
        raise ValueError("Anchor must be START,END")
    start_raw, end_raw = value.split(",", 1)
    return float(start_raw), float(end_raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio-volc-word-align",
        description="Run Volcengine ASR and emit normalized word-level timings.",
    )
    parser.add_argument("audio", help="Input audio/video file.")
    parser.add_argument("--env", default=str(PROJECT_ROOT / ".env"), help="Path to .env config.")
    parser.add_argument("--output", default=None, help="Write normalized JSON to this path.")
    parser.add_argument("--raw-output", default=None, help="Optionally write raw service JSON.")
    parser.add_argument("--phrase", action="append", default=[], help="Phrase to locate.")
    parser.add_argument("--anchor", default=None, help="Optional rough anchor START,END.")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--max-wait-seconds", type=float, default=120.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        for output_path in (args.output, args.raw_output):
            if output_path:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        config = load_volc_asr_config(args.env)
        input_path = Path(args.audio)
        with tempfile.TemporaryDirectory(prefix="auto-cut-volc-asr-") as temp_dir:
            asr_path = input_path
            if input_path.suffix.casefold() in _VIDEO_SUFFIXES:
                asr_path = extract_audio_from_video(
                    input_path,
                    Path(temp_dir) / f"{input_path.stem}.asr.m4a",
                )
            raw_payload = run_volc_asr(
                asr_path,
                config=config,
                timeout_seconds=args.timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
                max_wait_seconds=args.max_wait_seconds,
            )
        normalized = normalize_result(raw_payload)
        anchor_start, anchor_end = parse_anchor(args.anchor)
        if args.phrase:
            normalized["phrase_matches"] = {
                phrase: find_phrase_matches(
                    normalized["words"],
                    phrase,
                    anchor_start=anchor_start,
                    anchor_end=anchor_end,
                )
                for phrase in args.phrase
            }
        if args.raw_output:
            Path(args.raw_output).write_text(
                json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        output_text = json.dumps(normalized, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(output_text, encoding="utf-8")
        print(output_text)
        return 0
    except (OSError, TimeoutError, ValueError, VolcAsrError) as exc:
        print(f"audio-volc-word-align failed: {_redact(exc)}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
