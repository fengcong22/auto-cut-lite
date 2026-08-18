from __future__ import annotations

import argparse
import ast
import hashlib
import json
import posixpath
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.release.release_policy import (
    collect_release_paths,
    is_forbidden_path,
    normalize_archive_path,
    scan_text,
)

CONTRACT_FILE = "runtime-capability-contract.json"
MANIFEST_FILE = "capability-manifest.json"
CONTRACT_SCHEMA = "schemas/runtime-capability-contract.schema.json"
# Bind the public schema to the dependency-free validator so neither can silently drift.
CONTRACT_SCHEMA_SHA256 = "998db077bbe560d080d3817a7a5e056af5941023ea85d1586d837157c0cc9273"
AUDITOR_FILE = "scripts/release/audit_runtime_capabilities.py"
AUDITOR_TEST_FILE = "tests/test_runtime_capability_audit.py"
RUNTIME_CONTROL_FILES = (
    MANIFEST_FILE,
    CONTRACT_FILE,
    CONTRACT_SCHEMA,
    AUDITOR_FILE,
    AUDITOR_TEST_FILE,
)
CAPABILITY_FIELDS = {
    "id",
    "entrypoints",
    "required_paths",
    "dependency_imports",
    "external_service_hosts",
    "dynamic_service_policy",
    "external_tools",
    "verification_command",
}
DEPENDENCY_FIELDS = {"module", "distribution", "environment", "disposition"}
HOST_FIELDS = {"host", "schemes", "disposition"}
TOOL_FIELDS = {"name", "disposition"}
DEPENDENCY_ENVIRONMENTS = {"main", "audio"}
DEPENDENCY_DISPOSITIONS = {"direct", "transitive_optional"}
HOST_DISPOSITIONS = {
    "static_runtime",
    "bundled_index",
    "dependency_managed",
    "user_configured",
}
DYNAMIC_SERVICE_POLICIES = {
    "none",
    "validated_public_http_https",
    "user_configured_https",
    "provider_managed",
}
TOOL_DISPOSITIONS = {
    "required_local",
    "optional_local",
    "installed_on_first_run",
    "platform_provided",
    "unavailable_verification_only",
}
_URL_PATTERN = re.compile(r"(?i)\b(?P<scheme>https?|wss?)://(?P<host>[^/\s:'\"<>]+)")
_REPOSITORY_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?P<path>(?:assets|audio_sound|data|examples|presets|references|rules|schemas|scripts|skills|tools|workflows)/"
    r"[A-Za-z0-9_.\-/]+\.[A-Za-z0-9]+)"
)
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((?P<target>[^)]+)\)")
_MACHINE_PATH_CODES = {
    "absolute_local_path",
    "absolute_project_binding",
    "build_repository_path",
    "user_profile_path",
}
_SEMVER_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SUBPROCESS_SINKS = {"Popen", "call", "check_call", "check_output", "run"}
_RELATIVE_IMPORT_PREFIX = "\0relative:"
_UNSCANNABLE_EXTERNAL_TOOL = "\0unscannable-external-tool"


def _finding(
    code: str,
    *,
    summary: str,
    capability_id: str = "",
    path: str = "",
    evidence: str = "",
) -> dict[str, object]:
    finding: dict[str, object] = {"code": code, "summary": summary}
    if capability_id:
        finding["capability_id"] = capability_id
    if path:
        finding["path"] = path
    if evidence:
        finding["evidence_sha256"] = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    return finding


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return payload


def _canonical_json_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_contract_schema(path: Path) -> dict[str, Any]:
    schema = _load_json_object(path)
    if _canonical_json_sha256(schema) != CONTRACT_SCHEMA_SHA256:
        raise ValueError("runtime contract schema does not match the bundled validator")
    return schema


def _json_values_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _resolve_schema_reference(schema: dict[str, Any], reference: object) -> dict[str, Any]:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise ValueError("runtime contract schema contains an unsupported reference")
    current: object = schema
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise ValueError("runtime contract schema reference cannot be resolved")
        current = current[part]
    if not isinstance(current, dict):
        raise ValueError("runtime contract schema reference is not an object")
    return current


