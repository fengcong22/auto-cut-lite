from __future__ import annotations

import re
from typing import Any, Mapping

from .errors import PortableProjectError
from .version_policy import SUPPORTED_APP_IDENTITIES

COMPATIBILITY_SCHEMA_VERSION = 1
_VERSION_PATTERN = re.compile(r"\d+(?:\.\d+)*")
_SOURCE_SELECTION_BASES = frozenset(
    {
        "detector_single_app",
        "draft_path_owner",
        "explicit_source_app",
        "supplied_scalar",
    }
)
_AMBIGUITY_RESOLUTION_BASES = frozenset({"draft_path_owner", "explicit_source_app"})


def _canonical_app_name(value: object) -> str:
    name = str(value or "").strip()
    if name.casefold() not in SUPPORTED_APP_IDENTITIES:
        raise PortableProjectError(
            "unknown_source_app",
            "Source project application must be JianyingPro or CapCut",
        )
    return "JianyingPro" if name.casefold() == "jianyingpro" else "CapCut"


def _version_key(value: object) -> tuple[int, ...]:
    text = str(value or "").strip()
    if not text or _VERSION_PATTERN.fullmatch(text) is None:
        return ()
    result = tuple(int(part) for part in text.split("."))
    while len(result) > 1 and result[-1] == 0:
        result = result[:-1]
    return result


def versions_equal(left: object, right: object) -> bool:
    left_key = _version_key(left)
    return bool(left_key) and left_key == _version_key(right)


def _platform_version(content: Mapping[str, Any], field: str) -> str:
    block = content.get(field)
    if block is None:
        return ""
    if not isinstance(block, Mapping):
        raise PortableProjectError(
            "source_version_mismatch", "Draft platform metadata is malformed"
        )
    version = str(block.get("app_version") or "").strip()
    if version and not _version_key(version):
        raise PortableProjectError("source_version_mismatch", "Draft platform version is malformed")
    return version


def _structured_app_identities(detected: Mapping[str, Any]) -> tuple[list[str], bool]:
    identities: set[str] = set()
    structured = False

    if "detected_app_identities" in detected:
        structured = True
        raw_identities = detected.get("detected_app_identities")
        if not isinstance(raw_identities, (list, tuple, set, frozenset)):
            raise PortableProjectError(
                "unknown_source_app", "Detected source application evidence is malformed"
            )
        for raw_identity in raw_identities:
            identities.add(_canonical_app_name(raw_identity))

    if "candidates" in detected:
        structured = True
        candidates = detected.get("candidates")
        if not isinstance(candidates, (list, tuple)):
            raise PortableProjectError(
                "unknown_source_app", "Detected source application candidates are malformed"
            )
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise PortableProjectError(
                    "unknown_source_app", "Detected source application candidates are malformed"
                )
            identities.add(_canonical_app_name(candidate.get("app_name")))

    return sorted(identities), structured


def _resolve_app_evidence(detected: Mapping[str, Any]) -> tuple[str, str, list[str], str]:
    selected_app = _canonical_app_name(detected.get("app_name"))
    detected_app = _canonical_app_name(detected.get("detected_app_name") or selected_app)
    if detected_app != selected_app:
        raise PortableProjectError(
            "unknown_source_app",
            "Detected source application does not match the selected source application",
        )

    identities, structured = _structured_app_identities(detected)
    if identities and selected_app not in identities:
        raise PortableProjectError(
            "unknown_source_app",
            "Selected source application is absent from detected application evidence",
        )
    if not identities:
        identities = [detected_app]

    reported_ambiguity = detected.get("app_identity_ambiguous", False)
    if not isinstance(reported_ambiguity, bool):
        raise PortableProjectError(
            "unknown_source_app", "Detected source application ambiguity evidence is malformed"
        )
    ambiguous = reported_ambiguity or len(identities) > 1

    selection_basis = str(detected.get("selection_basis") or "").strip()
    if selection_basis and selection_basis not in _SOURCE_SELECTION_BASES:
        raise PortableProjectError(
            "unknown_source_app", "Source application selection basis is invalid"
        )
    if not selection_basis:
        selection_basis = "detector_single_app" if structured else "supplied_scalar"
    if ambiguous and selection_basis not in _AMBIGUITY_RESOLUTION_BASES:
        raise PortableProjectError(
            "unknown_source_app",
            "Source application ambiguity has not been resolved by draft ownership or an explicit selection",
        )

    return selected_app, detected_app, identities, selection_basis


