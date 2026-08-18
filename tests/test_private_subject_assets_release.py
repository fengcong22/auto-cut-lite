from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest


def _rgba_png() -> bytes:
    pixels = np.array(
        [
            [[0, 0, 255, 255], [0, 255, 0, 128]],
            [[255, 0, 0, 64], [255, 255, 255, 0]],
        ],
        dtype=np.uint8,
    )
    encoded, data = cv2.imencode(".png", pixels, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    assert encoded
    return data.tobytes()


_RGBA_PNG = _rgba_png()


def _module():
    try:
        return importlib.import_module("scripts.release.build_private_subject_assets")
    except ModuleNotFoundError:
        pytest.fail("private subject asset release builder is not implemented")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rewrite_private_bundle(
    source: Path,
    destination: Path,
    mutate,
) -> None:
    module = _module()
    with zipfile.ZipFile(source) as archive:
        payload = {
            info.filename: archive.read(info)
            for info in archive.infolist()
            if info.filename != "private-assets-manifest.json"
        }
        manifest = json.loads(archive.read("private-assets-manifest.json"))
    mutate(payload, manifest)
    manifest["files"] = sorted(
        manifest["files"], key=lambda row: (row["path"].casefold(), row["path"])
    )
    manifest["manifest_sha256"] = _sha(
        module.canonical_json(
            {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        )
    )
    with zipfile.ZipFile(destination, "w") as archive:
        for name, data in sorted(payload.items()):
            archive.writestr(name, data)
        archive.writestr("private-assets-manifest.json", module.canonical_json(manifest))


def _ready_profile(profile_root: Path) -> dict[str, object]:
    files = {
        "assets/hand.png": _RGBA_PNG,
        "scale-references/one.png": _RGBA_PNG,
        "scale-references/two.png": _RGBA_PNG,
        "approved-previews/preview.png": _RGBA_PNG,
    }
    for relative, data in files.items():
        path = profile_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    profile = {
        "schema_version": 1,
        "key": "senior-high-history",
        "stage_id": "senior-high",
        "subject_id": "history",
        "stage_name": "高中",
        "subject_name": "历史",
        "display_name": "高中历史",
        "aliases": ["高中历史"],
        "detection_evidence": [
            {
                "source": "explicit_user_command",
                "value": "高中历史指向物素材库",
                "confirmed": True,
            }
        ],
        "status": "ready",
        "missing_items": [],
        "problems": [],
        "assets": [
            {
                "asset_id": "primary-history-hand",
                "role": "hand",
                "anchor": [0.1, 0.2],
                "path": "assets/hand.png",
                "source_name": "hand.png",
                "sha256": _sha(files["assets/hand.png"]),
                "media_contract": {
                    "format": "png",
                    "has_alpha": True,
                    "width": 2,
                    "height": 2,
                },
            }
        ],
        "placement_policies": [],
        "scale_references": [
            {
                "reference_id": f"ref-{index}",
                "path": relative,
                "source_name": Path(relative).name,
                "sha256": _sha(files[relative]),
                "lesson": "lesson",
                "time": f"00:00:0{index}",
                "layout": f"layout-{index}",
                "full_frame": True,
                "confirmed": True,
                "canvas_size": {"width": 16, "height": 9},
                "visible_bbox": {"x": 1, "y": 1, "width": 1, "height": 1},
                "visible_width_ratio": 0.0625,
                "visible_height_ratio": 0.111,
            }
            for index, relative in enumerate(
                ("scale-references/one.png", "scale-references/two.png"), start=1
            )
        ],
        "approved_previews": [
            {
                "path": "approved-previews/preview.png",
                "source_name": "preview.png",
                "sha256": _sha(files["approved-previews/preview.png"]),
                "approved": True,
                "note": "approved",
            }
        ],
    }
    (profile_root / "profile.json").write_text(
        json.dumps(profile, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "status": "ready",
        "stage_id": "senior-high",
        "subject_id": "history",
        "key": "senior-high-history",
        "missing_items": [],
        "problems": [],
        "profile": profile,
    }


def test_private_profile_bundle_contains_only_referenced_sanitized_evidence(
    tmp_path: Path,
) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    (profile_root / "project-bindings.json").write_text("{}", encoding="utf-8")
    (profile_root / "unreferenced.png").write_bytes(_RGBA_PNG)
    output = tmp_path / "Auto-Cut-v1.7.0-private-assets-high-school-history.zip"

    result = module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=check_result,
    )

    assert result["status"] == "ready"
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "project-bindings.json" not in names
        assert "unreferenced.png" not in names
        assert "evidence/assets/hand-001.png" in names
        assert all("hand.png" not in name for name in names)
        intake = json.loads(archive.read("intake.json"))
        manifest = json.loads(archive.read("private-assets-manifest.json"))
    assert intake["assets"][0]["path"] == "evidence/assets/hand-001.png"
    assert "media_contract" not in intake["assets"][0]
    assert "source_name" not in intake["assets"][0]
    assert "visible_width_ratio" not in intake["scale_references"][0]
    assert manifest["profile_key"] == "senior-high-history"
    assert manifest["target_requires_explicit_binding"] is True
    assert all(":\\" not in row["path"] for row in manifest["files"])


def test_private_profile_bundle_rejects_non_exact_identity(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "other"
    check_result = _ready_profile(profile_root)
    check_result["profile"]["subject_id"] = "geography"

    with pytest.raises(ValueError, match="exact senior-high-history"):
        module.write_private_profile_bundle(
            profile_root,
            tmp_path / "private.zip",
            release_version="1.7.0",
            source_commit="a" * 40,
            check_result=check_result,
        )


def test_private_profile_bundle_rejects_hash_changed_evidence(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    (profile_root / "assets" / "hand.png").write_bytes(b"changed")

    with pytest.raises(ValueError, match="SHA-256"):
        module.write_private_profile_bundle(
            profile_root,
            tmp_path / "private.zip",
            release_version="1.7.0",
            source_commit="a" * 40,
            check_result=check_result,
        )


def test_private_profile_bundle_rejects_cross_directory_reference(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    check_result["profile"]["assets"][0]["path"] = "../project-bindings.json"
    (profile_root / "profile.json").write_text(
        json.dumps(check_result["profile"], ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="unsafe profile evidence path"):
        module.write_private_profile_bundle(
            profile_root,
            tmp_path / "private.zip",
            release_version="1.7.0",
            source_commit="a" * 40,
            check_result=check_result,
        )


def test_private_profile_bundle_refuses_existing_output(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    output = tmp_path / "private.zip"
    output.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="already exists"):
        module.write_private_profile_bundle(
            profile_root,
            output,
            release_version="1.7.0",
            source_commit="a" * 40,
            check_result=check_result,
        )


def test_private_profile_bundle_drops_machine_bound_profile_metadata(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    check_result["profile"]["detection_evidence"][0]["value"] = (
        "source path " + chr(67) + ":\\Users\\example-user\\private"
    )
    (profile_root / "profile.json").write_text(
        json.dumps(check_result["profile"], ensure_ascii=False), encoding="utf-8"
    )

    output = tmp_path / "private.zip"
    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=check_result,
    )

    with zipfile.ZipFile(output) as archive:
        intake_text = archive.read("intake.json").decode("utf-8")
    assert "example-user" not in intake_text
    assert "source path" not in intake_text


def test_private_profile_bundle_requires_image_evidence(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    source = profile_root / "approved-previews" / "preview.png"
    replacement = source.with_suffix(".bin")
    source.replace(replacement)
    preview = check_result["profile"]["approved_previews"][0]
    preview["path"] = "approved-previews/preview.bin"
    (profile_root / "profile.json").write_text(
        json.dumps(check_result["profile"], ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="image"):
        module.write_private_profile_bundle(
            profile_root,
            tmp_path / "private.zip",
            release_version="1.7.0",
            source_commit="a" * 40,
            check_result=check_result,
        )


def test_private_profile_bundle_rejects_disguised_non_image_bytes(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    preview_path = profile_root / "approved-previews" / "preview.png"
    preview_path.write_bytes(b'{"provider_output":"not an image"}')
    check_result["profile"]["approved_previews"][0]["sha256"] = _sha(preview_path.read_bytes())
    (profile_root / "profile.json").write_text(
        json.dumps(check_result["profile"], ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="decodable image"):
        module.write_private_profile_bundle(
            profile_root,
            tmp_path / "private.zip",
            release_version="1.7.0",
            source_commit="a" * 40,
            check_result=check_result,
        )


def test_private_profile_bundle_normalizes_images_and_records_both_hashes(
    tmp_path: Path,
) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    preview_path = profile_root / "approved-previews" / "preview.png"
    source_bytes = preview_path.read_bytes() + b"provider-secret-after-iend"
    preview_path.write_bytes(source_bytes)
    check_result["profile"]["approved_previews"][0]["sha256"] = _sha(source_bytes)
    (profile_root / "profile.json").write_text(
        json.dumps(check_result["profile"], ensure_ascii=False), encoding="utf-8"
    )
    output = tmp_path / "private.zip"

    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=check_result,
    )

    with zipfile.ZipFile(output) as archive:
        intake = json.loads(archive.read("intake.json"))
        manifest = json.loads(archive.read("private-assets-manifest.json"))
        normalized = archive.read("evidence/approved-previews/preview-001.png")
    row = next(
        item
        for item in manifest["files"]
        if item["path"] == "evidence/approved-previews/preview-001.png"
    )
    assert b"provider-secret-after-iend" not in normalized
    assert row["source_sha256"] == _sha(source_bytes)
    assert row["sha256"] == _sha(normalized)
    assert row["normalization"] == "decoded_png_v1"
    assert intake["approved_previews"][0]["sha256"] == _sha(normalized)
    source_pixels = cv2.imdecode(np.frombuffer(source_bytes, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    normalized_pixels = cv2.imdecode(
        np.frombuffer(normalized, dtype=np.uint8), cv2.IMREAD_UNCHANGED
    )
    assert np.array_equal(source_pixels, normalized_pixels)


def test_private_profile_bundle_uses_strict_intake_field_allowlists(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    scale = check_result["profile"]["scale_references"][0]
    scale["provider_output"] = {
        "request_id": "opaque-provider-request",
        "account_state": {"session": "must-not-migrate"},
    }
    check_result["profile"]["assets"][0]["request_id"] = "opaque-request"
    (profile_root / "profile.json").write_text(
        json.dumps(check_result["profile"], ensure_ascii=False), encoding="utf-8"
    )
    output = tmp_path / "private.zip"

    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=check_result,
    )

    with zipfile.ZipFile(output) as archive:
        intake = json.loads(archive.read("intake.json"))
    serialized = json.dumps(intake, ensure_ascii=False)
    assert "provider_output" not in serialized
    assert "request_id" not in serialized
    assert "account_state" not in serialized


def test_private_profile_bundle_replaces_free_text_with_migration_metadata(
    tmp_path: Path,
) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    profile = check_result["profile"]
    profile["aliases"] = ["sensitive-alias"]
    profile["detection_evidence"][0]["value"] = "sensitive-command"
    profile["assets"][0]["asset_id"] = "sensitive-asset-id"
    profile["scale_references"][0]["reference_id"] = "sensitive-reference-id"
    profile["scale_references"][0]["lesson"] = "sensitive-lesson"
    profile["approved_previews"][0]["note"] = "sensitive-preview-note"
    (profile_root / "profile.json").write_text(
        json.dumps(profile, ensure_ascii=False), encoding="utf-8"
    )
    output = tmp_path / "private.zip"

    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=check_result,
    )

    with zipfile.ZipFile(output) as archive:
        intake = json.loads(archive.read("intake.json"))
    serialized = json.dumps(intake, ensure_ascii=False)
    assert "sensitive-" not in serialized
    assert intake["aliases"] == ["高中历史"]
    assert intake["detection_evidence"] == [
        {
            "source": "explicit_user_command",
            "value": "迁移高中历史指向物素材库",
            "confirmed": True,
        }
    ]
    assert intake["assets"][0]["asset_id"] == "history-hand-001"
    assert intake["scale_references"][0]["reference_id"] == "history-scale-reference-001"
    assert "note" not in intake["approved_previews"][0]


def test_private_profile_bundle_verifies_before_publishing_final_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    output = tmp_path / "private.zip"

    def fail_verification(_path: Path) -> dict[str, object]:
        raise ValueError("verification failed")

    monkeypatch.setattr(module, "verify_private_subject_assets_bundle", fail_verification)

    with pytest.raises(ValueError, match="verification failed"):
        module.write_private_profile_bundle(
            profile_root,
            output,
            release_version="1.7.0",
            source_commit="a" * 40,
            check_result=check_result,
        )

    assert not output.exists()


def test_private_profile_bundle_never_runs_verifier_on_the_published_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    output = tmp_path / "private.zip"
    real_verify = module.verify_private_subject_assets_bundle
    verified_paths: list[Path] = []

    def verify_temporary_only(path: Path) -> dict[str, object]:
        candidate = Path(path)
        verified_paths.append(candidate)
        if candidate == output:
            raise AssertionError("published path was verified after publication")
        return real_verify(candidate)

    monkeypatch.setattr(module, "verify_private_subject_assets_bundle", verify_temporary_only)

    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=check_result,
    )

    assert len(verified_paths) == 1
    assert output.is_file()


def test_private_profile_bundle_reads_each_source_image_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    source = (profile_root / "assets" / "hand.png").resolve()
    real_read_bytes = Path.read_bytes
    source_reads = 0

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal source_reads
        if path.resolve() == source:
            source_reads += 1
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)

    module.write_private_profile_bundle(
        profile_root,
        tmp_path / "private.zip",
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=check_result,
    )

    assert source_reads == 1


def test_private_restore_guide_requires_a_new_empty_registry(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    output = tmp_path / "private.zip"
    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=_ready_profile(profile_root),
    )

    with zipfile.ZipFile(output) as archive:
        guide = archive.read("RESTORE.md").decode("utf-8")
    assert "subject-pointer-profiles.local" not in guide
    assert "Test-Path" in guide
    assert "new empty registry" in guide


@pytest.mark.parametrize(
    "path",
    [
        "NUL.txt",
        "evidence/COM1.png",
        "evidence/name. ",
        "evidence/name.",
    ],
)
def test_private_archive_paths_follow_windows_portability_rules(path: str) -> None:
    module = _module()

    with pytest.raises(ValueError, match="unsafe|reserved|Windows|canonical"):
        module._safe_relative_path(path)


def test_private_manifest_verifier_rejects_invalid_self_hash(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    output = tmp_path / "private.zip"
    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=check_result,
    )
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(output) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "private-assets-manifest.json":
                manifest = json.loads(data)
                manifest["manifest_sha256"] = "0" * 64
                data = module.canonical_json(manifest)
            target.writestr(info, data)

    with pytest.raises(ValueError, match="manifest hash"):
        module.verify_private_subject_assets_bundle(tampered)


def test_private_manifest_verifier_rejects_empty_one_file_intake(tmp_path: Path) -> None:
    module = _module()
    intake = module.canonical_json({})
    manifest = {
        "schema_version": 1,
        "release_version": "1.7.0",
        "source_commit": "a" * 40,
        "profile_key": "senior-high-history",
        "stage_id": "senior-high",
        "subject_id": "history",
        "target_requires_explicit_binding": True,
        "files": [
            {
                "path": "intake.json",
                "size": len(intake),
                "sha256": _sha(intake),
                "source_sha256": _sha(intake),
                "normalization": "generated_v1",
                "role": "registration",
                "rights": "user_owned_migration_approved",
            }
        ],
    }
    manifest["manifest_sha256"] = _sha(module.canonical_json(manifest))
    archive_path = tmp_path / "private.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("intake.json", intake)
        archive.writestr("private-assets-manifest.json", module.canonical_json(manifest))

    with pytest.raises(ValueError, match="intake|inventory|role"):
        module.verify_private_subject_assets_bundle(archive_path)


def test_private_manifest_verifier_rejects_role_path_mismatch(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    output = tmp_path / "private.zip"
    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=_ready_profile(profile_root),
    )
    tampered = tmp_path / "tampered.zip"

    def mutate(_payload, manifest) -> None:
        preview = next(
            row
            for row in manifest["files"]
            if row["path"].startswith("evidence/approved-previews/")
        )
        preview["role"] = "hand"

    _rewrite_private_bundle(output, tampered, mutate)

    with pytest.raises(ValueError, match="role|path|prefix"):
        module.verify_private_subject_assets_bundle(tampered)


def test_private_manifest_verifier_rejects_wrong_evidence_prefix(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    output = tmp_path / "private.zip"
    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=_ready_profile(profile_root),
    )
    tampered = tmp_path / "tampered.zip"

    def mutate(payload, manifest) -> None:
        old_path = "evidence/approved-previews/preview-001.png"
        new_path = "evidence/assets/preview-001.png"
        payload[new_path] = payload.pop(old_path)
        intake = json.loads(payload["intake.json"])
        intake["approved_previews"][0]["path"] = new_path
        payload["intake.json"] = module.canonical_json(intake)
        for row in manifest["files"]:
            if row["path"] == old_path:
                row["path"] = new_path
            elif row["path"] == "intake.json":
                row["size"] = len(payload["intake.json"])
                row["sha256"] = _sha(payload["intake.json"])
                row["source_sha256"] = row["sha256"]

    _rewrite_private_bundle(output, tampered, mutate)

    with pytest.raises(ValueError, match="path|prefix|role"):
        module.verify_private_subject_assets_bundle(tampered)


def test_private_manifest_verifier_rejects_intake_manifest_hash_mismatch(
    tmp_path: Path,
) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    output = tmp_path / "private.zip"
    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=_ready_profile(profile_root),
    )
    tampered = tmp_path / "tampered.zip"

    def mutate(payload, manifest) -> None:
        intake = json.loads(payload["intake.json"])
        intake["assets"][0]["sha256"] = "0" * 64
        payload["intake.json"] = module.canonical_json(intake)
        intake_row = next(row for row in manifest["files"] if row["path"] == "intake.json")
        intake_row["size"] = len(payload["intake.json"])
        intake_row["sha256"] = _sha(payload["intake.json"])
        intake_row["source_sha256"] = intake_row["sha256"]

    _rewrite_private_bundle(output, tampered, mutate)

    with pytest.raises(ValueError, match="intake|manifest|SHA-256|mapping"):
        module.verify_private_subject_assets_bundle(tampered)


def test_private_manifest_verifier_decodes_every_image_payload(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    output = tmp_path / "private.zip"
    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=_ready_profile(profile_root),
    )
    tampered = tmp_path / "tampered.zip"

    def mutate(payload, manifest) -> None:
        image_path = "evidence/approved-previews/preview-001.png"
        payload[image_path] = b"not a decodable image"
        image_hash = _sha(payload[image_path])
        intake = json.loads(payload["intake.json"])
        intake["approved_previews"][0]["sha256"] = image_hash
        payload["intake.json"] = module.canonical_json(intake)
        for row in manifest["files"]:
            if row["path"] == image_path:
                row["size"] = len(payload[image_path])
                row["sha256"] = image_hash
                row["source_sha256"] = image_hash
            elif row["path"] == "intake.json":
                row["size"] = len(payload["intake.json"])
                row["sha256"] = _sha(payload["intake.json"])
                row["source_sha256"] = row["sha256"]

    _rewrite_private_bundle(output, tampered, mutate)

    with pytest.raises(ValueError, match="image|PNG|decode"):
        module.verify_private_subject_assets_bundle(tampered)


def test_private_manifest_schema_encodes_exact_role_path_inventory() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "private-subject-assets-manifest.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    files = schema["properties"]["files"]

    assert files["minItems"] == 6
    alternatives = files["items"]["oneOf"]
    mappings = {
        (
            alternative["properties"]["role"]["const"],
            alternative["properties"]["path"].get("const")
            or alternative["properties"]["path"].get("pattern"),
        )
        for alternative in alternatives
    }
    assert mappings == {
        ("registration", "intake.json"),
        ("restore_guide", "RESTORE.md"),
        ("hand", r"^evidence/assets/hand-[0-9]{3}\.png$"),
        (
            "scale-references",
            r"^evidence/scale-references/scale-reference-[0-9]{3}\.png$",
        ),
        (
            "approved-previews",
            r"^evidence/approved-previews/preview-[0-9]{3}\.png$",
        ),
    }
    required_counts = {
        rule["contains"]["properties"]["role"]["const"]: rule["minContains"]
        for rule in files["allOf"]
    }
    assert required_counts == {
        "registration": 1,
        "restore_guide": 1,
        "hand": 1,
        "scale-references": 2,
        "approved-previews": 1,
    }


def test_private_profile_bundle_is_deterministic(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    for output in (first, second):
        module.write_private_profile_bundle(
            profile_root,
            output,
            release_version="1.7.0",
            source_commit="a" * 40,
            check_result=check_result,
        )

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert {info.compress_type for info in archive.infolist()} == {zipfile.ZIP_STORED}


def test_private_intake_round_trips_through_real_registry(tmp_path: Path) -> None:
    module = _module()
    profile_root = tmp_path / "senior-high-history"
    check_result = _ready_profile(profile_root)
    output = tmp_path / "private.zip"
    module.write_private_profile_bundle(
        profile_root,
        output,
        release_version="1.7.0",
        source_commit="a" * 40,
        check_result=check_result,
    )
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(output) as archive:
        archive.extractall(extracted)
    registry = tmp_path / "target-registry"
    script = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "auto-cut-subject-pointer-onboarding"
        / "scripts"
        / "profile_registry.py"
    )

    commands = (
        ["register", "--input", str(extracted / "intake.json")],
        ["check", "--stage-id", "senior-high", "--subject-id", "history"],
        ["validate"],
    )
    results = []
    for arguments in commands:
        result = subprocess.run(
            [sys.executable, str(script), *arguments, "--root", str(registry), "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        results.append(result)
    assert all(result.returncode == 0 for result in results), "\n".join(
        result.stdout + result.stderr for result in results
    )
    assert json.loads(results[1].stdout)["status"] == "ready"
    assert json.loads(results[2].stdout)[0]["status"] == "ready"


def test_private_builder_resolves_relative_registry_root_from_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    repo_root = tmp_path / "repo"
    profile_root = repo_root / "data" / "subject-pointer-profiles.local" / "senior-high-history"
    check_result = _ready_profile(profile_root)
    (repo_root / "VERSION").write_text("1.7.0\n", encoding="utf-8")
    observed: dict[str, Path] = {}

    def fake_registry(_repo_root: Path, registry_root: Path, command: str):
        observed[f"registry_{command}"] = registry_root
        if command == "check":
            return check_result
        return [{"key": "senior-high-history", "status": "ready", "problems": []}]

    def fake_write(profile_path: Path, output_path: Path, **_kwargs):
        observed["profile_root"] = profile_path
        output_path.write_bytes(b"fixture private bundle")
        return {"status": "ready"}

    monkeypatch.setattr(
        module,
        "capture_clean_release_source",
        lambda _root: module.ReleaseSource(version="1.7.0", source_commit="a" * 40),
    )
    monkeypatch.setattr(module, "assert_release_source_unchanged", lambda *_args: None)
    monkeypatch.setattr(module, "_run_registry", fake_registry)
    monkeypatch.setattr(module, "write_private_profile_bundle", fake_write)
    output = tmp_path / "Auto-Cut-v1.7.0-private-assets-high-school-history.zip"

    module.build_private_subject_assets(
        repo_root,
        Path("data/subject-pointer-profiles.local"),
        output,
    )

    expected = repo_root / "data" / "subject-pointer-profiles.local"
    assert observed["registry_check"] == expected
    assert observed["registry_validate"] == expected
    assert observed["profile_root"] == expected / "senior-high-history"


def test_private_builder_uses_captured_commit_version_not_worktree_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    output = tmp_path / "Auto-Cut-v1.7.0-private-assets-high-school-history.zip"
    captured = module.ReleaseSource(version="1.7.0", source_commit="a" * 40)
    monkeypatch.setattr(module, "capture_clean_release_source", lambda _root: captured)

    def stop_if_build_continues(*_args, **_kwargs):
        raise RuntimeError("captured commit inputs reached")

    monkeypatch.setattr(module, "_run_registry", stop_if_build_continues)

    with pytest.raises(RuntimeError, match="captured commit inputs"):
        module.build_private_subject_assets(
            repo_root,
            Path("data/subject-pointer-profiles.local"),
            output,
        )
