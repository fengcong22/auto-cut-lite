from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from audio_sound import pipeline as pipeline_module
from audio_sound.config import apply_runtime_overrides, load_preset
from audio_sound.pipeline import (
    NoiseWindow,
    RespiroDetectionResult,
    RuntimeOptions,
    _run_verified_adapter_command,
    apply_spectramini_style_cleanup_to_samples,
    attenuation_db_to_gain,
    build_batch_summary,
    build_breath_processing_plan,
    build_deepfilternet_command,
    build_ffmpeg_extract_command,
    build_ffmpeg_finalize_commands,
    build_ffmpeg_noise_sample_command,
    build_mastering_filter_chain,
    build_output_layout,
    build_output_root,
    build_respiro_detect_command,
    discover_media_files,
    duck_samples_for_windows,
    infer_breath_onset_windows,
    infer_breath_window_from_frame_rms,
    infer_pause_residual_cleanup_windows,
    measure_segment_levels,
    parse_noise_window,
    process_media_file,
    render_batch_summary_markdown,
    repair_deepfilternet_speech_dropouts,
    resolve_respiro_runtime,
    run_respiro_or_fallback_detection,
    verified_deepfilternet_runtime,
    verified_respiro_runtime,
)


def _verified_respiro_runtime(
    repo: Path,
    weights: Path,
    *,
    python_executable: str = "python",
) -> dict[str, object]:
    root = Path("D:/trusted")
    identity = f"respiro-en@rev-1;weights={'b' * 64};adapter=adapter-v1"
    section = {
        "_env": {
            "AUDIO_SOUND_RESPIRO_REPO": str(repo),
            "AUDIO_SOUND_RESPIRO_WEIGHTS": str(weights),
        },
        "adapter_path": "audio_sound/respiro_adapter.py",
    }
    with (
        mock.patch(
            "audio_sound.bootstrap._verify_respiro_runtime",
            return_value={"ok": True, "identity": identity, "notes": "verified"},
        ),
        mock.patch(
            "audio_sound.bootstrap._load_runtime_asset_section",
            return_value=(section, ""),
        ),
        mock.patch(
            "audio_sound.bootstrap._read_trusted_repo_file",
            return_value=(b"respiro-adapter", ""),
        ),
    ):
        runtime = verified_respiro_runtime(
            {
                "python": {"ok": True, "path": python_executable},
                "respiro_en": {"ok": True, "identity": identity},
            },
            python_executable=python_executable,
            repo_path=repo,
            weights_path=weights,
            repo_root=root,
        )
    assert runtime is not None
    return runtime


def _verified_deepfilter_runtime(
    *,
    python_executable: str = "python",
) -> dict[str, object]:
    model_sha256 = "a" * 64
    model_identity = f"model-v1;model={model_sha256};adapter=adapter-v1"
    model_runtime = {
        "ok": True,
        "identity": model_identity,
        "model_path": "D:/models/deepfilter-model.bin",
        "model_sha256": model_sha256,
        "adapter_path": "D:/trusted/deepfilter_adapter.py",
        "adapter_version": "adapter-v1",
        "_adapter_bytes": b"deepfilter-adapter",
        "_repo_root": "D:/repo",
    }
    with (
        mock.patch(
            "audio_sound.bootstrap._verify_deepfilternet_model",
            return_value=model_runtime,
        ),
        mock.patch(
            "audio_sound.bootstrap._read_trusted_repo_file",
            return_value=(b"deepfilternet==0.5.6\n", ""),
        ),
    ):
        runtime = verified_deepfilternet_runtime(
            {
                "python": {"ok": True, "path": python_executable},
                "deepfilternet": {
                    "ok": True,
                    "module_ok": True,
                    "model_ok": True,
                    "adapter_ok": True,
                    "identity": f"deepfilternet@0.5.6;{model_identity}",
                },
            },
            python_executable=python_executable,
            repo_root=Path("D:/repo"),
        )
    assert runtime is not None
    return runtime


def _execution_receipt_fixture(
    root: Path,
    *,
    kind: str,
    model_sha256: str | None,
    weights_sha256: str | None,
) -> tuple[dict[str, object], dict[str, object], Path, Path]:
    input_path = root / "input.wav"
    output_path = root / "output.wav"
    input_path.write_bytes(b"input-audio")
    output_path.write_bytes(b"processed-audio")
    adapter_bytes = b"trusted-adapter"
    runtime: dict[str, object] = {
        "identity": "verified-runtime",
        "asset_identity": f"{kind}-asset-v1",
        "python_executable": "audio-python",
        "model_path": root / "model.bin",
        "repo_path": root / "Respiro-en",
        "weights_path": root / "respiro-en.pt",
        "package_version": "0.5.6" if kind == "deepfilternet" else "respiro-en-v1",
        "revision": f"{kind}-revision-1",
        "license": "MIT",
        "model_sha256": model_sha256,
        "weights_sha256": weights_sha256,
        "adapter_path": root / "adapter.py",
        "adapter_version": "adapter-v1",
        "adapter_bytes": adapter_bytes,
        "repo_root": root,
        "execution_id": "current-run",
        "execution_started_ns": 0,
    }
    runtime = pipeline_module._mint_verified_runtime(runtime, kind=kind)
    receipt: dict[str, object] = {
        "schema_version": 1,
        "kind": kind,
        "asset_identity": runtime["asset_identity"],
        "package_version": runtime["package_version"],
        "revision": runtime["revision"],
        "license": runtime["license"],
        "model_sha256": model_sha256,
        "weights_sha256": weights_sha256,
        "adapter_version": runtime["adapter_version"],
        "adapter_sha256": hashlib.sha256(adapter_bytes).hexdigest(),
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "model_loaded": True,
        "execution_id": "current-run",
    }
    return runtime, receipt, input_path, output_path


