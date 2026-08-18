from __future__ import annotations

import json
import ntpath
import os
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from portable.build_importer import validate_build_receipt

from .draft_json import content_material_name_fields, content_material_path_fields
from .draft_policy import is_sensitive_draft_artifact
from .errors import PortableProjectError
from .hashing import canonical_json_sha256, sha256_file
from .manifest import (
    DRAFT_DIR_MARKER,
    DRAFT_ROOT_MARKER,
    IMPORTER_FILENAME,
    MANIFEST_FILENAME,
    MEDIA_MARKER,
    REPORT_FILENAME,
    logical_media_path,
    read_manifest,
    validate_material_id,
    validate_relative_path,
)
from .projection import installation_projection
from .provenance import validate_manifest_source_provenance
from .version_policy import SUPPORTED_APP_IDENTITIES

STRUCTURE_POLICIES = frozenset({"revision", "standard"})
_MAIN_VIDEO_TRACK_NAMES = frozenset({"original video", "main video", "video track", "source video"})


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _valid_byte_size(value: Any) -> bool:
    return type(value) is int and value >= 0


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _safe_package_file(package_dir: Path, relative_path: object) -> Path:
    safe_path = validate_relative_path(relative_path)
    candidate = package_dir.joinpath(*PurePosixPath(safe_path).parts)
    current = package_dir
    for part in PurePosixPath(safe_path).parts:
        current = current / part
        if _is_reparse(current):
            raise PortableProjectError(
                "unsafe_package_path",
                "Portable project contains a reparse point",
                {"path": safe_path},
            )
    return candidate


def directory_tree_receipt(
    root_dir: Path,
    *,
    error_code: str,
) -> dict[str, Any]:
    root = Path(root_dir).absolute()
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise PortableProjectError(error_code, "Directory tree is missing") from exc
    if _is_reparse(root) or not stat.S_ISDIR(root_metadata.st_mode):
        raise PortableProjectError(error_code, "Directory tree root is unsafe")

    entries: list[dict[str, Any]] = []

    def walk(directory: Path, relative_dir: PurePosixPath) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise PortableProjectError(error_code, "Directory tree could not be inspected") from exc
        for entry in children:
            path = Path(entry.path)
            relative = relative_dir / entry.name
            try:
                before = path.lstat()
            except OSError as exc:
                raise PortableProjectError(error_code, "Directory tree entry disappeared") from exc
            if _is_reparse(path):
                raise PortableProjectError(error_code, "Directory tree contains a reparse point")
            if stat.S_ISDIR(before.st_mode):
                entries.append({"path": relative.as_posix(), "type": "directory"})
                walk(path, relative)
                continue
            if not stat.S_ISREG(before.st_mode):
                raise PortableProjectError(
                    error_code, "Directory tree contains a non-regular entry"
                )
            digest = sha256_file(path)
            try:
                after = path.lstat()
            except OSError as exc:
                raise PortableProjectError(error_code, "Directory tree entry disappeared") from exc
            before_identity = (
                before.st_size,
                before.st_mtime_ns,
                before.st_dev,
                before.st_ino,
            )
            after_identity = (
                after.st_size,
                after.st_mtime_ns,
                after.st_dev,
                after.st_ino,
            )
            if (
                _is_reparse(path)
                or not stat.S_ISREG(after.st_mode)
                or before_identity != after_identity
            ):
                raise PortableProjectError(error_code, "Directory tree changed during inspection")
            entries.append(
                {
                    "path": relative.as_posix(),
                    "type": "file",
                    "byte_size": before.st_size,
                    "sha256": digest,
                }
            )

    walk(root, PurePosixPath())
    files = [entry for entry in entries if entry["type"] == "file"]
    directories = [str(entry["path"]) for entry in entries if entry["type"] == "directory"]
    return {
        "schema_version": 1,
        "directories": directories,
        "directory_count": len(directories),
        "files": files,
        "file_count": len(files),
        "byte_size": sum(int(entry["byte_size"]) for entry in files),
        "sha256": canonical_json_sha256(entries),
    }


def _safe_installed_path(installed_dir: Path, relative_path: object) -> Path:
    safe_path = validate_relative_path(relative_path)
    current = installed_dir
    for part in PurePosixPath(safe_path).parts:
        current = current / part
        if _is_reparse(current):
            raise PortableProjectError(
                "installed_draft_validation_failed",
                "Installed draft contains a reparse point",
            )
    return current


