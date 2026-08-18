from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from audio_sound import cli


class VerifiedRuntimeCliTests(unittest.TestCase):
    def test_doctor_applies_external_model_execution_policy(self) -> None:
        args = argparse.Namespace(
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            python_executable="audio-python",
        )
        detected = {
            "status": "full",
            "full": True,
            "degraded": False,
            "unavailable": False,
            "python": {"ok": True},
            "ffmpeg": {"ok": True},
            "ffprobe": {"ok": True},
            "deepfilternet": {"ok": True},
            "respiro_en": {"ok": True},
            "spectramini": {"ok": True},
        }
        output = io.StringIO()

        with (
            mock.patch("audio_sound.cli.detect_runtime", return_value=detected),
            redirect_stdout(output),
        ):
            exit_code = cli.command_doctor(args)

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["full"])
        self.assertEqual(
            payload["deepfilternet"]["execution_status"],
            "external_unavailable",
        )
        self.assertEqual(
            payload["respiro_en"]["execution_status"],
            "external_unavailable",
        )

    def test_clean_blocks_verified_but_external_deepfilternet_before_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "sample.wav"
            source.write_bytes(b"audio")
            args = cli.build_parser().parse_args(["clean", str(source), "--dry-run"])
            doctor_report = {
                "python": {"ok": True, "path": args.python_executable},
                "deepfilternet": {
                    "ok": True,
                    "module_ok": True,
                    "model_ok": True,
                    "adapter_ok": True,
                    "identity": "deepfilternet@0.5.6;verified-model",
                },
                "respiro_en": {"ok": False, "identity": ""},
            }
            with (
                mock.patch("audio_sound.cli.detect_runtime", return_value=doctor_report),
                mock.patch(
                    "audio_sound.cli.verified_deepfilternet_runtime",
                    return_value={"verified": True},
                ),
                mock.patch(
                    "audio_sound.cli.process_media_file",
                    return_value={"dry_run": True},
                ) as process_media_file,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "external_model_execution_unavailable",
                ):
                    cli.command_clean(args)

            process_media_file.assert_not_called()

    def test_clean_blocks_verified_but_external_respiro_before_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "sample.wav"
            repo = root / "Respiro-en"
            weights = root / "respiro-en.pt"
            source.write_bytes(b"audio")
            repo.mkdir()
            weights.write_bytes(b"weights")
            args = cli.build_parser().parse_args(
                [
                    "clean",
                    str(source),
                    "--dry-run",
                    "--skip-deepfilternet",
                    "--respiro-repo",
                    str(repo),
                    "--respiro-weights",
                    str(weights),
                ]
            )
            doctor_report = {
                "python": {"ok": True, "path": args.python_executable},
                "deepfilternet": {"ok": False},
                "respiro_en": {"ok": True, "identity": "verified-respiro"},
            }
            with (
                mock.patch("audio_sound.cli.detect_runtime", return_value=doctor_report),
                mock.patch(
                    "audio_sound.cli.verified_respiro_runtime",
                    return_value={"verified": True},
                ),
                mock.patch(
                    "audio_sound.cli.process_media_file",
                    return_value={"dry_run": True},
                ) as process_media_file,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "external_model_execution_unavailable",
                ):
                    cli.command_clean(args)

            process_media_file.assert_not_called()

    def test_clean_blocks_an_unverified_deepfilternet_runtime_before_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "sample.wav"
            source.write_bytes(b"audio")
            args = cli.build_parser().parse_args(["clean", str(source), "--dry-run"])
            doctor_report = {
                "status": "degraded",
                "deepfilternet": {
                    "ok": False,
                    "module_ok": True,
                    "model_ok": False,
                    "adapter_ok": False,
                    "identity": "deepfilternet@0.5.6",
                },
                "respiro_en": {"ok": False, "identity": ""},
            }
            with (
                mock.patch("audio_sound.cli.detect_runtime", return_value=doctor_report),
                mock.patch(
                    "audio_sound.cli.process_media_file", return_value={"dry_run": True}
                ) as process_media_file,
                mock.patch("audio_sound.pipeline.run_command") as run_command,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"unverified_deepfilternet_runtime.*--skip-deepfilternet",
                ):
                    cli.command_clean(args)

            process_media_file.assert_not_called()
            run_command.assert_not_called()

    def test_clean_blocks_configured_but_unverified_respiro_before_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "sample.wav"
            repo = root / "Respiro-en"
            weights = root / "respiro-en.pt"
            source.write_bytes(b"audio")
            repo.mkdir()
            weights.write_bytes(b"weights")
            args = cli.build_parser().parse_args(
                [
                    "clean",
                    str(source),
                    "--dry-run",
                    "--skip-deepfilternet",
                    "--respiro-repo",
                    str(repo),
                    "--respiro-weights",
                    str(weights),
                ]
            )
            doctor_report = {
                "status": "degraded",
                "deepfilternet": {"ok": False},
                "respiro_en": {
                    "ok": False,
                    "identity": "",
                    "notes": "verified runtime manifest is unavailable",
                },
            }
            with (
                mock.patch("audio_sound.cli.detect_runtime", return_value=doctor_report),
                mock.patch(
                    "audio_sound.cli.process_media_file", return_value={"dry_run": True}
                ) as process_media_file,
                mock.patch("audio_sound.pipeline.run_command") as run_command,
            ):
                with self.assertRaisesRegex(RuntimeError, "unverified_respiro_runtime"):
                    cli.command_clean(args)

            process_media_file.assert_not_called()
            run_command.assert_not_called()

    def test_clean_with_explicit_deepfilternet_skip_does_not_probe_model_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "sample.wav"
            source.write_bytes(b"audio")
            args = cli.build_parser().parse_args(
                ["clean", str(source), "--dry-run", "--skip-deepfilternet"]
            )
            with (
                mock.patch(
                    "audio_sound.cli.detect_runtime",
                    side_effect=AssertionError("doctor must not load a skipped model runtime"),
                ) as detect_runtime,
                mock.patch(
                    "audio_sound.cli.process_media_file", return_value={"dry_run": True}
                ) as process_media_file,
            ):
                exit_code = cli.command_clean(args)

            self.assertEqual(exit_code, 0)
            detect_runtime.assert_not_called()
            self.assertTrue(process_media_file.call_args.kwargs["skip_deepfilternet"])
            self.assertIsNone(process_media_file.call_args.kwargs["deepfilternet_runtime"])
