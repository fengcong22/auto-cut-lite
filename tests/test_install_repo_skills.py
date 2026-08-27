from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.install_repo_skills as installer


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage_repo_skills(base: Path, names: list[str]) -> tuple[Path, Path]:
    repo_root = base / "package"
    source_root = repo_root / "skills"
    for name in names:
        shutil.copytree(installer.REPO_SKILLS_ROOT / name, source_root / name)
    return source_root, repo_root / ".codex" / "skills"


class InstallRepoSkillsTests(unittest.TestCase):
    def test_repository_skill_set_stays_exactly_at_seventeen(self) -> None:
        expected = {
            "auto-cut",
            "auto-cut-animation-timing-revision",
            "auto-cut-audio-peak-target",
            "auto-cut-audio-restoration",
            "auto-cut-basic-oral-video",
            "auto-cut-draft-retention",
            "auto-cut-editable-ad-revision",
            "auto-cut-favorite-text-assets",
            "auto-cut-final-acceptance",
            "auto-cut-high-school-history-lite",
            "auto-cut-local-image-overlay-revision",
            "auto-cut-music-library-bgm",
            "auto-cut-pointer-targeting",
            "auto-cut-review-audio-precision",
            "auto-cut-revision-draft",
            "auto-cut-subject-pointer-onboarding",
            "auto-cut-text-safezone-animation-revision",
        }

        names = {path.name for path in installer.discover_repo_skills(installer.REPO_SKILLS_ROOT)}

        self.assertEqual(names, expected)
        self.assertEqual(len(names), 17)

    def test_discover_repo_skills_finds_skill_dirs(self) -> None:
        skills = installer.discover_repo_skills(installer.REPO_SKILLS_ROOT)
        names = {path.name for path in skills}
        self.assertIn("auto-cut-basic-oral-video", names)
        self.assertIn("auto-cut-audio-restoration", names)
        self.assertIn("auto-cut-subject-pointer-onboarding", names)
        self.assertIn("auto-cut", names)

    def test_repo_skills_use_auto_cut_names(self) -> None:
        skills = installer.discover_repo_skills(installer.REPO_SKILLS_ROOT)
        for skill_dir in skills:
            self.assertRegex(skill_dir.name, r"^auto-cut(?:-[a-z0-9-]+)?$")
            skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            match = re.search(r"^name:\s*(.+)$", skill_text, flags=re.MULTILINE)
            self.assertIsNotNone(match, f"missing name frontmatter in {skill_dir}")
            self.assertEqual(match.group(1).strip(), skill_dir.name)

    def test_repo_skill_display_names_use_auto_cut_chinese_suffix(self) -> None:
        skills = installer.discover_repo_skills(installer.REPO_SKILLS_ROOT)
        for skill_dir in skills:
            metadata_path = skill_dir / "agents" / "openai.yaml"
            metadata = metadata_path.read_text(encoding="utf-8")
            match = re.search(
                r'^\s*display_name:\s*"([^"]+)"\s*$',
                metadata,
                flags=re.MULTILINE,
            )
            self.assertIsNotNone(match, f"missing display_name in {metadata_path}")
            display_name = match.group(1)
            self.assertTrue(
                display_name.startswith("Auto-Cut "),
                f"nonstandard display_name in {metadata_path}: {display_name}",
            )
            self.assertRegex(
                display_name.removeprefix("Auto-Cut "),
                r"[\u4e00-\u9fff]",
                f"display_name needs a Chinese suffix in {metadata_path}",
            )

    def test_install_skills_dry_run_returns_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source, dest = stage_repo_skills(Path(tmpdir), ["auto-cut"])
            pairs = installer.install_skills(
                source,
                dest,
                selected_names=["auto-cut"],
                dry_run=True,
            )
            self.assertEqual(len(pairs), 1)
            self.assertEqual(pairs[0][0].name, "auto-cut")
            self.assertFalse(dest.exists())

    def test_install_skills_copies_skill_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source, dest = stage_repo_skills(Path(tmpdir), ["auto-cut"])
            pairs = installer.install_skills(
                source,
                dest,
                selected_names=["auto-cut"],
                dry_run=False,
            )
            self.assertEqual(len(pairs), 1)
            installed_dir = dest / "auto-cut"
            self.assertTrue((installed_dir / "SKILL.md").exists())
            self.assertTrue((installed_dir / "agents" / "openai.yaml").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_install_skills_rejects_codex_junction_before_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            repo_root = base / "package"
            source_root = repo_root / "skills"
            skill_root = source_root / "auto-cut"
            (skill_root / "agents").mkdir(parents=True)
            (skill_root / "SKILL.md").write_text("name: auto-cut\n", encoding="utf-8")
            (skill_root / "agents" / "openai.yaml").write_text(
                'display_name: "Auto-Cut test"\n', encoding="utf-8"
            )
            external = base / "outside-package"
            external.mkdir()
            junction = repo_root / ".codex"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")

            try:
                with self.assertRaises(RuntimeError) as raised:
                    installer.install_skills(
                        source_root,
                        junction / "skills",
                        selected_names=["auto-cut"],
                    )

                self.assertEqual(
                    getattr(raised.exception, "code", None), "unsafe_skill_install_path"
                )
                self.assertFalse((external / "skills").exists())
            finally:
                if os.path.lexists(junction):
                    junction.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_install_skills_rejects_junction_repo_root_before_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            real_root = base / "outside-package"
            real_source = real_root / "skills" / "auto-cut"
            real_source.mkdir(parents=True)
            (real_source / "SKILL.md").write_text("name: auto-cut\n", encoding="utf-8")
            lexical_root = base / "package-link"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(lexical_root), str(real_root)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")

            try:
                with self.assertRaises(RuntimeError) as raised:
                    installer.install_skills(
                        lexical_root / "skills",
                        lexical_root / ".codex" / "skills",
                        selected_names=["auto-cut"],
                    )

                self.assertEqual(
                    getattr(raised.exception, "code", None), "unsafe_skill_install_path"
                )
                self.assertEqual(getattr(raised.exception, "reason", None), "reparse_component")
                self.assertFalse((real_root / ".codex").exists())
            finally:
                if os.path.lexists(lexical_root):
                    lexical_root.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_install_skills_rejects_target_junction_before_rmtree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            repo_root = base / "package"
            source_root = repo_root / "skills"
            skill_root = source_root / "auto-cut"
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text("name: auto-cut\n", encoding="utf-8")
            dest_root = repo_root / ".codex" / "skills"
            dest_root.mkdir(parents=True)
            external = base / "outside-package"
            external.mkdir()
            outside_marker = external / "keep.txt"
            outside_marker.write_text("keep", encoding="utf-8")
            junction = dest_root / "auto-cut"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")

            try:
                with self.assertRaises(RuntimeError) as raised:
                    installer.install_skills(
                        source_root,
                        dest_root,
                        selected_names=["auto-cut"],
                    )

                self.assertEqual(
                    getattr(raised.exception, "code", None), "unsafe_skill_install_path"
                )
                self.assertTrue(outside_marker.exists())
                self.assertTrue(os.path.lexists(junction))
            finally:
                if os.path.lexists(junction):
                    junction.rmdir()

    def test_install_skills_rejects_destination_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            repo_root = base / "package"
            source_root = repo_root / "skills"
            skill_root = source_root / "auto-cut"
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text("name: auto-cut\n", encoding="utf-8")
            external_dest = base / "outside-package" / "skills"

            with self.assertRaises(RuntimeError) as raised:
                installer.install_skills(
                    source_root,
                    external_dest,
                    selected_names=["auto-cut"],
                )

            self.assertEqual(getattr(raised.exception, "code", None), "unsafe_skill_install_path")
            self.assertEqual(getattr(raised.exception, "reason", None), "target_outside_repo")
            self.assertFalse(external_dest.exists())

    def test_main_reports_structured_failure_for_destination_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            external_dest = Path(tmpdir) / "outside-package" / "skills"
            output = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    ["install_repo_skills.py", "--dest", str(external_dest)],
                ),
                redirect_stdout(output),
            ):
                exit_code = installer.main()

            rendered = output.getvalue()
            self.assertEqual(exit_code, 1)
            self.assertIn("status=failed", rendered)
            self.assertIn("code=unsafe_skill_install_path", rendered)
            self.assertIn("reason=target_outside_repo", rendered)
            self.assertFalse(external_dest.exists())

    def test_install_skills_copies_complete_subject_pointer_onboarding_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source, dest = stage_repo_skills(Path(tmpdir), ["auto-cut-subject-pointer-onboarding"])
            pairs = installer.install_skills(
                source,
                dest,
                selected_names=["auto-cut-subject-pointer-onboarding"],
                dry_run=False,
            )

            self.assertEqual(len(pairs), 1)
            installed_dir = dest / "auto-cut-subject-pointer-onboarding"
            expected_files = (
                "SKILL.md",
                "agents/openai.yaml",
                "references/handoff-contract.md",
                "references/intake-contract.md",
                "references/profile-schema.md",
                "scripts/profile_registry.py",
                "scripts/project_bindings.py",
                "scripts/render_reference_assets.py",
                "assets/pointer-material-reference.png",
                "assets/scale-reference-screenshot.png",
            )
            for relative_path in expected_files:
                self.assertTrue(
                    (installed_dir / relative_path).is_file(),
                    f"missing installed skill artifact: {relative_path}",
                )

    def test_installed_subject_pointer_onboarding_tree_matches_source_hashes(self) -> None:
        source_root = installer.REPO_SKILLS_ROOT / "auto-cut-subject-pointer-onboarding"
        with tempfile.TemporaryDirectory() as tmpdir:
            staged_source, dest = stage_repo_skills(
                Path(tmpdir), ["auto-cut-subject-pointer-onboarding"]
            )
            installer.install_skills(
                staged_source,
                dest,
                selected_names=["auto-cut-subject-pointer-onboarding"],
                dry_run=False,
            )
            installed_root = dest / "auto-cut-subject-pointer-onboarding"
            source_files = {
                path.relative_to(source_root) for path in source_root.rglob("*") if path.is_file()
            }
            installed_files = {
                path.relative_to(installed_root)
                for path in installed_root.rglob("*")
                if path.is_file()
            }

            self.assertEqual(installed_files, source_files)
            for relative_path in sorted(source_files):
                with self.subTest(relative_path=relative_path.as_posix()):
                    self.assertEqual(
                        file_hash(installed_root / relative_path),
                        file_hash(source_root / relative_path),
                    )

    def test_install_skills_copies_complete_audio_restoration_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source, dest = stage_repo_skills(Path(tmpdir), ["auto-cut-audio-restoration"])
            pairs = installer.install_skills(
                source,
                dest,
                selected_names=["auto-cut-audio-restoration"],
                dry_run=False,
            )

            self.assertEqual(len(pairs), 1)
            installed_dir = dest / "auto-cut-audio-restoration"
            expected_files = (
                "SKILL.md",
                "agents/openai.yaml",
                "references/acceptance.md",
                "references/commands.md",
                "references/spoken-segment-boundaries.md",
                "references/workflow.md",
            )
            for relative_path in expected_files:
                self.assertTrue(
                    (installed_dir / relative_path).is_file(),
                    f"missing installed skill artifact: {relative_path}",
                )

    def test_install_skills_copies_resumable_review_contracts(self) -> None:
        selected_names = [
            "auto-cut",
            "auto-cut-revision-draft",
            "auto-cut-review-audio-precision",
            "auto-cut-final-acceptance",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            source, dest = stage_repo_skills(Path(tmpdir), selected_names)
            pairs = installer.install_skills(
                source,
                dest,
                selected_names=selected_names,
                dry_run=False,
            )

            self.assertEqual([source.name for source, _ in pairs], sorted(selected_names))
            installed_text = "\n".join(
                (dest / name / "SKILL.md").read_text(encoding="utf-8") for name in selected_names
            )
            for required in (
                "review-document-run",
                "review-job-compile",
                "job_state.json",
                "job_timing.json",
                "complete final candidate audio SHA256",
                "one optional full preview",
            ):
                with self.subTest(required=required):
                    self.assertIn(required, installed_text)

    def test_portable_delivery_skill_trees_install_byte_for_byte(self) -> None:
        selected_names = ["auto-cut", "auto-cut-final-acceptance"]
        with tempfile.TemporaryDirectory() as tmpdir:
            staged_source, dest = stage_repo_skills(Path(tmpdir), selected_names)
            installer.install_skills(
                staged_source,
                dest,
                selected_names=selected_names,
                dry_run=False,
            )

            for name in selected_names:
                source_root = installer.REPO_SKILLS_ROOT / name
                installed_root = dest / name
                source_files = {
                    path.relative_to(source_root)
                    for path in source_root.rglob("*")
                    if path.is_file()
                }
                installed_files = {
                    path.relative_to(installed_root)
                    for path in installed_root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(installed_files, source_files)
                for relative_path in sorted(source_files):
                    with self.subTest(skill=name, path=relative_path.as_posix()):
                        self.assertEqual(
                            file_hash(installed_root / relative_path),
                            file_hash(source_root / relative_path),
                        )

    def test_resolve_codex_skills_root_defaults_to_repo_local(self) -> None:
        dest = installer.resolve_codex_skills_root()
        self.assertEqual(dest, installer.REPO_ROOT / ".codex" / "skills")

    def test_resolve_codex_skills_root_allows_explicit_dest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = installer.resolve_codex_skills_root(tmpdir)
            self.assertEqual(dest, Path(tmpdir).resolve())

    def test_first_use_prompt_documents_usage_modes(self) -> None:
        prompt = installer.FIRST_USE_PROMPT
        self.assertIn("现在有三种使用方式", prompt)
        self.assertIn("自然语言直接说", prompt)
        self.assertIn("点名总 skill", prompt)
        self.assertIn("点名单独 skill", prompt)
        self.assertIn("auto-cut-music-library-bgm", prompt)
        self.assertIn("仓库本地 .codex/skills", prompt)


if __name__ == "__main__":
    unittest.main()