def _load_json_object(path: Path, *, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableProjectError(code, "Required package JSON is not readable") from exc
    if not isinstance(payload, dict):
        raise PortableProjectError(code, "Required package JSON is not an object")
    return payload


def _is_absolute_path(value: str) -> bool:
    text = str(value).strip()
    return bool(text) and not text.startswith("@AUTOCUT_") and ntpath.isabs(text)


def _absolute_strings(payload: Any) -> Iterable[str]:
    if isinstance(payload, str):
        if _is_absolute_path(payload):
            yield payload
        return
    if isinstance(payload, dict):
        for value in payload.values():
            yield from _absolute_strings(value)
        return
    if isinstance(payload, list):
        for value in payload:
            yield from _absolute_strings(value)


def _validate_retained_json_paths(
    package: Path, file_rows: Mapping[str, Mapping[str, Any]]
) -> None:
    for relative in sorted(file_rows):
        if not relative.startswith("Draft/") or not relative.casefold().endswith(".json"):
            continue
        path = _safe_package_file(package, relative)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if list(_absolute_strings(payload)):
            raise PortableProjectError(
                "source_path_leaked",
                "A retained draft JSON file contains an absolute machine path",
                {"path": relative},
            )


def _material_id(row: Mapping[str, Any]) -> str:
    return str(row.get("id") or row.get("material_id") or row.get("music_id") or "").strip()


def _content_rows(
    content: Mapping[str, Any], *, location_prefix: str
) -> Iterable[tuple[str, str, Mapping[str, Any]]]:
    materials = content.get("materials")
    if not isinstance(materials, dict):
        return
    for bucket, rows in materials.items():
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if isinstance(row, dict) and content_material_path_fields(row):
                yield f"{location_prefix}:materials.{bucket}[{index}]", bucket, row


def _meta_rows(meta: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    buckets = meta.get("draft_materials")
    if not isinstance(buckets, list):
        return
    for bucket_index, bucket in enumerate(buckets):
        if not isinstance(bucket, dict) or not isinstance(bucket.get("value"), list):
            continue
        for index, row in enumerate(bucket["value"]):
            if isinstance(row, dict) and str(row.get("file_Path") or "").strip():
                yield f"meta:draft_materials[{bucket_index}].value[{index}]", row


def _track_is_visible(track: Mapping[str, Any]) -> bool:
    attribute = track.get("attribute", 0)
    return type(attribute) is int and not bool(attribute & 2)


def _track_rows(
    content: Mapping[str, Any], track_type: str, *, visible_only: bool = False
) -> list[Mapping[str, Any]]:
    tracks = content.get("tracks")
    if not isinstance(tracks, list):
        return []
    return [
        track
        for track in tracks
        if isinstance(track, dict)
        and str(track.get("type") or "").casefold() == track_type
        and (not visible_only or _track_is_visible(track))
    ]


def _segment_count(tracks: Iterable[Mapping[str, Any]]) -> int:
    return sum(
        len(track.get("segments")) for track in tracks if isinstance(track.get("segments"), list)
    )


def _validate_timeline_object_identities(content: Mapping[str, Any]) -> None:
    tracks = content.get("tracks")
    if not isinstance(tracks, list):
        raise PortableProjectError(
            "editable_structure_missing", "Draft does not expose editable tracks"
        )
    track_ids: set[str] = set()
    segment_ids: set[str] = set()
    for track in tracks:
        if not isinstance(track, dict):
            raise PortableProjectError("editable_structure_missing", "Editable track is malformed")
        track_id = str(track.get("id") or "").strip()
        if not track_id or track_id in track_ids:
            raise PortableProjectError(
                "editable_structure_missing",
                "Editable track identities must be non-empty and unique per timeline",
            )
        track_ids.add(track_id)
        segments = track.get("segments")
        if not isinstance(segments, list):
            raise PortableProjectError(
                "editable_structure_missing", "Editable track segments are malformed"
            )
        for segment in segments:
            if not isinstance(segment, dict):
                raise PortableProjectError(
                    "editable_structure_missing", "Editable segment is malformed"
                )
            segment_id = str(segment.get("id") or "").strip()
            if not segment_id or segment_id in segment_ids:
                raise PortableProjectError(
                    "editable_structure_missing",
                    "Editable segment identities must be non-empty and globally unique per timeline",
                )
            segment_ids.add(segment_id)


def _is_marker_track(track: Mapping[str, Any]) -> bool:
    name = str(track.get("name") or "").casefold()
    latin_words = set(re.findall(r"[a-z]+", name))
    return bool({"marker", "review"}.intersection(latin_words)) or any(
        token in name for token in ("修改", "校对", "标记")
    )


def _primary_video_track(
    video_tracks: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any]:
    tracks = [track for track in video_tracks if track.get("segments")]
    named_main = [
        track
        for track in tracks
        if str(track.get("name") or "").strip().casefold() in _MAIN_VIDEO_TRACK_NAMES
    ]
    if len(named_main) == 1:
        return named_main[0]
    if len(named_main) > 1:
        raise PortableProjectError(
            "editable_structure_missing", "Primary source video track is ambiguous"
        )
    non_preview = [
        track
        for track in tracks
        if str(track.get("name") or "").strip().casefold() != "final video"
    ]
    if len(non_preview) != 1:
        raise PortableProjectError(
            "editable_structure_missing", "Primary source video track is missing or ambiguous"
        )
    return non_preview[0]


def _declared_material_ids(content: Mapping[str, Any], bucket: str) -> set[str]:
    materials = content.get("materials")
    if not isinstance(materials, dict) or not isinstance(materials.get(bucket), list):
        return set()
    return {
        _material_id(row)
        for row in materials[bucket]
        if isinstance(row, dict)
        and _material_id(row)
        and (bucket == "texts" or content_material_path_fields(row))
    }


def _validate_track_material_links(
    content: Mapping[str, Any], tracks: Iterable[Mapping[str, Any]], *, bucket: str
) -> None:
    declared = _declared_material_ids(content, bucket)
    for track in tracks:
        segments = track.get("segments")
        if not isinstance(segments, list):
            raise PortableProjectError(
                "editable_structure_missing", "Editable track segments are malformed"
            )
        for segment in segments:
            if not isinstance(segment, dict):
                raise PortableProjectError(
                    "editable_structure_missing", "Editable segment is malformed"
                )
            material_id = str(segment.get("material_id") or "").strip()
            if not material_id or material_id not in declared:
                raise PortableProjectError(
                    "missing_local_media",
                    "Editable segment references an undeclared local material",
                    {"material_id": material_id},
                )


def _all_declared_material_ids(content: Mapping[str, Any]) -> set[str]:
    materials = content.get("materials")
    if not isinstance(materials, dict):
        return set()
    return {
        material_id
        for rows in materials.values()
        if isinstance(rows, list)
        for row in rows
        if isinstance(row, dict)
        for material_id in (_material_id(row),)
        if material_id
    }


def _validate_all_segment_material_links(content: Mapping[str, Any]) -> None:
    declared = _all_declared_material_ids(content)
    tracks = content.get("tracks")
    if not isinstance(tracks, list):
        raise PortableProjectError(
            "editable_structure_missing", "Draft does not expose editable tracks"
        )
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get("segments"), list):
            raise PortableProjectError("editable_structure_missing", "Editable track is malformed")
        for segment in track["segments"]:
            if not isinstance(segment, dict):
                raise PortableProjectError(
                    "editable_structure_missing", "Editable segment is malformed"
                )
            primary = str(segment.get("material_id") or "").strip()
            if not primary or primary not in declared:
                raise PortableProjectError(
                    "missing_local_media",
                    "Editable segment references an undeclared material",
                    {"material_id": primary},
                )
            extra_refs = segment.get("extra_material_refs", [])
            if extra_refs is None:
                extra_refs = []
            if not isinstance(extra_refs, list) or not all(
                isinstance(value, str) and value.strip() for value in extra_refs
            ):
                raise PortableProjectError(
                    "editable_structure_missing",
                    "Editable segment material references are malformed",
                )
            for material_id in extra_refs:
                if material_id not in declared:
                    raise PortableProjectError(
                        "missing_local_media",
                        "Editable segment references an undeclared material",
                        {"material_id": material_id},
                    )


def _validate_segment_timeranges(content: Mapping[str, Any]) -> None:
    timeline_duration = content.get("duration")
    if type(timeline_duration) is not int or timeline_duration <= 0:
        raise PortableProjectError(
            "editable_structure_missing", "Draft timeline duration is malformed"
        )
    tracks = content.get("tracks")
    if not isinstance(tracks, list):
        raise PortableProjectError(
            "editable_structure_missing", "Draft does not expose editable tracks"
        )
    for track in tracks:
        if not isinstance(track, dict):
            raise PortableProjectError("editable_structure_missing", "Editable track is malformed")
        segments = track.get("segments")
        if not isinstance(segments, list):
            raise PortableProjectError(
                "editable_structure_missing", "Editable track segments are malformed"
            )
        for segment in segments:
            if not isinstance(segment, dict):
                raise PortableProjectError(
                    "editable_structure_missing", "Editable segment is malformed"
                )
            timerange = segment.get("target_timerange")
            if not isinstance(timerange, dict):
                raise PortableProjectError(
                    "editable_structure_missing", "Editable segment target timerange is malformed"
                )
            start = timerange.get("start")
            duration = timerange.get("duration")
            if (
                type(start) is not int
                or type(duration) is not int
                or start < 0
                or duration <= 0
                or start + duration > timeline_duration
            ):
                raise PortableProjectError(
                    "editable_structure_missing", "Editable segment target timerange is invalid"
                )
            source_timerange = segment.get("source_timerange")
            if source_timerange is None:
                continue
            if not isinstance(source_timerange, dict):
                raise PortableProjectError(
                    "editable_structure_missing", "Editable segment source timerange is malformed"
                )
            source_start = source_timerange.get("start")
            source_duration = source_timerange.get("duration")
            if (
                type(source_start) is not int
                or type(source_duration) is not int
                or source_start < 0
                or source_duration <= 0
            ):
                raise PortableProjectError(
                    "editable_structure_missing", "Editable segment source timerange is invalid"
                )


def normalize_structure_policy(value: object) -> str:
    policy = str(value or "").strip().casefold()
    if policy not in STRUCTURE_POLICIES:
        raise PortableProjectError(
            "invalid_structure_policy", "Editable project structure policy is unsupported"
        )
    return policy


def timeline_structure_policy(
    project_policy: object,
    *,
    timeline_id: str,
    active_timeline_id: str,
) -> str:
    policy = normalize_structure_policy(project_policy)
    if policy == "revision" and timeline_id != active_timeline_id:
        return "standard"
    return policy


def _track_exposes_real_cut_boundaries(track: Mapping[str, Any]) -> bool:
    segments = track.get("segments")
    if not isinstance(segments, list) or len(segments) < 2:
        return False
    segment_ids = [
        str(segment.get("id") or "").strip() if isinstance(segment, dict) else ""
        for segment in segments
    ]
    if any(not segment_id for segment_id in segment_ids) or len(segment_ids) != len(
        set(segment_ids)
    ):
        return False
    ranges = sorted(
        (
            int(segment["target_timerange"]["start"]),
            int(segment["target_timerange"]["start"])
            + int(segment["target_timerange"]["duration"]),
        )
        for segment in segments
        if isinstance(segment, dict) and isinstance(segment.get("target_timerange"), dict)
    )
    if len(ranges) != len(segments) or len(set(ranges)) != len(ranges):
        return False
    return all(
        left_end <= right_start
        for (_left_start, left_end), (right_start, _right_end) in zip(
            ranges, ranges[1:], strict=False
        )
    )


def _track_covers_timeline(track: Mapping[str, Any], timeline_duration: int) -> bool:
    segments = track.get("segments")
    if not isinstance(segments, list) or not segments:
        return False
    ranges: list[tuple[int, int]] = []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("target_timerange"), dict):
            return False
        timerange = segment["target_timerange"]
        start = timerange.get("start")
        duration = timerange.get("duration")
        if type(start) is not int or type(duration) is not int:
            return False
        ranges.append((start, start + duration))
    covered_until = 0
    for start, end in sorted(ranges):
        if start > covered_until:
            return False
        covered_until = max(covered_until, end)
    return covered_until == timeline_duration