def collect_draft_platform_versions(
    root_content: Mapping[str, Any],
    timeline_contents: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, str]], str]:
    documents: list[tuple[str, Mapping[str, Any]]] = [("root", root_content)]
    documents.extend(
        (f"timeline:{timeline_id}", timeline_contents[timeline_id])
        for timeline_id in sorted(timeline_contents)
    )
    rows: list[dict[str, str]] = []
    effective_versions: list[str] = []
    for document, content in documents:
        platform = _platform_version(content, "platform")
        last_modified = _platform_version(content, "last_modified_platform")
        if platform and last_modified and not versions_equal(platform, last_modified):
            raise PortableProjectError(
                "source_version_mismatch",
                "Draft platform and last-modified versions do not agree",
                {"document": document},
            )
        rows.append(
            {
                "document": document,
                "platform": platform,
                "last_modified_platform": last_modified,
            }
        )
        effective_versions.append(last_modified or platform)

    populated = [value for value in effective_versions if value]
    if populated and len(populated) != len(effective_versions):
        raise PortableProjectError(
            "source_version_mismatch",
            "Draft platform version evidence is incomplete",
        )
    if populated:
        first = populated[0]
        if any(not versions_equal(first, value) for value in populated[1:]):
            raise PortableProjectError(
                "source_version_mismatch",
                "Root and timeline draft platform versions do not agree",
            )
        return rows, first
    return rows, ""


def source_provenance(
    root_content: Mapping[str, Any],
    timeline_contents: Mapping[str, Mapping[str, Any]],
    detected_environment: Mapping[str, Any] | None,
) -> dict[str, Any]:
    detected = dict(detected_environment or {})
    app_name, detected_app, detected_identities, selection_basis = _resolve_app_evidence(detected)
    selected_version = str(detected.get("app_version") or "").strip()
    detected_version = str(
        detected.get("detected_app_version")
        if "detected_app_version" in detected
        else selected_version
    ).strip()
    for version in (selected_version, detected_version):
        if version and not _version_key(version):
            raise PortableProjectError(
                "source_version_mismatch", "Detected source application version is malformed"
            )
    if (
        selected_version
        and detected_version
        and not versions_equal(selected_version, detected_version)
    ):
        raise PortableProjectError(
            "source_version_mismatch",
            "Selected and detected source application versions do not agree",
        )
    platform_rows, saved_version = collect_draft_platform_versions(root_content, timeline_contents)
    for corroborating_version in (selected_version, detected_version):
        if (
            saved_version
            and corroborating_version
            and not versions_equal(saved_version, corroborating_version)
        ):
            raise PortableProjectError(
                "source_version_mismatch",
                "Detected source version does not match the saved draft platform version",
            )
    source_version = saved_version or selected_version or detected_version
    return {
        "app_name": app_name,
        "app_version": source_version,
        "detected_source_app": detected_app,
        "detected_source_version": detected_version,
        "source_app_selection_basis": selection_basis,
        "detected_app_identities": detected_identities,
        "compatibility_schema_version": COMPATIBILITY_SCHEMA_VERSION,
        "draft_platform_versions": platform_rows,
    }


def validate_manifest_source_provenance(
    project: Mapping[str, Any],
    root_content: Mapping[str, Any],
    timeline_contents: Mapping[str, Mapping[str, Any]],
) -> None:
    expected = source_provenance(
        root_content,
        timeline_contents,
        {
            "app_name": project.get("source_app"),
            "app_version": project.get("detected_source_version"),
            "detected_app_name": project.get("detected_source_app"),
            "detected_app_version": project.get("detected_source_version"),
            "selection_basis": project.get("source_app_selection_basis"),
            "detected_app_identities": project.get("detected_app_identities"),
            "app_identity_ambiguous": len(project.get("detected_app_identities") or []) > 1,
        },
    )
    checks = {
        "source_app": expected["app_name"],
        "source_version": expected["app_version"],
        "detected_source_app": expected["detected_source_app"],
        "detected_source_version": expected["detected_source_version"],
        "source_app_selection_basis": expected["source_app_selection_basis"],
        "detected_app_identities": expected["detected_app_identities"],
        "compatibility_schema_version": expected["compatibility_schema_version"],
        "draft_platform_versions": expected["draft_platform_versions"],
    }
    if any(project.get(key) != value for key, value in checks.items()):
        raise PortableProjectError(
            "source_version_mismatch",
            "Manifest source provenance does not match the bundled draft",
        )
