from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from audio_sound.volc_asr import (
    DEFAULT_RESOURCE_ID,
    VOLC_ASR_ADAPTER_VERSION,
    VolcAsrConfig,
    VolcAsrError,
    _api_headers,
    default_volc_env_path,
    find_phrase_matches,
    load_volc_asr_config,
    main,
    normalize_result,
    run_volc_asr,
    submit_audio,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_RECEIPT = REPO_ROOT / "docs" / "audio-sound" / "volc-asr-source-receipt.json"


def _payload() -> dict[str, object]:
    return {
        "audio_info": {"duration": 1200},
        "result": {
            "text": "自动剪辑，语音测试",
            "utterances": [
                {
                    "start_time": 260,
                    "end_time": 1060,
                    "text": "自动剪辑，语音测试",
                    "words": [
                        {"text": "自动", "start_time": 260, "end_time": 420, "confidence": 0},
                        {"text": "剪辑", "start_time": 420, "end_time": 620, "confidence": 0.98},
                        {"text": "语音", "start_time": 700, "end_time": 860, "confidence": 0.97},
                        {"text": "测试", "start_time": 860, "end_time": 1060, "confidence": 0.99},
                    ],
                }
            ],
        },
    }


def _payload_with_evidence() -> dict[str, object]:
    payload = _payload()
    payload["_auto_cut_evidence"] = {
        "input_sha256": "a" * 64,
        "service_job_id": "fixture-job-id",
        "resource_id": DEFAULT_RESOURCE_ID,
        "adapter_version": VOLC_ASR_ADAPTER_VERSION,
    }
    return payload


def test_load_config_requires_credentials_without_echoing_values(tmp_path: Path) -> None:
    with pytest.raises(VolcAsrError, match="VOLC_ASR_APP_ID, VOLC_ASR_ACCESS_TOKEN") as exc_info:
        load_volc_asr_config(tmp_path / ".env")

    assert "token-value" not in str(exc_info.value)


def test_default_env_path_stays_repository_local_for_source_checkout(tmp_path: Path) -> None:
    assert default_volc_env_path(project_root=tmp_path, local_app_data=tmp_path / "local") == (
        tmp_path / ".env"
    )


def test_default_env_path_uses_target_local_state_for_portable_plugin(tmp_path: Path) -> None:
    runtime = tmp_path / "auto-cut-lite" / "runtime"
    manifest = runtime.parent / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name":"auto-cut-lite"}\n', encoding="utf-8")
    local = tmp_path / "local"

    assert default_volc_env_path(project_root=runtime, local_app_data=local) == (
        local / "Auto-Cut" / "auto-cut-lite" / "config" / ".env"
    )


def test_load_config_supports_standard_names_and_resource_default(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "VOLC_ASR_APP_ID=app-id\nVOLC_ASR_ACCESS_TOKEN=token-value\n",
        encoding="utf-8",
    )

    config = load_volc_asr_config(env_path)

    assert config.app_id == "app-id"
    assert config.access_token == "token-value"
    assert config.resource_id == DEFAULT_RESOURCE_ID
    assert "token-value" not in repr(config)


def test_load_config_supports_new_console_api_key_without_legacy_credentials(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("VOLC_ASR_API_KEY=token-value\n", encoding="utf-8")

    config = load_volc_asr_config(env_path)

    assert config.api_key == "token-value"
    assert config.app_id == ""
    assert config.access_token == ""
    assert config.authentication_mode == "new_console_api_key"
    assert "token-value" not in repr(config)


def test_load_config_rejects_mixed_legacy_and_new_console_credentials(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "VOLC_ASR_API_KEY=token-value\n"
        "VOLC_ASR_APP_ID=legacy-app\n"
        "VOLC_ASR_ACCESS_TOKEN=token-value\n",
        encoding="utf-8",
    )

    with pytest.raises(VolcAsrError, match="either.*API key.*legacy"):
        load_volc_asr_config(env_path)


def test_api_headers_keep_authentication_modes_mutually_exclusive() -> None:
    new_headers = _api_headers(VolcAsrConfig(api_key="token-value"), "request-id", sequence="-1")
    legacy_headers = _api_headers(
        VolcAsrConfig(app_id="legacy-app", access_token="token-value"), "request-id"
    )

    assert new_headers == {
        "X-Api-Key": "token-value",
        "X-Api-Resource-Id": DEFAULT_RESOURCE_ID,
        "X-Api-Request-Id": "request-id",
        "X-Api-Sequence": "-1",
    }
    assert legacy_headers == {
        "X-Api-App-Key": "legacy-app",
        "X-Api-Access-Key": "token-value",
        "X-Api-Resource-Id": DEFAULT_RESOURCE_ID,
        "X-Api-Request-Id": "request-id",
    }
    with pytest.raises(VolcAsrError, match="cannot be mixed"):
        _api_headers(
            VolcAsrConfig(app_id="legacy-app", access_token="token-value", api_key="token-value"),
            "request-id",
        )


@pytest.mark.parametrize("field", ["VOLC_ASR_SUBMIT_URL", "VOLC_ASR_QUERY_URL"])
def test_load_config_rejects_non_https_service_endpoint(tmp_path: Path, field: str) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "VOLC_ASR_APP_ID=app-id\n"
        "VOLC_ASR_ACCESS_TOKEN=token-value\n"
        f"{field}=http://127.0.0.1/private\n",
        encoding="utf-8",
    )

    with pytest.raises(VolcAsrError, match="HTTPS"):
        load_volc_asr_config(env_path)


def test_normalize_result_records_identity_and_zero_confidence() -> None:
    normalized = normalize_result(_payload_with_evidence())

    assert normalized["provider"] == "volc_asr"
    assert normalized["resource_id"] == DEFAULT_RESOURCE_ID
    assert normalized["adapter_version"] == VOLC_ASR_ADAPTER_VERSION
    assert normalized["audio_duration_seconds"] == 1.2
    assert normalized["words"][0] == {
        "text": "自动",
        "start": 0.26,
        "end": 0.42,
        "confidence": 0,
    }
    json.dumps(normalized, ensure_ascii=False)


def test_normalize_result_rejects_service_payload_without_attributable_evidence() -> None:
    with pytest.raises(VolcAsrError, match="evidence"):
        normalize_result(_payload())


def test_run_asr_binds_input_and_service_result_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio_bytes = b"RIFFattributable-audio"
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(audio_bytes)
    request_id = "11111111-2222-4333-8444-555555555555"

    monkeypatch.setattr("audio_sound.volc_asr.submit_audio", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "audio_sound.volc_asr.query_result",
        lambda **_kwargs: (_payload(), "20000000", "Success"),
    )
    monkeypatch.setattr("audio_sound.volc_asr.uuid.uuid4", lambda: request_id)
    config = type("Config", (), {"resource_id": "volc.test.bound-resource"})()

    raw_result = run_volc_asr(
        audio_path,
        config=config,
        poll_interval_seconds=0,
        max_wait_seconds=1,
    )
    normalized = normalize_result(raw_result)

    assert normalized["input_sha256"] == hashlib.sha256(audio_bytes).hexdigest()
    assert normalized["service_job_id"] == request_id
    assert normalized["resource_id"] == "volc.test.bound-resource"
    assert len(normalized["service_result_sha256"]) == 64
    assert normalized["word_timing_count"] == len(normalized["words"])
    serialized = json.dumps(normalized, sort_keys=True)
    assert "access_token" not in serialized.casefold()


@pytest.mark.parametrize(
    "words",
    [
        [],
        [{"text": "bad", "start_time": 100, "end_time": 100}],
        [{"text": "bad", "start_time": 200, "end_time": 100}],
        [
            {"text": "later", "start_time": 500, "end_time": 700},
            {"text": "earlier", "start_time": 300, "end_time": 600},
        ],
        [
            None,
            {"text": "valid", "start_time": 300, "end_time": 600},
        ],
    ],
)
def test_normalize_result_rejects_invalid_or_nonmonotonic_word_timings(
    words: list[object],
) -> None:
    payload = _payload_with_evidence()
    payload["result"]["utterances"][0]["words"] = words

    with pytest.raises(VolcAsrError, match="timing"):
        normalize_result(payload)


def test_normalize_result_discards_negative_whitespace_service_sentinel() -> None:
    payload = _payload_with_evidence()
    payload["result"]["utterances"][0]["words"].insert(
        2,
        {"text": " ", "start_time": -1, "end_time": -1},
    )

    normalized = normalize_result(payload)

    assert [word["text"] for word in normalized["words"]] == [
        "自动",
        "剪辑",
        "语音",
        "测试",
    ]
    assert normalized["discarded_word_rows"] == [
        {
            "utterance_index": 0,
            "word_index": 2,
            "text": " ",
            "raw_start": -1,
            "raw_end": -1,
            "reason": "service_whitespace_sentinel",
        }
    ]


def test_find_phrase_matches_ignores_punctuation_and_uses_anchor() -> None:
    normalized = normalize_result(_payload_with_evidence())

    matches = find_phrase_matches(
        normalized["words"],
        "自动剪辑，",
        anchor_start=0.2,
        anchor_end=0.7,
    )

    assert len(matches) == 1
    assert matches[0]["start"] == 0.26
    assert matches[0]["end"] == 0.62
    assert matches[0]["text"] == "自动剪辑"


def test_submit_audio_rejects_missing_file_before_network(tmp_path: Path) -> None:
    config = type(
        "Config",
        (),
        {
            "app_id": "app-id",
            "access_token": "token-value",
            "resource_id": DEFAULT_RESOURCE_ID,
            "submit_url": "https://example.invalid/submit",
            "uid": "auto-cut",
        },
    )()

    with pytest.raises(FileNotFoundError):
        submit_audio(
            tmp_path / "missing.wav",
            config=config,
            request_id="request-id",
            timeout_seconds=1,
        )


@pytest.mark.parametrize("processing_status", ["20000001", "20000002"])
def test_run_asr_polls_processing_until_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, processing_status: str
) -> None:
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFFfake")
    calls = {"query": 0}

    def fake_submit(*_args, **_kwargs) -> None:
        return None

    def fake_query(*_args, **_kwargs):
        calls["query"] += 1
        if calls["query"] == 1:
            return {}, processing_status, "Processing"
        return _payload(), "20000000", "Success"

    monkeypatch.setattr("audio_sound.volc_asr.submit_audio", fake_submit)
    monkeypatch.setattr("audio_sound.volc_asr.query_result", fake_query)
    monkeypatch.setattr("audio_sound.volc_asr.time.sleep", lambda _seconds: None)

    result = run_volc_asr(
        audio_path,
        config=type("Config", (), {"resource_id": DEFAULT_RESOURCE_ID})(),
        poll_interval_seconds=0,
        max_wait_seconds=1,
    )

    assert result["result"]["text"] == "自动剪辑，语音测试"
    assert calls["query"] == 2


