from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np


def _profile_registry_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "auto-cut-subject-pointer-onboarding"
        / "scripts"
        / "profile_registry.py"
    )
    spec = importlib.util.spec_from_file_location(
        "subject_pointer_profile_registry_unicode_test", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProfileRegistryUnicodePathTests(unittest.TestCase):
    def test_hand_media_contract_decodes_png_from_unicode_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "高中历史素材" / "小手.png"
            source_path.parent.mkdir(parents=True)
            pixels = np.zeros((3, 5, 4), dtype=np.uint8)
            pixels[:, :, 3] = 255
            encoded, payload = cv2.imencode(".png", pixels)
            self.assertTrue(encoded)
            source_path.write_bytes(payload.tobytes())

            contract = _profile_registry_module()._hand_media_contract(
                source_path, "intake.assets[0]"
            )

        self.assertEqual(
            contract,
            {
                "format": "png",
                "has_alpha": True,
                "width": 5,
                "height": 3,
            },
        )


if __name__ == "__main__":
    unittest.main()