def _validate_schema_instance(
    value: object,
    rule: dict[str, Any],
    schema: dict[str, Any],
    *,
    location: str = "$",
) -> None:
    reference = rule.get("$ref")
    if reference is not None:
        _validate_schema_instance(
            value,
            _resolve_schema_reference(schema, reference),
            schema,
            location=location,
        )
        return

    if "const" in rule and not _json_values_equal(value, rule["const"]):
        raise ValueError(f"{location} does not match its schema constant")
    if "enum" in rule and not any(_json_values_equal(value, item) for item in rule["enum"]):
        raise ValueError(f"{location} is not in its schema enum")

    expected_type = rule.get("type")
    type_matches = {
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
        "string": isinstance(value, str),
    }
    if expected_type is not None and not type_matches.get(str(expected_type), False):
        raise ValueError(f"{location} has the wrong schema type")

    if isinstance(value, str):
        minimum = rule.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{location} is shorter than its schema minimum")
        pattern = rule.get("pattern")
        if pattern is not None and (
            not isinstance(pattern, str) or re.search(pattern, value) is None
        ):
            raise ValueError(f"{location} does not match its schema pattern")

    if isinstance(value, list):
        minimum = rule.get("minItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{location} has fewer items than its schema minimum")
        if rule.get("uniqueItems") is True:
            canonical_items = [
                json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                for item in value
            ]
            if len(canonical_items) != len(set(canonical_items)):
                raise ValueError(f"{location} contains duplicate schema items")
        item_rule = rule.get("items")
        if item_rule is not None:
            if not isinstance(item_rule, dict):
                raise ValueError("runtime contract schema items rule is invalid")
            for index, item in enumerate(value):
                _validate_schema_instance(
                    item,
                    item_rule,
                    schema,
                    location=f"{location}[{index}]",
                )

    if isinstance(value, dict):
        required = rule.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise ValueError("runtime contract schema required rule is invalid")
        missing = [item for item in required if item not in value]
        if missing:
            raise ValueError(f"{location} is missing schema field {missing[0]}")
        properties = rule.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("runtime contract schema properties rule is invalid")
        if rule.get("additionalProperties") is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                raise ValueError(f"{location} has unexpected schema field {unexpected[0]}")
        for key, child_rule in properties.items():
            if key not in value:
                continue
            if not isinstance(child_rule, dict):
                raise ValueError("runtime contract schema property rule is invalid")
            _validate_schema_instance(
                value[key],
                child_rule,
                schema,
                location=f"{location}.{key}",
            )


def _validate_release_version(value: object, source: str) -> str:
    if not isinstance(value, str) or _SEMVER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{source} release_version is not semantic version x.y.z")
    return value


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]*", value) is not None


def _validate_string_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field_name} must be a string array")
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} contains duplicates")
    return value