def test_main_creates_nested_output_directories_before_service_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "normalized" / "nested" / "result.json"
    raw_output_path = tmp_path / "raw" / "nested" / "service.json"
    service_calls = 0

    def fake_run(*_args, **_kwargs):
        nonlocal service_calls
        service_calls += 1
        assert output_path.parent.is_dir()
        assert raw_output_path.parent.is_dir()
        return _payload_with_evidence()

    monkeypatch.setattr("audio_sound.volc_asr.load_volc_asr_config", lambda _path: object())
    monkeypatch.setattr("audio_sound.volc_asr.run_volc_asr", fake_run)

    exit_code = main(
        [
            str(tmp_path / "input.wav"),
            "--output",
            str(output_path),
            "--raw-output",
            str(raw_output_path),
        ]
    )

    assert exit_code == 0
    assert service_calls == 1
    assert json.loads(output_path.read_text(encoding="utf-8"))["provider"] == "volc_asr"
    assert json.loads(raw_output_path.read_text(encoding="utf-8")) == _payload_with_evidence()


def test_main_rejects_uncreatable_output_parent_before_service_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    service_calls = 0

    def fake_run(*_args, **_kwargs):
        nonlocal service_calls
        service_calls += 1
        return _payload_with_evidence()

    monkeypatch.setattr("audio_sound.volc_asr.load_volc_asr_config", lambda _path: object())
    monkeypatch.setattr("audio_sound.volc_asr.run_volc_asr", fake_run)

    exit_code = main(
        [
            str(tmp_path / "input.wav"),
            "--output",
            str(blocked_parent / "result.json"),
            "--raw-output",
            str(tmp_path / "raw" / "service.json"),
        ]
    )

    assert exit_code == 1
    assert service_calls == 0


