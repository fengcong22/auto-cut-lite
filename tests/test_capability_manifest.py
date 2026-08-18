from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from scripts import full_setup

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "capability-manifest.json"
CONTRACT_PATH = REPO_ROOT / "runtime-capability-contract.json"
SCHEMA_PATH = REPO_ROOT / "schemas" / "capability-manifest.schema.json"

REQUIRED_IDS = {
    "editable_draft_contract",
    "auto_cut_skills",
    "main_python_dependencies",
    "playwright_chromium",
    "jianying_smoke_draft",
    "favorite_text_assets",
    "subject_pointer_profiles",
    "feishu_notifications",
    "ffmpeg",
    "ffprobe",
    "audio_runtime",
    "spectramini_cleanup",
    "deepfilternet",
    "respiro",
    "volc_asr_alignment",
    "sami_tts",
    "edge_tts",
    "cloud_materials",
    "subtitle_material_matching",
    "carnac_overlay",
}
CLASSIFICATION_FIELDS = {
    "bundled",
    "installed_on_first_run",
    "requires_local_jianying",
    "requires_user_authorization",
    "requires_user_assets",
    "unavailable",
}


def test_static_manifest_validates_and_covers_every_required_capability() -> None:
    manifest = full_setup.load_and_validate_capability_manifest(MANIFEST_PATH)

    assert manifest["release_version"] == "1.7.0"
    capabilities = manifest["capabilities"]
    ids = [capability["id"] for capability in capabilities]
    assert len(ids) == len(set(ids))
    assert REQUIRED_IDS == set(ids)
    for capability in capabilities:
        assert all(type(capability[field]) is bool for field in CLASSIFICATION_FIELDS)
        assert isinstance(capability["verification_command"], str)
        assert capability["verification_command"].strip()
        assert capability["actual_result"]["status"] in full_setup.CAPABILITY_STATUSES
        assert capability["actual_result"]["code"] in full_setup.CAPABILITY_RESULT_CODES


def test_manifest_keeps_machine_owned_and_unverified_capabilities_truthful() -> None:
    manifest = full_setup.load_and_validate_capability_manifest(MANIFEST_PATH)
    by_id = {capability["id"]: capability for capability in manifest["capabilities"]}

    assert by_id["favorite_text_assets"]["requires_user_assets"] is True
    assert by_id["favorite_text_assets"]["actual_result"]["status"] == "pending"
    assert by_id["subject_pointer_profiles"]["requires_user_assets"] is True
    assert by_id["subject_pointer_profiles"]["actual_result"]["status"] == "pending"
    assert by_id["feishu_notifications"]["requires_user_authorization"] is True
    assert by_id["feishu_notifications"]["actual_result"]["status"] == "pending"
    assert by_id["volc_asr_alignment"]["requires_user_authorization"] is True
    assert by_id["volc_asr_alignment"]["actual_result"]["status"] == "pending"
    assert by_id["sami_tts"]["requires_local_jianying"] is True
    assert by_id["edge_tts"]["installed_on_first_run"] is True
    assert by_id["cloud_materials"]["actual_result"]["status"] == "degraded"
    assert by_id["subtitle_material_matching"]["actual_result"]["status"] == "degraded"
    assert by_id["carnac_overlay"]["actual_result"]["code"] == "requires_local_software"
    for capability_id in ("deepfilternet", "respiro"):
        assert by_id[capability_id]["unavailable"] is True
        assert by_id[capability_id]["actual_result"]["status"] == "unavailable"
        assert by_id[capability_id]["actual_result"]["code"] == "unverified_external_model"


