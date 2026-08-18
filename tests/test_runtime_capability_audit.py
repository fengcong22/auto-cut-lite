from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.release import audit_runtime_capabilities as auditor

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDITOR_PATH = REPO_ROOT / "scripts" / "release" / "audit_runtime_capabilities.py"
CONTRACT_PATH = REPO_ROOT / "runtime-capability-contract.json"
SCHEMA_PATH = REPO_ROOT / "schemas" / "runtime-capability-contract.schema.json"


def test_runtime_capability_auditor_is_bundled() -> None:
    assert AUDITOR_PATH.is_file()


def test_runtime_capability_contract_and_schema_are_bundled() -> None:
    assert CONTRACT_PATH.is_file()
    assert SCHEMA_PATH.is_file()


def test_runtime_capability_auditor_exposes_stable_api() -> None:
    assert callable(getattr(auditor, "audit_runtime_capabilities", None))
    assert callable(getattr(auditor, "main", None))


def _capability(
    capability_id: str = "demo",
    *,
    required_paths: list[str] | None = None,
    verification_command: str = "python scripts/demo.py --check",
) -> dict[str, object]:
    return {
        "id": capability_id,
        "entrypoints": ["scripts/demo.py"],
        "required_paths": required_paths or ["scripts/demo.py"],
        "dependency_imports": [],
        "external_service_hosts": [],
        "dynamic_service_policy": "none",
        "external_tools": [],
        "verification_command": verification_command,
    }


