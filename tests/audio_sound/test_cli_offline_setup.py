from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audio_sound import cli


class OfflineSetupCliTests(unittest.TestCase):
    def test_setup_parser_accepts_verified_offline_runtime_inputs(self) -> None:
        wheelhouse = str(Path("offline") / "wheelhouse" / "audio")
        ffmpeg = str(Path("offline") / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe")
        ffprobe = str(Path("offline") / "tools" / "ffmpeg" / "bin" / "ffprobe.exe")
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "setup",
                "--offline-wheelhouse",
                wheelhouse,
                "--ffmpeg-bin",
                ffmpeg,
                "--ffprobe-bin",
                ffprobe,
            ]
        )

        self.assertEqual(args.offline_wheelhouse, wheelhouse)
        self.assertTrue(args.ffmpeg_bin.endswith("ffmpeg.exe"))
        self.assertTrue(args.ffprobe_bin.endswith("ffprobe.exe"))

    def test_setup_command_forwards_verified_offline_runtime_inputs(self) -> None:
        wheelhouse = str(Path("offline") / "wheelhouse" / "audio")
        ffmpeg = str(Path("offline") / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe")
        ffprobe = str(Path("offline") / "tools" / "ffmpeg" / "bin" / "ffprobe.exe")
        args = argparse.Namespace(
            python_executable="python",
            local_wheel=None,
            local_wheel_sha256=None,
            index_url=None,
            offline_wheelhouse=wheelhouse,
            ffmpeg_bin=ffmpeg,
            ffprobe_bin=ffprobe,
        )
        with mock.patch("audio_sound.cli.run_install") as run_install:
            run_install.return_value = {"ok": True}
            exit_code = cli.command_setup(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            run_install.call_args.kwargs["offline_wheelhouse"],
            wheelhouse,
        )
        self.assertTrue(run_install.call_args.kwargs["ffmpeg_bin"].endswith("ffmpeg.exe"))

    def test_ordinary_commands_default_to_persisted_offline_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            tool_root = repo_root / "tmp" / "offline-runtime" / "tools" / "ffmpeg" / "bin"
            tool_root.mkdir(parents=True)
            ffmpeg = tool_root / "ffmpeg.exe"
            ffprobe = tool_root / "ffprobe.exe"
            ffmpeg.write_bytes(b"ffmpeg")
            ffprobe.write_bytes(b"ffprobe")

            with mock.patch.object(cli, "PROJECT_ROOT", repo_root):
                parser = cli.build_parser()
                doctor = parser.parse_args(["doctor"])
                clean = parser.parse_args(["clean", "demo.wav"])
                inspect = parser.parse_args(["inspect", "demo.wav"])

            self.assertEqual(doctor.ffmpeg_bin, str(ffmpeg))
            self.assertEqual(doctor.ffprobe_bin, str(ffprobe))
            self.assertEqual(clean.ffmpeg_bin, str(ffmpeg))
            self.assertEqual(clean.ffprobe_bin, str(ffprobe))
            self.assertEqual(inspect.ffprobe_bin, str(ffprobe))


if __name__ == "__main__":
    unittest.main()
