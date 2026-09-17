"""Conservative review intake and explicit, non-executing whole-source labels."""

from __future__ import annotations

import copy
import math
import re
from typing import Any, Mapping

GLOBAL_KIND = "global_review"
GLOBAL_STATUS = "label_only_global_review"
DOCUMENT_ORDER_TIMING = "document_order_fallback"
_CLOCK = re.compile(r"\d{1,3}\s*[:：]\s*\d{1,2}")
_REGION = re.compile(
    r"修改意见|审核意见|审阅意见|校对意见|剪辑需求|处理要求|修改要求|全片要求|全片意见"
)
_ACTION = re.compile(
    r"删除|删掉|去掉|隐藏|遮挡|替换|改成|改为|换成|调整|提前|延后|添加|增加|补充|缩短|延长|放大|缩小|加快|减慢|变声|降噪|粗剪|精剪|不要|需要|请|校对|检查|有误|不对|修正|修复|音量|字幕|停顿"
)
_GLOBAL = re.compile(
    r"全片|整片|全程|整段(?:视频|录屏|音频|素材)?|整个(?:视频|录屏|音频|素材|工程)|全部(?:视频|素材)|统一"
)
_LOCAL = re.compile(r"这里|此处|这句|这一句|这段话|这个字|开头|结尾|片头|片尾|出现时|出现的时候")


def review_region(text: str) -> bool:
    return bool(_REGION.search(text))


def review_candidate(text: str, *, checkbox: bool = False) -> bool:
    """Called only inside a selected review/material region (or for a checkbox)."""
    value = text.strip()
    if not value or value.endswith(("：", ":")) or value in {"修改意见", "剪辑需求", "录屏"}:
        return False
    if re.match(r"^(?:例如|示例|举例|课程正文|文件名|附件大小|备注说明)\s*[:：]", value):
        return False
    if re.fullmatch(r"[\d.]+\s*(?:KB|MB|GB|字节)", value, re.I):
        return False
    if re.fullmatch(r"[^\n]+\.(?:mp4|mov|wav|mp3|m4a|pptx|pdf|docx|png|jpg)", value, re.I):
        return False
    return checkbox or bool(_CLOCK.search(value) or _ACTION.search(value))


def select_review_rows(
    blocks: list[dict[str, Any]], *, bounded: bool = False
) -> list[dict[str, Any]]:
    """Preserve mixed paragraph/list/checkbox order; never promote ordinary prose."""
    result = []
    active = bounded
    video_id = ""
    for block in blocks:
        kind = str(block.get("kind") or block.get("type") or "text").casefold()
        text = str(block.get("source_text", block.get("text", "")))
        if kind in {"heading", "header", "title"} or re.fullmatch(r"h[1-6]", kind):
            active = bounded or review_region(text)
            video_id = ""
            continue
        if kind == "attachment":
            if str(block.get("mime") or "").startswith("video/") or re.search(
                r"\.(mp4|mov|webm|m4v)$",
                str(block.get("name") or block.get("filename") or ""),
                re.I,
            ):
                video_id = str(block.get("asset_id") or block.get("token") or "")
                active = True
            continue
        if review_region(text) and text.strip().endswith((":", "：")):
            active = True
            continue
        checkbox = kind == "checkbox"
        if not (active or checkbox) or not review_candidate(text, checkbox=checkbox):
            continue
        row = copy.deepcopy(block)
        row.pop("kind", None)
        row.pop("type", None)
        row["source_text"] = text
        evidence = row.setdefault("evidence", {})
        evidence["review_intake"] = {
            "format": kind,
            "basis": "selected_region" if bounded else "review_region_or_material_checklist",
            "checked": block.get("checked") if checkbox else None,
        }
        if video_id:
            evidence["review_intake"]["target_asset_id"] = video_id
        result.append(row)
    return result


def _get(project: Any, key: str, default: Any = None) -> Any:
    return (
        project.get(key, default)
        if isinstance(project, Mapping)
        else getattr(project, key, default)
    )


def scope_window(scope: Mapping[str, Any], project: Any) -> tuple[float, float]:
    """Derive the label's display anchor from its source, never from missing timing."""
    if scope.get("kind") not in {"source", "project"} or not scope.get("basis"):
        raise ValueError("Whole-source review needs explicit scope evidence")
    pairs = _get(project, "source_pairs", []) or []
    durations = [float(row.get("video_duration_seconds") or 0) for row in pairs]
    if any(not math.isfinite(value) or value <= 0 for value in durations):
        raise ValueError("Whole-source review needs verified source durations")
    total = sum(durations) if pairs else float(_get(project, "media_duration_seconds", 0) or 0)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Whole-source review needs a positive source duration")
    if scope["kind"] == "project":
        return 0.0, total
    index = scope.get("target_pair_index")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < (len(pairs) or 1):
        raise ValueError("Whole-source review has an ambiguous or invalid target source")
    return (sum(durations[:index]), sum(durations[: index + 1])) if pairs else (0.0, total)