def _write_minimal_contract_repo(
    tmp_path: Path,
    *,
    capabilities: list[dict[str, object]] | None = None,
    manifest_ids: list[str] | None = None,
) -> tuple[Path, list[str]]:
    root = tmp_path / "Auto-Cut"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "release").mkdir()
    (root / "schemas").mkdir()
    (root / "tests").mkdir()
    (root / "scripts" / "demo.py").write_text("print('ready')\n", encoding="utf-8")
    (root / "scripts" / "release" / "audit_runtime_capabilities.py").write_text(
        "# runtime capability audit fixture\n", encoding="utf-8"
    )
    (root / "tests" / "test_runtime_capability_audit.py").write_text(
        "# runtime capability audit test fixture\n", encoding="utf-8"
    )
    contract_capabilities = capabilities or [_capability()]
    manifest_capability_ids = manifest_ids or [str(contract_capabilities[0]["id"])]
    verification_by_id = {
        str(row["id"]): str(row["verification_command"]) for row in contract_capabilities
    }
    manifest = {
        "schema_version": 1,
        "release_version": "1.6.1",
        "capabilities": [
            {
                "id": capability_id,
                "verification_command": verification_by_id.get(
                    capability_id, "python scripts/demo.py --check"
                ),
            }
            for capability_id in manifest_capability_ids
        ],
    }
    contract = {
        "$schema": "schemas/runtime-capability-contract.schema.json",
        "schema_version": 1,
        "release_version": "1.6.1",
        "capabilities": contract_capabilities,
    }
    (root / "capability-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "runtime-capability-contract.json").write_text(json.dumps(contract), encoding="utf-8")
    (root / "schemas" / "runtime-capability-contract.schema.json").write_text(
        SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    release_paths = [
        "capability-manifest.json",
        "runtime-capability-contract.json",
        "schemas/runtime-capability-contract.schema.json",
        "scripts/release/audit_runtime_capabilities.py",
        "tests/test_runtime_capability_audit.py",
        "scripts/demo.py",
    ]
    return root, release_paths


def _finding_codes(result: dict[str, object]) -> set[str]:
    return {str(row["code"]) for row in result["findings"]}


def test_minimal_declared_runtime_contract_is_ready(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert result["status"] == "ready"
    assert result["declared_capability_ids"] == ["demo"]
    assert result["manifest_ids"] == ["demo"]
    assert result["findings"] == []


def test_audit_rejects_missing_required_runtime_path(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(
        tmp_path,
        capabilities=[_capability(required_paths=["scripts/missing.py"])],
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "missing_runtime_path" in _finding_codes(result)


def test_audit_rejects_runtime_path_omitted_from_release_inventory(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    release_paths.remove("scripts/demo.py")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "runtime_path_not_released" in _finding_codes(result)


@pytest.mark.parametrize(
    "control_path",
    [
        "capability-manifest.json",
        "runtime-capability-contract.json",
        "schemas/runtime-capability-contract.schema.json",
        "scripts/release/audit_runtime_capabilities.py",
        "tests/test_runtime_capability_audit.py",
    ],
)
def test_audit_rejects_control_file_omitted_from_release_inventory(
    tmp_path: Path, control_path: str
) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    release_paths.remove(control_path)

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "runtime_control_file_not_released" in _finding_codes(result)


@pytest.mark.parametrize(
    "control_path",
    [
        "capability-manifest.json",
        "runtime-capability-contract.json",
        "schemas/runtime-capability-contract.schema.json",
        "scripts/release/audit_runtime_capabilities.py",
        "tests/test_runtime_capability_audit.py",
    ],
)
def test_audit_rejects_missing_runtime_control_file(tmp_path: Path, control_path: str) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / control_path).unlink()

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "missing_runtime_control_file" in _finding_codes(result)


def test_audit_rejects_manifest_and_contract_id_mismatch(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path, manifest_ids=["other"])

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "capability_id_set_mismatch" in _finding_codes(result)


def test_audit_rejects_verification_command_mismatch(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    manifest_path = root / "capability-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"][0]["verification_command"] = "python other.py"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "verification_command_mismatch" in _finding_codes(result)


def test_audit_rejects_unsafe_contract_paths(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(
        tmp_path,
        capabilities=[_capability(required_paths=["D:/codex/private.py"])],
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "contract_invalid" in _finding_codes(result)


def test_audit_rejects_matching_non_semver_contract_and_manifest_versions(
    tmp_path: Path,
) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    for name in ("runtime-capability-contract.json", "capability-manifest.json"):
        path = root / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["release_version"] = "release-one"
        path.write_text(json.dumps(payload), encoding="utf-8")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "contract_invalid" in _finding_codes(result)


def test_audit_validates_non_semver_manifest_version_instead_of_only_comparing_it(
    tmp_path: Path,
) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    manifest_path = root / "capability-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["release_version"] = "release-one"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert any(
        finding["code"] == "contract_invalid"
        and finding["summary"] == "runtime capability contract could not be validated"
        for finding in result["findings"]
    )


def test_audit_rejects_unparseable_bundled_contract_schema(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "schemas" / "runtime-capability-contract.schema.json").write_text(
        "{not-json\n", encoding="utf-8"
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "contract_invalid" in _finding_codes(result)


def test_audit_rejects_bundled_contract_schema_that_mismatches_validator(
    tmp_path: Path,
) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    schema_path = root / "schemas" / "runtime-capability-contract.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["release_version"]["pattern"] = "^.+$"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "contract_invalid" in _finding_codes(result)


def test_repository_runtime_contract_schema_is_closed_and_ids_match_manifest() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    manifest = json.loads((REPO_ROOT / "capability-manifest.json").read_text(encoding="utf-8"))

    assert set(contract) == {"$schema", "schema_version", "release_version", "capabilities"}
    assert contract["$schema"] == "schemas/runtime-capability-contract.schema.json"
    assert contract["schema_version"] == 1
    assert contract["release_version"] == manifest["release_version"]
    assert {row["id"] for row in contract["capabilities"]} == {
        row["id"] for row in manifest["capabilities"]
    }
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["capability"]["additionalProperties"] is False
    assert set(schema["$defs"]["capability"]["required"]) == auditor.CAPABILITY_FIELDS


@pytest.mark.parametrize(
    "source",
    [
        "import missingpkg\n",
        "from missingpkg import Client\n",
        "import importlib\nimportlib.import_module('missingpkg')\n",
        "import importlib.util\nimportlib.util.find_spec('missingpkg')\n",
        "try:\n    import missingpkg\nexcept ImportError:\n    missingpkg = None\n",
    ],
)
def test_audit_rejects_undeclared_static_and_dynamic_imports(tmp_path: Path, source: str) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "scripts" / "demo.py").write_text(source, encoding="utf-8")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_dependency_import" in _finding_codes(result)


def test_audit_rejects_declared_dependency_missing_from_requirements(tmp_path: Path) -> None:
    capability = _capability()
    capability["dependency_imports"] = [
        {
            "module": "requests",
            "distribution": "requests",
            "environment": "main",
            "disposition": "direct",
        }
    ]
    root, release_paths = _write_minimal_contract_repo(tmp_path, capabilities=[capability])
    (root / "scripts" / "demo.py").write_text("import requests\n", encoding="utf-8")
    (root / "requirements.txt").write_text("playwright==1.52.0\n", encoding="utf-8")
    release_paths.append("requirements.txt")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "dependency_requirement_missing" in _finding_codes(result)


def test_audit_accepts_declared_dependency_and_local_import(tmp_path: Path) -> None:
    capability = _capability(required_paths=["scripts/demo.py", "scripts/local_module.py"])
    capability["dependency_imports"] = [
        {
            "module": "requests",
            "distribution": "requests",
            "environment": "main",
            "disposition": "direct",
        }
    ]
    root, release_paths = _write_minimal_contract_repo(tmp_path, capabilities=[capability])
    (root / "scripts" / "demo.py").write_text(
        "import json\nimport requests\nfrom local_module import run\n", encoding="utf-8"
    )
    (root / "scripts" / "local_module.py").write_text("def run(): return True\n", encoding="utf-8")
    (root / "requirements.txt").write_text("requests==2.32.5\n", encoding="utf-8")
    release_paths.extend(["requirements.txt", "scripts/local_module.py"])

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert result["status"] == "ready"


def test_python_evidence_preserves_complete_dotted_imports() -> None:
    imports, _, _ = auditor._python_evidence(
        "import xml.etree.ElementTree\n"
        "from utils.missing_module import run\n"
        "import importlib\n"
        "importlib.import_module('requests.sessions')\n"
    )

    assert imports == {
        "importlib",
        "requests.sessions",
        "utils.missing_module",
        "xml.etree.ElementTree",
    }


def test_audit_rejects_missing_dotted_local_module_when_parent_has_other_files(
    tmp_path: Path,
) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "scripts" / "utils").mkdir()
    (root / "scripts" / "utils" / "existing.py").write_text("VALUE = True\n", encoding="utf-8")
    (root / "scripts" / "demo.py").write_text(
        "from utils.missing_module import run\n", encoding="utf-8"
    )
    release_paths.append("scripts/utils/existing.py")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_dependency_import" in _finding_codes(result)


@pytest.mark.parametrize(
    ("source_path", "local_path"),
    [
        ("scripts/demo.py", "localpkg/helpers.py"),
        ("examples/demo.py", "examples/localpkg/helpers.py"),
        ("examples/demo.py", "scripts/localpkg/helpers.py"),
        ("examples/demo.py", "scripts/vendor/localpkg/helpers.py"),
        ("scripts/demo.py", "scripts/localpkg/helpers/worker.py"),
    ],
)
def test_dotted_local_module_supports_runtime_search_roots_and_namespaces(
    source_path: str, local_path: str
) -> None:
    assert auditor._is_local_module("localpkg.helpers", source_path, {local_path})


def test_audit_rejects_missing_relative_module_from_audio_package(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "audio_sound").mkdir()
    (root / "audio_sound" / "demo.py").write_text(
        "from .missing_module import run\n", encoding="utf-8"
    )
    release_paths.append("audio_sound/demo.py")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_dependency_import" in _finding_codes(result)


def test_audit_accepts_existing_parent_relative_module(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "audio_sound" / "nested").mkdir(parents=True)
    (root / "audio_sound" / "nested" / "demo.py").write_text(
        "from ..pkg import run\n", encoding="utf-8"
    )
    (root / "audio_sound" / "pkg.py").write_text("def run(): return True\n", encoding="utf-8")
    release_paths.extend(["audio_sound/nested/demo.py", "audio_sound/pkg.py"])

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert result["status"] == "ready", result["findings"]


def test_audit_rejects_missing_relative_alias_outside_its_exact_package(
    tmp_path: Path,
) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "audio_sound").mkdir()
    (root / "audio_sound" / "demo.py").write_text(
        "from . import missing_module\n", encoding="utf-8"
    )
    (root / "scripts" / "audio_sound").mkdir()
    (root / "scripts" / "audio_sound" / "missing_module.py").write_text(
        "VALUE = True\n", encoding="utf-8"
    )
    release_paths.extend(["audio_sound/demo.py", "scripts/audio_sound/missing_module.py"])

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_dependency_import" in _finding_codes(result)


def test_audit_accepts_existing_relative_alias_modules(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "audio_sound").mkdir()
    (root / "audio_sound" / "demo.py").write_text(
        "from . import assets, exceptions, util\n", encoding="utf-8"
    )
    modules = [f"audio_sound/{name}.py" for name in ("assets", "exceptions", "util")]
    for module in modules:
        (root / module).write_text("VALUE = True\n", encoding="utf-8")
    release_paths.extend(["audio_sound/demo.py", *modules])

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert result["status"] == "ready", result["findings"]


def test_audio_only_dependency_does_not_cover_main_runtime_source(tmp_path: Path) -> None:
    capability = _capability()
    capability["dependency_imports"] = [
        {
            "module": "requests",
            "distribution": "requests",
            "environment": "audio",
            "disposition": "direct",
        }
    ]
    root, release_paths = _write_minimal_contract_repo(tmp_path, capabilities=[capability])
    (root / "scripts" / "demo.py").write_text("import requests\n", encoding="utf-8")
    (root / "requirements-audio.lock").write_text("requests==2.32.5\n", encoding="utf-8")
    release_paths.append("requirements-audio.lock")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_dependency_import" in _finding_codes(result)


@pytest.mark.parametrize("audio_source", ["audio_sound/demo.py", "scripts/audio/demo.py"])
def test_main_only_dependency_does_not_cover_audio_runtime_source(
    tmp_path: Path, audio_source: str
) -> None:
    capability = _capability(required_paths=["scripts/demo.py", audio_source])
    capability["dependency_imports"] = [
        {
            "module": "requests",
            "distribution": "requests",
            "environment": "main",
            "disposition": "direct",
        }
    ]
    root, release_paths = _write_minimal_contract_repo(tmp_path, capabilities=[capability])
    source = root / audio_source
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("import requests\n", encoding="utf-8")
    (root / "requirements.txt").write_text("requests==2.32.5\n", encoding="utf-8")
    release_paths.extend(["requirements.txt", audio_source])

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_dependency_import" in _finding_codes(result)


def test_audit_accepts_dotted_stdlib_and_declared_external_descendants(
    tmp_path: Path,
) -> None:
    capability = _capability()
    capability["dependency_imports"] = [
        {
            "module": "requests",
            "distribution": "requests",
            "environment": "main",
            "disposition": "direct",
        }
    ]
    root, release_paths = _write_minimal_contract_repo(tmp_path, capabilities=[capability])
    (root / "scripts" / "demo.py").write_text(
        "import xml.etree.ElementTree\nimport requests.sessions\n", encoding="utf-8"
    )
    (root / "requirements.txt").write_text("requests==2.32.5\n", encoding="utf-8")
    release_paths.append("requirements.txt")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert result["status"] == "ready"


def test_audit_rejects_undeclared_import_from_literal_dependency_registry(
    tmp_path: Path,
) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "scripts" / "demo.py").write_text(
        "import importlib\n"
        "MAIN_DEPENDENCY_IMPORTS = {'missing-dist': 'missingpkg.client'}\n"
        "for module_name in MAIN_DEPENDENCY_IMPORTS.values():\n"
        "    importlib.import_module(module_name)\n",
        encoding="utf-8",
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_dependency_import" in _finding_codes(result)


def test_audit_rejects_undeclared_tool_from_one_step_command_binding(
    tmp_path: Path,
) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "scripts" / "demo.py").write_text(
        "import subprocess\n"
        "command = ['missing-tool', '--version']\n"
        "subprocess.run(command, check=False)\n",
        encoding="utf-8",
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_external_tool" in _finding_codes(result)


def test_audit_accepts_declared_tool_from_one_step_command_binding(
    tmp_path: Path,
) -> None:
    capability = _capability()
    capability["external_tools"] = [{"name": "declared-tool", "disposition": "optional_local"}]
    root, release_paths = _write_minimal_contract_repo(tmp_path, capabilities=[capability])
    (root / "scripts" / "demo.py").write_text(
        "import subprocess as process\n"
        "command = ('declared-tool', '--version')\n"
        "process.run(command, check=False)\n",
        encoding="utf-8",
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert result["status"] == "ready", result["findings"]


def test_audit_rejects_undeclared_tool_from_module_command_binding(
    tmp_path: Path,
) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "scripts" / "demo.py").write_text(
        "import subprocess\n"
        "COMMAND = ['cross-scope-tool', '--version']\n"
        "def probe():\n"
        "    subprocess.run(COMMAND, check=False)\n",
        encoding="utf-8",
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_external_tool" in _finding_codes(result)


def test_audit_uses_binding_before_call_despite_later_reassignment(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "scripts" / "demo.py").write_text(
        "import subprocess\n"
        "COMMAND = ['before-call-tool', '--version']\n"
        "subprocess.run(COMMAND, check=False)\n"
        "COMMAND = ['after-call-tool', '--version']\n",
        encoding="utf-8",
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_external_tool" in _finding_codes(result)


@pytest.mark.parametrize(
    "bindings",
    [
        ("COMMAND = ['first-tool']\n" "COMMAND = ['second-tool']\n"),
        ("if __name__ == '__main__':\n" "    COMMAND = ['branch-tool']\n"),
    ],
    ids=["multiple-prior-bindings", "conditional-binding"],
)
def test_audit_fails_closed_for_uncertain_command_binding(tmp_path: Path, bindings: str) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "scripts" / "demo.py").write_text(
        "import subprocess\n" + bindings + "subprocess.run(COMMAND, check=False)\n",
        encoding="utf-8",
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "unscannable_external_tool" in _finding_codes(result)


def test_audit_does_not_fail_closed_for_runtime_resolved_conditional_path(
    tmp_path: Path,
) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "scripts" / "demo.py").write_text(
        "import subprocess\n"
        "if should_probe():\n"
        "    executable = find_executable()\n"
        "    subprocess.Popen(executable)\n",
        encoding="utf-8",
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert result["status"] == "ready", result["findings"]


def test_audit_does_not_treat_unrelated_run_function_as_subprocess(
    tmp_path: Path,
) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "scripts" / "demo.py").write_text(
        "def run(value):\n" "    return value\n" "run(['not-an-executable'])\n",
        encoding="utf-8",
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert result["status"] == "ready", result["findings"]


def test_main_dependency_registry_must_equal_direct_contract_modules(
    tmp_path: Path,
) -> None:
    capability = _capability(
        "main_python_dependencies",
        required_paths=["scripts/demo.py", "scripts/full_setup.py"],
    )
    capability["dependency_imports"] = [
        {
            "module": module,
            "distribution": module,
            "environment": "main",
            "disposition": "direct",
        }
        for module in ("requests", "missingpkg")
    ]
    root, release_paths = _write_minimal_contract_repo(tmp_path, capabilities=[capability])
    (root / "scripts" / "full_setup.py").write_text(
        "MAIN_DEPENDENCY_IMPORTS = {'requests': 'requests'}\n",
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text(
        "requests==2.32.5\nmissingpkg==1.0.0\n", encoding="utf-8"
    )
    release_paths.extend(["requirements.txt", "scripts/full_setup.py"])

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "dependency_registry_contract_mismatch" in _finding_codes(result)


def test_main_dependency_registry_must_be_one_literal_string_mapping(
    tmp_path: Path,
) -> None:
    capability = _capability(
        "main_python_dependencies",
        required_paths=["scripts/demo.py", "scripts/full_setup.py"],
    )
    capability["dependency_imports"] = [
        {
            "module": "requests",
            "distribution": "requests",
            "environment": "main",
            "disposition": "direct",
        }
    ]
    root, release_paths = _write_minimal_contract_repo(tmp_path, capabilities=[capability])
    (root / "scripts" / "full_setup.py").write_text(
        "MAIN_DEPENDENCY_IMPORTS = dict(requests='requests')\n",
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text("requests==2.32.5\n", encoding="utf-8")
    release_paths.extend(["requirements.txt", "scripts/full_setup.py"])

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "dependency_registry_contract_mismatch" in _finding_codes(result)


def test_audit_rejects_external_service_without_manifest_capability(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "scripts" / "demo.py").write_text(
        'API_URL = "https://api.example.com/v1/analyze"\n', encoding="utf-8"
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_external_service" in _finding_codes(result)


def test_audit_accepts_declared_external_service_and_optional_tool(tmp_path: Path) -> None:
    capability = _capability()
    capability["external_service_hosts"] = [
        {
            "host": "api.example.com",
            "schemes": ["https"],
            "disposition": "static_runtime",
        }
    ]
    capability["external_tools"] = [{"name": "Carnac.exe", "disposition": "optional_local"}]
    root, release_paths = _write_minimal_contract_repo(tmp_path, capabilities=[capability])
    (root / "scripts" / "demo.py").write_text(
        'import shutil\nAPI_URL = "https://api.example.com/v1"\nshutil.which("Carnac.exe")\n',
        encoding="utf-8",
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert result["status"] == "ready"


def test_audit_rejects_undeclared_external_tool(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "scripts" / "demo.py").write_text(
        'import shutil\nshutil.which("Carnac.exe")\n', encoding="utf-8"
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "undeclared_external_tool" in _finding_codes(result)


def test_audit_rejects_missing_repository_reference_in_current_docs(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text(
        "Run `examples/missing_demo.py` for the supported workflow.\n", encoding="utf-8"
    )
    release_paths.append("docs/guide.md")

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "missing_repository_reference" in _finding_codes(result)


def test_audit_rejects_machine_bound_runtime_path_without_echoing_it(tmp_path: Path) -> None:
    root, release_paths = _write_minimal_contract_repo(tmp_path)
    private_path = "D:/codex/private-project"
    (root / "scripts" / "demo.py").write_text(
        f'PROJECT_ROOT = "{private_path}"\n', encoding="utf-8"
    )

    result = auditor.audit_runtime_capabilities(root, release_paths)

    assert "machine_bound_runtime_path" in _finding_codes(result)
    assert private_path not in json.dumps(result)


def test_repository_release_tree_has_zero_runtime_capability_findings() -> None:
    release_paths = auditor.discover_release_paths(REPO_ROOT)
    release_paths.extend(
        [
            "runtime-capability-contract.json",
            "schemas/runtime-capability-contract.schema.json",
            "scripts/release/audit_runtime_capabilities.py",
            "tests/test_runtime_capability_audit.py",
        ]
    )

    result = auditor.audit_runtime_capabilities(REPO_ROOT, release_paths)

    assert result["status"] == "ready", result["findings"]
    assert result["findings"] == []


def test_audit_cli_runs_without_git_and_emits_machine_readable_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _release_paths = _write_minimal_contract_repo(tmp_path)

    code = auditor.main(["--repo-root", str(root), "--json"])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["status"] == "ready"
    assert output["findings"] == []


def test_audit_cli_script_runs_outside_repo_without_pythonpath(tmp_path: Path) -> None:
    root, _release_paths = _write_minimal_contract_repo(tmp_path)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [
            sys.executable,
            str(AUDITOR_PATH),
            "--repo-root",
            str(root),
            "--json",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "ready"
