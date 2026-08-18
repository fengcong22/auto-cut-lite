from __future__ import annotations

import argparse
import unittest
from unittest import mock

from audio_sound import cli


class LocalWheelSetupCliTests(unittest.TestCase):
    def test_setup_parser_accepts_verified_local_wheel(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "setup",
                "--local-wheel",
                "tmp/torch.whl",
                "--local-wheel-sha256",
                "a" * 64,
                "--index-url",
                "https://mirrors.cloud.tencent.com/pypi/simple",
            ]
        )
        self.assertEqual(args.local_wheel, "tmp/torch.whl")
        self.assertEqual(args.local_wheel_sha256, "a" * 64)
        self.assertEqual(args.index_url, "https://mirrors.cloud.tencent.com/pypi/simple")

    def test_setup_forwards_verified_local_wheel(self) -> None:
        args = argparse.Namespace(
            python_executable="audio-python",
            local_wheel="tmp/torch.whl",
            local_wheel_sha256="a" * 64,
            index_url="https://mirrors.cloud.tencent.com/pypi/simple",
        )
        with mock.patch("audio_sound.cli.run_install") as run_install:
            run_install.return_value = {"ok": True, "code": "ok", "data": {}}
            exit_code = cli.command_setup(args)

        self.assertEqual(exit_code, 0)
        run_install.assert_called_once_with(
            repo_root=cli.PROJECT_ROOT,
            python_executable="audio-python",
            local_wheel="tmp/torch.whl",
            local_wheel_sha256="a" * 64,
            index_url="https://mirrors.cloud.tencent.com/pypi/simple",
        )


if __name__ == "__main__":
    unittest.main()