def test_thin_entrypoint_and_console_command_are_bundled() -> None:
    thin_entrypoint = REPO_ROOT / "scripts" / "audio" / "volc_word_align.py"
    source = thin_entrypoint.read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "from audio_sound.volc_asr import main" in source
    assert "D:\\codex" not in source
    assert 'audio-volc-word-align = "audio_sound.volc_asr:main"' in pyproject


def test_source_receipt_closes_local_candidate_provenance() -> None:
    receipt = json.loads(SOURCE_RECEIPT.read_text(encoding="utf-8"))

    assert receipt["schema_version"] == 1
    assert receipt["source_label"] == "Audio-sound-release-local-volc-candidate"
    assert receipt["origin"] == "local_first_party_worktree"
    assert receipt["license"] == "MIT"
    assert receipt["adapter_version"] == VOLC_ASR_ADAPTER_VERSION
    assert receipt["verification_test"] == "tests/audio_sound/test_volc_asr.py"
    assert receipt["items"] == [
        {
            "source_path": "audio_sound/volc_asr.py",
            "source_size": 13699,
            "source_sha256": "3293d86dab356acb1cc2969826a0a262132aa58a4b8ccded1e51fa637770eba4",
            "treatment": "adapted",
            "destination": "audio_sound/volc_asr.py",
        },
        {
            "source_path": "scripts/volc_word_align.py",
            "source_size": 265,
            "source_sha256": "b8fe486c3d8d7c53d8bd6204cc6a592ae17b3d83004f2c21d1b8c5ec7f6073fd",
            "treatment": "adapted",
            "destination": "scripts/audio/volc_word_align.py",
        },
        {
            "source_path": "tests/test_volc_asr.py",
            "source_size": 4985,
            "source_sha256": "1a48bb5b0e1f5fc039b246f095b6fd02ab03b4b41c576ea1052c2b37a316537a",
            "treatment": "adapted",
            "destination": "tests/audio_sound/test_volc_asr.py",
        },
    ]
    assert all((REPO_ROOT / item["destination"]).is_file() for item in receipt["items"])
    serialized = json.dumps(receipt)
    assert "D:\\codex" not in serialized
    assert "access_token" not in serialized.casefold()
