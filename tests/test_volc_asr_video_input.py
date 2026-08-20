from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audio_sound.volc_asr import extract_audio_from_video


class VolcAsrVideoInputTests(unittest.TestCase):
    def test_extracts_local_video_audio_as_compact_m4a(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            output = root / "source.asr.m4a"
            source.write_bytes(b"video")

            def fake_run(command: list[str], **_kwargs: object) -> object:
                Path(command[-1]).write_bytes(b"audio")
                return type("Completed", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

            with patch("audio_sound.volc_asr.subprocess.run", side_effect=fake_run) as run:
                result = extract_audio_from_video(source, output, ffmpeg_bin="ffmpeg-test")

        self.assertEqual(result, output)
        self.assertEqual(output.suffix, ".m4a")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "ffmpeg-test")
        self.assertIn("-vn", command)
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-b:a") + 1], "64k")


if __name__ == "__main__":
    unittest.main()
