import json
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def repository_mode(package_root: Path) -> str:
    return "git" if (package_root / ".git").exists() else "archive"


def repository_paths(package_root: Path) -> set[str]:
    if repository_mode(package_root) == "git":
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=package_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return {line.replace("\\", "/") for line in result.stdout.splitlines()}

    inventory_path = package_root / "release-inventory.json"
    if inventory_path.is_file():
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        return {item["path"] for item in inventory["files"]}
    return {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_file()
    }


def assert_release_paths(
    test_case: unittest.TestCase, package_root: Path, expected_paths: set[str]
) -> None:
    available_paths = repository_paths(package_root)
    for relative_path in expected_paths:
        with test_case.subTest(relative_path=relative_path):
            test_case.assertTrue((package_root / relative_path).is_file())
            test_case.assertIn(relative_path, available_paths)


def assert_local_state_not_released(
    test_case: unittest.TestCase, package_root: Path, relative_paths: tuple[str, ...]
) -> None:
    available_paths = repository_paths(package_root)
    for relative_path in relative_paths:
        with test_case.subTest(relative_path=relative_path):
            if repository_mode(package_root) == "git":
                ignored = subprocess.run(
                    ["git", "check-ignore", "--quiet", "--", relative_path],
                    cwd=package_root,
                    check=False,
                )
                test_case.assertEqual(ignored.returncode, 0)
            else:
                test_case.assertFalse((package_root / relative_path).exists())
            test_case.assertNotIn(relative_path, available_paths)


def read_repo_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def markdown_section(relative_path: str, heading: str) -> str:
    text = read_repo_text(relative_path)
    match = re.search(
        rf"(?ms)^#{{2,6}}\s+{re.escape(heading)}\s*$\n" rf"(?P<body>.*?)(?=^#{{1,6}}\s+|\Z)",
        text,
    )
    if match is None:
        raise AssertionError(f"missing Markdown section {heading!r} in {relative_path}")
    return match.group("body")


def skill_frontmatter(skill_text: str) -> str:
    match = re.match(r"\A---\s*\n(?P<body>.*?)\n---", skill_text, flags=re.DOTALL)
    if match is None:
        raise AssertionError("missing skill YAML frontmatter")
    return match.group("body")


def normalize_contract_text(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold(), flags=re.UNICODE))


def assert_normalized_terms(
    test_case: unittest.TestCase, text: str, terms: tuple[str, ...]
) -> None:
    normalized = normalize_contract_text(text)
    for term in terms:
        with test_case.subTest(term=term):
            test_case.assertIn(normalize_contract_text(term), normalized)


def inline_code_tokens(text: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"`([^`\n]+)`", text)}