def test_runtime_contract_tracks_persisted_browser_audio_and_pointer_consumers() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    by_id = {capability["id"]: capability for capability in contract["capabilities"]}

    playwright = by_id["playwright_chromium"]
    assert "scripts/web_recorder.py" in playwright["entrypoints"]
    assert (
        "skills/auto-cut-subject-pointer-onboarding/scripts/render_reference_assets.py"
        in playwright["entrypoints"]
    )
    assert "audio_sound/config.py" in playwright["required_paths"]

    main = by_id["main_python_dependencies"]
    assert "audio_sound/config.py" in main["required_paths"]

    pointer = by_id["subject_pointer_profiles"]
    assert (
        "skills/auto-cut-subject-pointer-onboarding/scripts/render_reference_assets.py"
        in pointer["entrypoints"]
    )
    assert "audio_sound/config.py" in pointer["required_paths"]
    assert any(
        dependency["module"] == "playwright"
        and dependency["environment"] == "main"
        and dependency["disposition"] == "direct"
        for dependency in pointer["dependency_imports"]
    )

    audio = by_id["audio_runtime"]
    assert "scripts/audio/audio_skill_workflow.py" in audio["entrypoints"]
    assert "audio_sound/skill_workflow.py" in audio["required_paths"]

    for capability_id in ("ffmpeg", "ffprobe"):
        assert "audio_sound/config.py" in by_id[capability_id]["required_paths"]


def test_manifest_describes_recording_probe_and_isolated_private_restore() -> None:
    manifest = full_setup.load_and_validate_capability_manifest(MANIFEST_PATH)
    by_id = {capability["id"]: capability for capability in manifest["capabilities"]}

    playwright_text = json.dumps(by_id["playwright_chromium"], ensure_ascii=False)
    assert "1011" in playwright_text
    assert "record_video_dir" in playwright_text
    assert "tmp/offline-runtime/browsers" in playwright_text
    assert "WinLDD revision 1007" in playwright_text
    assert "MIT and Apache-2.0" in playwright_text

    for capability_id in ("ffmpeg", "ffprobe"):
        capability_text = json.dumps(by_id[capability_id], ensure_ascii=False)
        assert "minimal" in capability_text
        assert "8.1.2" in capability_text
        assert "n8.1.2" in capability_text
        assert "no external codec libraries" in capability_text
        assert "tmp/offline-runtime/tools/ffmpeg" in capability_text

    pointer_text = json.dumps(by_id["subject_pointer_profiles"], ensure_ascii=False)
    assert "new empty isolated registry" in pointer_text


def test_manifest_schema_closes_classifications_status_and_result_code() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    capability_schema = schema["$defs"]["capability"]
    result_schema = schema["$defs"]["actualResult"]

    assert CLASSIFICATION_FIELDS <= set(capability_schema["required"])
    assert all(
        capability_schema["properties"][field] == {"type": "boolean"}
        for field in CLASSIFICATION_FIELDS
    )
    assert set(result_schema["properties"]["status"]["enum"]) == set(full_setup.CAPABILITY_STATUSES)
    assert set(result_schema["properties"]["code"]["enum"]) == set(
        full_setup.CAPABILITY_RESULT_CODES
    )
    assert schema["additionalProperties"] is False
    assert capability_schema["additionalProperties"] is False
    assert result_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "mutate, expected_error",
    [
        (
            lambda payload: payload["capabilities"].append(
                copy.deepcopy(payload["capabilities"][0])
            ),
            "duplicate capability id",
        ),
        (
            lambda payload: payload["capabilities"][0].pop("bundled"),
            "bundled",
        ),
        (
            lambda payload: payload["capabilities"][0]["actual_result"].update({"status": "full"}),
            "status",
        ),
        (
            lambda payload: payload["capabilities"][0]["actual_result"].update({"code": "made_up"}),
            "code",
        ),
        (lambda payload: payload.pop("$schema"), "\\$schema"),
        (lambda payload: payload.update({"unexpected": True}), "unexpected field"),
        (
            lambda payload: payload["capabilities"][0]["actual_result"].update({"status": []}),
            "status",
        ),
    ],
)
def test_manifest_validator_rejects_invalid_contracts(
    tmp_path: Path, mutate, expected_error: str
) -> None:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    mutate(payload)
    path = tmp_path / "capability-manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=expected_error):
        full_setup.load_and_validate_capability_manifest(path)


def test_static_manifest_contains_no_machine_path_or_credential_value() -> None:
    text = MANIFEST_PATH.read_text(encoding="utf-8")

    assert not re.search(r"(?i)\b[A-Z]:[\\/]", text)
    for forbidden in (
        "C:\\Users",
        "D:\\codex",
        "app_secret",
        "access_token",
        "device_code",
        "oc_example_notification_chat",
    ):
        assert forbidden not in text