def _validate_contract(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if set(payload) != {"$schema", "schema_version", "release_version", "capabilities"}:
        raise ValueError("runtime contract has unexpected or missing top-level fields")
    if (
        payload["$schema"] != CONTRACT_SCHEMA
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
    ):
        raise ValueError("runtime contract schema identity is invalid")
    _validate_release_version(payload["release_version"], "runtime contract")
    capabilities = payload["capabilities"]
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError("runtime contract capabilities must be a non-empty array")

    ids: list[str] = []
    for capability in capabilities:
        if not isinstance(capability, dict) or set(capability) != CAPABILITY_FIELDS:
            raise ValueError("runtime capability has unexpected or missing fields")
        capability_id = capability["id"]
        if not _valid_identifier(capability_id):
            raise ValueError("runtime capability id is invalid")
        ids.append(capability_id)
        for field_name in ("entrypoints", "required_paths"):
            for raw_path in _validate_string_list(capability[field_name], field_name):
                normalize_archive_path(raw_path)
        if not capability["entrypoints"]:
            raise ValueError("runtime capability entrypoints must not be empty")
        if (
            not isinstance(capability["verification_command"], str)
            or not capability["verification_command"].strip()
        ):
            raise ValueError("runtime capability verification_command is invalid")
        if capability["dynamic_service_policy"] not in DYNAMIC_SERVICE_POLICIES:
            raise ValueError("runtime capability dynamic_service_policy is invalid")

        dependencies = capability["dependency_imports"]
        if not isinstance(dependencies, list):
            raise ValueError("dependency_imports must be an array")
        for dependency in dependencies:
            if not isinstance(dependency, dict) or set(dependency) != DEPENDENCY_FIELDS:
                raise ValueError("dependency import declaration is invalid")
            if not all(
                isinstance(dependency[field], str) and dependency[field] for field in dependency
            ):
                raise ValueError("dependency import declaration contains an empty value")
            if dependency["environment"] not in DEPENDENCY_ENVIRONMENTS:
                raise ValueError("dependency import environment is invalid")
            if dependency["disposition"] not in DEPENDENCY_DISPOSITIONS:
                raise ValueError("dependency import disposition is invalid")

        hosts = capability["external_service_hosts"]
        if not isinstance(hosts, list):
            raise ValueError("external_service_hosts must be an array")
        for host in hosts:
            if not isinstance(host, dict) or set(host) != HOST_FIELDS:
                raise ValueError("external service host declaration is invalid")
            if not isinstance(host["host"], str) or not host["host"]:
                raise ValueError("external service host is invalid")
            schemes = _validate_string_list(host["schemes"], "schemes")
            if not schemes or any(
                scheme not in {"http", "https", "ws", "wss"} for scheme in schemes
            ):
                raise ValueError("external service schemes are invalid")
            if host["disposition"] not in HOST_DISPOSITIONS:
                raise ValueError("external service disposition is invalid")

        tools = capability["external_tools"]
        if not isinstance(tools, list):
            raise ValueError("external_tools must be an array")
        for tool in tools:
            if not isinstance(tool, dict) or set(tool) != TOOL_FIELDS:
                raise ValueError("external tool declaration is invalid")
            if not isinstance(tool["name"], str) or not tool["name"]:
                raise ValueError("external tool name is invalid")
            if tool["disposition"] not in TOOL_DISPOSITIONS:
                raise ValueError("external tool disposition is invalid")
    if len(ids) != len(set(ids)):
        raise ValueError("runtime contract contains duplicate capability ids")
    return capabilities


def _safe_relative_file(root: Path, relative_path: str) -> Path:
    parts = PurePosixPath(relative_path).parts
    return root.joinpath(*parts)


def _runtime_python_path(path: str) -> bool:
    pure = PurePosixPath(path)
    if pure.suffix.casefold() != ".py" or not pure.parts:
        return False
    if pure.parts[0] in {"audio_sound", "examples", "references"}:
        return True
    if pure.parts[0] == "scripts":
        return len(pure.parts) < 2 or pure.parts[1] != "release"
    if pure.parts[:2] == ("tools", "recording"):
        return True
    return pure.parts[0] == "skills" and "scripts" in pure.parts[2:]


def _runtime_environment(path: str) -> str:
    parts = PurePosixPath(path).parts
    if parts[:1] == ("audio_sound",) or parts[:2] == ("scripts", "audio"):
        return "audio"
    return "main"


def _current_reference_path(path: str) -> bool:
    pure = PurePosixPath(path)
    if not pure.parts:
        return False
    if len(pure.parts) == 1:
        return path in {"AGENTS.md", "README.md", "SKILL.md"}
    if path == "references/README.md":
        return False
    if pure.parts[0] in {"examples", "references", "rules", "skills"}:
        return pure.suffix.casefold() in {".md", ".py", ".json", ".yaml", ".yml"}
    if pure.parts[0] != "docs":
        return False
    lowered_parts = {part.casefold() for part in pure.parts}
    lowered_name = pure.name.casefold()
    return not (
        "history" in lowered_parts
        or "upstream-control" in lowered_parts
        or lowered_name in {"readme.upstream.md", "provenance.md"}
        or "source-migration" in lowered_name
        or "source-receipt" in lowered_name
    )


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _literal_string_mapping(node: ast.AST) -> dict[str, str] | None:
    if not isinstance(node, ast.Dict):
        return None
    result: dict[str, str] = {}
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if key_node is None:
            return None
        key = _static_string(key_node)
        value = _static_string(value_node)
        if key is None or value is None or key in result:
            return None
        result[key] = value
    return result


def _top_level_dependency_registries(
    tree: ast.Module,
) -> dict[str, list[dict[str, str] | None]]:
    registries: dict[str, list[dict[str, str] | None]] = {}
    for statement in tree.body:
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        elif isinstance(statement, ast.AugAssign):
            targets = [statement.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id.endswith("DEPENDENCY_IMPORTS"):
                mapping = _literal_string_mapping(value) if value is not None else None
                registries.setdefault(target.id, []).append(mapping)
    return registries


@dataclass(frozen=True)
class _NameBinding:
    position: tuple[int, int]
    kind: str
    value: ast.AST | str | None = None
    uncertain: bool = False


@dataclass
class _LexicalScope:
    parent: _LexicalScope | None
    bindings: dict[str, list[_NameBinding]] = field(default_factory=dict)
    calls: list[ast.Call] = field(default_factory=list)

    def bind(
        self,
        name: str,
        node: ast.AST,
        kind: str,
        value: ast.AST | str | None = None,
        *,
        uncertain: bool = False,
    ) -> None:
        position = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        self.bindings.setdefault(name, []).append(_NameBinding(position, kind, value, uncertain))


def _bound_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.List, ast.Tuple)):
        return [name for item in target.elts for name in _bound_names(item)]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    return []


def _bind_function_arguments(
    scope: _LexicalScope, node: ast.FunctionDef | ast.AsyncFunctionDef
) -> None:
    arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    if node.args.vararg is not None:
        arguments.append(node.args.vararg)
    if node.args.kwarg is not None:
        arguments.append(node.args.kwarg)
    for argument in arguments:
        scope.bind(argument.arg, argument, "other")


