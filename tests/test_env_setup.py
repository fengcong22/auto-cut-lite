from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class EnvSetupTests(unittest.TestCase):
    def test_installed_runtime_imports_root_package_from_external_cwd_and_isolated_python(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._assert_installed_runtime_import(Path(tmp_dir))

    def _assert_installed_runtime_import(self, tmp_path: Path) -> None:
        runtime_root = tmp_path / "installed" / "runtime"
        scripts_dir = runtime_root / "scripts"
        utils_dir = scripts_dir / "utils"
        vendor_dir = scripts_dir / "vendor"
        audio_package = runtime_root / "audio_sound"
        external_cwd = tmp_path / "external-cwd"

        utils_dir.mkdir(parents=True)
        vendor_dir.mkdir()
        external_cwd.mkdir()
        (utils_dir / "__init__.py").write_text("", encoding="utf-8")
        shutil.copy2(REPO_ROOT / "scripts" / "utils" / "env_setup.py", utils_dir)
        shutil.copy2(REPO_ROOT / "scripts" / "utils" / "skill_path.py", utils_dir)
        shutil.copytree(
            REPO_ROOT / "audio_sound",
            audio_package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        probe = scripts_dir / "jy_wrapper.py"
        probe.write_text(
            "import json\n"
            "import sys\n"
            "from pathlib import Path\n"
            "from utils.env_setup import setup_env\n"
            "setup_env()\n"
            "import audio_sound\n"
            "print(json.dumps({\n"
            '    "path_prefix": [str(Path(item).resolve()) for item in sys.path[:3]],\n'
            '    "audio_sound_file": str(Path(audio_sound.__file__).resolve()),\n'
            "}))\n",
            encoding="utf-8",
        )

        isolated_env = tmp_path / "isolated-python"
        venv.EnvBuilder(with_pip=False).create(isolated_env)
        python = isolated_env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        environment = os.environ.copy()
        environment.pop("JY_SKILL_ROOT", None)
        environment.pop("PYTHONPATH", None)

        completed = subprocess.run(
            [str(python), str(probe)],
            cwd=external_cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(
            payload["path_prefix"],
            [
                str(scripts_dir.resolve()),
                str(vendor_dir.resolve()),
                str(runtime_root.resolve()),
            ],
        )
        self.assertEqual(
            payload["audio_sound_file"],
            str((audio_package / "__init__.py").resolve()),
        )