def _flattened_preview_lacks_editable_coverage(
    tracks: Iterable[Mapping[str, Any]],
    *,
    preview_name: str,
    timeline_duration: int,
) -> bool:
    populated = [track for track in tracks if track.get("segments")]
    if not populated:
        return False
    preview_tracks = [
        track
        for track in populated
        if str(track.get("name") or "").strip().casefold() == preview_name
    ]
    if len(preview_tracks) == len(populated):
        return True
    if not any(_track_covers_timeline(track, timeline_duration) for track in preview_tracks):
        return False
    return not any(
        _track_covers_timeline(track, timeline_duration)
        for track in populated
        if track not in preview_tracks
    )


def _text_material_visible_text(material: Mapping[str, Any]) -> str:
    content = material.get("content")
    if not isinstance(content, str):
        return ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict) or not isinstance(parsed.get("text"), str):
        return ""
    text = parsed["text"]
    return (
        text
        if any(unicodedata.category(character)[0] not in {"C", "M", "Z"} for character in text)
        else ""
    )


def _validate_revision_markers(
    content: Mapping[str, Any], marker_tracks: Iterable[Mapping[str, Any]]
) -> None:
    materials = content.get("materials")
    text_rows = materials.get("texts") if isinstance(materials, dict) else None
    if not isinstance(text_rows, list) or not text_rows:
        return
    indexed: dict[str, list[Mapping[str, Any]]] = {}
    for row in text_rows:
        if isinstance(row, dict):
            indexed.setdefault(_material_id(row), []).append(row)

    segment_ids: set[str] = set()
    material_ids: set[str] = set()
    for track in marker_tracks:
        segments = track.get("segments")
        if not isinstance(segments, list):
            raise PortableProjectError(
                "editable_structure_missing", "Revision marker track is malformed"
            )
        for segment in segments:
            if not isinstance(segment, dict):
                raise PortableProjectError(
                    "editable_structure_missing", "Revision marker segment is malformed"
                )
            segment_id = str(segment.get("id") or "").strip()
            material_id = str(segment.get("material_id") or "").strip()
            material_matches = indexed.get(material_id, [])
            if (
                not segment_id
                or segment_id in segment_ids
                or not material_id
                or material_id in material_ids
                or len(material_matches) != 1
                or not _text_material_visible_text(material_matches[0])
            ):
                raise PortableProjectError(
                    "editable_structure_missing",
                    "Revision markers require unique identities and non-empty visible text",
                )
            segment_ids.add(segment_id)
            material_ids.add(material_id)