class PipelineTests(unittest.TestCase):
    def test_external_model_policy_never_reports_verified_assets_as_full_execution(
        self,
    ) -> None:
        policy = getattr(pipeline_module, "apply_external_model_execution_policy", None)

        self.assertIsNotNone(policy, "external model runtime policy must exist")
        assert policy is not None
        report = policy(
            {
                "status": "full",
                "full": True,
                "degraded": False,
                "unavailable": False,
                "python": {"ok": True},
                "ffmpeg": {"ok": True},
                "ffprobe": {"ok": True},
                "deepfilternet": {"ok": True, "identity": "verified-deepfilter"},
                "respiro_en": {"ok": True, "identity": "verified-respiro"},
                "spectramini": {"ok": True},
            }
        )

        self.assertEqual(report["status"], "degraded")
        self.assertFalse(report["full"])
        self.assertTrue(report["degraded"])
        self.assertFalse(report["unavailable"])
        for component_name in ("deepfilternet", "respiro_en"):
            self.assertFalse(report[component_name]["ok"])
            self.assertTrue(report[component_name]["asset_verification_ok"])
            self.assertEqual(
                report[component_name]["execution_status"],
                "external_unavailable",
            )

    def test_execution_receipt_validator_accepts_complete_bound_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_path = root / "input.wav"
            output_path = root / "output.wav"
            input_path.write_bytes(b"input-audio")
            output_path.write_bytes(b"processed-audio")
            adapter_bytes = b"trusted-adapter"
            runtime = {
                "identity": "verified-runtime",
                "asset_identity": "deepfilter-model-v1",
                "python_executable": "audio-python",
                "model_path": root / "model.bin",
                "package_version": "0.5.6",
                "revision": "deepfilter-revision-1",
                "license": "MIT",
                "model_sha256": "a" * 64,
                "weights_sha256": None,
                "adapter_path": root / "adapter.py",
                "adapter_version": "adapter-v1",
                "adapter_bytes": adapter_bytes,
                "repo_root": root,
                "execution_id": "current-run",
                "execution_started_ns": 0,
            }
            runtime = pipeline_module._mint_verified_runtime(
                runtime,
                kind="deepfilternet",
            )
            receipt = {
                "schema_version": 1,
                "kind": "deepfilternet",
                "asset_identity": runtime["asset_identity"],
                "package_version": runtime["package_version"],
                "revision": runtime["revision"],
                "license": runtime["license"],
                "model_sha256": runtime["model_sha256"],
                "weights_sha256": None,
                "adapter_version": runtime["adapter_version"],
                "adapter_sha256": hashlib.sha256(adapter_bytes).hexdigest(),
                "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
                "model_loaded": True,
                "execution_id": "current-run",
            }
            validator = getattr(pipeline_module, "validate_execution_receipt", None)

            self.assertIsNotNone(validator, "execution receipt validator must exist")
            assert validator is not None
            validated = validator(
                receipt,
                kind="deepfilternet",
                expected_runtime=runtime,
                input_path=input_path,
                output_path=output_path,
            )

        self.assertEqual(validated, receipt)

    def test_execution_receipt_validator_rejects_missing_identity_fields(self) -> None:
        validator = getattr(pipeline_module, "validate_execution_receipt", None)

        self.assertIsNotNone(validator, "execution receipt validator must exist")
        assert validator is not None
        with self.assertRaisesRegex(RuntimeError, "invalid_execution_receipt"):
            validator(
                {"schema_version": 1, "kind": "deepfilternet", "model_loaded": True},
                kind="deepfilternet",
                expected_runtime={},
                input_path=Path("D:/input.wav"),
                output_path=Path("D:/output.wav"),
            )

    def test_deepfilternet_receipt_rejects_a_weights_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime, receipt, input_path, output_path = _execution_receipt_fixture(
                Path(tmp_dir),
                kind="deepfilternet",
                model_sha256="a" * 64,
                weights_sha256="b" * 64,
            )

            with self.assertRaisesRegex(RuntimeError, "weights_sha256"):
                pipeline_module.validate_execution_receipt(
                    receipt,
                    kind="deepfilternet",
                    expected_runtime=runtime,
                    input_path=input_path,
                    output_path=output_path,
                )

    def test_respiro_receipt_requires_a_lowercase_weights_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime, receipt, input_path, output_path = _execution_receipt_fixture(
                Path(tmp_dir),
                kind="respiro",
                model_sha256=None,
                weights_sha256=None,
            )

            with self.assertRaisesRegex(RuntimeError, "weights_sha256"):
                pipeline_module.validate_execution_receipt(
                    receipt,
                    kind="respiro",
                    expected_runtime=runtime,
                    input_path=input_path,
                    output_path=output_path,
                )

    def test_respiro_receipt_rejects_a_model_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime, receipt, input_path, output_path = _execution_receipt_fixture(
                Path(tmp_dir),
                kind="respiro",
                model_sha256="a" * 64,
                weights_sha256="b" * 64,
            )

            with self.assertRaisesRegex(RuntimeError, "model_sha256"):
                pipeline_module.validate_execution_receipt(
                    receipt,
                    kind="respiro",
                    expected_runtime=runtime,
                    input_path=input_path,
                    output_path=output_path,
                )

    def test_execution_receipt_validator_rejects_no_op_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_path = root / "input.wav"
            output_path = root / "output.wav"
            input_path.write_bytes(b"same-audio")
            output_path.write_bytes(b"same-audio")
            adapter_bytes = b"trusted-adapter"
            digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
            runtime = {
                "identity": "verified-runtime",
                "asset_identity": "deepfilter-model-v1",
                "python_executable": "audio-python",
                "model_path": root / "model.bin",
                "package_version": "0.5.6",
                "revision": "deepfilter-revision-1",
                "license": "MIT",
                "model_sha256": "a" * 64,
                "weights_sha256": None,
                "adapter_path": root / "adapter.py",
                "adapter_version": "adapter-v1",
                "adapter_bytes": adapter_bytes,
                "repo_root": root,
                "execution_id": "current-run",
                "execution_started_ns": 0,
            }
            runtime = pipeline_module._mint_verified_runtime(
                runtime,
                kind="deepfilternet",
            )
            receipt = {
                "schema_version": 1,
                "kind": "deepfilternet",
                "asset_identity": runtime["asset_identity"],
                "package_version": runtime["package_version"],
                "revision": runtime["revision"],
                "license": runtime["license"],
                "model_sha256": runtime["model_sha256"],
                "weights_sha256": None,
                "adapter_version": runtime["adapter_version"],
                "adapter_sha256": hashlib.sha256(adapter_bytes).hexdigest(),
                "input_sha256": digest,
                "output_sha256": digest,
                "model_loaded": True,
                "execution_id": "current-run",
            }
            validator = getattr(pipeline_module, "validate_execution_receipt", None)

            self.assertIsNotNone(validator, "execution receipt validator must exist")
            assert validator is not None
            with self.assertRaisesRegex(RuntimeError, "no_op_output"):
                validator(
                    receipt,
                    kind="deepfilternet",
                    expected_runtime=runtime,
                    input_path=input_path,
                    output_path=output_path,
                )

    def test_execution_receipt_validator_rejects_stale_output_from_an_older_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_path = root / "input.wav"
            output_path = root / "output.wav"
            input_path.write_bytes(b"input-audio")
            output_path.write_bytes(b"stale-processed-audio")
            adapter_bytes = b"trusted-adapter"
            runtime = {
                "identity": "verified-runtime",
                "asset_identity": "deepfilter-model-v1",
                "python_executable": "audio-python",
                "model_path": root / "model.bin",
                "package_version": "0.5.6",
                "revision": "deepfilter-revision-1",
                "license": "MIT",
                "model_sha256": "a" * 64,
                "weights_sha256": None,
                "adapter_path": root / "adapter.py",
                "adapter_version": "adapter-v1",
                "adapter_bytes": adapter_bytes,
                "repo_root": root,
                "execution_id": "current-run",
                "execution_started_ns": output_path.stat().st_mtime_ns + 1,
            }
            runtime = pipeline_module._mint_verified_runtime(
                runtime,
                kind="deepfilternet",
            )
            receipt = {
                "schema_version": 1,
                "kind": "deepfilternet",
                "asset_identity": runtime["asset_identity"],
                "package_version": runtime["package_version"],
                "revision": runtime["revision"],
                "license": runtime["license"],
                "model_sha256": runtime["model_sha256"],
                "weights_sha256": None,
                "adapter_version": runtime["adapter_version"],
                "adapter_sha256": hashlib.sha256(adapter_bytes).hexdigest(),
                "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
                "model_loaded": True,
                "execution_id": "current-run",
            }

            with self.assertRaisesRegex(RuntimeError, "stale_output"):
                pipeline_module.validate_execution_receipt(
                    receipt,
                    kind="deepfilternet",
                    expected_runtime=runtime,
                    input_path=input_path,
                    output_path=output_path,
                )

    def test_execution_receipt_validator_rejects_field_complete_forged_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_path = root / "input.wav"
            output_path = root / "output.wav"
            input_path.write_bytes(b"input-audio")
            output_path.write_bytes(b"processed-audio")
            adapter_bytes = b"trusted-adapter"
            runtime = {
                "identity": "forged-runtime",
                "asset_identity": "deepfilter-model-v1",
                "python_executable": "audio-python",
                "model_path": root / "model.bin",
                "package_version": "0.5.6",
                "revision": "deepfilter-revision-1",
                "license": "MIT",
                "model_sha256": "a" * 64,
                "weights_sha256": None,
                "adapter_path": root / "adapter.py",
                "adapter_version": "adapter-v1",
                "adapter_bytes": adapter_bytes,
                "repo_root": root,
                "execution_id": "current-run",
                "execution_started_ns": 0,
            }
            receipt = {
                "schema_version": 1,
                "kind": "deepfilternet",
                "asset_identity": runtime["asset_identity"],
                "package_version": runtime["package_version"],
                "revision": runtime["revision"],
                "license": runtime["license"],
                "model_sha256": runtime["model_sha256"],
                "weights_sha256": None,
                "adapter_version": runtime["adapter_version"],
                "adapter_sha256": hashlib.sha256(adapter_bytes).hexdigest(),
                "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
                "model_loaded": True,
                "execution_id": "current-run",
            }

            with self.assertRaisesRegex(RuntimeError, "unverified_execution_runtime"):
                pipeline_module.validate_execution_receipt(
                    receipt,
                    kind="deepfilternet",
                    expected_runtime=runtime,
                    input_path=input_path,
                    output_path=output_path,
                )

    def test_verified_adapter_command_blocks_external_model_execution(self) -> None:
        command = [
            "audio-python",
            "D:/trusted/deepfilter_adapter.py",
            "--model",
            "D:/models/model.bin",
        ]
        runtime = _verified_deepfilter_runtime(python_executable="audio-python")

        with (
            mock.patch(
                "audio_sound.pipeline._reverify_runtime_for_execution",
                return_value=True,
            ) as reverify,
            mock.patch("audio_sound.bootstrap._run_trusted_adapter") as trusted_runner,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "external_model_execution_unavailable",
            ):
                _run_verified_adapter_command(command, verified_runtime=runtime)

        reverify.assert_not_called()
        trusted_runner.assert_not_called()

    def test_deepfilternet_execution_preflight_rejects_changed_lock_version(self) -> None:
        runtime = _verified_deepfilter_runtime(python_executable="audio-python")
        current = {
            "ok": True,
            "identity": runtime["asset_identity"],
            "model_path": runtime["model_path"],
            "model_sha256": runtime["model_sha256"],
            "adapter_path": runtime["adapter_path"],
            "adapter_version": runtime["adapter_version"],
            "_adapter_bytes": runtime["adapter_bytes"],
        }
        with (
            mock.patch(
                "audio_sound.bootstrap._verify_deepfilternet_model",
                return_value=current,
            ),
            mock.patch(
                "audio_sound.bootstrap._probe_deepfilternet_adapter",
                return_value={"ok": True},
            ),
            mock.patch(
                "audio_sound.bootstrap._read_trusted_repo_file",
                return_value=(b"deepfilternet==0.5.7\n", ""),
            ),
        ):
            ready = pipeline_module._reverify_runtime_for_execution(
                runtime,
                kind="deepfilternet",
            )

        self.assertFalse(ready)

    def test_deepfilternet_execution_preflight_rejects_changed_package_version(self) -> None:
        runtime = _verified_deepfilter_runtime(python_executable="audio-python")
        current = {
            "ok": True,
            "identity": runtime["asset_identity"],
            "model_path": runtime["model_path"],
            "model_sha256": runtime["model_sha256"],
            "adapter_path": runtime["adapter_path"],
            "adapter_version": runtime["adapter_version"],
            "_adapter_bytes": runtime["adapter_bytes"],
        }
        with (
            mock.patch(
                "audio_sound.bootstrap._verify_deepfilternet_model",
                return_value=current,
            ),
            mock.patch(
                "audio_sound.bootstrap._probe_deepfilternet_adapter",
                return_value={"ok": True},
            ),
            mock.patch(
                "audio_sound.bootstrap._read_trusted_repo_file",
                return_value=(b"deepfilternet==0.5.6\n", ""),
            ),
            mock.patch(
                "audio_sound.bootstrap._inspect_python_runtime",
                return_value={
                    "ok": True,
                    "deepfilternet_ok": True,
                    "deepfilternet_identity": "deepfilternet@999.0",
                },
            ),
        ):
            ready = pipeline_module._reverify_runtime_for_execution(
                runtime,
                kind="deepfilternet",
            )

        self.assertFalse(ready)

    def test_verified_adapter_runner_blocks_external_model_before_reverification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            model = root / "tools" / "deepfilter-model.bin"
            adapter = root / "audio_sound" / "deepfilter_adapter.py"
            manifest = root / "audio_sound" / "runtime-manifest.json"
            model.parent.mkdir(parents=True)
            adapter.parent.mkdir(parents=True)
            original_model = b"verified-model"
            adapter_bytes = b"print('adapter')\n"
            model.write_bytes(original_model)
            adapter.write_bytes(adapter_bytes)
            model_sha256 = hashlib.sha256(original_model).hexdigest()
            adapter_sha256 = hashlib.sha256(adapter_bytes).hexdigest()
            model_identity = f"model-v1;model={model_sha256};adapter=adapter-v1"
            manifest.write_text(
                json.dumps(
                    {
                        "assets": {
                            "deepfilternet": {
                                "path": "tools/deepfilter-model.bin",
                                "size": len(original_model),
                                "sha256": model_sha256,
                                "version": "model-v1",
                                "license": "MIT",
                                "adapter_path": "audio_sound/deepfilter_adapter.py",
                                "adapter_sha256": adapter_sha256,
                                "adapter_version": "adapter-v1",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            def trusted_read(_root: Path, relative: Path):
                if relative == Path("requirements-audio.lock"):
                    return (b"deepfilternet==0.5.6\n", "")
                return ((root / relative).read_bytes(), "")

            with mock.patch(
                "audio_sound.bootstrap._read_trusted_repo_file",
                side_effect=trusted_read,
            ):
                runtime = verified_deepfilternet_runtime(
                    {
                        "python": {"ok": True, "path": "audio-python"},
                        "deepfilternet": {
                            "ok": True,
                            "module_ok": True,
                            "model_ok": True,
                            "adapter_ok": True,
                            "identity": f"deepfilternet@0.5.6;{model_identity}",
                        },
                    },
                    python_executable="audio-python",
                    repo_root=root,
                )
                assert runtime is not None
                model.write_bytes(b"replacement-model")
                command = build_deepfilternet_command(
                    raw_wav=root / "audio.wav",
                    output_dir=root / "output",
                    preset=load_preset("safe"),
                    python_executable="audio-python",
                    verified_runtime=runtime,
                )
                with mock.patch("audio_sound.bootstrap._run_trusted_adapter") as runner:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "external_model_execution_unavailable",
                    ):
                        _run_verified_adapter_command(command, verified_runtime=runtime)

            runner.assert_not_called()

    def test_verified_adapter_runner_blocks_external_weights_before_reverification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "tools" / "Respiro-en"
            weights = root / "tools" / "respiro-en.pt"
            adapter = root / "audio_sound" / "respiro_adapter.py"
            manifest = root / "audio_sound" / "runtime-manifest.json"
            repo.mkdir(parents=True)
            revision = "c" * 40
            git_dir = repo / ".git"
            git_dir.mkdir()
            (git_dir / "HEAD").write_text(revision + "\n", encoding="ascii")
            weights.parent.mkdir(parents=True, exist_ok=True)
            adapter.parent.mkdir(parents=True, exist_ok=True)
            original_weights = b"verified-weights"
            adapter_bytes = b"print('adapter')\n"
            weights.write_bytes(original_weights)
            adapter.write_bytes(adapter_bytes)
            weights_sha256 = hashlib.sha256(original_weights).hexdigest()
            adapter_sha256 = hashlib.sha256(adapter_bytes).hexdigest()
            identity = f"respiro-en@{revision};weights={weights_sha256};adapter=adapter-v1"
            manifest.write_text(
                json.dumps(
                    {
                        "assets": {
                            "respiro_en": {
                                "repo": {"revision": revision, "license": "MIT"},
                                "weights": {
                                    "size": len(original_weights),
                                    "sha256": weights_sha256,
                                },
                                "adapter": {
                                    "path": "audio_sound/respiro_adapter.py",
                                    "sha256": adapter_sha256,
                                    "version": "adapter-v1",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / ".env").write_text(
                "AUDIO_SOUND_RESPIRO_REPO=tools/Respiro-en\n"
                "AUDIO_SOUND_RESPIRO_WEIGHTS=tools/respiro-en.pt\n",
                encoding="utf-8",
            )

            def trusted_read(_root: Path, relative: Path):
                return ((root / relative).read_bytes(), "")

            with (
                mock.patch(
                    "audio_sound.bootstrap._read_trusted_repo_file",
                    side_effect=trusted_read,
                ),
                mock.patch(
                    "audio_sound.bootstrap._run_trusted_adapter",
                ) as runner,
            ):
                runtime = verified_respiro_runtime(
                    {
                        "python": {"ok": True, "path": "audio-python"},
                        "respiro_en": {"ok": True, "identity": identity},
                    },
                    python_executable="audio-python",
                    repo_path=repo,
                    weights_path=weights,
                    repo_root=root,
                )
                self.assertIsNone(runtime)
                self.assertEqual(runner.call_count, 0)

            self.assertEqual(runner.call_count, 0)

    def test_resolve_respiro_runtime_prefers_cli_over_env(self) -> None:
        runtime = resolve_respiro_runtime(
            respiro_repo="D:/cli/repo",
            respiro_weights="D:/cli/respiro-en.pt",
            env_values={
                "AUDIO_SOUND_RESPIRO_REPO": "D:/env/repo",
                "AUDIO_SOUND_RESPIRO_WEIGHTS": "D:/env/respiro-en.pt",
            },
        )
        self.assertEqual(runtime["repo_path"], Path("D:/cli/repo"))
        self.assertEqual(runtime["weights_path"], Path("D:/cli/respiro-en.pt"))

    def test_resolve_respiro_runtime_resolves_relative_env_paths_from_repo_root(self) -> None:
        runtime = resolve_respiro_runtime(
            respiro_repo=None,
            respiro_weights=None,
            env_values={
                "AUDIO_SOUND_RESPIRO_REPO": "tools/audio_sound_runtime/Respiro-en",
                "AUDIO_SOUND_RESPIRO_WEIGHTS": "tools/audio_sound_runtime/respiro-en.pt",
            },
        )

        self.assertEqual(
            runtime["repo_path"],
            Path(__file__).resolve().parents[2] / "tools" / "audio_sound_runtime" / "Respiro-en",
        )
        self.assertEqual(
            runtime["weights_path"],
            Path(__file__).resolve().parents[2] / "tools" / "audio_sound_runtime" / "respiro-en.pt",
        )

    def test_resolve_respiro_runtime_rejects_relative_paths_that_escape_repo_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "remain under the repository root"):
            resolve_respiro_runtime(
                respiro_repo="../outside/Respiro-en",
                respiro_weights="../outside/respiro-en.pt",
            )

    def test_resolve_respiro_runtime_rejects_anchored_non_absolute_paths(self) -> None:
        for unsafe_path in ("C:outside", r"\outside", "../outside"):
            with self.subTest(unsafe_path=unsafe_path):
                with self.assertRaisesRegex(ValueError, "remain under the repository root"):
                    resolve_respiro_runtime(
                        respiro_repo=unsafe_path,
                        respiro_weights="tools/respiro-en.pt",
                    )

    def test_resolve_respiro_runtime_normalizes_relative_paths_inside_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)

            runtime = resolve_respiro_runtime(
                respiro_repo="tools/Respiro-en",
                respiro_weights="tools/respiro-en.pt",
                repo_root=root,
            )

        self.assertEqual(runtime["repo_path"], root / "tools" / "Respiro-en")
        self.assertEqual(runtime["weights_path"], root / "tools" / "respiro-en.pt")

    def test_run_respiro_or_fallback_detection_uses_fallback_when_assets_missing(self) -> None:
        result = run_respiro_or_fallback_detection(
            audio_path=Path("D:/out/audio_clean.wav"),
            ffmpeg_bin="ffmpeg",
            respiro_repo=None,
            respiro_weights=None,
            python_executable="python",
            threshold=0.064,
            min_length_ms=20,
            fallback_config={
                "low_threshold_db": -44,
                "high_threshold_db": -32,
                "silence_min_duration": 0.08,
                "min_breath_ms": 70,
                "max_breath_ms": 320,
                "pre_roll_ms": 25,
                "fade_ms": 14,
                "floor_gain": 0.0,
                "analysis_hop_ms": 18,
                "analysis_scan_ms": 260,
                "noise_floor_ms": 120,
                "speech_start_ratio": 0.78,
                "breath_over_noise_ratio": 2.6,
                "speech_over_breath_ratio": 2.15,
                "speech_confirm_frames": 2,
            },
        )
        self.assertEqual(result.windows, [])
        self.assertEqual(result.mode, "fallback")
        self.assertFalse(result.assets_present)
        self.assertFalse(result.attempted)
        self.assertFalse(result.succeeded)

    def test_run_respiro_or_fallback_detection_uses_respiro_when_command_succeeds(self) -> None:
        repo = Path("D:/models/Respiro-en")
        weights = Path("D:/models/respiro-en.pt")

        class Result:
            returncode = 0
            stdout = '{"intervals":[{"start_seconds":1.25,"end_seconds":1.6}]}'
            stderr = ""

        with (
            mock.patch("pathlib.Path.exists", return_value=True),
            mock.patch(
                "audio_sound.pipeline._run_verified_adapter_command",
                return_value=Result(),
            ) as adapter_runner,
            mock.patch("audio_sound.pipeline.run_command") as raw_runner,
        ):
            result = run_respiro_or_fallback_detection(
                audio_path=Path("D:/out/audio_raw.wav"),
                ffmpeg_bin="ffmpeg",
                respiro_repo=repo,
                respiro_weights=weights,
                python_executable="python",
                threshold=0.064,
                min_length_ms=20,
                fallback_config={},
                verified_runtime=_verified_respiro_runtime(repo, weights),
            )

        adapter_runner.assert_called_once()
        raw_runner.assert_not_called()

        self.assertEqual(result.mode, "respiro")
        self.assertTrue(result.assets_present)
        self.assertTrue(result.attempted)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.windows, [NoiseWindow(start_seconds=1.25, end_seconds=1.6)])
        assert result.command is not None
        self.assertEqual(
            result.command[:2],
            ["python", "D:\\trusted\\audio_sound\\respiro_adapter.py"],
        )
        self.assertIn("D:\\out\\audio_raw.wav", result.command)

    def test_run_respiro_or_fallback_detection_falls_back_when_respiro_command_fails(self) -> None:
        repo = Path("D:/models/Respiro-en")
        weights = Path("D:/models/respiro-en.pt")
        fallback_windows = [NoiseWindow(start_seconds=0.5, end_seconds=0.8)]

        class Result:
            returncode = 1
            stdout = ""
            stderr = "boom"

        with (
            mock.patch("pathlib.Path.exists", return_value=True),
            mock.patch(
                "audio_sound.pipeline._run_verified_adapter_command",
                return_value=Result(),
            ),
            mock.patch(
                "audio_sound.pipeline.detect_breath_onset_windows",
                return_value=fallback_windows,
            ),
        ):
            result = run_respiro_or_fallback_detection(
                audio_path=Path("D:/out/audio_raw.wav"),
                ffmpeg_bin="ffmpeg",
                respiro_repo=repo,
                respiro_weights=weights,
                python_executable="python",
                threshold=0.064,
                min_length_ms=20,
                fallback_config={},
                verified_runtime=_verified_respiro_runtime(repo, weights),
            )

        self.assertEqual(result.mode, "fallback")
        self.assertTrue(result.assets_present)
        self.assertTrue(result.attempted)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.windows, fallback_windows)
        self.assertEqual(result.error, "boom")

    def test_run_respiro_or_fallback_detection_segments_long_audio(self) -> None:
        repo = Path("D:/models/Respiro-en")
        weights = Path("D:/models/respiro-en.pt")

        class Result:
            def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        calls: list[list[str]] = []
        exists_map = {
            "D:\\out\\audio_raw.wav",
            "D:\\models\\Respiro-en",
            "D:\\models\\respiro-en.pt",
        }

        def fake_exists(path_obj: Path) -> bool:
            path_str = str(path_obj)
            return path_str in exists_map or "segment_" in path_str

        def fake_run(command: list[str]):
            calls.append(command)
            if command[0] == "ffmpeg":
                return Result(0)
            segment_id = 0 if any("segment_0000.wav" in part for part in command) else 1
            stdout = (
                '{"intervals":[{"start_seconds":1.0,"end_seconds":1.2}]}'
                if segment_id == 0
                else '{"intervals":[{"start_seconds":2.0,"end_seconds":2.3}]}'
            )
            return Result(0, stdout=stdout)

        def fake_adapter_run(command: list[str], **_kwargs):
            return fake_run(command)

        with (
            mock.patch("pathlib.Path.exists", fake_exists),
            mock.patch(
                "audio_sound.pipeline._read_wave_duration_seconds",
                return_value=130.0,
            ),
            mock.patch(
                "audio_sound.pipeline.ensure_tool",
                side_effect=lambda value: value,
            ),
            mock.patch(
                "audio_sound.pipeline.run_command",
                side_effect=fake_run,
            ),
            mock.patch(
                "audio_sound.pipeline._run_verified_adapter_command",
                side_effect=fake_adapter_run,
            ),
        ):
            result = run_respiro_or_fallback_detection(
                audio_path=Path("D:/out/audio_raw.wav"),
                ffmpeg_bin="ffmpeg",
                respiro_repo=repo,
                respiro_weights=weights,
                python_executable="python",
                threshold=0.064,
                min_length_ms=20,
                fallback_config={},
                verified_runtime=_verified_respiro_runtime(repo, weights),
            )

        self.assertEqual(result.mode, "respiro")
        self.assertTrue(result.succeeded)
        self.assertEqual(
            result.windows,
            [
                NoiseWindow(start_seconds=1.0, end_seconds=1.2),
                NoiseWindow(start_seconds=121.0, end_seconds=121.3),
            ],
        )
        ffmpeg_calls = [command for command in calls if command[0] == "ffmpeg"]
        detect_calls = [command for command in calls if command[0] == "python"]
        self.assertEqual(len(ffmpeg_calls), 2)
        self.assertEqual(len(detect_calls), 2)

    def test_run_respiro_rejects_existing_but_unverified_assets_before_the_runner(self) -> None:
        repo = Path("D:/models/Respiro-en")
        weights = Path("D:/models/respiro-en.pt")
        with (
            mock.patch("pathlib.Path.exists", return_value=True),
            mock.patch("audio_sound.pipeline.run_command") as run_command,
        ):
            with self.assertRaisesRegex(RuntimeError, "unverified_respiro_runtime"):
                run_respiro_or_fallback_detection(
                    audio_path=Path("D:/out/audio_raw.wav"),
                    ffmpeg_bin="ffmpeg",
                    respiro_repo=repo,
                    respiro_weights=weights,
                    python_executable="python",
                    threshold=0.064,
                    min_length_ms=20,
                    fallback_config={},
                    verified_runtime=None,
                )

        run_command.assert_not_called()

    def test_verified_respiro_runtime_rejects_raw_paths_without_doctor_identity(self) -> None:
        runtime = verified_respiro_runtime(
            {"respiro_en": {"ok": False, "identity": ""}},
            python_executable="python",
            repo_path=Path("D:/models/Respiro-en"),
            weights_path=Path("D:/models/respiro-en.pt"),
        )
        self.assertIsNone(runtime)

    def test_verified_respiro_runtime_binds_manifest_paths_and_adapter(self) -> None:
        repo = Path("D:/models/Respiro-en")
        weights = Path("D:/models/respiro-en.pt")
        identity = f"respiro-en@rev-1;weights={'b' * 64};adapter=adapter-v1"
        section = {
            "_env": {
                "AUDIO_SOUND_RESPIRO_REPO": str(repo),
                "AUDIO_SOUND_RESPIRO_WEIGHTS": str(weights),
            },
            "adapter_path": "audio_sound/respiro_adapter.py",
        }
        with (
            mock.patch(
                "audio_sound.bootstrap._verify_respiro_runtime",
                return_value={"ok": True, "identity": identity, "notes": "verified"},
            ),
            mock.patch(
                "audio_sound.bootstrap._load_runtime_asset_section",
                return_value=(section, ""),
            ),
            mock.patch(
                "audio_sound.bootstrap._read_trusted_repo_file",
                return_value=(b"adapter", ""),
            ),
        ):
            runtime = verified_respiro_runtime(
                {
                    "python": {"ok": True, "path": "python"},
                    "respiro_en": {
                        "ok": False,
                        "asset_verification_ok": True,
                        "execution_status": "external_unavailable",
                        "identity": identity,
                    },
                },
                python_executable="python",
                repo_root=Path("D:/repo"),
                repo_path=repo,
                weights_path=weights,
            )

        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertEqual(runtime["repo_path"], repo)
        self.assertEqual(runtime["weights_path"], weights)
        self.assertEqual(runtime["adapter_path"], Path("D:/repo/audio_sound/respiro_adapter.py"))

    def test_apply_spectramini_style_cleanup_to_samples_reduces_click_and_breath_frames(
        self,
    ) -> None:
        from array import array

        samples = array("h", [0, 100, 120, 8000, -8000, 120, 110, 100, 90, 80, 70, 60])
        cleaned = apply_spectramini_style_cleanup_to_samples(
            samples,
            breath_windows=[NoiseWindow(start_seconds=0.0, end_seconds=0.0001)],
            sample_rate=48000,
            attenuation_db=18.0,
            mouth_declick_sensitivity=0.6,
            fade_ms=0.0,
        )
        self.assertLess(max(abs(value) for value in cleaned), max(abs(value) for value in samples))

    def test_spectramini_smoke_fails_closed_when_production_helper_raises(self) -> None:
        required_checks = {
            "int16_output",
            "shape_preserved",
            "finite_output",
            "breath_rms_reduced",
            "click_peak_reduced",
            "memory_roundtrip_ok",
            "feature_finite",
            "deterministic",
        }
        with mock.patch(
            "audio_sound.pipeline.apply_spectramini_style_cleanup_to_samples",
            side_effect=RuntimeError("production helper failed"),
        ) as production_helper:
            report = pipeline_module.run_spectramini_style_smoke()

        production_helper.assert_called_once()
        self.assertFalse(report["runtime_ok"])
        self.assertEqual(report["smoke_status"], "failed")
        self.assertEqual(
            report["algorithm_identity"],
            "auto_cut_spectramini_style_smoke_v1",
        )
        self.assertEqual(set(report["checks"]), required_checks)
        self.assertTrue(all(value is False for value in report["checks"].values()))
        self.assertIn("RuntimeError", report["error"])

    def test_repair_deepfilternet_speech_dropouts_restores_short_real_speech_window(self) -> None:
        from array import array

        sample_rate = 100
        reference = array("h", [0] * 90)
        for index in range(0, 20):
            reference[index] = 5200
        for index in range(20, 26):
            reference[index] = 4200
        for index in range(26, 50):
            reference[index] = 5200
        processed = array("h", reference)
        for index in range(20, 26):
            processed[index] = 120

        repaired, windows = repair_deepfilternet_speech_dropouts(
            reference,
            processed,
            sample_rate=sample_rate,
            window_seconds=0.06,
            hop_seconds=0.01,
            reference_peak_db_min=-24.0,
            reference_rms_db_min=-38.0,
            processed_peak_db_max=-34.0,
            processed_rms_db_max=-46.0,
            copy_padding_seconds=0.0,
            max_repair_duration_seconds=0.16,
            context_window_seconds=0.18,
            context_gap_seconds=0.0,
            context_peak_db_min=-20.0,
            context_rms_db_min=-32.0,
        )

        self.assertEqual(windows, [NoiseWindow(start_seconds=0.2, end_seconds=0.26)])
        self.assertEqual(repaired[20:26], reference[20:26])

    def test_repair_deepfilternet_speech_dropouts_skips_isolated_leading_artifact(self) -> None:
        from array import array

        sample_rate = 100
        reference = array("h", [0] * 90)
        for index in range(20, 26):
            reference[index] = 4200
        for index in range(50, 80):
            reference[index] = 5200
        processed = array("h", reference)
        for index in range(20, 26):
            processed[index] = 120

        repaired, windows = repair_deepfilternet_speech_dropouts(
            reference,
            processed,
            sample_rate=sample_rate,
            window_seconds=0.06,
            hop_seconds=0.01,
            reference_peak_db_min=-24.0,
            reference_rms_db_min=-38.0,
            processed_peak_db_max=-34.0,
            processed_rms_db_max=-46.0,
            copy_padding_seconds=0.0,
            max_repair_duration_seconds=0.16,
            context_window_seconds=0.18,
            context_gap_seconds=0.0,
            context_peak_db_min=-20.0,
            context_rms_db_min=-32.0,
        )

        self.assertEqual(windows, [])
        self.assertEqual(repaired, processed)

    def test_repair_deepfilternet_speech_dropouts_skips_long_low_energy_regions(self) -> None:
        from array import array

        sample_rate = 100
        reference = array("h", [0] * 120)
        for index in range(10, 40):
            reference[index] = 3200
        processed = array("h", [0] * 120)

        repaired, windows = repair_deepfilternet_speech_dropouts(
            reference,
            processed,
            sample_rate=sample_rate,
            window_seconds=0.06,
            hop_seconds=0.01,
            reference_peak_db_min=-24.0,
            reference_rms_db_min=-38.0,
            processed_peak_db_max=-34.0,
            processed_rms_db_max=-46.0,
            copy_padding_seconds=0.0,
            max_repair_duration_seconds=0.16,
        )

        self.assertEqual(windows, [])
        self.assertEqual(repaired, processed)

    def test_attenuation_db_to_gain_converts_db_to_linear_floor(self) -> None:
        self.assertAlmostEqual(attenuation_db_to_gain(18.0), 0.12589254117941673)

    def test_build_respiro_detect_command_passes_repo_weights_and_threshold(self) -> None:
        repo = Path("D:/models/Respiro-en")
        weights = Path("D:/models/respiro-en.pt")
        command = build_respiro_detect_command(
            audio_path=Path("D:/out/audio_raw.wav"),
            python_executable="python",
            repo_path=repo,
            weights_path=weights,
            threshold=0.5,
            min_length_ms=30,
            verified_runtime=_verified_respiro_runtime(repo, weights),
        )
        self.assertEqual(
            command[0:2],
            ["python", "D:\\trusted\\audio_sound\\respiro_adapter.py"],
        )
        self.assertEqual(
            command[2:],
            [
                "--repo",
                "D:\\models\\Respiro-en",
                "--weights",
                "D:\\models\\respiro-en.pt",
                "--audio",
                "D:\\out\\audio_raw.wav",
                "--threshold",
                "0.5",
                "--min-length-ms",
                "30",
                "--json",
            ],
        )

    def test_build_respiro_detect_command_rejects_a_field_complete_forged_receipt(self) -> None:
        repo = Path("D:/models/Respiro-en")
        weights = Path("D:/models/respiro-en.pt")
        forged_runtime = {
            "identity": "forged",
            "repo_path": repo,
            "weights_path": weights,
            "adapter_path": Path("D:/trusted/respiro_adapter.py"),
            "adapter_bytes": b"forged-adapter",
            "repo_root": Path("D:/repo"),
        }
        with self.assertRaisesRegex(RuntimeError, "unverified_respiro_runtime"):
            build_respiro_detect_command(
                audio_path=Path("D:/out/audio_raw.wav"),
                python_executable="python",
                repo_path=repo,
                weights_path=weights,
                threshold=0.5,
                min_length_ms=30,
                verified_runtime=forged_runtime,
            )

    def test_build_respiro_detect_command_rejects_a_different_python_than_self_check(
        self,
    ) -> None:
        repo = Path("D:/models/Respiro-en")
        weights = Path("D:/models/respiro-en.pt")
        runtime = _verified_respiro_runtime(
            repo,
            weights,
            python_executable="verified-python",
        )
        with self.assertRaisesRegex(RuntimeError, "unverified_respiro_runtime"):
            build_respiro_detect_command(
                audio_path=Path("D:/out/audio_raw.wav"),
                python_executable="different-python",
                repo_path=repo,
                weights_path=weights,
                threshold=0.5,
                min_length_ms=30,
                verified_runtime=runtime,
            )

    def test_build_breath_processing_plan_prefers_respiro_then_spectra_then_deepfilternet(
        self,
    ) -> None:
        preset = load_preset("safe")
        plan = build_breath_processing_plan(
            preset,
            attenuation_db=18.0,
            skip_spectramini=False,
            skip_deepfilternet=False,
        )
        self.assertEqual(
            [step["type"] for step in plan], ["respiro", "spectramini", "deepfilternet"]
        )
        self.assertEqual(plan[0]["attenuation_db"], 18.0)

    def test_build_breath_processing_plan_can_skip_optional_stages(self) -> None:
        preset = load_preset("safe")
        plan = build_breath_processing_plan(
            preset,
            attenuation_db=12.0,
            skip_spectramini=True,
            skip_deepfilternet=True,
        )
        self.assertEqual([step["type"] for step in plan], ["respiro"])

    def test_discover_media_files_supports_audio_and_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "voice.wav").write_bytes(b"")
            (root / "clip.mp4").write_bytes(b"")
            nested = root / "nested"
            nested.mkdir()
            (nested / "lesson.mov").write_bytes(b"")
            (nested / "notes.txt").write_text("ignore", encoding="utf-8")

            top_only = discover_media_files(root, recursive=False)
            recursive = discover_media_files(root, recursive=True)

            self.assertEqual([item.name for item in top_only], ["clip.mp4", "voice.wav"])
            self.assertEqual(
                [item.name for item in recursive], ["clip.mp4", "lesson.mov", "voice.wav"]
            )

    def test_build_output_layout_uses_source_name_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_root = Path(tmp_dir) / "output"
            layout = build_output_layout(
                input_path=Path("D:/media/My Clip 01.mp4"),
                output_root=output_root,
                run_slug="20260527-120000",
            )

            self.assertEqual(layout.job_name, "20260527-120000_My Clip 01")
            self.assertTrue(str(layout.preprocess_dir).endswith("audio_preprocess"))
            self.assertTrue(str(layout.transcript_dir).endswith("transcript_ready"))
            self.assertEqual(layout.raw_wav.name, "My Clip 01_raw.wav")
            self.assertEqual(layout.denoised_wav.name, "My Clip 01_df.wav")
            self.assertEqual(layout.noise_sample_wav.name, "My Clip 01_noise_sample.wav")
            self.assertEqual(layout.clean_wav.name, "My Clip 01_clean.wav")
            self.assertEqual(layout.transcript_mp3.name, "My Clip 01_transcript.mp3")
            self.assertEqual(layout.report_json.name, "audio_process_report.json")

    def test_build_output_layout_uses_neutral_paths_when_deepfilternet_is_skipped(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            layout = build_output_layout(
                input_path=Path("D:/media/My Clip 01.mp4"),
                output_root=Path(tmp_dir) / "output",
                run_slug="20260527-120000",
                skip_deepfilternet=True,
            )

        self.assertEqual(layout.denoised_wav.name, "My Clip 01_local_pre_master.wav")
        self.assertIsNone(layout.deepfilternet_dir)

    def test_build_output_layout_preserves_unicode_source_name_for_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_root = Path(tmp_dir) / "output"
            source = Path("C:/Users/example-user/Downloads/?????????-??.wav")
            layout = build_output_layout(
                input_path=source,
                output_root=output_root,
                run_slug="20260527-120000",
            )

            self.assertEqual(layout.job_name, "20260527-120000_?????????-??")
            self.assertEqual(layout.raw_wav.name, "?????????-??_raw.wav")
            self.assertEqual(layout.clean_wav.name, "?????????-??_clean.wav")
            self.assertEqual(layout.transcript_mp3.name, "?????????-??_transcript.mp3")

    def test_build_ffmpeg_extract_command_uses_preset_values(self) -> None:
        preset = load_preset("safe")
        command = build_ffmpeg_extract_command(
            input_path=Path("D:/input/source.mp4"),
            raw_wav=Path("D:/out/audio_raw.wav"),
            preset=preset,
            ffmpeg_bin="ffmpeg",
        )
        self.assertEqual(
            command,
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-nostdin",
                "-i",
                "D:\\input\\source.mp4",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                "D:\\out\\audio_raw.wav",
            ],
        )

    def test_build_deepfilternet_command_rejects_an_unverified_runtime(self) -> None:
        preset = load_preset("safe")
        with self.assertRaisesRegex(RuntimeError, "unverified_deepfilternet_runtime"):
            build_deepfilternet_command(
                raw_wav=Path("D:/out/audio_raw.wav"),
                output_dir=Path("D:/out/df"),
                preset=preset,
                python_executable="python",
                verified_runtime=None,
            )

    def test_build_deepfilternet_command_rejects_path_only_runtime_evidence(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unverified_deepfilternet_runtime"):
            build_deepfilternet_command(
                raw_wav=Path("D:/out/audio_raw.wav"),
                output_dir=Path("D:/out/df"),
                preset=load_preset("safe"),
                python_executable="python",
                verified_runtime={
                    "adapter_path": Path("D:/trusted/deepfilter_adapter.py"),
                    "model_path": Path("D:/models/deepfilter-model.bin"),
                    "identity": "forged",
                },
            )

    def test_build_deepfilternet_command_rejects_a_field_complete_forged_receipt(self) -> None:
        forged_runtime = {
            "identity": "forged",
            "model_path": Path("D:/models/deepfilter-model.bin"),
            "adapter_path": Path("D:/trusted/deepfilter_adapter.py"),
            "adapter_bytes": b"forged-adapter",
            "repo_root": Path("D:/repo"),
        }
        with self.assertRaisesRegex(RuntimeError, "unverified_deepfilternet_runtime"):
            build_deepfilternet_command(
                raw_wav=Path("D:/out/audio_raw.wav"),
                output_dir=Path("D:/out/df"),
                preset=load_preset("safe"),
                python_executable="python",
                verified_runtime=forged_runtime,
            )

    def test_build_deepfilternet_command_rejects_a_different_python_than_self_check(
        self,
    ) -> None:
        runtime = _verified_deepfilter_runtime(python_executable="verified-python")
        with self.assertRaisesRegex(RuntimeError, "unverified_deepfilternet_runtime"):
            build_deepfilternet_command(
                raw_wav=Path("D:/out/audio_raw.wav"),
                output_dir=Path("D:/out/df"),
                preset=load_preset("safe"),
                python_executable="different-python",
                verified_runtime=runtime,
            )

    def test_build_deepfilternet_command_rejects_a_mutated_verified_receipt(self) -> None:
        runtime = _verified_deepfilter_runtime()
        runtime["adapter_bytes"] = b"replacement-after-verification"
        with self.assertRaisesRegex(RuntimeError, "unverified_deepfilternet_runtime"):
            build_deepfilternet_command(
                raw_wav=Path("D:/out/audio_raw.wav"),
                output_dir=Path("D:/out/df"),
                preset=load_preset("safe"),
                python_executable="python",
                verified_runtime=runtime,
            )

    def test_build_deepfilternet_command_uses_only_the_verified_adapter_and_model(self) -> None:
        preset = load_preset("safe")
        command = build_deepfilternet_command(
            raw_wav=Path("D:/out/audio_raw.wav"),
            output_dir=Path("D:/out/df"),
            preset=preset,
            python_executable="python",
            verified_runtime=_verified_deepfilter_runtime(),
        )
        self.assertEqual(
            command,
            [
                "python",
                "D:\\trusted\\deepfilter_adapter.py",
                "--model",
                "D:\\models\\deepfilter-model.bin",
                "--output-dir",
                "D:\\out\\df",
                "--post-filter",
                "D:\\out\\audio_raw.wav",
            ],
        )
        self.assertNotIn("-m", command)
        self.assertNotIn("df.enhance", command)

    def test_verified_deepfilternet_runtime_rejects_an_installed_module_without_assets(
        self,
    ) -> None:
        runtime = verified_deepfilternet_runtime(
            {
                "deepfilternet": {
                    "ok": False,
                    "module_ok": True,
                    "model_ok": False,
                    "adapter_ok": False,
                    "identity": "deepfilternet@0.5.6",
                }
            },
            python_executable="python",
        )
        self.assertIsNone(runtime)

    def test_verified_deepfilternet_runtime_binds_doctor_and_manifest_identity(self) -> None:
        model_sha256 = "a" * 64
        model_identity = f"model-v1;model={model_sha256};adapter=adapter-v1"
        model_runtime = {
            "ok": True,
            "identity": model_identity,
            "model_path": "D:/models/deepfilter-model.bin",
            "model_sha256": model_sha256,
            "adapter_path": "D:/trusted/deepfilter_adapter.py",
            "adapter_version": "adapter-v1",
            "_adapter_bytes": b"adapter",
            "_repo_root": "D:/repo",
        }
        with (
            mock.patch(
                "audio_sound.bootstrap._verify_deepfilternet_model",
                return_value=model_runtime,
            ),
            mock.patch(
                "audio_sound.bootstrap._read_trusted_repo_file",
                return_value=(b"deepfilternet==0.5.6\n", ""),
            ),
        ):
            runtime = verified_deepfilternet_runtime(
                {
                    "python": {"ok": True, "path": "python"},
                    "deepfilternet": {
                        "ok": False,
                        "asset_verification_ok": True,
                        "execution_status": "external_unavailable",
                        "module_ok": True,
                        "model_ok": True,
                        "adapter_ok": True,
                        "identity": f"deepfilternet@0.5.6;{model_identity}",
                    },
                },
                python_executable="python",
                repo_root=Path("D:/repo"),
            )

        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertEqual(runtime["model_sha256"], model_sha256)
        self.assertEqual(runtime["adapter_version"], "adapter-v1")

    def test_verified_deepfilternet_runtime_rejects_a_mismatched_lock_version(self) -> None:
        model_sha256 = "a" * 64
        model_identity = f"model-v1;model={model_sha256};adapter=adapter-v1"
        model_runtime = {
            "ok": True,
            "identity": model_identity,
            "model_path": "D:/models/deepfilter-model.bin",
            "model_sha256": model_sha256,
            "adapter_path": "D:/trusted/deepfilter_adapter.py",
            "adapter_version": "adapter-v1",
            "_adapter_bytes": b"adapter",
            "_repo_root": "D:/repo",
        }
        with (
            mock.patch(
                "audio_sound.bootstrap._verify_deepfilternet_model",
                return_value=model_runtime,
            ),
            mock.patch(
                "audio_sound.bootstrap._read_trusted_repo_file",
                return_value=(b"deepfilternet==0.5.7\n", ""),
            ),
        ):
            runtime = verified_deepfilternet_runtime(
                {
                    "python": {"ok": True, "path": "python"},
                    "deepfilternet": {
                        "ok": True,
                        "module_ok": True,
                        "model_ok": True,
                        "adapter_ok": True,
                        "identity": f"deepfilternet@0.5.6;{model_identity}",
                    },
                },
                python_executable="python",
                repo_root=Path("D:/repo"),
            )

        self.assertIsNone(runtime)

    def test_verified_deepfilternet_runtime_rejects_a_non_locked_package_version(self) -> None:
        model_sha256 = "a" * 64
        model_identity = f"model-v1;model={model_sha256};adapter=adapter-v1"
        model_runtime = {
            "ok": True,
            "identity": model_identity,
            "model_path": "D:/models/deepfilter-model.bin",
            "model_sha256": model_sha256,
            "adapter_path": "D:/trusted/deepfilter_adapter.py",
            "adapter_version": "adapter-v1",
            "_adapter_bytes": b"adapter",
            "_repo_root": "D:/repo",
        }
        with mock.patch(
            "audio_sound.bootstrap._verify_deepfilternet_model",
            return_value=model_runtime,
        ):
            runtime = verified_deepfilternet_runtime(
                {
                    "python": {"ok": True, "path": "python"},
                    "deepfilternet": {
                        "ok": True,
                        "module_ok": True,
                        "model_ok": True,
                        "adapter_ok": True,
                        "identity": f"deepfilternet@999.0;{model_identity}",
                    },
                },
                python_executable="python",
                repo_root=Path(__file__).resolve().parents[2],
            )

        self.assertIsNone(runtime)

    def test_process_media_file_rejects_unverified_deepfilternet_before_ffprobe(self) -> None:
        with (
            mock.patch(
                "audio_sound.pipeline.ffprobe_media",
                side_effect=AssertionError("ffprobe must not run"),
            ) as ffprobe_media,
            mock.patch("audio_sound.pipeline.run_command") as run_command,
        ):
            with self.assertRaisesRegex(RuntimeError, "unverified_deepfilternet_runtime"):
                process_media_file(
                    Path("D:/input.wav"),
                    preset_name="safe",
                    preset=load_preset("safe"),
                    output_root=Path("D:/output"),
                    runtime=RuntimeOptions(),
                    run_slug="20260723-120000",
                )

        ffprobe_media.assert_not_called()
        run_command.assert_not_called()

    def test_process_media_file_rejects_verified_external_deepfilternet_before_ffprobe(
        self,
    ) -> None:
        with mock.patch("audio_sound.pipeline.ffprobe_media") as ffprobe_media:
            with self.assertRaisesRegex(
                RuntimeError,
                "external_model_execution_unavailable",
            ):
                process_media_file(
                    Path("D:/input.wav"),
                    preset_name="safe",
                    preset=load_preset("safe"),
                    output_root=Path("D:/output"),
                    runtime=RuntimeOptions(dry_run=True, python_executable="python"),
                    run_slug="20260723-120000",
                    deepfilternet_runtime=_verified_deepfilter_runtime(),
                )

        ffprobe_media.assert_not_called()

    def test_process_media_file_rejects_unverified_respiro_before_ffprobe(self) -> None:
        with (
            mock.patch(
                "audio_sound.pipeline.ffprobe_media",
                side_effect=AssertionError("ffprobe must not run"),
            ) as ffprobe_media,
            mock.patch("audio_sound.pipeline.run_command") as run_command,
        ):
            with self.assertRaisesRegex(RuntimeError, "unverified_respiro_runtime"):
                process_media_file(
                    Path("D:/input.wav"),
                    preset_name="safe",
                    preset=load_preset("safe"),
                    output_root=Path("D:/output"),
                    runtime=RuntimeOptions(),
                    run_slug="20260723-120000",
                    respiro_repo=Path("D:/models/Respiro-en"),
                    respiro_weights=Path("D:/models/respiro-en.pt"),
                    skip_deepfilternet=True,
                )

        ffprobe_media.assert_not_called()
        run_command.assert_not_called()

    def test_process_media_file_rejects_verified_external_respiro_before_ffprobe(self) -> None:
        repo = Path("D:/models/Respiro-en")
        weights = Path("D:/models/respiro-en.pt")
        with mock.patch("audio_sound.pipeline.ffprobe_media") as ffprobe_media:
            with self.assertRaisesRegex(
                RuntimeError,
                "external_model_execution_unavailable",
            ):
                process_media_file(
                    Path("D:/input.wav"),
                    preset_name="safe",
                    preset=load_preset("safe"),
                    output_root=Path("D:/output"),
                    runtime=RuntimeOptions(dry_run=True, python_executable="python"),
                    run_slug="20260723-120000",
                    respiro_repo=repo,
                    respiro_weights=weights,
                    skip_deepfilternet=True,
                    respiro_verified_runtime=_verified_respiro_runtime(repo, weights),
                )

        ffprobe_media.assert_not_called()

    def test_build_ffmpeg_finalize_commands_include_clean_and_transcript_exports(self) -> None:
        preset = load_preset("review")
        commands = build_ffmpeg_finalize_commands(
            denoised_wav=Path("D:/out/audio_df.wav"),
            clean_wav=Path("D:/out/audio_clean.wav"),
            transcript_mp3=Path("D:/out/transcript_ready/audio.mp3"),
            preset=preset,
            ffmpeg_bin="ffmpeg",
        )
        self.assertEqual(len(commands), 2)
        self.assertIn("loudnorm=I=-16.8:LRA=7.0:TP=-3.0:print_format=json", commands[0][7])
        self.assertEqual(commands[0][8:10], ["-ar", "48000"])
        self.assertEqual(commands[1][-2:], ["192k", "D:\\out\\transcript_ready\\audio.mp3"])

    def test_measure_segment_levels_reports_peak_and_rms_dbfs(self) -> None:
        from array import array

        samples = array("h", [0, 0, 1000, -1000])
        peak_db, rms_db = measure_segment_levels(samples, start_index=0, end_index=len(samples))
        self.assertLess(peak_db, -20.0)
        self.assertLess(rms_db, peak_db)

    def test_infer_pause_residual_cleanup_windows_captures_core_silence_and_tiny_bridge(
        self,
    ) -> None:
        from array import array

        sample_rate = 10
        samples = array("h", [0] * 40)
        samples[18] = 20
        samples[19] = -20
        silence_candidates = [
            {"start_seconds": 0.0, "end_seconds": 1.8, "duration_seconds": 1.8},
            {"start_seconds": 2.0, "end_seconds": 4.0, "duration_seconds": 2.0},
        ]

        windows = infer_pause_residual_cleanup_windows(
            samples,
            sample_rate=sample_rate,
            silence_candidates=silence_candidates,
            min_neighbor_silence_duration=0.6,
            bridge_max_duration=0.3,
            bridge_peak_db=-50.0,
            bridge_rms_db=-55.0,
            core_pad_seconds=0.1,
        )

        self.assertEqual(
            windows,
            [
                NoiseWindow(start_seconds=0.1, end_seconds=1.7),
                NoiseWindow(start_seconds=1.8, end_seconds=2.0),
                NoiseWindow(start_seconds=2.1, end_seconds=3.9),
            ],
        )

    def test_parse_noise_window_accepts_start_end_seconds(self) -> None:
        window = parse_noise_window("161.583729:169.578792")
        self.assertEqual(window, NoiseWindow(start_seconds=161.583729, end_seconds=169.578792))

    def test_parse_noise_window_rejects_invalid_ranges(self) -> None:
        with self.assertRaises(ValueError):
            parse_noise_window("3.0:3.0")

        with self.assertRaises(ValueError):
            parse_noise_window("bad-value")

    def test_infer_breath_onset_windows_finds_short_bridge_between_low_and_high_silence(
        self,
    ) -> None:
        windows = infer_breath_onset_windows(
            [
                {"start_seconds": 12.0, "end_seconds": 12.92, "duration_seconds": 0.92},
            ],
            [
                {"start_seconds": 12.9, "end_seconds": 13.18, "duration_seconds": 0.28},
            ],
            min_breath_seconds=0.08,
            max_breath_seconds=0.45,
            pre_roll_seconds=0.03,
        )
        self.assertEqual(windows, [NoiseWindow(start_seconds=12.89, end_seconds=13.18)])

    def test_duck_samples_for_windows_attentuates_target_region(self) -> None:
        from array import array

        samples = array("h", [1000] * 20)
        duck_samples_for_windows(
            samples,
            sample_rate=10,
            windows=[NoiseWindow(start_seconds=0.5, end_seconds=1.0)],
            floor_gain=0.0,
            fade_ms=0.0,
        )
        self.assertEqual(samples[4], 1000)
        self.assertEqual(samples[5], 0)
        self.assertEqual(samples[9], 0)
        self.assertEqual(samples[10], 1000)

    def test_infer_breath_window_from_frame_rms_detects_breath_before_speech(self) -> None:
        inferred = infer_breath_window_from_frame_rms(
            [8.0, 11.0, 14.0, 38.0, 45.0],
            silence_floor_rms=2.0,
            hop_seconds=0.02,
            min_breath_seconds=0.04,
            max_breath_seconds=0.14,
            speech_start_ratio=0.75,
            breath_over_noise_ratio=3.0,
            speech_over_breath_ratio=2.2,
            speech_confirm_frames=2,
        )
        self.assertEqual(inferred, (0.0, 0.06))

    def test_infer_breath_window_from_frame_rms_rejects_near_silence(self) -> None:
        inferred = infer_breath_window_from_frame_rms(
            [2.1, 2.2, 2.0, 2.3, 2.2],
            silence_floor_rms=2.0,
            hop_seconds=0.02,
            min_breath_seconds=0.04,
            max_breath_seconds=0.14,
            speech_start_ratio=0.75,
            breath_over_noise_ratio=3.0,
            speech_over_breath_ratio=2.2,
            speech_confirm_frames=2,
        )
        self.assertIsNone(inferred)

    def test_build_ffmpeg_noise_sample_command_concatenates_multiple_segments(self) -> None:
        preset = load_preset("voice-isolate")
        command = build_ffmpeg_noise_sample_command(
            source_wav=Path("D:/out/audio_df.wav"),
            noise_sample_wav=Path("D:/out/audio_noise_sample.wav"),
            noise_windows=[
                NoiseWindow(start_seconds=143.089208, end_seconds=144.093687),
                NoiseWindow(start_seconds=161.583729, end_seconds=169.578792),
            ],
            preset=preset,
            ffmpeg_bin="ffmpeg",
        )
        self.assertEqual(
            command[:8],
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-nostdin",
                "-i",
                "D:\\out\\audio_df.wav",
                "-filter_complex",
                command[7],
            ],
        )
        self.assertIn(
            "[0:a]atrim=start=143.089208:end=144.093687,asetpts=PTS-STARTPTS[s0]", command[7]
        )
        self.assertIn(
            "[0:a]atrim=start=161.583729:end=169.578792,asetpts=PTS-STARTPTS[s1]", command[7]
        )
        self.assertIn("[s0][s1]concat=n=2:v=0:a=1[outa]", command[7])
        self.assertEqual(command[-1], "D:\\out\\audio_noise_sample.wav")

    def test_build_ffmpeg_finalize_commands_use_noise_sample_profile_when_present(self) -> None:
        preset = load_preset("voice-isolate")
        commands = build_ffmpeg_finalize_commands(
            denoised_wav=Path("D:/out/audio_df.wav"),
            clean_wav=Path("D:/out/audio_clean.wav"),
            transcript_mp3=Path("D:/out/transcript_ready/audio.mp3"),
            preset=preset,
            ffmpeg_bin="ffmpeg",
            noise_sample_wav=Path("D:/out/audio_noise_sample.wav"),
            noise_sample_duration=8.5,
        )
        self.assertEqual(len(commands), 2)
        filter_complex_index = commands[0].index("-filter_complex") + 1
        filter_complex = commands[0][filter_complex_index]
        self.assertIn("afftdn=", filter_complex)
        self.assertIn("sn=start", filter_complex)
        self.assertIn("atrim=start=8.5", filter_complex)
        self.assertIn("adeclick=", filter_complex)
        self.assertIn("deesser=", filter_complex)
        self.assertNotIn("sidechaingate=", filter_complex)
        self.assertNotIn("asplit=2", filter_complex)
        self.assertEqual(filter_complex.count("afftdn="), 1)
        self.assertEqual(filter_complex.count("loudnorm="), 1)
        self.assertEqual(filter_complex.count("adeclick="), 1)
        self.assertEqual(filter_complex.count("sidechaingate="), 0)
        self.assertIn("-map", commands[0])
        self.assertEqual(commands[0][-1], "D:\\out\\audio_clean.wav")

    def test_build_ffmpeg_finalize_commands_can_opt_into_legacy_breath_filters(self) -> None:
        preset = apply_runtime_overrides(
            load_preset("voice-isolate"),
            enable_legacy_breath_filters=True,
        )
        commands = build_ffmpeg_finalize_commands(
            denoised_wav=Path("D:/out/audio_df.wav"),
            clean_wav=Path("D:/out/audio_clean.wav"),
            transcript_mp3=Path("D:/out/transcript_ready/audio.mp3"),
            preset=preset,
            ffmpeg_bin="ffmpeg",
            noise_sample_wav=Path("D:/out/audio_noise_sample.wav"),
            noise_sample_duration=8.5,
        )
        filter_complex_index = commands[0].index("-filter_complex") + 1
        filter_complex = commands[0][filter_complex_index]
        self.assertIn("sidechaingate=", filter_complex)
        self.assertIn("asplit=2", filter_complex)

    def test_build_mastering_filter_chain_omits_invalid_zero_makeup(self) -> None:
        preset = load_preset("safe")
        filter_chain = build_mastering_filter_chain(preset)
        self.assertIn("agate=", filter_chain)
        self.assertNotIn("makeup=0", filter_chain)

    def test_repair_filter_chain_contains_declick_and_deesser(self) -> None:
        preset = load_preset("repair")
        filter_chain = build_mastering_filter_chain(preset)
        self.assertIn("adeclick=", filter_chain)
        self.assertIn("deesser=", filter_chain)
        self.assertIn("speechnorm=", filter_chain)

    def test_repair_soft_filter_chain_keeps_declick_but_relaxes_processing(self) -> None:
        preset = load_preset("repair-soft")
        filter_chain = build_mastering_filter_chain(preset)
        self.assertIn("adeclick=", filter_chain)
        self.assertIn("deesser=", filter_chain)
        self.assertNotIn("speechnorm=", filter_chain)

    def test_final_filter_chain_keeps_declick_and_deesser_without_speechnorm(self) -> None:
        preset = load_preset("final")
        filter_chain = build_mastering_filter_chain(preset)
        self.assertIn("adeclick=", filter_chain)
        self.assertIn("deesser=", filter_chain)
        self.assertNotIn("speechnorm=", filter_chain)

    def test_process_media_file_skip_spectramini_preserves_raw_audio_with_breath_windows(
        self,
    ) -> None:
        from array import array

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "voice.wav"
            source.write_bytes(b"fake")
            run_slug = "20260527-120000"
            output_root = build_output_root(root / "output", run_slug)
            layout = build_output_layout(
                input_path=source,
                output_root=output_root,
                run_slug=run_slug,
                skip_deepfilternet=True,
            )
            original_samples = array("h", [1000] * 4800)
            preset = load_preset("safe")
            preset["filters"]["pause_residual_cleanup"]["enabled"] = False

            def write_wav(path: Path, samples: array) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(path), "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(48000)
                    writer.writeframes(samples.tobytes())

            def fake_run(command: list[str]):
                output_path = Path(command[-1])
                if output_path == layout.raw_wav:
                    write_wav(layout.raw_wav, original_samples)
                elif output_path == layout.clean_wav:
                    layout.clean_wav.write_bytes(layout.denoised_wav.read_bytes())
                elif output_path == layout.transcript_mp3:
                    layout.transcript_mp3.write_bytes(b"mp3")

                class Result:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return Result()

            respiro_result = RespiroDetectionResult(
                windows=[NoiseWindow(start_seconds=0.02, end_seconds=0.04)],
                mode="fallback",
                assets_present=False,
                attempted=False,
                succeeded=False,
            )
            with (
                mock.patch("audio_sound.pipeline.ensure_tool", side_effect=lambda value: value),
                mock.patch("audio_sound.pipeline.run_command", side_effect=fake_run),
                mock.patch(
                    "audio_sound.pipeline.run_respiro_or_fallback_detection",
                    return_value=respiro_result,
                ),
                mock.patch(
                    "audio_sound.pipeline.ffprobe_media",
                    return_value={"duration_seconds": 0.1, "sample_rate": 48000, "channels": 1},
                ),
            ):
                report = process_media_file(
                    source,
                    preset_name="safe",
                    preset=preset,
                    output_root=output_root,
                    runtime=RuntimeOptions(
                        ffmpeg_bin="ffmpeg",
                        ffprobe_bin="ffprobe",
                        python_executable="python",
                    ),
                    run_slug=run_slug,
                    input_metadata={"duration_seconds": 0.1, "sample_rate": 48000, "channels": 1},
                    skip_spectramini=True,
                    skip_deepfilternet=True,
                )

            with wave.open(str(layout.raw_wav), "rb") as reader:
                processed_samples = array("h")
                processed_samples.frombytes(reader.readframes(reader.getnframes()))

            self.assertEqual(processed_samples, original_samples)
            self.assertFalse(report["spectramini_applied"])
            self.assertEqual(
                report["model_execution"]["deepfilternet"],
                {
                    "status": "skipped",
                    "attempted": False,
                    "succeeded": False,
                    "execution_receipt": None,
                },
            )
            self.assertEqual(
                report["model_execution"]["respiro"],
                {
                    "status": "fallback",
                    "attempted": False,
                    "succeeded": False,
                    "execution_receipt": None,
                },
            )
            self.assertIn("Local fallback breath detection", report["processing_steps"])
            self.assertNotIn("Breath detection via Respiro-en", report["processing_steps"])
            self.assertNotIn("Primary denoise via DeepFilterNet", report["processing_steps"])
            self.assertTrue(layout.denoised_wav.exists())
            self.assertEqual(layout.denoised_wav.name, "voice_local_pre_master.wav")
            self.assertFalse(any(layout.job_dir.rglob("*_df.wav")))
            self.assertFalse(
                any(path.name == "deepfilternet_out" for path in layout.job_dir.rglob("*"))
            )
            markdown = pipeline_module.render_markdown_report(report)
            self.assertIn("DeepFilterNet status: `skipped`", markdown)
            self.assertIn("Respiro status: `fallback`", markdown)

    def test_process_media_file_dry_run_reports_local_standalone_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "voice.wav"
            source.write_bytes(b"fake")
            preset = load_preset("safe")
            runtime = RuntimeOptions(dry_run=True, python_executable="python")
            report = process_media_file(
                source,
                preset_name="safe",
                preset=preset,
                output_root=build_output_root(root / "output", "20260527-120000"),
                runtime=runtime,
                run_slug="20260527-120000",
                input_metadata={"duration_seconds": 10.0, "sample_rate": 48000, "channels": 1},
                skip_deepfilternet=True,
            )

            self.assertTrue(report["dry_run"])
            self.assertEqual(report["backend"], "local")
            self.assertEqual(len(report["commands"]), 3)
            self.assertIn("Local fallback breath detection", report["processing_steps"])
            self.assertNotIn("Breath detection via Respiro-en", report["processing_steps"])
            self.assertIn("SpectraMini-style breath control", report["processing_steps"])
            self.assertNotIn("Primary denoise via DeepFilterNet", report["processing_steps"])
            self.assertEqual(
                report["model_execution"]["deepfilternet"]["status"],
                "skipped",
            )
            self.assertEqual(report["execution_policy"], "external_models_fail_closed")
            self.assertEqual(
                Path(report["outputs"]["denoised_wav"]).name,
                "voice_local_pre_master.wav",
            )
            self.assertFalse(
                any("deepfilternet" in str(note).casefold() for note in report["notes"])
            )
            self.assertTrue(report["outputs"]["clean_wav"].endswith("voice_clean.wav"))

    def test_process_media_file_dry_run_with_noise_windows_adds_noise_sample_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "voice.wav"
            source.write_bytes(b"fake")
            preset = load_preset("voice-isolate")
            runtime = RuntimeOptions(dry_run=True, python_executable="python")
            report = process_media_file(
                source,
                preset_name="voice-isolate",
                preset=preset,
                output_root=build_output_root(root / "output", "20260527-120000"),
                runtime=runtime,
                run_slug="20260527-120000",
                input_metadata={"duration_seconds": 10.0, "sample_rate": 48000, "channels": 1},
                noise_windows=[NoiseWindow(start_seconds=1.0, end_seconds=2.5)],
                skip_deepfilternet=True,
            )

            self.assertTrue(report["dry_run"])
            self.assertEqual(len(report["commands"]), 4)
            self.assertEqual(report["noise_sample_duration_seconds"], 1.5)
            self.assertEqual(report["noise_windows"][0]["start_seconds"], 1.0)
            self.assertIn("Local fallback breath detection", report["processing_steps"])
            self.assertNotIn("Breath detection via Respiro-en", report["processing_steps"])
            self.assertIn("SpectraMini-style breath control", report["processing_steps"])
            self.assertIn("Capture noise sample from selected windows", report["processing_steps"])
            self.assertIn(
                "Noise-print denoise via afftdn sample capture", report["processing_steps"]
            )
            self.assertNotIn(
                "Speech-presence breath ducking via sidechaingate", report["processing_steps"]
            )
            self.assertNotIn(
                "Breath-onset cleanup before speech entries", report["processing_steps"]
            )
            self.assertNotIn("Secondary FFmpeg denoise via afftdn", report["processing_steps"])

    def test_process_media_file_dry_run_can_opt_into_legacy_breath_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "voice.wav"
            source.write_bytes(b"fake")
            preset = apply_runtime_overrides(
                load_preset("voice-isolate"),
                enable_legacy_breath_filters=True,
            )
            runtime = RuntimeOptions(dry_run=True, python_executable="python")
            report = process_media_file(
                source,
                preset_name="voice-isolate",
                preset=preset,
                output_root=build_output_root(root / "output", "20260527-120000"),
                runtime=runtime,
                run_slug="20260527-120000",
                input_metadata={"duration_seconds": 10.0, "sample_rate": 48000, "channels": 1},
                noise_windows=[NoiseWindow(start_seconds=1.0, end_seconds=2.5)],
                skip_deepfilternet=True,
            )

            self.assertIn(
                "Speech-presence breath ducking via sidechaingate", report["processing_steps"]
            )
            self.assertIn("Breath-onset cleanup before speech entries", report["processing_steps"])

    def test_process_media_file_executes_noise_sample_after_local_copy_is_placed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "voice.wav"
            source.write_bytes(b"fake")
            output_root = build_output_root(root / "output", "20260527-120000")
            preset = load_preset("voice-isolate")
            layout = build_output_layout(
                input_path=source,
                output_root=output_root,
                run_slug="20260527-120000",
                skip_deepfilternet=True,
            )

            def write_fake_wav(path: Path) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(path), "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(48000)
                    writer.writeframes(b"\x00\x00" * 4800)

            def fake_run(command: list[str]):
                cmd_text = " ".join(command)
                if str(layout.noise_sample_wav) in cmd_text:
                    self.assertTrue(layout.denoised_wav.exists())
                if str(layout.raw_wav) in cmd_text:
                    write_fake_wav(layout.raw_wav)
                elif str(layout.noise_sample_wav) in cmd_text:
                    write_fake_wav(layout.noise_sample_wav)
                elif str(layout.clean_wav) in cmd_text:
                    write_fake_wav(layout.clean_wav)
                elif str(layout.transcript_mp3) in cmd_text:
                    layout.transcript_dir.mkdir(parents=True, exist_ok=True)
                    layout.transcript_mp3.write_bytes(b"mp3")

                class Result:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return Result()

            from unittest import mock

            with (
                mock.patch("audio_sound.pipeline.ensure_tool", side_effect=lambda value: value),
                mock.patch(
                    "audio_sound.pipeline.run_command",
                    side_effect=fake_run,
                ) as raw_runner,
                mock.patch(
                    "audio_sound.pipeline.ffprobe_media",
                    return_value={"duration_seconds": 10.0, "sample_rate": 48000, "channels": 1},
                ),
            ):
                report = process_media_file(
                    source,
                    preset_name="voice-isolate",
                    preset=preset,
                    output_root=output_root,
                    runtime=RuntimeOptions(
                        ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", python_executable="python"
                    ),
                    run_slug="20260527-120000",
                    input_metadata={"duration_seconds": 10.0, "sample_rate": 48000, "channels": 1},
                    noise_windows=[NoiseWindow(start_seconds=1.0, end_seconds=2.5)],
                    skip_deepfilternet=True,
                )

            self.assertFalse(
                any(
                    call.args[0][1] == "D:\\trusted\\deepfilter_adapter.py"
                    for call in raw_runner.call_args_list
                    if len(call.args[0]) > 1
                )
            )
            self.assertTrue(layout.denoised_wav.exists())
            self.assertEqual(report["noise_sample_duration_seconds"], 1.5)

    def test_build_batch_summary_renders_markdown(self) -> None:
        summary = build_batch_summary(
            preset_name="safe",
            preset_description="spoken word",
            input_path=Path("D:/batch"),
            output_root=Path("D:/out"),
            reports=[
                {
                    "input_file": "D:/batch/a.wav",
                    "outputs": {
                        "clean_wav": "D:/out/20260527-120000_a/audio_preprocess/audio_clean.wav",
                        "transcript_mp3": "D:/out/20260527-120000_a/transcript_ready/audio.mp3",
                        "report_md": "D:/out/20260527-120000_a/audio_preprocess/audio_process_report.md",
                        "report_json": "D:/out/20260527-120000_a/audio_preprocess/audio_process_report.json",
                    },
                }
            ],
        )
        markdown = render_batch_summary_markdown(summary)
        self.assertEqual(summary["files_processed"], 1)
        self.assertIn("audio_clean.wav", markdown)
        self.assertIn("spoken word", markdown)


if __name__ == "__main__":
    unittest.main()