def bind_global_reviews(rows: list[dict[str, Any]], project: Any) -> list[dict[str, Any]]:
    """Only explicit whole-scope wording or known whole-source profiles are labels."""
    result = copy.deepcopy(rows)
    pairs = _get(project, "source_pairs", []) or []
    for row in result:
        text = str(row.get("source_text") or "")
        evidence = row.setdefault("evidence", {})
        supplied = evidence.get("review_scope") or row.get("scope")
        if row.get("start") is not None or row.get("end") is not None or _CLOCK.search(text):
            if row.get("kind") != GLOBAL_KIND:
                continue
        profile_text = text.strip().strip("。；;！! ")
        profile = not _LOCAL.search(text) and (
            bool(re.search(r"(?:使用|采用|换成|改成|用).*(?:音色|变声)", text))
            or bool(re.search(r"(?:工具栏.*(?:隐藏|遮挡|去掉)|(?:隐藏|遮挡|去掉).*工具栏)", text))
            or profile_text in {"粗剪", "精剪", "请粗剪", "请精剪"}
        )
        if not supplied and not _GLOBAL.search(text) and not profile:
            continue
        if isinstance(supplied, str):
            if supplied in {"local", "unknown"}:
                continue
            if supplied not in {"source", "project"}:
                raise ValueError("Unknown review scope")
            scope = {"kind": supplied, "basis": "structured_scope"}
        else:
            scope = copy.deepcopy(supplied) if isinstance(supplied, dict) else {}
        if not scope:
            scope = {
                "kind": "project" if re.search(r"整个工程|全部视频|全部素材", text) else "source",
                "basis": "explicit_whole_scope" if _GLOBAL.search(text) else "whole_source_profile",
            }
        if scope.get("kind") not in {"source", "project"}:
            continue
        scope.setdefault("basis", "structured_scope")
        if scope["kind"] == "source" and "target_pair_index" not in scope:
            intake = evidence.get("review_intake") or {}
            if "target_pair_index" in intake:
                scope["target_pair_index"] = intake["target_pair_index"]
            elif not intake.get("target_unresolved") and len(pairs) <= 1:
                scope["target_pair_index"] = 0
            else:
                # Keep this item visible in the ledger, but never guess which source.
                evidence["scope_status"] = "unresolved_target"
                continue
        scope["placement_basis"] = "scope_start"
        evidence["review_scope"] = scope
        evidence["original_kind"] = evidence.get("original_kind", row.get("kind", "review_only"))
        evidence["timing_source"] = "scope_start"
        evidence["execution_status"] = GLOBAL_STATUS
        row.update(kind=GLOBAL_KIND, execution_required=False, execution_status=GLOBAL_STATUS)
        row.pop("start", None)
        row.pop("end", None)
        try:
            start, end = scope_window(scope, project)
        except ValueError:
            # Initial URL compilation can precede probing. The writer must resolve this.
            continue
        scope.update(start=start, end=end)
        row["start"] = start
    return result


def place_missing_review_labels(rows: list[dict[str, Any]], project: Any) -> None:
    """Place untimed local labels in document order, never providing edit timing.

    Pending ASR items stay pending. An ASR planner must explicitly downgrade an
    unresolved item before this helper may place it. Recompute display fallbacks
    after ASR so the predecessor is the final label, not its original search hint.
    """
    duration = float(_get(project, "media_duration_seconds", 0) or 0)
    previous: tuple[str, float] | None = None
    for row in rows:
        evidence = row.setdefault("evidence", {})
        if row.get("kind") == GLOBAL_KIND or evidence.get("scope_status") == "unresolved_target":
            continue
        timing = evidence.get("timing_source")
        fallback = timing == DOCUMENT_ORDER_TIMING
        point = row.get("start")
        if point is None:
            for key in (
                "target_time",
                "review_search_hint_seconds",
                "resolved_review_timestamp_seconds",
            ):
                if evidence.get(key) is not None:
                    point = evidence[key]
                    break
        if fallback or (point is None and row.get("end") is None and timing != "asr"):
            requested = previous[1] + 2.0 if previous else 0.0
            start = min(requested, max(0.0, duration - 0.01)) if duration > 0 else requested
            placement = {
                "basis": "after_previous_label" if previous else "first_local_label",
                "previous_item_id": previous[0] if previous else "",
                "requested_time": round(requested, 6),
                "resolved_time": round(start, 6),
                "display_only": True,
                "clamped": start != requested,
            }
            evidence.update(
                timing_source=DOCUMENT_ORDER_TIMING,
                review_timestamp_role="display_only",
                resolved_time=round(start, 6),
                label_placement=placement,
                execution_status="label_only_unresolved",
            )
            evidence.pop("asr_alignment", None)
            row.update(
                start=round(start, 6),
                end=round(start + 0.8, 6),
                execution_required=False,
                execution_status="label_only_unresolved",
            )
            point = start
        if (
            isinstance(point, (int, float))
            and not isinstance(point, bool)
            and math.isfinite(point)
            and point >= 0
        ):
            # The writer displays all Lite labels for two seconds, clipping at EOF.
            start = min(float(point), max(0.0, duration - 0.01)) if duration > 0 else float(point)
            previous = (str(row.get("id") or row.get("item_id") or ""), start)


def document_order_marker_time(evidence: Mapping[str, Any]) -> float | None:
    """Validate the explicit display-only receipt; it is never an ASR receipt."""
    if evidence.get("timing_source") != DOCUMENT_ORDER_TIMING:
        return None
    placement = evidence.get("label_placement") or {}
    point = placement.get("resolved_time")
    if (
        evidence.get("review_timestamp_role") != "display_only"
        or placement.get("display_only") is not True
        or placement.get("basis") not in {"first_local_label", "after_previous_label"}
        or not isinstance(point, (float, int))
        or isinstance(point, bool)
        or not math.isfinite(point)
        or point < 0
        or evidence.get("resolved_time") != point
    ):
        raise ValueError("Invalid document-order display-only label receipt")
    return float(point)