def draft_structure_receipt(
    root_content: Mapping[str, Any],
    active_content: Mapping[str, Any],
    *,
    structure_policy: str = "revision",
) -> dict[str, Any]:
    policy = normalize_structure_policy(structure_policy)
    root_tracks = root_content.get("tracks")
    active_tracks = active_content.get("tracks")
    if not isinstance(root_tracks, list) or not isinstance(active_tracks, list):
        raise PortableProjectError(
            "editable_structure_missing", "Draft does not expose editable tracks"
        )
    if root_tracks != active_tracks:
        raise PortableProjectError(
            "editable_structure_missing", "Root and active editable tracks do not agree"
        )
    _validate_timeline_object_identities(root_content)
    _validate_timeline_object_identities(active_content)
    _validate_segment_timeranges(root_content)
    _validate_segment_timeranges(active_content)
    root_all_video_tracks = _track_rows(root_content, "video")
    root_all_audio_tracks = _track_rows(root_content, "audio")
    root_all_text_tracks = _track_rows(root_content, "text")
    root_text_tracks = _track_rows(root_content, "text", visible_only=True)
    root_marker_tracks = [track for track in root_text_tracks if _is_marker_track(track)]
    all_video_tracks = _track_rows(active_content, "video")
    all_audio_tracks = _track_rows(active_content, "audio")
    all_text_tracks = _track_rows(active_content, "text")
    video_tracks = _track_rows(active_content, "video", visible_only=True)
    audio_tracks = _track_rows(active_content, "audio", visible_only=True)
    text_tracks = _track_rows(active_content, "text", visible_only=True)
    marker_tracks = [track for track in text_tracks if _is_marker_track(track)]
    video_segments = _segment_count(video_tracks)
    audio_segments = _segment_count(audio_tracks)
    if not video_tracks or not audio_tracks or video_segments == 0 or audio_segments == 0:
        raise PortableProjectError(
            "editable_structure_missing",
            "Draft must retain source video and audio tracks with editable segments",
        )
    primary_video_track = _primary_video_track(video_tracks)
    primary_track_id = str(primary_video_track.get("id") or "").strip()
    if (
        not primary_track_id
        or sum(
            str(track.get("id") or "").strip() == primary_track_id
            for track in active_tracks
            if isinstance(track, Mapping)
        )
        != 1
    ):
        raise PortableProjectError(
            "editable_structure_missing",
            "Primary source video track requires a stable unique identity",
        )
    timeline_duration = int(active_content["duration"])
    if policy == "revision" and (
        not _track_exposes_real_cut_boundaries(primary_video_track)
        or not _track_covers_timeline(primary_video_track, timeline_duration)
        or not marker_tracks
        or _segment_count(marker_tracks) == 0
    ):
        raise PortableProjectError(
            "editable_structure_missing",
            "Draft must retain visible cut structure and independently traceable markers",
        )
    if policy == "revision":
        _validate_revision_markers(root_content, root_marker_tracks)
        _validate_revision_markers(active_content, marker_tracks)
    _validate_track_material_links(root_content, root_all_video_tracks, bucket="videos")
    _validate_track_material_links(root_content, root_all_audio_tracks, bucket="audios")
    _validate_track_material_links(root_content, root_all_text_tracks, bucket="texts")
    _validate_all_segment_material_links(root_content)
    _validate_track_material_links(active_content, all_video_tracks, bucket="videos")
    _validate_track_material_links(active_content, all_audio_tracks, bucket="audios")
    _validate_track_material_links(active_content, all_text_tracks, bucket="texts")
    _validate_all_segment_material_links(active_content)
    full_video_preview = any(
        str(track.get("name") or "").strip().casefold() == "final video"
        and _track_covers_timeline(track, timeline_duration)
        for track in video_tracks
    )
    if (
        full_video_preview and not _track_covers_timeline(primary_video_track, timeline_duration)
    ) or _flattened_preview_lacks_editable_coverage(
        audio_tracks,
        preview_name="final audio",
        timeline_duration=timeline_duration,
    ):
        raise PortableProjectError(
            "editable_structure_missing", "Draft only contains flattened preview tracks"
        )
    return {
        "structure_policy": policy,
        "track_digest": canonical_json_sha256(active_tracks),
        "video_track_count": len(video_tracks),
        "audio_track_count": len(audio_tracks),
        "video_segment_count": video_segments,
        "audio_segment_count": audio_segments,
        "marker_track_count": len(marker_tracks),
        "marker_segment_count": _segment_count(marker_tracks),
    }


