import os
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_PATH = os.fspath(REPO_ROOT / "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.insert(0, SCRIPTS_PATH)

from utils.pointer_validation import _decode_asset, _read_image


class PointerValidationUnicodePathTests(unittest.TestCase):
    def test_unicode_png_path_decodes_with_alpha(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir) / "高中历史" / "指向物"
            directory.mkdir(parents=True)
            path = directory / "小手.png"
            pixels = np.zeros((7, 11, 4), dtype=np.uint8)
            pixels[:, :, 3] = 255
            ok, encoded = cv2.imencode(".png", pixels)
            self.assertTrue(ok)
            path.write_bytes(encoded.tobytes())

            decoded = _read_image(path, cv2.IMREAD_UNCHANGED)

            self.assertIsNotNone(decoded)
            self.assertEqual(decoded.shape, (7, 11, 4))
            self.assertEqual(_decode_asset(path), (11, 7, 4))


if __name__ == "__main__":
    unittest.main()
