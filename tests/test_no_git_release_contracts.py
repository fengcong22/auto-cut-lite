import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from test_repository_contracts import repository_mode, repository_paths

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_repository_contract_ignores_parent_git_metadata(tmp_path):
    parent = tmp_path / "parent-repo"
    package = parent / "extracted-package"
    (parent / ".git").mkdir(parents=True)
    package.mkdir()

    assert repository_mode(package) == "archive"


def test_archive_repository_paths_come_from_package_inventory(tmp_path):
    parent = tmp_path / "parent-repo"
    package = parent / "extracted-package"
    (parent / ".git").mkdir(parents=True)
    package.mkdir()
    (package / "release-inventory.json").write_text(
        json.dumps(
            {
                "files": [
                    {"path": "README.md", "size": 42, "sha256": "0" * 64},
                    {"path": "skills/auto-cut/SKILL.md", "size": 84, "sha256": "1" * 64},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert repository_paths(package) == {
        "README.md",
        "skills/auto-cut/SKILL.md",
    }


def test_archive_repository_paths_fall_back_to_package_files(tmp_path):
    parent = tmp_path / "parent-repo"
    package = parent / "extracted-package"
    (parent / ".git").mkdir(parents=True)
    (package / "skills" / "auto-cut").mkdir(parents=True)
    (package / "README.md").write_text("Auto-Cut", encoding="utf-8")
    (package / "skills" / "auto-cut" / "SKILL.md").write_text(
        "---\nname: auto-cut\n---\n", encoding="utf-8"
    )

    assert repository_paths(package) == {
        "README.md",
        "skills/auto-cut/SKILL.md",
    }


def test_archive_repository_release_boundaries_do_not_consult_parent_git(tmp_path):
    parent = tmp_path / "parent-repo"
    package = parent / "extracted-package"
    (parent / ".git").mkdir(parents=True)

    fixture_paths = {
        ".gitignore",
        "README.md",
        "docs/auto-cut-notifications.md",
        "skills/auto-cut-subject-pointer-onboarding/assets/pointer-material-reference.png",
        "skills/auto-cut-subject-pointer-onboarding/assets/scale-reference-screenshot.png",
        "skills/auto-cut-subject-pointer-onboarding/references/handoff-contract.md",
        "tests/test_repository_contracts.py",
    }
    for relative_path in fixture_paths:
        source = REPO_ROOT / relative_path
        destination = package / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    (package / "release-inventory.json").write_text(
        json.dumps({"files": [{"path": path} for path in sorted(fixture_paths)]}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(package / "tests" / "test_repository_contracts.py"),
            "TestRepositoryContracts.test_notification_setup_docs_and_release_boundary_are_explicit",
            "TestRepositoryContracts.test_subject_pointer_release_tracks_guides_and_handoff_but_ignores_local_state",
        ],
        cwd=package,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_audio_source_migration_contract_runs_current_checks_without_git(tmp_path):
    parent = tmp_path / "parent-repo"
    package = parent / "extracted-package"
    (parent / ".git").mkdir(parents=True)

    manifest_relative = "docs/audio-sound/source-migration-manifest.json"
    manifest = json.loads((REPO_ROOT / manifest_relative).read_text(encoding="utf-8"))
    fixture_paths = {
        manifest_relative,
        "docs/audio-sound/README.md",
        "docs/audio-sound/PROVENANCE.md",
        "tests/audio_sound/test_source_migration_contract.py",
    }
    fixture_paths.update(
        destination for item in manifest["items"] for destination in item["destinations"]
    )
    fixture_paths.update(item["destination"] for item in manifest["retained_legacy_artifacts"])

    inventory_files = []
    for relative_path in fixture_paths:
        source = REPO_ROOT / relative_path
        destination = package / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        data = destination.read_bytes()
        inventory_files.append(
            {
                "path": relative_path,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )

    (package / "release-inventory.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "archive-fixture",
                "source_commit": "archive-fixture",
                "files": sorted(inventory_files, key=lambda item: item["path"]),
                "inventory_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["AUDIO_SOUND_CLEAN_SOURCE_ROOT"] = str(package / "missing-clean-source")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/audio_sound/test_source_migration_contract.py",
            "-q",
        ],
        cwd=package,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped" in result.stdout