class TestRepositoryContracts(unittest.TestCase):
    def test_user_action_required_notification_contract_is_shared_closed_and_local(self):
        reference_path = "skills/auto-cut/references/user-action-required.md"
        reference = read_repo_text(reference_path)
        agents = read_repo_text("AGENTS.md")
        router = read_repo_text("skills/auto-cut/SKILL.md")

        for action_code in (
            "subject_identity",
            "subject_evidence",
            "preview_approval",
            "project_binding",
            "authorization",
            "high_risk_confirmation",
        ):
            with self.subTest(action_code=action_code):
                self.assertIn(f"`{action_code}`", reference)

        for owner_text in (agents, router):
            with self.subTest(owner="AGENTS" if owner_text is agents else "router"):
                self.assertIn("user-action-required.md", owner_text)

        assert_normalized_terms(
            self,
            reference,
            (
                "persist the durable wait before notification delivery",
                "full blocking question",
                "originating Codex task",
                "notification-only",
                "replies, reactions, or messages do not approve",
                "ordinary clarification",
                "progress update",
                "do not notify",
                "delivery failure does not alter the wait",
                "accept resolution only in the originating Codex task",
                "operation:authorization",
                "operation:high-risk",
                "interactive-only",
                "never create or reuse the subject-pointer external_wait",
                "unavailable summary is a safe status projection, not a receipt",
                "attempt_count is null because attempts are unknown",
            ),
        )
        self.assertIn("`review-job-wait-open`", reference)
        self.assertIn("`deliver_user_action_required`", reference)
        self.assertIn("`notify`", reference)
        notify_commands = re.findall(
            r"```powershell\s*(python scripts/auto_cut_notifications\.py notify.*?)```",
            reference,
            flags=re.DOTALL,
        )
        self.assertEqual(len(notify_commands), 2)
        for action_code, synthetic_item_id in (
            ("authorization", "operation:authorization"),
            ("high_risk_confirmation", "operation:high-risk"),
        ):
            with self.subTest(interactive_action=action_code):
                command = next(
                    candidate
                    for candidate in notify_commands
                    if f"--action-code {action_code}" in candidate
                )
                for required_argument in (
                    "--input-digest",
                    "--project-key",
                    "--action-code",
                    "--item-id",
                    "--prompt-revision",
                    "--json",
                ):
                    self.assertIn(required_argument, command)
                self.assertIn(f"--item-id {synthetic_item_id}", command)
        self.assertLess(
            normalize_contract_text(reference).index(
                normalize_contract_text("persist the durable wait before notification delivery")
            ),
            normalize_contract_text(reference).index(
                normalize_contract_text("deliver_user_action_required")
            ),
        )

    def test_notification_setup_docs_and_release_boundary_are_explicit(self):
        ignore_lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        expected_ignored = (
            "data/auto-cut-notifications.local.json",
            "data/.auto-cut-notifications.local.json.*.tmp",
            "data/auto-cut-notification-receipts.local/",
        )
        for relative_path in expected_ignored:
            with self.subTest(relative_path=relative_path):
                self.assertEqual(ignore_lines.count(relative_path), 1)

        ignored_samples = (
            "data/auto-cut-notifications.local.json",
            "data/.auto-cut-notifications.local.json.crash-leftover.tmp",
            "data/auto-cut-notification-receipts.local/example.json",
        )
        assert_local_state_not_released(self, REPO_ROOT, ignored_samples)

        readme = read_repo_text("README.md")
        self.assertIn("docs/auto-cut-notifications.md", readme)
        notification_docs = read_repo_text("docs/auto-cut-notifications.md")
        assert_normalized_terms(
            self,
            notification_docs,
            (
                "lark-cli",
                "configuration",
                "authentication",
                "recipient approval",
                "content approval",
                "identity approval",
                "dry-run",
                "optional",
                "notification-only",
                "never resolves an operation",
                "replies, reactions, or messages do not approve",
                "setup-preview",
                "setup-enable",
                "status",
                "disable",
                "notify",
            ),
        )

    def test_subject_pointer_release_tracks_guides_and_handoff_but_ignores_local_state(self):
        release_paths = {
            "skills/auto-cut-subject-pointer-onboarding/assets/pointer-material-reference.png",
            "skills/auto-cut-subject-pointer-onboarding/assets/scale-reference-screenshot.png",
            "skills/auto-cut-subject-pointer-onboarding/references/handoff-contract.md",
        }
        assert_release_paths(self, REPO_ROOT, release_paths)

        local_state_paths = (
            "data/subject-pointer-profiles.local/project-bindings.json",
            "data/subject-pointer-profiles.local/profiles/senior-high-history/profile.json",
        )
        ignore_lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(ignore_lines.count("data/subject-pointer-profiles.local/"), 1)
        assert_local_state_not_released(self, REPO_ROOT, local_state_paths)

    def test_revision_input_spec_documents_subject_pointer_external_wait_and_return(self):
        wait_contract = markdown_section(
            "docs/revision-input-spec.md", "Subject-pointer external wait and return"
        )

        assert_normalized_terms(
            self,
            wait_contract,
            (
                "external_wait",
                "job_state.json",
                "subject_pointer_onboarding.json",
                "artifact_sha256",
                "verify the current sidecar SHA-256",
                "pending pointer item IDs",
                "independent non-pointer phases may continue",
                "pointer-dependent phases remain pending",
                "return to auto-cut-pointer-targeting",
                "reload the current project binding",
                "fresh registry check",
                "resolve the external wait",
                "rerun subject_pointer_profile_gate",
            ),
        )

    def test_pyproject_declares_python_and_dev_dependencies(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("[project]", pyproject)
        self.assertIn('requires-python = ">=3.10,<3.13"', pyproject)
        self.assertIn("[project.optional-dependencies]", pyproject)
        self.assertIn('"pytest>=8,<9"', pyproject)
        self.assertIn('"ruff>=0.6,<1"', pyproject)
        self.assertIn('"black>=24,<26"', pyproject)
        self.assertIn('"av>=14,<18"', pyproject)

    def test_requirements_dev_installs_runtime_and_quality_tools(self):
        requirements = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

        self.assertIn("-r requirements.txt", requirements)
        self.assertIn("pytest>=8,<9", requirements)
        self.assertIn("ruff>=0.6,<1", requirements)
        self.assertIn("black>=24,<26", requirements)
        self.assertIn("imageio-ffmpeg>=0.5,<1", requirements)
        self.assertIn("av==17.1.0", (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8"))

    def test_black_excludes_vendored_and_project_bound_sources(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        black_section = pyproject.split("[tool.black]", 1)[1].split("[tool.ruff]", 1)[0]
        black_pattern_match = re.search(r"extend-exclude = '([^']+)'", black_section)

        self.assertIsNotNone(black_pattern_match)
        black_pattern = re.compile(black_pattern_match.group(1))
        self.assertIsNotNone(black_pattern.search("/scripts/vendor/example.py"))
        self.assertIsNotNone(black_pattern.search("/make_tmall_example.py"))
        self.assertIsNotNone(black_pattern.search("/edit_example.py"))
        self.assertIsNone(black_pattern.search("/workflows/make_tmall_example.py"))
        self.assertIsNone(black_pattern.search("/workflows/edit_example.py"))

        ruff_section = pyproject.split("[tool.ruff]", 1)[1].split("[tool.ruff.lint]", 1)[0]
        self.assertIn('extend-exclude = ["./make_tmall_*.py", "./edit_*.py"]', ruff_section)

    def test_black_preserves_audio_source_migration_identity(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn(
            'force-exclude = "tests/audio_sound/test_cli.py"',
            pyproject,
        )

    def test_root_skill_is_a_short_compatibility_pointer(self):
        text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("skills/auto-cut/SKILL.md", text)
        self.assertIn("python scripts/install_repo_skills.py", text)
        self.assertLess(len(text.splitlines()), 80)

    def test_readme_documents_repo_skill_installation_and_invocation_modes(self):
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("python scripts/install_repo_skills.py", text)
        self.assertIn("自然语言直接说", text)
        self.assertIn("点名总 skill", text)
        self.assertIn("点名单独 skill", text)

    def test_revision_markers_preserve_source_ledger_text_verbatim(self):
        revision_skill = read_repo_text("skills/auto-cut-revision-draft/SKILL.md")
        frontmatter = skill_frontmatter(revision_skill)
        agents_contract = markdown_section("AGENTS.md", "Source-ledger Marker Fidelity")
        marker_contract = markdown_section(
            "docs/revision-input-spec.md", "Source-ledger marker identity and recovery"
        )

        assert_normalized_terms(self, frontmatter, ("per-source-item review markers",))
        assert_normalized_terms(
            self,
            agents_contract,
            (
                "source_text",
                "code-point-for-code-point",
                "timestamps",
                "punctuation",
                "whitespace",
                "line breaks",
                "literal question marks",
            ),
        )
        assert_normalized_terms(
            self,
            marker_contract,
            (
                "summary",
                "prefix",
                "ID",
                "translation",
                "truncation",
                "stable source item ID",
                "multiple internal edits",
                "duplicate source item IDs",
                "identical source_text",
                "different IDs",
                "separate",
            ),
        )

        all_marker_contracts = "\n".join(
            (
                read_repo_text("AGENTS.md"),
                revision_skill,
                read_repo_text("skills/auto-cut-revision-draft/references/checklist.md"),
            )
        )
        normalized_contracts = normalize_contract_text(all_marker_contracts)
        for obsolete in (
            "per-change review markers",
            "short and specific",
            "short inspectable way",
            "per requested edit",
            "each requested modification",
        ):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(normalize_contract_text(obsolete), normalized_contracts)

    def test_source_text_recovery_is_canonical_warned_and_nonblocking(self):
        recovery_contract = markdown_section(
            "docs/revision-input-spec.md", "Source-ledger marker identity and recovery"
        )
        reviewability = markdown_section(
            "skills/auto-cut-revision-draft/references/checklist.md", "Reviewability"
        )

        assert_normalized_terms(
            self,
            recovery_contract,
            (
                "latest doc_items",
                "canonical",
                "automatic document re-read recovery",
                "most complete unmodified candidate",
                "verbatim_status unverified_source_unavailable",
                "warning",
                "not claim exactness",
                "not a standalone final failure",
            ),
        )
        assert_normalized_terms(
            self,
            reviewability,
            (
                "source-text recovery",
                "nonblocking",
                "does not stop editing",
                "prevent draft generation",
            ),
        )

        review_items = markdown_section("docs/revision-input-spec.md", "`review_items`")
        self.assertTrue(
            {"bgm_replace", "audio_level", "noise_cleanup"} <= inline_code_tokens(review_items)
        )

    def test_saved_draft_marker_receipts_cover_root_and_active_timeline(self):
        saved_contract = markdown_section(
            "docs/revision-input-spec.md", "Marker receipts and saved-draft validation"
        )
        api = read_repo_text("docs/api.md")
        api_start = api.index("- `add_review_markers(...)` writes")
        api_end = api.index("Keyframe item shape:", api_start)
        marker_api = api[api_start:api_end]

        assert_normalized_terms(
            self,
            saved_contract,
            (
                "root draft_content json",
                "active Timelines main_timeline_id draft_content json",
                "exact marker text",
                "marker count",
                "receipt mapping",
                "mapped timeline start",
                "explicit empty source ledger",
                "extra markers",
            ),
        )
        assert_normalized_terms(
            self, marker_api, ("dynamically allocated", "actual saved text tracks")
        )
        self.assertIn("review marker n", inline_code_tokens(marker_api))
        self.assertNotIn("校对标记1..3", api)

        receipt_fields = {
            "item_id",
            "source_text",
            "verbatim_status",
            "segment_id",
            "material_id",
            "track_name",
            "start_time",
            "duration",
        }
        self.assertTrue(receipt_fields <= inline_code_tokens(marker_api))

    def test_pointer_lifecycle_skills_require_speech_first_character_handoff_and_tail_evidence(
        self,
    ):
        pointer_gate = markdown_section(
            "skills/auto-cut-pointer-targeting/SKILL.md",
            "Pointer Lifecycle Evidence Gate",
        )
        workflow = markdown_section(
            "skills/auto-cut-pointer-targeting/references/workflow.md",
            "6. Decide Duration And Disappearance",
        )
        final_rules = markdown_section(
            "skills/auto-cut-final-acceptance/SKILL.md", "Pass/Fail Rules"
        )
        final_checklist = markdown_section(
            "skills/auto-cut-final-acceptance/references/checklist.md",
            "Visual Evidence",
        )

        expected_terms = (
            "search hint",
            "review_timestamp_role",
            "boundary_control",
            "lifecycle_mode",
            "speech_anchor",
            "start_alignment",
            "first_character_target",
            "source_pointer_handoff",
            "recorded_pointer_visibility",
            "clean_cover_window",
            "motion_preservation",
            "clean_media_sha256",
            "clean_layers",
            "timeline_window",
            "tail_scan",
            "pointer_absent_after",
            "pointer_relevant_after",
            "source-synchronous",
            "first middle last frames",
            "target_timerange start end",
            "media duration",
            "source_timerange",
            "source_media_sha256",
            "source_track_name",
            "source_material_id",
            "comparison_exclusion_regions",
        )
        assert_normalized_terms(self, pointer_gate, expected_terms)
        assert_normalized_terms(self, workflow, expected_terms)
        assert_normalized_terms(
            self,
            final_rules,
            (
                "rough timestamp",
                "start_alignment",
                "first_character_target",
                "source_pointer_handoff",
                "clean_cover_window",
                "tail_scan",
                "first middle last frames",
                "target_timerange start end",
                "media duration",
                "source_timerange",
                "source_media_sha256",
                "source_track_name",
                "source_material_id",
                "comparison_exclusion_regions",
            ),
        )
        assert_normalized_terms(
            self,
            final_checklist,
            (
                "speech_anchor",
                "recorded_pointer_visibility",
                "pointer_absent_after",
                "pointer_relevant_after",
                "source-synchronous",
                "first middle last frames",
                "target_timerange start end",
                "media duration",
                "source_timerange",
                "source_media_sha256",
                "source_track_name",
                "source_material_id",
                "comparison_exclusion_regions",
            ),
        )

    def test_pointer_skills_require_editable_residual_cover_and_opened_state_proof(self):
        pointer_gate = markdown_section(
            "skills/auto-cut-pointer-targeting/SKILL.md",
            "Pointer Lifecycle Evidence Gate",
        )
        workflow = markdown_section(
            "skills/auto-cut-pointer-targeting/references/workflow.md",
            "6. Decide Duration And Disappearance",
        )
        final_rules = markdown_section(
            "skills/auto-cut-final-acceptance/SKILL.md", "Pass/Fail Rules"
        )
        final_checklist = markdown_section(
            "skills/auto-cut-final-acceptance/references/checklist.md",
            "Visual Evidence",
        )
        expected_terms = (
            "residual_pointer_cover",
            "transparent_roi_still_cover",
            "hard_edge_rectangle",
            "opaque_regions",
            "exactly one",
            "trajectory_bounds",
            "trajectory_receipt",
            "source_clean_frame_sequence_v1",
            "source_media_path",
            "source_media_sha256",
            "safety_margin_px",
            "alpha exactly 255",
            "alpha outside",
            "background_samples",
            "background not static",
            "source-synchronous cleanup video",
            "final_composite_samples",
            "recorded_first_visible",
            "recorded_midpoint",
            "recorded_last_visible",
            "distinct artifact",
            "visual_inspection",
            "opened_jianying_canvas_manual_inspection_v2",
            "editor_context_path",
            "editor_context_sha256",
            "editor_canvas_rect",
            "automatic template",
            "full-canvas template matching",
            "external hash-bound capture record",
            "no evaluable recorded-hand pixels",
            "receipt source frames",
            "every bound source frame",
            "real pts",
            "half-open window boundaries",
            "background samples bound to trajectory clean-frame rows",
            "every clean frame",
            "low-texture",
            "multi-peak",
            "registered opened-canvas residual",
            "low-opacity recorded-hand remnants",
            "registration mask excludes the recorded-hand trajectory and editable pointer region",
            "positive rounded editor crop dimensions",
            "windows_jianying_window_capture_v1",
            "playhead time",
            "saved position keyframes",
            "curvetype=line",
            "nonlinear",
            "zero hands",
            "two hands",
            "saved source-video segments",
            "root and active timeline variants independently",
            "only one hand",
            "added pointer remains independently editable",
            "opened JianYing",
        )

        for contract in (pointer_gate, workflow, final_rules, final_checklist):
            assert_normalized_terms(self, contract, expected_terms)

    def test_pointer_skills_require_zero_hand_clean_frames_and_trajectory_turn_samples(self):
        contracts = (
            markdown_section(
                "skills/auto-cut-pointer-targeting/SKILL.md",
                "Pointer Lifecycle Evidence Gate",
            ),
            markdown_section(
                "skills/auto-cut-pointer-targeting/references/workflow.md",
                "6. Decide Duration And Disappearance",
            ),
            markdown_section("skills/auto-cut-final-acceptance/SKILL.md", "Pass/Fail Rules"),
            markdown_section(
                "skills/auto-cut-final-acceptance/references/checklist.md",
                "Visual Evidence",
            ),
        )
        expected_terms = (
            "zero registered-hand instances in every clean frame",
            "source-template detection union",
            "trajectory extrema",
            "direction changes",
            "additional opened JianYing samples",
            "one decoded-frame interval",
            "connected residual evidence",
            "isolated glyph-color coincidence",
            "pointer_cover.clean_frame_recorded_pointer_present",
            "pointer_cover.trajectory_bounds_miss_detected_pointer",
            "pointer_cover.opened_jianying_trajectory_samples_incomplete",
            "pointer_cover.source_pointer_trajectory_unverifiable",
        )
        for contract in contracts:
            assert_normalized_terms(self, contract, expected_terms)

    def test_skills_require_conditional_opened_state_and_full_restart_for_external_draft_refresh(
        self,
    ):
        contracts = (
            markdown_section("skills/auto-cut-revision-draft/SKILL.md", "5. Draft Safety"),
            markdown_section("skills/auto-cut-revision-draft/references/checklist.md", "Safety"),
            markdown_section(
                "skills/auto-cut-pointer-targeting/SKILL.md",
                "Pointer Lifecycle Evidence Gate",
            ),
            markdown_section(
                "skills/auto-cut-pointer-targeting/references/workflow.md",
                "6. Decide Duration And Disappearance",
            ),
            markdown_section("skills/auto-cut-final-acceptance/SKILL.md", "Pass/Fail Rules"),
            markdown_section(
                "skills/auto-cut-final-acceptance/references/checklist.md",
                "Visual Evidence",
            ),
        )
        expected_terms = (
            "externally written new draft",
            "returning to the draft home page is not a refresh",
            "complete JianYing process exit and relaunch",
            "only when the target draft is not visible",
            "protect unsaved work",
            "once the target draft is open",
            "do not exit",
            "draft name",
            "timeline ID",
        )
        for contract in contracts:
            assert_normalized_terms(self, contract, expected_terms)

        skill_profile = markdown_section(
            "skills/auto-cut-final-acceptance/SKILL.md", "Routed Gate Profile"
        )
        checklist_profile = markdown_section(
            "skills/auto-cut-final-acceptance/references/checklist.md",
            "Routed Gate Profile",
        )
        for profile in (skill_profile, checklist_profile):
            assert_normalized_terms(
                self,
                profile,
                (
                    "opened-state gate is conditional",
                    "ordinary revisions do not launch JianYing",
                    "pointer cover",
                    "opened-draft display drift",
                ),
            )

    def test_semantic_pause_contract_requires_hash_bound_sentence_gap(self):
        alignment_contracts = (
            read_repo_text("skills/auto-cut-review-audio-precision/SKILL.md"),
            read_repo_text("skills/auto-cut-review-audio-precision/references/workflow.md"),
            read_repo_text("docs/revision-input-spec.md"),
        )
        alignment_terms = (
            "rough timestamp is a search hint",
            "source ASR path",
            "source ASR SHA-256",
            "requested source time",
            "resolved source time",
            "utterance gap",
            "midpoint",
            "guard time",
            "spoken word",
            "frame source time",
            "audio delivery plan",
            "after pause alignment",
            "full-candidate reverse ASR",
            "preceding sentence tail",
            "following sentence onset",
            "utterance-only ASR is insufficient",
            "real word or character timing",
            "semantic pause edit",
            "pause_adjustments",
            "correspond one-to-one",
        )
        for contract in alignment_contracts:
            assert_normalized_terms(self, contract, alignment_terms)

        acceptance_contracts = (
            read_repo_text("skills/auto-cut-revision-draft/references/checklist.md"),
            read_repo_text("skills/auto-cut-final-acceptance/SKILL.md"),
            read_repo_text("skills/auto-cut-final-acceptance/references/checklist.md"),
        )
        acceptance_terms = (
            "source ASR path",
            "source ASR SHA-256",
            "requested source time",
            "resolved source time",
            "utterance gap",
            "midpoint",
            "guard time",
            "spoken word",
            "frame source time",
            "audio delivery plan",
            "after pause alignment",
            "full-candidate reverse ASR",
            "preceding sentence tail",
            "following sentence onset",
            "utterance-only ASR is insufficient",
            "real word or character timing",
            "semantic pause edit",
            "pause_adjustments",
            "correspond one-to-one",
        )
        for contract in acceptance_contracts:
            assert_normalized_terms(self, contract, acceptance_terms)

        revision_spec = read_repo_text("docs/revision-input-spec.md")
        required_schema_fields = (
            "project.media_duration_seconds",
            "pause_alignment.source_asr_path",
            "pause_alignment.source_asr_sha256",
            "pause_alignment.source_audio_sha256",
            "pause_alignment.source_video_sha256",
            "pause_alignment.alignment_audio_path",
            "pause_alignment.alignment_audio_sha256",
            "pause_alignment.source_asr_identity",
            "pause_adjustments[*].frame_source_time",
            "pause_adjustments[*].frame_sha256",
            "processed_audio.audio_delivery_plan_sha256",
        )
        for field in required_schema_fields:
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", revision_spec)

        for contract in (
            read_repo_text("skills/auto-cut-review-audio-precision/SKILL.md"),
            read_repo_text("skills/auto-cut-final-acceptance/references/checklist.md"),
        ):
            assert_normalized_terms(
                self,
                contract,
                (
                    "source audio SHA-256",
                    "source video SHA-256",
                    "ASR identity",
                    "preprocessing",
                    "frame matches source video",
                    "audio delivery plan SHA-256",
                ),
            )

    def test_revision_ui_contract_is_offline_first_and_reports_controller_calls(self):
        offline_contracts = (
            markdown_section("skills/auto-cut-revision-draft/SKILL.md", "5. Draft Safety"),
            markdown_section("skills/auto-cut-revision-draft/references/checklist.md", "Safety"),
            read_repo_text("docs/revision-input-spec.md"),
        )
        offline_terms = (
            "offline",
            "opened_verify",
            "export",
            "default",
            "must not construct JianyingController",
            ".locked",
            "fallback draft",
        )
        for contract in offline_contracts:
            assert_normalized_terms(self, contract, offline_terms)

        report_contracts = (
            markdown_section("skills/auto-cut-final-acceptance/SKILL.md", "Routed Gate Profile"),
            markdown_section(
                "skills/auto-cut-final-acceptance/references/checklist.md",
                "Routed Gate Profile",
            ),
            read_repo_text("docs/revision-input-spec.md"),
        )
        report_terms = (
            "offline",
            "opened_verify",
            "export",
            "ui_mode",
            "opened_state_required",
            "opened_state_reason",
            "opened_state_status",
            "controller_calls",
        )
        for contract in report_contracts:
            assert_normalized_terms(self, contract, report_terms)

    def test_final_acceptance_routes_always_on_and_conditional_gates(self):
        skill_profile = markdown_section(
            "skills/auto-cut-final-acceptance/SKILL.md", "Routed Gate Profile"
        )
        checklist_profile = markdown_section(
            "skills/auto-cut-final-acceptance/references/checklist.md",
            "Routed Gate Profile",
        )

        assert_normalized_terms(
            self,
            skill_profile,
            (
                "always-on cheap gates",
                "source coverage",
                "execution evidence",
                "draft existence",
                "editable structure",
                "verbatim markers",
                "audio delivery",
                "conditional expensive gates",
                "audio precision join",
                "pause",
                "visual",
                "pointer",
                "animation",
            ),
        )

        gate_tokens = inline_code_tokens(checklist_profile)
        always_gates = {
            "source_coverage",
            "execution_evidence",
            "draft_exists",
            "editable_structure",
            "verbatim_markers",
            "audio_delivery",
        }
        conditional_gates = {
            "audio_precision",
            "audio_join",
            "pause_fit",
            "visual",
            "pointer",
            "animation",
        }
        self.assertTrue(always_gates <= gate_tokens)
        self.assertTrue(conditional_gates <= gate_tokens)
        assert_normalized_terms(
            self,
            checklist_profile,
            (
                "BGM",
                "level",
                "noise",
                "skip reverse ASR",
                "explicitly required",
                "attributable evidence",
                "cannot disable",
                "detected work",
                "unknown execution types",
                "structured review",
                "fail results",
                "strict final CLI",
                "draft is missing",
                "source-text recovery",
                "nonblocking",
            ),
        )

    def test_spoken_audio_changes_keep_one_full_candidate_reverse_asr(self):
        validation_rules = markdown_section(
            "skills/auto-cut-review-audio-precision/SKILL.md", "Validation Rules"
        )

        assert_normalized_terms(
            self,
            validation_rules,
            (
                "spoken-audio change",
                "final full-candidate reverse ASR",
                "delivered candidate",
                "local seam",
                "must_keep",
                "visual-context windows",
                "targeted",
            ),
        )

    def test_spoken_audio_contract_cross_checks_transcript_and_hit_evidence(self):
        contracts = (
            read_repo_text("skills/auto-cut-review-audio-precision/SKILL.md"),
            read_repo_text("skills/auto-cut-review-audio-precision/references/workflow.md"),
            read_repo_text("skills/auto-cut-final-acceptance/references/checklist.md"),
            read_repo_text("docs/revision-input-spec.md"),
        )
        for contract in contracts:
            assert_normalized_terms(
                self,
                contract,
                (
                    "local transcript",
                    "local transcript must contain alphanumeric content",
                    "transcript aliases must agree after normalization",
                    "non-empty item contract",
                    "strategy and delete",
                    "explicit must_keep field",
                    "delete phrase",
                    "every must_keep phrase",
                    "delete_hit_adjudication",
                    "classification=kept_recurrence",
                    "occurrence_role",
                    "local_context",
                    "context_anchor",
                    "not a substring of the delete phrase",
                    "after removing the delete phrase",
                    "exactly one positive delete_hit",
                    "multiple positive delete_hits",
                    "multiple local transcript delete occurrences",
                    "including overlapping occurrences",
                    "per-hit adjudication",
                    "positive delete_hit must match the item delete phrase",
                    "candidate SHA-256 participates in the duration cache key",
                    "latest canonical doc item kind",
                    "spoken-delete and semantic-pause evidence are validated independently",
                    "pass_adjudicated",
                    "non-empty reason",
                    "final_gap between 0 and 0.2 seconds",
                    "no_extra_deletion_contract=pass",
                ),
            )

    def test_automatic_repair_contract_protects_live_project_state(self):
        contracts = (
            read_repo_text("skills/auto-cut-review-audio-precision/SKILL.md"),
            read_repo_text("skills/auto-cut-review-audio-precision/references/workflow.md"),
            read_repo_text("skills/auto-cut-final-acceptance/references/checklist.md"),
            read_repo_text("docs/revision-input-spec.md"),
        )
        for contract in contracts:
            assert_normalized_terms(
                self,
                contract,
                (
                    "repair callback must not mutate the live project",
                    "must not write saved draft files directly",
                    "return a scoped RevisionRequest",
                ),
            )

    def test_router_sends_review_jobs_through_verbatim_acceptance(self):
        repository_contract = markdown_section("skills/auto-cut/SKILL.md", "Repository Contract")

        assert_normalized_terms(
            self,
            repository_contract,
            (
                "canonical source-ledger recovery",
                "verbatim marker validation",
                "routed final-acceptance profile",
            ),
        )

    def test_source_document_jobs_default_to_resumable_review_tooling(self):
        router_contract = markdown_section("skills/auto-cut/SKILL.md", "Repository Contract")
        operator_contract = markdown_section(
            "docs/minimal-command-sop.md", "Source-document Review Jobs"
        )

        assert_normalized_terms(
            self,
            router_contract,
            (
                "natural-language source-document jobs",
                "review-job-compile",
                "default",
                "resumable job tooling",
                "legacy direct scripts",
                "compatible",
                "cannot claim optimized source-document final acceptance without evidence",
            ),
        )
        assert_normalized_terms(
            self,
            operator_contract,
            (
                "review-job-compile",
                "review-job-status",
                "review-job-cache-inspect",
                "read-only",
                "does not mutate",
                "tmp",
                "ignored",
            ),
        )

    def test_resumable_cache_identities_and_invalidation_are_explicit(self):
        cache_contract = markdown_section(
            "docs/revision-input-spec.md", "Cache Identity And Invalidation"
        )
        audio_contract = markdown_section(
            "skills/auto-cut-review-audio-precision/SKILL.md",
            "Resumable ASR Evidence",
        )

        assert_normalized_terms(
            self,
            cache_contract,
            (
                "source audio SHA256",
                "preprocessing",
                "provider",
                "model resource ID",
                "adapter version",
                "complete final candidate audio SHA256",
                "video SHA256",
                "source window time",
                "extraction parameters",
                "tool version",
                "draft timeline digest",
                "affected window",
                "render settings",
                "renderer version",
                "document token",
                "revision identity",
                "extraction schema",
                "missing versions",
                "hash mismatch",
                "changed cut",
                "changed preprocessing",
                "invalidate",
            ),
        )
        assert_normalized_terms(
            self,
            audio_contract,
            (
                "complete final candidate audio SHA256",
                "provider model resource",
                "adapter version",
                "spoken-audio change",
                "new hash",
                "one final full-candidate reverse ASR",
                "cache",
                "required gate",
                "skipped evidence",
            ),
        )

    def test_resumable_pipeline_has_seven_atomic_phases_and_isolated_resume(self):
        pipeline_contract = markdown_section(
            "docs/revision-input-spec.md", "Atomic Phases And Resume"
        )
        expected_phases = (
            "document_snapshot_ledger",
            "source_materials_hashes",
            "source_asr_visual_index",
            "classified_edit_acceptance_plans",
            "processed_local_media_evidence",
            "saved_editable_draft_marker_receipts",
            "final_acceptance",
        )

        positions = []
        for phase in expected_phases:
            with self.subTest(phase=phase):
                token = f"`{phase}`"
                self.assertIn(token, pipeline_contract)
                positions.append(pipeline_contract.index(token))
        self.assertEqual(positions, sorted(positions))
        assert_normalized_terms(
            self,
            pipeline_contract,
            (
                "atomic job_state.json checkpoint",
                "corrupt",
                "mismatched",
                "reruns only that phase",
                "missing source text recovery",
                "never cancels unrelated phases",
                "draft generation",
            ),
        )

    def test_pipeline_bounds_readonly_work_and_serializes_all_writes(self):
        pipeline_contract = markdown_section(
            "skills/auto-cut-revision-draft/SKILL.md",
            "Resumable Source-document Jobs",
        )

        assert_normalized_terms(
            self,
            pipeline_contract,
            (
                "bounded",
                "independent read-only preparation",
                "JianYing writes",
                "saved-draft inspection",
                "serialized",
                "ordered Feishu writes",
                "serialized globally",
                "overlapping timeline repairs",
                "prohibited",
                "no two writers",
                "same draft",
            ),
        )

    def test_local_evidence_windows_never_replace_global_acceptance(self):
        evidence_contract = markdown_section(
            "skills/auto-cut-final-acceptance/SKILL.md", "Resumable Evidence Gate"
        )

        assert_normalized_terms(
            self,
            evidence_contract,
            (
                "configured context before and after",
                "one optional full preview",
                "explicitly requested",
                "acceptance profile",
                "local checks",
                "never replace",
                "global structure",
                "timeline validation",
                "final full-candidate reverse ASR",
                "spoken-audio changes",
            ),
        )

    def test_job_timing_and_benchmark_contract_is_attributable(self):
        telemetry_contract = markdown_section("AGENTS.md", "Resumable Review Pipeline")

        timing_fields = {
            "started_at",
            "finished_at",
            "elapsed_seconds",
            "active_seconds",
            "wait_seconds",
            "cache_hit",
            "retry_count",
            "worker_count",
            "item_ids",
            "input_digest",
            "output_digest",
            "unresolved_item_ids",
        }
        self.assertTrue(timing_fields <= inline_code_tokens(telemetry_contract))
        assert_normalized_terms(
            self,
            telemetry_contract,
            (
                "job_state.json",
                "job_timing.json",
                "cache hit miss",
                "no secrets",
                "active compute",
                "external API wait",
                "application user blocking",
                "first optimized real job",
                "baseline",
                "media duration",
                "item gate mix",
                "not wall clock alone",
            ),
        )

    def test_pipeline_optimization_preserves_accuracy_invariants(self):
        telemetry_contract = markdown_section("AGENTS.md", "Resumable Review Pipeline")

        assert_normalized_terms(
            self,
            telemetry_contract,
            (
                "cannot remove evidence",
                "flatten the draft",
                "import full QA narration",
                "exact markers",
                "must_keep",
                "visual",
                "pointer",
                "animation gates",
                "missing source text",
                "block editing",
            ),
        )

    def test_complete_project_delivery_requires_the_fixed_path_mirror(self):
        router_contract = markdown_section("skills/auto-cut/SKILL.md", "Complete Project Delivery")

        assert_normalized_terms(
            self,
            router_contract,
            (
                "complete project",
                "migratable project",
                "full Auto-Cut delivery",
                "intermediate drafts are exempt",
                "editable draft acceptance",
                "absolute path",
                "byte-for-byte",
                "draft-root-check",
                "draft-mirror-deliver",
                "does not open JianYing",
                "does not rewrite draft JSON",
            ),
        )
        self.assertIn("not final delivery evidence", router_contract)

    def test_native_acceptance_distinguishes_receipt_and_second_computer_boundaries(self):
        native_contract = markdown_section(
            "skills/auto-cut-final-acceptance/SKILL.md", "Fixed-Path Native Delivery"
        )
        assert_normalized_terms(
            self,
            native_contract,
            (
                "currentCustomDraftPath",
                "absolute path",
                "another physical computer",
                "left the loading state",
                "previewed",
                "remained editable",
            ),
        )

    def test_native_checklist_requires_tree_identity_and_no_mutation(self):
        checklist = markdown_section(
            "skills/auto-cut-final-acceptance/references/checklist.md",
            "Fixed-Path Native Delivery",
        )
        assert_normalized_terms(
            self,
            checklist,
            (
                "destination already exists",
                "refuse overwrite",
                "source tree",
                "target tree",
                "json_rewritten=false",
                "ui_invoked=false",
                "portable_package_invoked=false",
                "SHA-256",
                "continuous source-stability gate",
                "quiet window",
                "temporary sibling",
                "retry",
                "source_stable_before_copy=true",
                "source_stable_after_copy=true",
                "source_stable_at_promotion=true",
            ),
        )

    def test_native_delivery_is_documented_without_a_new_skill_or_importer(self):
        readme = read_repo_text("README.md")
        source_catalog = read_repo_text("skills/auto-cut/references/skill-catalog.md")
        docs_catalog = read_repo_text("docs/skill-catalog.md")

        assert_normalized_terms(
            self,
            readme,
            (
                "Complete JianYing Project Delivery",
                "absolute path",
                "Resources/local",
                "Resources/audioAlg",
                "currentCustomDraftPath",
                "draft-mirror-deliver",
            ),
        )
        self.assertNotIn("AutoCut工程导入工具.exe", readme)
        self.assertNotIn("python scripts/portable_project_tool.py package", readme)
        for catalog in (source_catalog, docs_catalog):
            self.assertIn("完整工程", catalog)
            self.assertIn("换电脑继续编辑", catalog)
            self.assertIn("auto-cut-final-acceptance", catalog)

    def test_native_docs_use_receipt_paths_and_closed_root_policy(self):
        readme = read_repo_text("README.md")
        router_contract = markdown_section("skills/auto-cut/SKILL.md", "Complete Project Delivery")
        acceptance_contract = markdown_section(
            "skills/auto-cut-final-acceptance/SKILL.md", "Fixed-Path Native Delivery"
        )
        combined = "\n".join((readme, router_contract, acceptance_contract))

        for required in (
            "draft-root-check",
            "draft-mirror-deliver",
            "receipt-json",
            "source_tree_sha256",
            "target_tree_sha256",
            "currentCustomDraftPath",
        ):
            with self.subTest(required=required):
                self.assertIn(required, combined)
        self.assertNotIn("python scripts/portable_project_tool.py package", combined)
        self.assertNotIn("python scripts/portable_project_tool.py import", combined)
        assert_normalized_terms(
            self,
            combined,
            (
                "absolute path",
                "refuse overwrite",
                "byte-for-byte",
            ),
        )

    def test_native_docs_disclose_integrity_only_trust_boundary(self):
        readme = read_repo_text("README.md")
        acceptance_contract = markdown_section(
            "skills/auto-cut-final-acceptance/SKILL.md", "Fixed-Path Native Delivery"
        )
        checklist = markdown_section(
            "skills/auto-cut-final-acceptance/references/checklist.md",
            "Fixed-Path Native Delivery",
        )

        for document in (readme, acceptance_contract, checklist):
            assert_normalized_terms(
                self,
                document,
                (
                    "SHA-256",
                    "source tree",
                    "target tree",
                    "byte-for-byte",
                    "trusted transfer",
                ),
            )

    def test_native_docs_require_complete_tree_identity_and_path_evidence(self):
        readme = read_repo_text("README.md")
        router_contract = markdown_section("skills/auto-cut/SKILL.md", "Complete Project Delivery")
        acceptance_contract = markdown_section(
            "skills/auto-cut-final-acceptance/SKILL.md", "Fixed-Path Native Delivery"
        )
        checklist = markdown_section(
            "skills/auto-cut-final-acceptance/references/checklist.md",
            "Fixed-Path Native Delivery",
        )
        combined = "\n".join((readme, router_contract, acceptance_contract, checklist))

        for required in (
            "schema_version",
            "source_tree_sha256",
            "target_tree_sha256",
            "tree_sha256",
            "directory_count",
            "file_count",
            "byte_size",
            "currentCustomDraftPath",
            "configured_target_path_missing",
        ):
            with self.subTest(required=required):
                self.assertIn(required, combined)
        assert_normalized_terms(
            self,
            combined,
            (
                "empty directories",
                "absolute path",
                "json_rewritten",
                "native_editor_invoked",
            ),
        )

    def test_native_skills_close_path_and_overwrite_loopholes(self):
        router_contract = markdown_section("skills/auto-cut/SKILL.md", "Complete Project Delivery")
        acceptance_contract = markdown_section(
            "skills/auto-cut-final-acceptance/SKILL.md", "Fixed-Path Native Delivery"
        )
        checklist = markdown_section(
            "skills/auto-cut-final-acceptance/references/checklist.md",
            "Fixed-Path Native Delivery",
        )
        combined = "\n".join((router_contract, acceptance_contract, checklist))

        assert_normalized_terms(
            self,
            combined,
            (
                "configured_target_path_missing",
                "destination already exists",
                "source tree",
                "target tree",
                "reparse",
                "byte-for-byte",
            ),
        )

    def test_native_docs_preserve_editable_draft_and_material_tree(self):
        contracts = (
            read_repo_text("README.md"),
            markdown_section("skills/auto-cut/SKILL.md", "Complete Project Delivery"),
            markdown_section(
                "skills/auto-cut-final-acceptance/SKILL.md", "Fixed-Path Native Delivery"
            ),
            markdown_section(
                "skills/auto-cut-final-acceptance/references/checklist.md",
                "Fixed-Path Native Delivery",
            ),
        )

        for contract in contracts:
            assert_normalized_terms(
                self,
                contract,
                (
                    "editable timeline",
                    "source video",
                    "source audio",
                    "local materials",
                    "Resources/local",
                    "Resources/audioAlg",
                ),
            )
            self.assertNotIn(
                normalize_contract_text("same visible primary non-preview track"),
                normalize_contract_text(contract),
            )

    def test_auto_cut_router_documents_fail_closed_reliability_gates(self):
        contract = markdown_section("skills/auto-cut/SKILL.md", "Complete Project Delivery")

        assert_normalized_terms(
            self,
            contract,
            (
                "source tree",
                "target tree",
                "byte-for-byte",
                "destination already exists",
                "reparse",
                "currentCustomDraftPath",
            ),
        )
        self.assertIn("`json_rewritten`", contract)

    def test_final_acceptance_skill_documents_fail_closed_reliability_gates(self):
        contracts = (
            markdown_section(
                "skills/auto-cut-final-acceptance/SKILL.md", "Fixed-Path Native Delivery"
            ),
            markdown_section(
                "skills/auto-cut-final-acceptance/references/checklist.md",
                "Fixed-Path Native Delivery",
            ),
        )

        for contract in contracts:
            assert_normalized_terms(
                self,
                contract,
                (
                    "source tree",
                    "target tree",
                    "byte-for-byte",
                    "destination already exists",
                    "currentCustomDraftPath",
                ),
            )
            self.assertIn("`json_rewritten`", contract)

    def test_native_readme_design_and_plan_document_reliability_gates(self):
        documents = (read_repo_text("README.md"),)

        for document in documents:
            assert_normalized_terms(
                self,
                document,
                (
                    "source tree",
                    "target tree",
                    "byte-for-byte",
                    "currentCustomDraftPath",
                    "does not open JianYing",
                ),
            )
            self.assertIn("json_rewritten", document)

        combined = "\n".join(documents)
        for field in ("source_tree_sha256", "target_tree_sha256", "currentCustomDraftPath"):
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", combined)


if __name__ == "__main__":
    unittest.main()