class _ScopeCollector(ast.NodeVisitor):
    def __init__(
        self,
        scope: _LexicalScope,
        scopes: list[_LexicalScope],
        *,
        uncertain: bool = False,
    ) -> None:
        self.scope = scope
        self.scopes = scopes
        self.uncertain = uncertain

    def _bind(
        self, name: str, node: ast.AST, kind: str, value: ast.AST | str | None = None
    ) -> None:
        self.scope.bind(name, node, kind, value, uncertain=self.uncertain)

    def _bind_other(self, target: ast.AST, node: ast.AST) -> None:
        for name in _bound_names(target):
            self._bind(name, node, "other")

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._bind(target.id, node, "assignment", node.value)
            else:
                self._bind_other(target, node)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._bind(node.target.id, node, "assignment", node.value)
        else:
            self._bind_other(node.target, node)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._bind_other(node.target, node)
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._bind_other(node.target, node)
        self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        self._bind_other(node.target, node)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._bind_other(item.optional_vars, node)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind(node.name, node, "other")
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._bind_other(target, node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".", 1)[0]
            kind = "subprocess_module" if alias.name == "subprocess" else "other"
            self._bind(name, node, kind)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            name = alias.asname or alias.name
            if node.level == 0 and node.module == "subprocess" and alias.name in _SUBPROCESS_SINKS:
                self._bind(name, node, "subprocess_callable", alias.name)
            else:
                self._bind(name, node, "other")

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        branch_collector = _ScopeCollector(self.scope, self.scopes, uncertain=True)
        for statement in [*node.body, *node.orelse]:
            branch_collector.visit(statement)

    def _visit_definition_expressions(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for expression in [
            *node.decorator_list,
            *node.args.defaults,
            *(default for default in node.args.kw_defaults if default is not None),
        ]:
            self.visit(expression)
        if node.returns is not None:
            self.visit(node.returns)

    def _collect_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        child_scope = _LexicalScope(parent=self.scope)
        self.scopes.append(child_scope)
        _bind_function_arguments(child_scope, node)
        child_collector = _ScopeCollector(child_scope, self.scopes)
        for statement in node.body:
            child_collector.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._bind(node.name, node, "other")
        self._visit_definition_expressions(node)
        self._collect_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._bind(node.name, node, "other")
        self._visit_definition_expressions(node)
        self._collect_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._bind(node.name, node, "other")
        for expression in [*node.decorator_list, *node.bases]:
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._collect_function(statement)
            elif isinstance(statement, ast.ClassDef):
                self.visit_ClassDef(statement)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Call(self, node: ast.Call) -> None:
        self.scope.calls.append(node)
        self.generic_visit(node)


def _lexical_scopes(tree: ast.Module) -> list[_LexicalScope]:
    module_scope = _LexicalScope(parent=None)
    scopes = [module_scope]
    collector = _ScopeCollector(module_scope, scopes)
    for statement in tree.body:
        collector.visit(statement)
    return scopes


def _bindings_at_call(scope: _LexicalScope, name: str, call: ast.Call) -> list[_NameBinding]:
    position = (getattr(call, "lineno", 0), getattr(call, "col_offset", 0))
    current: _LexicalScope | None = scope
    while current is not None:
        prior = [
            binding for binding in current.bindings.get(name, []) if binding.position < position
        ]
        if prior:
            return prior
        current = current.parent
    return []


def _binding_at_call(scope: _LexicalScope, name: str, call: ast.Call) -> _NameBinding | None:
    bindings = _bindings_at_call(scope, name, call)
    return bindings[0] if len(bindings) == 1 and not bindings[0].uncertain else None


def _subprocess_sink(scope: _LexicalScope, call: ast.Call) -> str | None:
    function = call.func
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        if function.attr not in _SUBPROCESS_SINKS:
            return None
        binding = _binding_at_call(scope, function.value.id, call)
        if binding is not None and binding.kind == "subprocess_module":
            return function.attr
        return None
    if isinstance(function, ast.Name):
        binding = _binding_at_call(scope, function.id, call)
        if binding is not None and binding.kind == "subprocess_callable":
            return str(binding.value)
    return None


def _command_sequence(
    scope: _LexicalScope, call: ast.Call
) -> tuple[ast.List | ast.Tuple | None, bool]:
    if not call.args:
        return None, False
    command = call.args[0]
    if isinstance(command, (ast.List, ast.Tuple)):
        return command, False
    if not isinstance(command, ast.Name):
        return None, False
    bindings = _bindings_at_call(scope, command.id, call)
    static_bindings = [
        binding
        for binding in bindings
        if binding.kind == "assignment" and isinstance(binding.value, (ast.List, ast.Tuple))
    ]
    if not static_bindings:
        return None, False
    ambiguous = len(bindings) > 1 or any(binding.uncertain for binding in bindings)
    if len(bindings) != 1 or ambiguous:
        return None, ambiguous
    binding = bindings[0]
    if binding.kind == "assignment" and isinstance(binding.value, (ast.List, ast.Tuple)):
        return binding.value, False
    return None, False


def _subprocess_evidence(tree: ast.Module) -> tuple[set[str], set[str]]:
    imports: set[str] = set()
    tools: set[str] = set()
    for scope in _lexical_scopes(tree):
        for call in scope.calls:
            if _subprocess_sink(scope, call) is None:
                continue
            command, ambiguous = _command_sequence(scope, call)
            if ambiguous:
                tools.add(_UNSCANNABLE_EXTERNAL_TOOL)
                continue
            if command is None or not command.elts:
                continue
            tool = _static_string(command.elts[0])
            if tool:
                tools.add(tool)
            values = [_static_string(item) for item in command.elts]
            for index, value in enumerate(values[:-1]):
                if value == "-m" and values[index + 1]:
                    imports.add(str(values[index + 1]))
    return imports, tools


def _resolve_relative_modules(source_path: str, node: ast.ImportFrom) -> set[str]:
    package_parts = list(PurePosixPath(source_path).parent.parts)
    if not package_parts or node.level > len(package_parts):
        return set()
    retained = len(package_parts) - (node.level - 1)
    base_parts = package_parts[:retained]
    if node.module:
        return {".".join([*base_parts, *node.module.split(".")])}
    aliases = {alias.name for alias in node.names if alias.name != "*"}
    return {".".join([*base_parts, alias]) for alias in aliases}


def _python_evidence(
    text: str, *, source_path: str = ""
) -> tuple[set[str], set[tuple[str, str]], set[str]]:
    tree = ast.parse(text)
    imports: set[str] = set()
    hosts: set[tuple[str, str]] = set()
    tools: set[str] = set()
    for assignments in _top_level_dependency_registries(tree).values():
        for mapping in assignments:
            if mapping is not None:
                imports.update(mapping.values())
    subprocess_imports, subprocess_tools = _subprocess_evidence(tree)
    imports.update(subprocess_imports)
    tools.update(subprocess_tools)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imports.add(node.module)
            elif node.level and source_path:
                imports.update(
                    f"{_RELATIVE_IMPORT_PREFIX}{module}"
                    for module in _resolve_relative_modules(source_path, node)
                )
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if (
                name.rsplit(".", 1)[-1] in {"import_module", "find_spec", "__import__"}
                and node.args
            ):
                module = _static_string(node.args[0])
                if module:
                    imports.add(module)
            if name.endswith("shutil.which") and node.args:
                tool = _static_string(node.args[0])
                if tool:
                    tools.add(tool)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _URL_PATTERN.finditer(node.value):
                hosts.add((match.group("scheme").casefold(), match.group("host").casefold()))
    return imports, hosts, tools


def _local_module_closure(release_paths: set[str]) -> set[str]:
    modules: set[str] = set()
    for path in release_paths:
        pure = PurePosixPath(path)
        if pure.suffix != ".py":
            continue
        parts = list(pure.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        for length in range(1, len(parts) + 1):
            modules.add(".".join(parts[:length]))
    return modules


def _is_local_module(
    module: str,
    source_path: str,
    release_paths: set[str],
    *,
    local_modules: set[str] | None = None,
) -> bool:
    module_path = PurePosixPath(*module.split("."))
    source_parent = PurePosixPath(source_path).parent
    closure = local_modules if local_modules is not None else _local_module_closure(release_paths)
    search_roots = {
        PurePosixPath(),
        source_parent,
        PurePosixPath("scripts"),
        PurePosixPath("scripts/vendor"),
    }
    for search_root in search_roots:
        candidate = (search_root / module_path).as_posix().replace("/", ".")
        if candidate in closure:
            return True
    return False


def _normalized_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _requirement_distributions(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    distributions: set[str] = set()
    for line in text.splitlines():
        candidate = line.split("#", 1)[0].strip()
        match = re.match(r"([A-Za-z0-9_.-]+)", candidate)
        if match:
            distributions.add(_normalized_distribution(match.group(1)))
    return distributions


def _declared_runtime_evidence(
    capabilities: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, dict[str, Any]]], set[tuple[str, str]], set[str]]:
    dependencies: dict[str, dict[str, dict[str, Any]]] = {
        environment: {} for environment in DEPENDENCY_ENVIRONMENTS
    }
    hosts: set[tuple[str, str]] = set()
    tools: set[str] = set()
    for capability in capabilities:
        for dependency in capability["dependency_imports"]:
            environment = str(dependency["environment"])
            dependencies[environment][str(dependency["module"])] = dependency
        for host in capability["external_service_hosts"]:
            hosts.update(
                (str(scheme).casefold(), str(host["host"]).casefold()) for scheme in host["schemes"]
            )
        for tool in capability["external_tools"]:
            tools.add(PurePosixPath(str(tool["name"])).stem.casefold())
    return dependencies, hosts, tools


def _dependency_is_declared(
    module: str,
    environment: str,
    dependencies: dict[str, dict[str, dict[str, Any]]],
) -> bool:
    return any(
        module == declared_module or module.startswith(f"{declared_module}.")
        for declared_module in dependencies.get(environment, {})
    )


def _scan_declared_dependencies(
    root: Path,
    capabilities: list[dict[str, Any]],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    main_requirements = _requirement_distributions(root / "requirements.txt")
    audio_requirements = _requirement_distributions(root / "requirements-audio.lock")
    for capability in capabilities:
        capability_id = str(capability["id"])
        for dependency in capability["dependency_imports"]:
            expected = (
                main_requirements if dependency["environment"] == "main" else audio_requirements
            )
            if _normalized_distribution(str(dependency["distribution"])) not in expected:
                findings.append(
                    _finding(
                        "dependency_requirement_missing",
                        capability_id=capability_id,
                        summary="declared dependency is absent from its runtime requirements",
                        evidence=str(dependency["distribution"]),
                    )
                )
    return findings


def _scan_main_dependency_registry(
    root: Path,
    capabilities: list[dict[str, Any]],
) -> list[dict[str, object]]:
    capability = next(
        (row for row in capabilities if row.get("id") == "main_python_dependencies"),
        None,
    )
    if capability is None:
        return []
    expected_modules = {
        str(dependency["module"])
        for dependency in capability["dependency_imports"]
        if dependency["environment"] == "main" and dependency["disposition"] == "direct"
    }
    assignments: list[dict[str, str] | None] = []
    try:
        text = (root / "scripts" / "full_setup.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(text)
        assignments = _top_level_dependency_registries(tree).get("MAIN_DEPENDENCY_IMPORTS", [])
    except (OSError, UnicodeError, SyntaxError, ValueError):
        pass
    actual_modules = (
        set(assignments[0].values())
        if len(assignments) == 1 and assignments[0] is not None
        else None
    )
    if actual_modules == expected_modules:
        return []
    evidence = {
        "missing": sorted(expected_modules - (actual_modules or set())),
        "extra": sorted((actual_modules or set()) - expected_modules),
        "literal_assignment_count": len(assignments),
    }
    return [
        _finding(
            "dependency_registry_contract_mismatch",
            capability_id="main_python_dependencies",
            path="scripts/full_setup.py",
            summary="MAIN_DEPENDENCY_IMPORTS differs from the main direct dependency contract",
            evidence=json.dumps(evidence, ensure_ascii=True, sort_keys=True),
        )
    ]


def _scan_runtime_sources(
    root: Path,
    release_paths: set[str],
    capabilities: list[dict[str, Any]],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    dependencies, declared_hosts, declared_tools = _declared_runtime_evidence(capabilities)
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    local_modules = _local_module_closure(release_paths)
    for path in sorted(release_paths):
        if not _runtime_python_path(path):
            continue
        source = _safe_relative_file(root, path)
        try:
            text = source.read_text(encoding="utf-8-sig")
            imports, hosts, tools = _python_evidence(text, source_path=path)
        except (OSError, UnicodeError, SyntaxError, ValueError):
            findings.append(
                _finding(
                    "unscannable_runtime_source",
                    path=path,
                    summary="runtime Python source could not be parsed",
                )
            )
            continue
        for module in sorted(imports):
            if module.startswith(_RELATIVE_IMPORT_PREFIX):
                relative_module = module.removeprefix(_RELATIVE_IMPORT_PREFIX)
                if relative_module in local_modules:
                    continue
                findings.append(
                    _finding(
                        "undeclared_dependency_import",
                        path=path,
                        summary="runtime source imports a missing relative module",
                        evidence=relative_module,
                    )
                )
                continue
            top_level_module = module.split(".", 1)[0]
            if top_level_module in stdlib or _is_local_module(
                module, path, release_paths, local_modules=local_modules
            ):
                continue
            if not _dependency_is_declared(module, _runtime_environment(path), dependencies):
                findings.append(
                    _finding(
                        "undeclared_dependency_import",
                        path=path,
                        summary="runtime source imports an undeclared dependency",
                        evidence=module,
                    )
                )
        for scheme, host in sorted(hosts):
            if host in {"127.0.0.1", "localhost"}:
                continue
            if (scheme, host) not in declared_hosts:
                findings.append(
                    _finding(
                        "undeclared_external_service",
                        path=path,
                        summary="runtime source uses an undeclared external service",
                        evidence=f"{scheme}://{host}",
                    )
                )
        for tool in sorted(tools):
            if tool == _UNSCANNABLE_EXTERNAL_TOOL:
                findings.append(
                    _finding(
                        "unscannable_external_tool",
                        path=path,
                        summary="runtime source invokes an external tool through an ambiguous command binding",
                    )
                )
                continue
            normalized = PurePosixPath(tool).stem.casefold()
            if normalized not in declared_tools:
                findings.append(
                    _finding(
                        "undeclared_external_tool",
                        path=path,
                        summary="runtime source probes or invokes an undeclared external tool",
                        evidence=tool,
                    )
                )
    return findings


def _scan_bundled_index_hosts(
    root: Path,
    release_paths: set[str],
    capabilities: list[dict[str, Any]],
) -> list[dict[str, object]]:
    _, declared_hosts, _ = _declared_runtime_evidence(capabilities)
    findings: list[dict[str, object]] = []
    for path in sorted(release_paths):
        if not (path.startswith("data/cloud_") and path.endswith(".csv")):
            continue
        try:
            text = _safe_relative_file(root, path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        for match in _URL_PATTERN.finditer(text):
            evidence = (match.group("scheme").casefold(), match.group("host").casefold())
            if evidence not in declared_hosts:
                findings.append(
                    _finding(
                        "undeclared_external_service",
                        path=path,
                        summary="bundled material index uses an undeclared external service",
                        evidence=f"{evidence[0]}://{evidence[1]}",
                    )
                )
    return findings


def _scan_repository_references(root: Path, release_paths: set[str]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for source_path in sorted(release_paths):
        if not _current_reference_path(source_path):
            continue
        try:
            text = _safe_relative_file(root, source_path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        pure_source = PurePosixPath(source_path)
        references: list[tuple[str, bool]] = []
        for match in _MARKDOWN_LINK.finditer(text):
            target = match.group("target").split("#", 1)[0].strip()
            if target and "://" not in target and not target.startswith(("#", "<")):
                references.append((target.replace("\\", "/"), True))
        text_without_links = _MARKDOWN_LINK.sub("", text)
        references.extend(
            (match.group("path"), False)
            for match in _REPOSITORY_REFERENCE.finditer(text_without_links)
        )
        for reference, source_relative in references:
            if source_relative:
                resolved = posixpath.normpath(
                    (pure_source.parent / PurePosixPath(reference)).as_posix()
                )
                candidates = [resolved]
            else:
                candidates = [reference]
                if pure_source.parts[0] == "skills" and reference.startswith(
                    ("assets/", "references/", "scripts/")
                ):
                    candidates.insert(
                        0,
                        (PurePosixPath(*pure_source.parts[:2]) / reference).as_posix(),
                    )
            if any(is_forbidden_path(candidate) for candidate in candidates):
                continue
            if any("aaaaaaaaaaaa" in candidate.casefold() for candidate in candidates):
                continue
            if not any(candidate in release_paths for candidate in candidates):
                findings.append(
                    _finding(
                        "missing_repository_reference",
                        path=source_path,
                        summary="current documentation references a missing repository file",
                        evidence=reference,
                    )
                )
    return findings


def _scan_machine_paths(root: Path, release_paths: set[str]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for path in sorted(release_paths):
        if not (_runtime_python_path(path) or _current_reference_path(path)):
            continue
        try:
            text = _safe_relative_file(root, path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        for privacy_finding in scan_text(path, text):
            if privacy_finding.code in _MACHINE_PATH_CODES:
                findings.append(
                    _finding(
                        "machine_bound_runtime_path",
                        path=path,
                        summary="runtime or current documentation contains a machine-bound path",
                        evidence=privacy_finding.summary,
                    )
                )
    return findings


def _deduplicate_findings(findings: list[dict[str, object]]) -> list[dict[str, object]]:
    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    for finding in findings:
        identity = json.dumps(finding, ensure_ascii=False, sort_keys=True)
        if identity not in seen:
            seen.add(identity)
            unique.append(finding)
    return unique


def audit_runtime_capabilities(repo_root: Path, release_paths: Iterable[str]) -> dict[str, object]:
    root = repo_root.resolve()
    findings: list[dict[str, object]] = []
    checked_paths: list[str] = []
    for raw_path in release_paths:
        try:
            checked_paths.append(normalize_archive_path(str(raw_path)))
        except ValueError:
            findings.append(
                _finding(
                    "contract_invalid",
                    summary="release inventory contains an unsafe path",
                    evidence=str(raw_path),
                )
            )
    checked_paths = sorted(set(checked_paths))
    released = set(checked_paths)
    for path in RUNTIME_CONTROL_FILES:
        if not _safe_relative_file(root, path).is_file():
            findings.append(
                _finding(
                    "missing_runtime_control_file",
                    path=path,
                    summary="runtime capability control file is missing",
                )
            )
        if path not in released:
            findings.append(
                _finding(
                    "runtime_control_file_not_released",
                    path=path,
                    summary="runtime capability control file is absent from the release inventory",
                )
            )
    declared_ids: list[str] = []
    manifest_ids: list[str] = []
    capabilities: list[dict[str, Any]] = []
    try:
        contract_schema = _load_contract_schema(root / CONTRACT_SCHEMA)
        contract = _load_json_object(root / CONTRACT_FILE)
        manifest = _load_json_object(root / MANIFEST_FILE)
        _validate_schema_instance(contract, contract_schema, contract_schema)
        capabilities = _validate_contract(contract)
        _validate_release_version(manifest.get("release_version"), "capability manifest")
        manifest_capabilities = manifest.get("capabilities")
        if not isinstance(manifest_capabilities, list):
            raise ValueError("capability manifest capabilities must be an array")
        manifest_by_id: dict[str, dict[str, Any]] = {}
        for capability in manifest_capabilities:
            if not isinstance(capability, dict) or not _valid_identifier(capability.get("id")):
                raise ValueError("capability manifest contains an invalid capability")
            manifest_by_id[str(capability["id"])] = capability
        if len(manifest_by_id) != len(manifest_capabilities):
            raise ValueError("capability manifest contains duplicate ids")
        declared_ids = sorted(str(capability["id"]) for capability in capabilities)
        manifest_ids = sorted(manifest_by_id)
        if contract["release_version"] != manifest.get("release_version"):
            findings.append(
                _finding(
                    "contract_invalid",
                    summary="runtime contract and manifest release versions differ",
                )
            )
        if declared_ids != manifest_ids:
            findings.append(
                _finding(
                    "capability_id_set_mismatch",
                    summary="runtime contract and manifest capability sets differ",
                )
            )

        for capability in capabilities:
            capability_id = str(capability["id"])
            manifest_capability = manifest_by_id.get(capability_id)
            if manifest_capability is not None and capability["verification_command"] != (
                manifest_capability.get("verification_command")
            ):
                findings.append(
                    _finding(
                        "verification_command_mismatch",
                        capability_id=capability_id,
                        summary="runtime contract verification command differs from the manifest",
                    )
                )
            required_paths = sorted(
                set([*capability["entrypoints"], *capability["required_paths"]])
            )
            for raw_path in required_paths:
                try:
                    path = normalize_archive_path(raw_path)
                except ValueError:
                    findings.append(
                        _finding(
                            "contract_invalid",
                            capability_id=capability_id,
                            summary="runtime capability contains an unsafe path",
                            evidence=str(raw_path),
                        )
                    )
                    continue
                if not _safe_relative_file(root, path).is_file():
                    findings.append(
                        _finding(
                            "missing_runtime_path",
                            capability_id=capability_id,
                            path=path,
                            summary="declared runtime path is missing",
                        )
                    )
                if path not in released:
                    findings.append(
                        _finding(
                            "runtime_path_not_released",
                            capability_id=capability_id,
                            path=path,
                            summary="declared runtime path is absent from the release inventory",
                        )
                    )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        findings.append(
            _finding(
                "contract_invalid",
                summary="runtime capability contract could not be validated",
                evidence=type(exc).__name__,
            )
        )
    if capabilities:
        released = set(checked_paths)
        findings.extend(_scan_declared_dependencies(root, capabilities))
        findings.extend(_scan_main_dependency_registry(root, capabilities))
        findings.extend(_scan_runtime_sources(root, released, capabilities))
        findings.extend(_scan_bundled_index_hosts(root, released, capabilities))
        findings.extend(_scan_repository_references(root, released))
        findings.extend(_scan_machine_paths(root, released))
    findings = _deduplicate_findings(findings)
    return {
        "schema_version": 1,
        "status": "ready" if not findings else "failed",
        "declared_capability_ids": declared_ids,
        "manifest_ids": manifest_ids,
        "checked_paths": checked_paths,
        "findings": findings,
    }


def discover_release_paths(repo_root: Path) -> list[str]:
    root = repo_root.resolve()
    inventory_path = root / "release-inventory.json"
    if inventory_path.is_file():
        inventory = _load_json_object(inventory_path)
        files = inventory.get("files")
        if not isinstance(files, list):
            raise ValueError("release inventory files must be an array")
        paths = [
            normalize_archive_path(str(row["path"]))
            for row in files
            if isinstance(row, dict) and isinstance(row.get("path"), str)
        ]
        if len(paths) != len(files):
            raise ValueError("release inventory contains an invalid file row")
        return sorted(set(paths))
    if (root / ".git").exists():
        try:
            completed = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            tracked = completed.stdout.decode("utf-8").split("\0")
            return [
                path
                for path in collect_release_paths(candidate for candidate in tracked if candidate)
                if _safe_relative_file(root, path).is_file()
            ]
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit Auto-Cut runtime capability coverage.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.repo_root)
        result = audit_runtime_capabilities(root, discover_release_paths(root))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        result = {
            "schema_version": 1,
            "status": "failed",
            "declared_capability_ids": [],
            "manifest_ids": [],
            "checked_paths": [],
            "findings": [
                _finding(
                    "contract_invalid",
                    summary="runtime capability audit could not start",
                    evidence=type(exc).__name__,
                )
            ],
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Runtime capability audit: {result['status']}")
        print(f"Findings: {len(result['findings'])}")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