def _validate_file_rows(package: Path, manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise PortableProjectError("invalid_manifest", "Manifest file inventory is empty")
    indexed: dict[str, dict[str, Any]] = {}
    casefolded: set[str] = set()
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            raise PortableProjectError("invalid_manifest", "Manifest file row is invalid")
        relative = validate_relative_path(raw_row.get("path"))
        if relative.casefold() in casefolded:
            raise PortableProjectError("invalid_manifest", "Manifest contains duplicate file paths")
        casefolded.add(relative.casefold())
        if not _valid_byte_size(raw_row.get("byte_size")) or not _valid_sha256(
            raw_row.get("sha256")
        ):
            raise PortableProjectError(
                "invalid_manifest", "Manifest file size or SHA-256 field is invalid"
            )
        file_path = _safe_package_file(package, relative)
        if not file_path.exists() or not file_path.is_file():
            raise PortableProjectError(
                "manifest_hash_mismatch", "A listed package file is missing", {"path": relative}
            )
        size = file_path.stat().st_size
        digest = sha256_file(file_path)
        if size != raw_row.get("byte_size") or digest != raw_row.get("sha256"):
            raise PortableProjectError(
                "manifest_hash_mismatch",
                "A listed package file failed size or SHA-256 validation",
                {"path": relative},
            )
        indexed[relative] = dict(raw_row)

    actual: set[str] = set()
    for root, directories, files in os.walk(package, followlinks=False):
        root_path = Path(root)
        for directory in directories:
            path = root_path / directory
            if _is_reparse(path):
                raise PortableProjectError(
                    "unsafe_package_path", "Portable project contains a reparse point"
                )
        for filename in files:
            path = root_path / filename
            if _is_reparse(path) or not path.is_file():
                raise PortableProjectError(
                    "unsafe_package_path", "Portable project contains a non-regular file"
                )
            relative = path.relative_to(package).as_posix()
            if relative not in {MANIFEST_FILENAME, REPORT_FILENAME}:
                actual.add(relative)
    if actual != set(indexed):
        raise PortableProjectError(
            "manifest_hash_mismatch",
            "Package file inventory does not match the manifest",
            {
                "missing": sorted(set(indexed) - actual),
                "unlisted": sorted(actual - set(indexed)),
            },
        )
    return indexed


def _manifest_directory_paths(manifest: Mapping[str, Any]) -> list[str]:
    rows = manifest.get("directories")
    if not isinstance(rows, list) or not all(isinstance(value, str) for value in rows):
        raise PortableProjectError("invalid_manifest", "Manifest directory inventory is invalid")
    normalized = [validate_relative_path(value) for value in rows]
    casefolded = [value.casefold() for value in normalized]
    if normalized != sorted(normalized) or len(casefolded) != len(set(casefolded)):
        raise PortableProjectError(
            "invalid_manifest", "Manifest directory inventory is not deterministic"
        )
    return normalized


def _validate_directory_rows(package: Path, manifest: Mapping[str, Any]) -> list[str]:
    declared = _manifest_directory_paths(manifest)
    actual = sorted(
        path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_dir()
    )
    if actual != declared:
        raise PortableProjectError(
            "manifest_hash_mismatch",
            "Package directory inventory does not match the manifest",
            {
                "missing": sorted(set(declared) - set(actual)),
                "unlisted": sorted(set(actual) - set(declared)),
            },
        )
    return declared


def _expected_installed_directory_paths(manifest: Mapping[str, Any]) -> set[str]:
    return set(installation_projection(manifest)["directories"])


def _validate_materials(
    package: Path,
    manifest: Mapping[str, Any],
    file_rows: Mapping[str, Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
) -> None:
    rows = manifest.get("materials")
    if not isinstance(rows, list):
        raise PortableProjectError("invalid_manifest", "Manifest material rows are invalid")
    materials: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PortableProjectError("invalid_manifest", "Manifest material row is invalid")
        material_id = validate_material_id(row.get("material_id"))
        if material_id in materials:
            raise PortableProjectError("invalid_manifest", "Material IDs must be unique")
        basename = str(row.get("basename") or "")
        relative = validate_relative_path(row.get("package_path"))
        expected_logical = logical_media_path(str(row.get("kind") or ""), material_id, basename)
        expected_relative = f"Media/{expected_logical.removeprefix(f'{MEDIA_MARKER}/')}"
        if relative != expected_relative or relative not in file_rows:
            raise PortableProjectError(
                "manifest_hash_mismatch", "Material path does not match its package identity"
            )
        file_row = file_rows[relative]
        if not _valid_byte_size(row.get("byte_size")) or not _valid_sha256(row.get("sha256")):
            raise PortableProjectError(
                "invalid_manifest", "Material size or SHA-256 field is invalid"
            )
        if row.get("byte_size") != file_row.get("byte_size") or row.get("sha256") != file_row.get(
            "sha256"
        ):
            raise PortableProjectError(
                "manifest_hash_mismatch", "Material hash does not match the file inventory"
            )
        materials[material_id] = dict(row)

    locations: dict[str, list[str]] = {material_id: [] for material_id in materials}
    content_keys = [
        "Draft/draft_content.json",
        *sorted(
            key
            for key in documents
            if key.startswith("Draft/Timelines/") and key.endswith("/draft_content.json")
        ),
    ]
    active_id = str((manifest.get("project") or {}).get("active_timeline_id") or "")
    name_mismatches = 0
    for key in content_keys:
        content = documents[key]
        if key == "Draft/draft_content.json":
            prefix = "root"
        else:
            timeline_id = PurePosixPath(key).parts[-2]
            prefix = (
                f"active_timeline:{timeline_id}"
                if timeline_id == active_id
                else f"timeline:{timeline_id}"
            )
        for location, bucket, item in _content_rows(content, location_prefix=prefix):
            material_id = _material_id(item)
            if material_id not in materials:
                raise PortableProjectError(
                    "missing_local_media", "Draft references a material absent from the manifest"
                )
            manifest_row = materials[material_id]
            expected = logical_media_path(
                str(manifest_row["kind"]), material_id, str(manifest_row["basename"])
            )
            if any(item.get(field) != expected for field in content_material_path_fields(item)):
                raise PortableProjectError(
                    "source_path_leaked", "Draft material path is not package-local"
                )
            display_fields = content_material_name_fields(item, bucket=bucket)
            if any(item.get(field) != manifest_row["basename"] for field in display_fields):
                name_mismatches += 1
            locations[material_id].append(location)

    meta = documents["Draft/draft_meta_info.json"]
    if (
        meta.get("draft_root_path") != DRAFT_ROOT_MARKER
        or meta.get("draft_fold_path") != DRAFT_DIR_MARKER
    ):
        raise PortableProjectError(
            "source_path_leaked", "Draft metadata still contains machine-specific locations"
        )
    for location, item in _meta_rows(meta):
        material_id = str(item.get("id") or "").strip()
        if material_id not in materials:
            raise PortableProjectError(
                "missing_local_media", "Draft metadata references a material absent from manifest"
            )
        manifest_row = materials[material_id]
        expected = logical_media_path(
            str(manifest_row["kind"]), material_id, str(manifest_row["basename"])
        )
        if item.get("file_Path") != expected:
            raise PortableProjectError(
                "source_path_leaked", "Draft metadata material path is not package-local"
            )
        if item.get("extra_info") != manifest_row["basename"]:
            name_mismatches += 1
        locations[material_id].append(location)

    if name_mismatches:
        raise PortableProjectError(
            "material_name_mismatch",
            "JianYing material display names do not match packaged basenames",
            {"count": name_mismatches},
        )
    for material_id, row in materials.items():
        declared_locations = row.get("json_locations")
        if (
            not isinstance(declared_locations, list)
            or not all(isinstance(value, str) for value in declared_locations)
            or len(declared_locations) != len(set(declared_locations))
        ):
            raise PortableProjectError("invalid_manifest", "Material JSON locations are invalid")
        if sorted(locations[material_id]) != sorted(declared_locations):
            raise PortableProjectError(
                "material_reference_mismatch",
                "Material JSON reference coverage does not match the manifest",
                {"material_id": material_id},
            )


def _validate_non_file_dependencies(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    rows = manifest.get("non_file_dependencies")
    if not isinstance(rows, list):
        raise PortableProjectError("invalid_manifest", "Non-file dependency rows are invalid")
    normalized: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for raw_row in rows:
        if not isinstance(raw_row, dict) or set(raw_row) != {"kind", "name", "resource_id"}:
            raise PortableProjectError("invalid_manifest", "Non-file dependency row is invalid")
        kind = raw_row.get("kind")
        name = raw_row.get("name")
        resource_id = raw_row.get("resource_id")
        if (
            not isinstance(kind, str)
            or not kind.strip()
            or not isinstance(name, str)
            or not isinstance(resource_id, str)
            or not resource_id.strip()
        ):
            raise PortableProjectError("invalid_manifest", "Non-file dependency row is invalid")
        identity = (kind, resource_id)
        if identity in identities:
            raise PortableProjectError(
                "invalid_manifest", "Non-file dependency identities must be unique"
            )
        identities.add(identity)
        normalized.append({"kind": kind, "name": name, "resource_id": resource_id})
    if normalized != sorted(normalized, key=lambda row: (row["kind"], row["resource_id"])):
        raise PortableProjectError(
            "invalid_manifest", "Non-file dependency rows are not deterministic"
        )
    return normalized


def _validate_no_sensitive_draft_artifacts(
    file_rows: Iterable[Mapping[str, Any]], directory_rows: Iterable[str]
) -> None:
    for relative in directory_rows:
        if relative.startswith("Draft/") and is_sensitive_draft_artifact(
            relative, is_directory=True
        ):
            raise PortableProjectError(
                "unsafe_draft_artifact",
                "Portable draft contains a sensitive or source-control artifact",
            )
    for row in file_rows:
        relative = validate_relative_path(row.get("path"))
        if relative.startswith("Draft/") and is_sensitive_draft_artifact(
            relative, is_directory=False
        ):
            raise PortableProjectError(
                "unsafe_draft_artifact",
                "Portable draft contains a sensitive or source-control artifact",
            )


def validate_package(package_dir: Path, *, require_report: bool = True) -> dict[str, Any]:
    package = Path(package_dir).resolve()
    if not package.is_dir():
        raise PortableProjectError("package_not_found", "Portable project directory is missing")
    manifest = read_manifest(package)
    if list(_absolute_strings(manifest)):
        raise PortableProjectError(
            "source_path_leaked", "Public manifest contains an absolute source path"
        )
    report_path = package / REPORT_FILENAME
    existing_report: dict[str, Any] | None = None
    if report_path.is_file():
        existing_report = _load_json_object(report_path, code="invalid_validation_report")
        if list(_absolute_strings(existing_report)):
            raise PortableProjectError(
                "source_path_leaked", "Public validation report contains an absolute source path"
            )
    elif require_report:
        raise PortableProjectError(
            "validation_report_mismatch", "Portable project validation report is missing"
        )

    file_rows = _validate_file_rows(package, manifest)
    directory_rows = _validate_directory_rows(package, manifest)
    installation_projection(manifest)
    _validate_no_sensitive_draft_artifacts(file_rows.values(), directory_rows)
    _validate_retained_json_paths(package, file_rows)
    importer = manifest.get("importer")
    if not isinstance(importer, dict) or importer.get("path") != IMPORTER_FILENAME:
        raise PortableProjectError("invalid_manifest", "Importer manifest entry is missing")
    verified_importer = validate_build_receipt(
        _safe_package_file(package, IMPORTER_FILENAME), importer
    )
    if any(importer.get(key) != value for key, value in verified_importer.items()):
        raise PortableProjectError(
            "importer_receipt_invalid", "Importer manifest build identity is inconsistent"
        )
    importer_row = file_rows.get(IMPORTER_FILENAME)
    if not _valid_byte_size(importer.get("byte_size")) or not _valid_sha256(importer.get("sha256")):
        raise PortableProjectError("invalid_manifest", "Importer size or SHA-256 field is invalid")
    if not importer_row or (
        importer.get("byte_size") != importer_row.get("byte_size")
        or importer.get("sha256") != importer_row.get("sha256")
    ):
        raise PortableProjectError(
            "importer_hash_mismatch", "Importer executable is not bound to its build receipt"
        )

    project = manifest.get("project")
    documents_spec = manifest.get("documents")
    draft_copy = manifest.get("draft_copy")
    if (
        not isinstance(project, dict)
        or not isinstance(documents_spec, dict)
        or not isinstance(draft_copy, dict)
    ):
        raise PortableProjectError("invalid_manifest", "Draft document inventory is missing")
    source_app = str(project.get("source_app") or "").strip()
    if source_app.casefold() not in SUPPORTED_APP_IDENTITIES:
        raise PortableProjectError(
            "unknown_source_app",
            "Source project application must be JianyingPro or CapCut",
        )
    excluded_paths = draft_copy.get("excluded_paths")
    transient_policy_version = draft_copy.get("transient_policy_version")
    if (
        type(transient_policy_version) is not int
        or transient_policy_version != 1
        or not isinstance(excluded_paths, list)
    ):
        raise PortableProjectError(
            "invalid_manifest", "Draft transient-file policy is missing or unsupported"
        )
    normalized_excluded: list[str] = []
    for value in excluded_paths:
        relative = validate_relative_path(value)
        if not relative.startswith("Draft/") or relative in file_rows:
            raise PortableProjectError(
                "invalid_manifest", "Draft transient-file receipt is inconsistent"
            )
        normalized_excluded.append(relative)
    if normalized_excluded != sorted(set(normalized_excluded)):
        raise PortableProjectError(
            "invalid_manifest", "Draft transient-file receipt is not deterministic"
        )
    timeline_ids = project.get("timeline_ids")
    if not isinstance(timeline_ids, list) or not all(
        isinstance(value, str) and value.strip() for value in timeline_ids
    ):
        raise PortableProjectError("invalid_manifest", "Timeline inventory is invalid")
    if timeline_ids != sorted(timeline_ids) or len(
        {value.casefold() for value in timeline_ids}
    ) != len(timeline_ids):
        raise PortableProjectError(
            "invalid_manifest", "Timeline inventory must be deterministic and unique"
        )
    expected_document_paths = {
        "Draft/draft_content.json",
        "Draft/draft_meta_info.json",
        "Draft/draft_virtual_store.json",
        *(f"Draft/Timelines/{timeline_id}/draft_content.json" for timeline_id in timeline_ids),
    }
    rewritten_json = documents_spec.get("rewritten_json")
    if (
        not isinstance(rewritten_json, list)
        or not all(isinstance(value, str) for value in rewritten_json)
        or len(rewritten_json) != len(set(rewritten_json))
    ):
        raise PortableProjectError("invalid_manifest", "Draft document inventory is invalid")
    declared_document_paths = set(rewritten_json)
    if declared_document_paths != expected_document_paths:
        raise PortableProjectError(
            "draft_document_coverage_mismatch",
            "Root, metadata, and timeline coverage is incomplete",
        )
    actual_timeline_paths = {
        path.relative_to(package).as_posix()
        for path in (package / "Draft" / "Timelines").glob("*/draft_content.json")
        if path.is_file()
    }
    if actual_timeline_paths != expected_document_paths - {
        "Draft/draft_content.json",
        "Draft/draft_meta_info.json",
        "Draft/draft_virtual_store.json",
    }:
        raise PortableProjectError(
            "draft_document_coverage_mismatch", "Package contains undeclared or missing timelines"
        )
    documents = {
        relative: _load_json_object(
            _safe_package_file(package, relative), code="invalid_draft_document"
        )
        for relative in expected_document_paths
    }
    if any(list(_absolute_strings(document)) for document in documents.values()):
        raise PortableProjectError(
            "source_path_leaked", "Portable draft contains an absolute machine path"
        )
    active_id = str(project.get("active_timeline_id") or "")
    active_key = f"Draft/Timelines/{active_id}/draft_content.json"
    root = documents["Draft/draft_content.json"]
    if timeline_ids:
        if active_key not in documents:
            raise PortableProjectError(
                "draft_document_coverage_mismatch", "Active timeline document is missing"
            )
        active = documents[active_key]
    else:
        active = root
    if root.get("id") != active_id or active.get("id") != active_id:
        raise PortableProjectError(
            "draft_document_coverage_mismatch", "Root and active timeline identities do not agree"
        )
    timeline_contents = {
        timeline_id: documents[f"Draft/Timelines/{timeline_id}/draft_content.json"]
        for timeline_id in timeline_ids
    }
    validate_manifest_source_provenance(project, root, timeline_contents)
    root_ids = {
        _material_id(row) for _location, _bucket, row in _content_rows(root, location_prefix="root")
    }
    active_ids = {
        _material_id(row)
        for _location, _bucket, row in _content_rows(
            active, location_prefix=f"active_timeline:{active_id}"
        )
    }
    if root_ids != active_ids:
        raise PortableProjectError(
            "material_reference_mismatch", "Root and active timeline materials do not agree"
        )
    _validate_materials(package, manifest, file_rows, documents)
    structure_policy = normalize_structure_policy(project.get("structure_policy"))
    structure = draft_structure_receipt(root, active, structure_policy=structure_policy)
    if structure != manifest.get("structure"):
        raise PortableProjectError(
            "editable_structure_missing", "Editable track and marker structure changed"
        )
    declared_timeline_structures = manifest.get("timeline_structures")
    if not isinstance(declared_timeline_structures, dict) or set(
        declared_timeline_structures
    ) != set(timeline_ids):
        raise PortableProjectError(
            "editable_structure_missing", "Declared timeline structure receipts are incomplete"
        )
    for timeline_id in timeline_ids:
        timeline_content = documents[f"Draft/Timelines/{timeline_id}/draft_content.json"]
        timeline_structure = draft_structure_receipt(
            timeline_content,
            timeline_content,
            structure_policy=timeline_structure_policy(
                structure_policy,
                timeline_id=timeline_id,
                active_timeline_id=active_id,
            ),
        )
        if timeline_structure != declared_timeline_structures[timeline_id]:
            raise PortableProjectError(
                "editable_structure_missing",
                "A declared timeline editable structure changed",
                {"timeline_id": timeline_id},
            )
    non_file_dependencies = _validate_non_file_dependencies(manifest)
    result = {
        "status": "portable_static_ready",
        "manifest_sha256": str(manifest.get("manifest_sha256") or ""),
        "missing_local_media": 0,
        "source_machine_media_paths": 0,
        "material_name_mismatch": 0,
        "material_count": len(manifest.get("materials") or []),
        "directory_count": len(directory_rows),
        "file_count": len(file_rows),
        "timeline_count": len(timeline_ids),
        **structure,
        "structure": structure,
        "timeline_structures": declared_timeline_structures,
        "non_file_dependency_count": len(non_file_dependencies),
        "non_file_dependencies": non_file_dependencies,
    }
    if existing_report is not None and canonical_json_sha256(
        existing_report
    ) != canonical_json_sha256(result):
        raise PortableProjectError(
            "validation_report_mismatch",
            "Saved validation report does not match freshly computed package evidence",
        )
    return result


def _is_fully_qualified_windows_path(value: object) -> bool:
    text = str(value or "").strip().replace("/", "\\")
    if text.startswith(("\\\\?\\", "\\\\.\\")):
        return False
    drive, tail = ntpath.splitdrive(text)
    if len(drive) == 2 and drive[0].isalpha() and drive[1] == ":":
        return tail.startswith("\\")
    if drive.startswith("\\\\") and tail.startswith("\\"):
        return len([part for part in drive[2:].split("\\") if part]) == 2
    return False


def _load_installed_documents(
    installed: Path, manifest: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    project = manifest.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("timeline_ids"), list):
        raise PortableProjectError("invalid_manifest", "Timeline inventory is invalid")
    relatives = {
        "draft_content.json",
        "draft_meta_info.json",
        "draft_virtual_store.json",
        *(f"Timelines/{timeline_id}/draft_content.json" for timeline_id in project["timeline_ids"]),
    }
    return {
        relative: _load_json_object(
            _safe_installed_path(installed, relative),
            code="installed_draft_validation_failed",
        )
        for relative in relatives
    }


def validate_installed_project(
    installed_draft_dir: Path,
    package_or_manifest: Path | Mapping[str, Any],
    *,
    expected_final_draft_dir: Path | None = None,
) -> dict[str, Any]:
    installed = Path(installed_draft_dir).absolute()
    if not installed.is_dir() or _is_reparse(installed):
        raise PortableProjectError(
            "installed_draft_validation_failed", "Installed draft directory is missing or unsafe"
        )
    if isinstance(package_or_manifest, Mapping):
        manifest = dict(package_or_manifest)
    else:
        manifest = read_manifest(Path(package_or_manifest).resolve())
    initial_tree_receipt = directory_tree_receipt(
        installed,
        error_code="installed_draft_validation_failed",
    )
    expected_directories = _expected_installed_directory_paths(manifest)
    actual_directories = set(initial_tree_receipt["directories"])
    if actual_directories != expected_directories:
        raise PortableProjectError(
            "installed_draft_validation_failed",
            "Installed directory inventory does not match the package",
        )
    final_draft = Path(expected_final_draft_dir or installed).absolute()
    documents = _load_installed_documents(installed, manifest)
    project = manifest.get("project")
    materials = manifest.get("materials")
    if not isinstance(project, dict) or not isinstance(materials, list):
        raise PortableProjectError("invalid_manifest", "Installed project identity is missing")
    active_id = str(project.get("active_timeline_id") or "")
    timeline_ids = project.get("timeline_ids")
    if not isinstance(timeline_ids, list):
        raise PortableProjectError("invalid_manifest", "Timeline inventory is invalid")
    root = documents["draft_content.json"]
    if timeline_ids:
        active_key = f"Timelines/{active_id}/draft_content.json"
        if active_key not in documents:
            raise PortableProjectError(
                "installed_draft_validation_failed", "Installed active timeline is missing"
            )
        active = documents[active_key]
    else:
        active = root
    if root.get("id") != active_id or active.get("id") != active_id:
        raise PortableProjectError(
            "installed_draft_validation_failed",
            "Installed root and active timeline identities do not agree",
        )

    indexed_materials: dict[str, dict[str, Any]] = {}
    for raw_row in materials:
        if not isinstance(raw_row, dict):
            raise PortableProjectError("invalid_manifest", "Material row is invalid")
        material_id = validate_material_id(raw_row.get("material_id"))
        package_path = validate_relative_path(raw_row.get("package_path"))
        if not package_path.startswith("Media/") or material_id in indexed_materials:
            raise PortableProjectError("invalid_manifest", "Material identity is invalid")
        projected_file = _safe_installed_path(installed, package_path)
        if _is_reparse(projected_file) or not projected_file.is_file():
            raise PortableProjectError(
                "external_media_after_import",
                "An installed material is missing from the draft Media directory",
                {"material_id": material_id},
            )
        if projected_file.stat().st_size != raw_row.get("byte_size") or sha256_file(
            projected_file
        ) != raw_row.get("sha256"):
            raise PortableProjectError(
                "installed_draft_validation_failed",
                "An installed material failed size or SHA-256 validation",
                {"material_id": material_id},
            )
        indexed_materials[material_id] = {
            **raw_row,
            "expected_path": str(final_draft.joinpath(*PurePosixPath(package_path).parts)),
        }

    locations: dict[str, list[str]] = {material_id: [] for material_id in indexed_materials}
    content_documents = {
        "draft_content.json": root,
        **{
            key: value
            for key, value in documents.items()
            if key.startswith("Timelines/") and key.endswith("/draft_content.json")
        },
    }
    root_material_ids = {
        _material_id(row) for _location, _bucket, row in _content_rows(root, location_prefix="root")
    }
    active_material_ids = {
        _material_id(row)
        for _location, _bucket, row in _content_rows(
            active, location_prefix=f"active_timeline:{active_id}"
        )
    }
    if root_material_ids != active_material_ids:
        raise PortableProjectError(
            "installed_draft_validation_failed",
            "Installed root and active material maps do not agree",
        )
    for relative, content in content_documents.items():
        if relative == "draft_content.json":
            prefix = "root"
        else:
            timeline_id = PurePosixPath(relative).parts[-2]
            prefix = (
                f"active_timeline:{timeline_id}"
                if timeline_id == active_id
                else f"timeline:{timeline_id}"
            )
        for location, bucket, item in _content_rows(content, location_prefix=prefix):
            material_id = _material_id(item)
            manifest_row = indexed_materials.get(material_id)
            if manifest_row is None:
                raise PortableProjectError(
                    "external_media_after_import",
                    "Installed draft references material outside the package manifest",
                )
            path_values = [
                str(item.get(field) or "") for field in content_material_path_fields(item)
            ]
            if not path_values or any(
                not _is_fully_qualified_windows_path(value)
                or ntpath.normcase(ntpath.normpath(value))
                != ntpath.normcase(ntpath.normpath(str(manifest_row["expected_path"])))
                for value in path_values
            ):
                raise PortableProjectError(
                    "external_media_after_import",
                    "Installed draft contains an external material path",
                    {"material_id": material_id},
                )
            if any(
                item.get(field) != manifest_row.get("basename")
                for field in content_material_name_fields(item, bucket=bucket)
            ):
                raise PortableProjectError(
                    "material_name_mismatch", "Installed material display name is inconsistent"
                )
            locations[material_id].append(location)

    meta = documents["draft_meta_info.json"]
    if ntpath.normcase(ntpath.normpath(str(meta.get("draft_root_path") or ""))) != ntpath.normcase(
        ntpath.normpath(str(final_draft.parent))
    ) or ntpath.normcase(
        ntpath.normpath(str(meta.get("draft_fold_path") or ""))
    ) != ntpath.normcase(
        ntpath.normpath(str(final_draft))
    ):
        raise PortableProjectError(
            "installed_draft_validation_failed", "Installed draft metadata paths are inconsistent"
        )
    for location, item in _meta_rows(meta):
        material_id = str(item.get("id") or "").strip()
        manifest_row = indexed_materials.get(material_id)
        value = str(item.get("file_Path") or "")
        if (
            manifest_row is None
            or not _is_fully_qualified_windows_path(value)
            or ntpath.normcase(ntpath.normpath(value))
            != ntpath.normcase(ntpath.normpath(str(manifest_row["expected_path"])))
        ):
            raise PortableProjectError(
                "external_media_after_import",
                "Installed metadata contains an external material path",
            )
        if item.get("extra_info") != manifest_row.get("basename"):
            raise PortableProjectError(
                "material_name_mismatch", "Installed metadata display name is inconsistent"
            )
        locations[material_id].append(location)
    for material_id, manifest_row in indexed_materials.items():
        declared_locations = manifest_row.get("json_locations")
        if (
            not isinstance(declared_locations, list)
            or not all(isinstance(value, str) for value in declared_locations)
            or len(declared_locations) != len(set(declared_locations))
            or sorted(locations[material_id]) != sorted(declared_locations)
        ):
            raise PortableProjectError(
                "installed_draft_validation_failed",
                "Installed material reference locations changed",
                {"material_id": material_id},
            )

    project = manifest.get("project")
    structure_policy = normalize_structure_policy(
        project.get("structure_policy") if isinstance(project, dict) else None
    )
    validate_manifest_source_provenance(
        project,
        root,
        {
            timeline_id: documents[f"Timelines/{timeline_id}/draft_content.json"]
            for timeline_id in timeline_ids
        },
    )
    structure = draft_structure_receipt(root, active, structure_policy=structure_policy)
    if structure != manifest.get("structure"):
        raise PortableProjectError(
            "installed_draft_validation_failed", "Installed editable structure changed"
        )
    declared_timeline_structures = manifest.get("timeline_structures")
    if not isinstance(declared_timeline_structures, dict) or set(
        declared_timeline_structures
    ) != set(timeline_ids):
        raise PortableProjectError(
            "installed_draft_validation_failed",
            "Installed timeline structure receipts are incomplete",
        )
    for timeline_id in timeline_ids:
        timeline_content = documents[f"Timelines/{timeline_id}/draft_content.json"]
        timeline_structure = draft_structure_receipt(
            timeline_content,
            timeline_content,
            structure_policy=timeline_structure_policy(
                structure_policy,
                timeline_id=timeline_id,
                active_timeline_id=active_id,
            ),
        )
        if timeline_structure != declared_timeline_structures[timeline_id]:
            raise PortableProjectError(
                "installed_draft_validation_failed",
                "An installed timeline editable structure changed",
                {"timeline_id": timeline_id},
            )
    final_tree_receipt = directory_tree_receipt(
        installed,
        error_code="installed_draft_validation_failed",
    )
    if final_tree_receipt != initial_tree_receipt:
        raise PortableProjectError(
            "installed_draft_validation_failed",
            "Installed draft changed during validation",
        )
    non_file_dependencies = _validate_non_file_dependencies(manifest)
    return {
        "status": "installed_static_ready",
        "manifest_sha256": str(manifest.get("manifest_sha256") or ""),
        "external_media_after_import": 0,
        "material_name_mismatch": 0,
        "material_count": len(indexed_materials),
        "directory_count": len(actual_directories),
        "file_count": final_tree_receipt["file_count"],
        "timeline_count": len(timeline_ids),
        **structure,
        "structure": structure,
        "timeline_structures": declared_timeline_structures,
        "non_file_dependency_count": len(non_file_dependencies),
        "non_file_dependencies": non_file_dependencies,
        "installed_tree_receipt": final_tree_receipt,
    }
