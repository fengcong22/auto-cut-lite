import json
import math
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional


@dataclass(frozen=True)
class RevisionProject:
    draft_name: str
    source_video: str
    source_audio: str = ""
    replacement_audio: str = ""
    project_key: str = ""
    media_duration_seconds: float = 0.0


@dataclass(frozen=True)
class RevisionEdit:
    op_type: str
    start: float
    end: float
    label: str
    detail: str = ""
    audio_path: str = ""
    doc_item_id: str = ""
    source_kind: str = ""
    asset_paths: List[str] = field(default_factory=list)
    visual_plan: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)
    validation: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RevisionMarker:
    label: str
    start: float
    end: float
    detail: str = ""
    doc_item_id: str = ""


@dataclass(frozen=True)
class PauseAdjustment:
    item_id: str
    source_time: float
    duration: float
    frame_path: str
    frame_sha256: str = ""
    reason: str = ""
    requested_source_time: Optional[float] = None
    frame_source_time: Optional[float] = None
    boundary_evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AudioDeliverySegment:
    segment_id: str
    role: str
    asset_path: str
    track_name: str
    source_start: float
    timeline_start: float
    duration: float
    volume: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    doc_item_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AudioDeliveryPlan:
    mode: str = "legacy"
    pending: bool = False
    forbid_full_length_segments: bool = False
    max_single_segment_ratio: float = 0.9
    validation_only_audio_paths: List[str] = field(default_factory=list)
    segments: List[AudioDeliverySegment] = field(default_factory=list)


@dataclass(frozen=True)
class PreservationRules:
    source_video_material: bool = True
    separated_audio_material: bool = True
    replacement_audio_material: bool = True
    keep_cut_points: bool = True
    keep_review_markers_separate: bool = True


@dataclass(frozen=True)
class RevisionReviewItem:
    item_id: str
    kind: str
    source_text: str
    source: str = ""
    start: Optional[float] = None
    end: Optional[float] = None
    execution_required: bool = True
    evidence: Dict[str, Any] = field(default_factory=dict)
    validation: Dict[str, Any] = field(default_factory=dict)
    verbatim_status: str = "verified"
    execution_status: str = ""


@dataclass(frozen=True)
class AcceptanceRules:
    expected_review_item_count: Optional[int] = None
    expected_review_item_ids: List[str] = field(default_factory=list)
    require_review_items: bool = False
    require_execution_evidence: bool = True
    require_audio_validation: bool = False
    require_visual_evidence: bool = True
    require_pause_validation: bool = False
    require_subject_pointer_binding: bool = False
    require_pointer_lifecycle_evidence: bool = False
    require_final_acceptance: bool = False
    _explicit_require_execution_evidence: bool = False
    _explicit_require_audio_validation: bool = False
    _explicit_require_visual_evidence: bool = False
    _explicit_require_pause_validation: bool = False


@dataclass(frozen=True)
class RevisionRequest:
    project: RevisionProject
    edits: List[RevisionEdit]
    markers: List[RevisionMarker]
    preserve: PreservationRules
    review_items: List[RevisionReviewItem] = field(default_factory=list)
    acceptance: AcceptanceRules = field(default_factory=AcceptanceRules)
    processed_audio: Dict[str, Any] = field(default_factory=dict)
    pause_adjustments: List[PauseAdjustment] = field(default_factory=list)
    pause_alignment: Dict[str, Any] = field(default_factory=dict)
    audio_delivery_plan: AudioDeliveryPlan = field(default_factory=AudioDeliveryPlan)
    workflow_mode: str = "full"
    lite_cut_layout: str = "split_gap"


def _as_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc


def _as_finite_float(value: Any, field_name: str) -> float:
    result = _as_float(value, field_name)
    if not math.isfinite(result):
        raise ValueError(f"Invalid {field_name}: {value!r}")
    return result


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("Revision request root must be a JSON object.")
    return payload


def _load_json_any(path: str) -> Any:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}
_REVIEW_ID_PATTERN = re.compile(r"(修改|校对)\s*0*(\d+)", re.IGNORECASE)
_REPLACEMENT_GLYPH_PATTERN = re.compile(r"\?{2,}")
_POINTER_KEYWORDS = (
    "小手",
    "手指",
    "指向",
    "箭头",
    "下划线",
    "圈出",
    "圈选",
    "标注",
    "高亮",
    "放大镜",
    "hand",
    "pointer",
    "arrow",
    "underline",
)
_VISUAL_OBJECT_KEYWORDS = (
    "红圈",
    "绿圈",
    "红框",
    "绿框",
    "圈中文字",
    "圈中的文字",
    "圈内文字",
    "框中文字",
    "框中的文字",
    "框内文字",
)
_ANIMATION_KEYWORDS = (
    "动画",
    "翻页",
    "稳定帧",
    "入场",
    "出场",
    "铺色",
    "绿框",
    "橙色字",
    "直线",
)
_ANIMATION_ACTION_KEYWORDS = ("提前", "推迟", "延后", "加快", "变速", "挪", "移到", "改到", "调到")
_CONTENT_CHANGE_ACTION_KEYWORDS = (
    "改为",
    "改成",
    "修改为",
    "更正为",
    "替换为",
    "换成",
)
_VISUAL_CONTENT_KEYWORDS = (
    "画面内容",
    "画面文字",
    "图中文字",
    "图中数据",
    "文字内容",
    "数据",
    "数字",
    "日期",
    "年份",
    "公式",
    "标题",
    "标签",
)
_DELETE_KEYWORDS = ("删除", "删掉", "剪掉", "去掉", "移除")
_COLORED_TEXT_REFERENCE_KEYWORDS = (
    "蓝色字",
    "蓝字",
    "红色字",
    "红字",
    "标色字",
    "颜色字",
    "着色字",
)
_ELLIPSIS_MARKERS = ("…", "...", "。。。")
_EXECUTION_KEYWORDS = (
    "修改",
    "删除",
    "删掉",
    "剪掉",
    "去掉",
    "移除",
    "替换",
    "更换",
    "重做",
    "重新添加",
    "添加",
    "加小手",
    "加箭头",
    "提前",
    "推迟",
    "加快",
    "缩短",
    "延长",
    "调",
)
_AUDIO_KINDS = {
    "spoken_delete",
    "speech_delete",
    "audio_delete",
    "phrase_delete",
    "range_delete",
    "ellipsis_range_delete",
    "colored_span_delete",
    "gap_delete",
    "tail_cleanup",
    "tail_particle_delete",
    "pause_delete",
    "audio_repair",
    "replace_audio",
}
_LITE_ASR_TIMING_KINDS = _AUDIO_KINDS | {
    "breath_cleanup",
    "mouth_noise_cleanup",
    "noise_cleanup",
    "pause_timing_review",
    "pronunciation_repair",
    "semantic_pause_adjustment",
    "speech_repair",
}
_LITE_AUDIO_TIMING_TEXT_HINTS = (
    "audio",
    "breath",
    "mouth noise",
    "pause",
    "pronunciation",
    "speech",
    "voice",
    "口误",
    "呼吸",
    "咬字",
    "喷麦",
    "嘴音",
    "噪音",
    "停顿",
    "爆音",
    "语音",
    "语速",
    "读音",
    "音频",
)
_VISUAL_KINDS = {
    "pointer_overlay",
    "animation_timing",
    "visual_content_edit",
    "visual_delete",
    "visual_insert",
    "visual_overlay",
    "overlay",
}

_LITE_PAUSE_LABEL_ONLY_KINDS = {
    "gap_add",
    "gap_adjustment",
    "gap_delete",
    "gap_extend",
    "gap_remove",
    "gap_shorten",
    "page_turn",
    "pause_add",
    "pause_adjustment",
    "pause_delete",
    "pause_extend",
    "pause_extension",
    "pause_remove",
    "pause_shorten",
    "pause_timing_review",
    "semantic_pause_adjustment",
}
_LITE_LABEL_ONLY_KINDS = _LITE_PAUSE_LABEL_ONLY_KINDS | {
    "animation_timing",
    "page_turn",
    "release_boundary",
    "state_release",
    "state_reveal",
    "timing",
    "visual_content_edit",
}
_LITE_PAUSE_CHANGE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:增加|加长|延长|缩短|减少|减短|变长|变短|调整|修改).{0,16}(?:停顿|间隔|空白)",
        r"(?:停顿|间隔|空白).{0,16}(?:增加|加长|延长|缩短|减少|减短|变长|变短|调整|修改|删除|删掉|去掉|移除|取消|加\s*\+?\s*\d|减\s*\d)",
        r"(?:停顿|间隔|空白|pause|gap).{0,8}[+-]\s*\d+(?:\.\d+)?\s*(?:s|秒|秒钟)?",
        r"[+-]\s*\d+(?:\.\d+)?\s*(?:s|秒|秒钟)?\s*(?:的)?(?:停顿|间隔|空白|pause|gap)",
        r"(?:停顿|间隔|空白|pause|gap).{0,12}\d+(?:\.\d+)?\s*(?:s|秒|秒钟)",
        r"\d+(?:\.\d+)?\s*(?:s|秒|秒钟).{0,6}(?:的)?(?:停顿|间隔|空白|pause|gap)",
        r"(?:删除|删掉|去掉|移除|取消).{0,6}(?:这段|这个|该段|该)?(?:停顿|间隔|空白)",
        r"(?:add|increase|extend|shorten|reduce|remove|delete|adjust).{0,16}(?:pause|gap)",
        r"(?:pause|gap).{0,16}(?:add|increase|extend|shorten|reduce|remove|delete|adjust)",
    )
)
_LITE_POINTER_KINDS = {
    "add_arrow",
    "add_hand",
    "add_pointer",
    "arrow",
    "arrow_overlay",
    "circle",
    "circle_overlay",
    "hand",
    "hand_overlay",
    "hand_pointer",
    "magnifier",
    "magnifier_overlay",
    "pointer",
    "pointer_overlay",
    "underline",
    "underline_overlay",
}
_LITE_POINTER_CLEANUP_HINTS = (
    "clean-cover",
    "clean cover",
    "cleanup",
    "遮挡",
    "清理原小手",
    "清除原小手",
    "去掉原小手",
    "移除原小手",
    "盖住原小手",
    "覆盖原小手",
)
_LITE_POINTER_REMOVAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:删除|删掉|去掉|去除|移除|拿掉|清除|清理|取消).{0,16}?(?:小手|手指|指针|箭头|下划线|圈出|圈选|标注|高亮|放大镜)",
        r"(?:小手|手指|指针|箭头|下划线|圈出|圈选|标注|高亮|放大镜).{0,16}?(?:删除|删掉|去掉|去除|移除|拿掉|清除|清理|取消)",
        r"不要(?!\s*(?:删除|删掉|去掉|去除|移除|拿掉|清除|清理)).{0,12}?(?:小手|手指|指针|箭头|下划线|圈出|圈选|标注|高亮|放大镜)",
        r"(?:小手|手指|指针|箭头|下划线|圈出|圈选|标注|高亮|放大镜).{0,12}?不要(?:了|即可|就行)?",
        r"(?:删除|删掉|去掉|去除|移除|拿掉|清除|清理|取消)(?=\s*(?:后|$|[，,。；;]))",
        r"(?:remove|delete|clear).{0,16}?(?:hand|pointer|arrow|underline|circle|highlight|magnifier)",
        r"(?:hand|pointer|arrow|underline|circle|highlight|magnifier).{0,16}?(?:remove|delete|clear)",
    )
)
_LITE_POINTER_REINSERT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:重新|再)\s*(?:添加|新增|加|插入|放置|放入|贴上|贴入)\s*(?:一个)?\s*(?:新的?)?\s*(?:小手|手指|指针|箭头|下划线|圈出|圈选|标注|高亮|放大镜)",
        r"(?:重新|再)\s*(?:添加|新增|加|插入|放置|放入|贴上|贴入)(?:\s*一个)?(?:\s*新的?)?\s*(?=$|[，,。；;、])",
        r"(?:加回|放回|贴回|补回)",
        r"(?:添加|新增|加|插入|放置|放入|贴上|贴入)\s*(?:一个)?\s*(?:新的?)?\s*(?:小手|手指|指针|箭头|下划线|圈出|圈选|标注|高亮|放大镜)",
        r"(?:re-?add|add\s+back|put\s+back|reinsert).{0,12}(?:hand|pointer|arrow|underline|circle|highlight|magnifier)",
        r"(?:re-?add|add\s+back|put\s+back|reinsert)\s*(?=$|[,.;])",
        r"(?:then|after(?:wards)?|and)\s+(?:add|insert|place).{0,12}(?:hand|pointer|arrow|underline|circle|highlight|magnifier)",
    )
)
_LITE_DURATION_CHANGE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:视频|画面|镜头|片段|整体|时长).{0,16}(?:延长|缩短|增加|减少|加速|减速|变速|快放|慢放|补长)",
        r"(?:延长|缩短|增加|减少|加速|减速|变速|快放|慢放|补长).{0,16}(?:视频|画面|镜头|片段|整体|时长)",
        r"(?:插入|增加|补|删除|删掉|去掉).{0,10}\d+(?:\.\d+)?\s*(?:s|秒|秒钟).{0,8}(?:空白|画面|视频|时长|静帧|停留)?",
        r"(?:duration|freeze|hold|speed|time[ _-]?stretch|slow[ _-]?motion)",
    )
)
_LITE_PRECISION_DELETE_KINDS = {
    "audio_delete",
    "colored_span_delete",
    "ellipsis_range_delete",
    "phrase_delete",
    "range_delete",
    "speech_delete",
    "speech_tail_cleanup",
    "spoken_delete",
    "tail_cleanup",
    "tail_particle_delete",
}
_LITE_KNOWN_VISUAL_KINDS = _LITE_POINTER_KINDS | {
    "image_overlay",
    "overlay",
    "visual_insert",
    "visual_overlay",
    "visual_replace",
    "visual_content_edit",
}


def _lite_pointer_cleanup_suffix(source_text: str) -> Optional[str]:
    """Return text after the last explicit pointer cleanup, if present."""

    folded = str(source_text or "").casefold()
    removal_ends = [
        match.end()
        for pattern in _LITE_POINTER_REMOVAL_PATTERNS
        for match in pattern.finditer(folded)
    ]
    if removal_ends:
        return folded[max(removal_ends) :]

    hint_ends = []
    for hint in _LITE_POINTER_CLEANUP_HINTS:
        normalized_hint = hint.casefold()
        start = folded.rfind(normalized_hint)
        if start >= 0:
            hint_ends.append(start + len(normalized_hint))
    return folded[max(hint_ends) :] if hint_ends else None


def lite_execution_required(
    kind: str,
    source_text: str,
    requested: bool,
) -> bool:
    """Return the effective execution flag for the fixed Lite visual contract."""

    normalized_kind = str(kind or "").strip().casefold()
    if lite_duration_change_is_label_only(normalized_kind, source_text):
        return False
    if normalized_kind in _LITE_LABEL_ONLY_KINDS:
        return False
    if normalized_kind in _LITE_PRECISION_DELETE_KINDS:
        return bool(requested)
    if normalized_kind not in _LITE_KNOWN_VISUAL_KINDS:
        return False

    cleanup_suffix = _lite_pointer_cleanup_suffix(source_text)
    if cleanup_suffix is not None:
        reinsert_requested = any(
            pattern.search(cleanup_suffix) for pattern in _LITE_POINTER_REINSERT_PATTERNS
        )
        if not reinsert_requested:
            return False
    return bool(requested)


def lite_pause_change_is_label_only(kind: str, source_text: str) -> bool:
    """Return whether a Lite item requests a duration-changing pause edit."""

    normalized_kind = str(kind or "").strip().casefold()
    if normalized_kind in _LITE_PAUSE_LABEL_ONLY_KINDS:
        return True
    if any(token in normalized_kind for token in ("pause", "gap")) and any(
        action in normalized_kind
        for action in (
            "add",
            "adjust",
            "change",
            "delete",
            "extend",
            "extension",
            "increase",
            "remove",
            "shorten",
            "timing",
        )
    ):
        return True
    text = str(source_text or "")
    return any(pattern.search(text) for pattern in _LITE_PAUSE_CHANGE_PATTERNS)


def lite_duration_change_is_label_only(kind: str, source_text: str) -> bool:
    """Fail closed for every Lite duration edit except ASR-proved speech deletion."""

    normalized_kind = str(kind or "").strip().casefold()
    if lite_pause_change_is_label_only(normalized_kind, source_text):
        return True
    if any(
        token in normalized_kind
        for token in (
            "duration",
            "freeze",
            "hold",
            "slow_motion",
            "speed",
            "time_stretch",
        )
    ):
        return True
    return any(pattern.search(str(source_text or "")) for pattern in _LITE_DURATION_CHANGE_PATTERNS)


def _canonical_execution_status(value: Any) -> str:
    candidate = str(value or "").strip()
    normalized = re.sub(r"[\s-]+", "_", candidate).casefold()
    if normalized == "label_only":
        return "label_only_unresolved"
    if normalized.startswith("label_only_"):
        return normalized
    return candidate


def _nested_execution_statuses(value: Any, *, status_value: bool = False) -> Iterable[str]:
    """Yield execution statuses from arbitrarily nested JSON-shaped metadata."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized_key = re.sub(r"[^a-z0-9]+", "", str(key or "").casefold())
            if normalized_key == "executionstatus":
                yield from _nested_execution_statuses(nested, status_value=True)
            elif normalized_key == "status":
                for candidate in _nested_execution_statuses(nested, status_value=True):
                    if candidate.casefold().startswith("label_only_"):
                        yield candidate
            if isinstance(nested, (dict, list, tuple)):
                yield from _nested_execution_statuses(nested)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            yield from _nested_execution_statuses(nested, status_value=status_value)
        return
    if status_value and value is not None:
        candidate = _canonical_execution_status(value)
        if candidate:
            yield candidate


def resolve_execution_status(*sources: Any) -> str:
    """Prefer any nested label-only status, then the first ordinary status."""

    statuses: List[str] = []
    for source in sources:
        if isinstance(source, (Mapping, list, tuple)):
            statuses.extend(_nested_execution_statuses(source))
            continue
        statuses.extend(_nested_execution_statuses(source, status_value=True))
    normalized = list(dict.fromkeys(status for status in statuses if status))
    return next(
        (status for status in normalized if status.casefold().startswith("label_only_")),
        normalized[0] if normalized else "",
    )


def review_item_execution_status(item: RevisionReviewItem) -> str:
    """Read the authoritative internal execution status from one review item."""

    return resolve_execution_status(
        getattr(item, "execution_status", ""),
        getattr(item, "evidence", None),
        getattr(item, "validation", None),
    )


def lite_unresolved_timebase_status(item: Any) -> str:
    """Return an unresolved canonical timebase status carried by a Lite item."""

    if isinstance(item, Mapping):
        evidence = item.get("evidence")
    else:
        evidence = getattr(item, "evidence", None)
    if not isinstance(evidence, Mapping):
        return ""
    timebase = evidence.get("timebase")
    if not isinstance(timebase, Mapping):
        return ""
    status = str(timebase.get("status") or "").strip().casefold()
    return status if status.startswith("unresolved") else ""


def lite_review_item_execution_status(item: RevisionReviewItem) -> str:
    """Make an unresolved timebase an authoritative Lite marker-only state."""

    if lite_unresolved_timebase_status(item):
        return "label_only_unresolved"
    return review_item_execution_status(item)


def lite_review_item_execution_required(item: RevisionReviewItem) -> bool:
    """Apply internal label-only state and the fixed Lite kind policy."""

    if lite_review_item_execution_status(item).casefold().startswith("label_only_"):
        return False
    return lite_execution_required(
        item.kind,
        item.source_text,
        item.execution_required,
    )


def lite_timing_source(kind: str, source_text: str = "") -> str:
    """Choose the authoritative Lite timing source for one review item."""

    normalized_kind = str(kind or "").strip().casefold()
    if normalized_kind in _LITE_ASR_TIMING_KINDS or lite_pause_change_is_label_only(
        normalized_kind, source_text
    ):
        return "asr"
    folded = str(source_text or "").casefold()
    if any(hint.casefold() in folded for hint in _LITE_AUDIO_TIMING_TEXT_HINTS):
        return "asr"
    return "review_timestamp"


_VISUAL_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
_PASS_STATUSES = {
    "pass",
    "passed",
    "ok",
    "done",
    "complete",
    "completed",
    "executed",
    "validated",
    "pass_adjudicated",
}
_FAIL_STATUSES = {"fail", "failed", "error", "invalid", "unresolved"}
_AUDIO_VALIDATION_PASS_STATUSES = {
    "pass",
    "passed",
    "ok",
    "validated",
    "complete",
    "completed",
    "pass_adjudicated",
}
_SEMANTIC_JOIN_FORBIDDEN_PHRASES = (
    "\u9636\u6bb5\u6027\u3002\u6210\u5c31",  # 阶段性。成就
    "\u53d1\u53d1\u660e",  # 发发明
    "\u7985\u8ba9\u5236\u3002\u4e8e",  # 禅让制。于
    "\u5b83\u8bf4\u660e\u3002",  # 它说明。
    "\u590f\u671d\u3002\u7acb\u7684",  # 夏朝。立的
    "\u3002\u7acb\u7684\u3002",  # 。立的。
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def _optional_float(value: Any, field_name: str) -> Optional[float]:
    if value is None or value == "":
        return None
    return _as_float(value, field_name)


def _optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc


def _extract_review_id(text: str, fallback: str) -> str:
    match = _REVIEW_ID_PATTERN.search(text or "")
    if match:
        return f"{match.group(1)}{int(match.group(2)):02d}"
    return fallback


def _has_replacement_glyphs(value: Any) -> bool:
    return bool(_REPLACEMENT_GLYPH_PATTERN.search(str(value or "")))


def _clean_review_label(
    label: Any, *, doc_item_id: str = "", detail: str = "", fallback: str = ""
) -> str:
    raw = str(label or "").strip()
    candidate_id = _extract_review_id(f"{doc_item_id} {raw} {detail}", "").strip()
    if raw and not _has_replacement_glyphs(raw):
        return raw
    if candidate_id:
        return candidate_id
    return fallback or raw


def _review_id_track_token(review_id: str, fallback_idx: int = 0) -> str:
    match = _REVIEW_ID_PATTERN.search(str(review_id or ""))
    if match:
        prefix = "modify" if match.group(1) == "修改" else "check"
        return f"{prefix}{int(match.group(2)):02d}"
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(review_id or "")).strip("_")
    if cleaned:
        return cleaned
    return f"item{fallback_idx + 1:03d}"


def _clean_track_name(
    name: Any, *, item_id: str = "", fallback_idx: int = 0, role: str = ""
) -> str:
    raw = str(name or "").strip()
    if raw and not _has_replacement_glyphs(raw):
        return raw
    token = _review_id_track_token(item_id, fallback_idx)
    suffix = re.sub(r"[^A-Za-z0-9_-]+", "_", str(role or "")).strip("_")
    return f"Visual Overlay {token}" + (f" {suffix}" if suffix else "")


def _normalize_review_id(value: str) -> str:
    raw = str(value or "").strip()
    match = _REVIEW_ID_PATTERN.search(raw)
    if match:
        return f"{match.group(1)}{int(match.group(2)):02d}".lower()
    return re.sub(r"\s+", "", raw).lower()


def _fingerprint_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").lower(), flags=re.UNICODE)


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    return any(keyword.lower() in text.lower() for keyword in keywords)


def _classify_review_text(text: str) -> str:
    normalized = str(text or "")
    if _contains_any(normalized, _POINTER_KEYWORDS):
        return "pointer_overlay"
    if _contains_any(normalized, _VISUAL_OBJECT_KEYWORDS) and _contains_any(
        normalized, _ANIMATION_ACTION_KEYWORDS
    ):
        return "animation_timing"
    if _contains_any(normalized, _ANIMATION_KEYWORDS) and _contains_any(
        normalized, _ANIMATION_ACTION_KEYWORDS + _CONTENT_CHANGE_ACTION_KEYWORDS
    ):
        return "animation_timing"
    if _contains_any(normalized, _CONTENT_CHANGE_ACTION_KEYWORDS) and (
        _contains_any(normalized, _VISUAL_OBJECT_KEYWORDS)
        or _contains_any(normalized, _VISUAL_CONTENT_KEYWORDS)
    ):
        return "visual_content_edit"
    if _contains_any(normalized, _VISUAL_OBJECT_KEYWORDS) and _contains_any(
        normalized, _DELETE_KEYWORDS
    ):
        return "visual_delete"
    if (
        "中间" in normalized
        and _contains_any(normalized, ("停顿", "空白", "间隔"))
        and len(re.findall(r"[“「『\"].+?[”」』\"]", normalized)) >= 2
    ):
        return "gap_delete"
    if _contains_any(normalized, _DELETE_KEYWORDS) and _contains_any(
        normalized, ("停顿", "空白", "间隔")
    ):
        return "pause_delete"
    # A reviewer may paste the spoken sentence itself and omit the word
    # “删除”.  A timestamp plus quoted speech is an executable spoken edit,
    # not a marker-only note.  Ellipses mean the full spoken range between the
    # two anchors, while a quoted phrase without ellipsis is a phrase delete.
    # A plain-text reference to "blue/red text" does not prove whether the
    # reviewer means spoken words or text visible in the picture.  The review
    # compiler promotes this only when the Feishu rich-text runs carry the
    # actual review color.
    if _contains_any(normalized, _COLORED_TEXT_REFERENCE_KEYWORDS):
        return "review_only"
    if _contains_any(normalized, _ELLIPSIS_MARKERS) and _has_quoted_review_text(normalized):
        return "ellipsis_range_delete"
    if _has_quoted_review_text(normalized) and _has_review_time_hint(normalized):
        return "phrase_delete"
    if _contains_any(normalized, _DELETE_KEYWORDS):
        return "spoken_delete"
    # Unrecognized instructions are intentionally review-only in Lite. New
    # execution behavior must be added to the maintained allowlist explicitly.
    return "review_only"


def _has_review_time_hint(text: str) -> bool:
    return bool(re.search(r"\d{1,2}\s*[:：]\s*\d{1,2}", str(text or "")))


def _has_quoted_review_text(text: str) -> bool:
    return bool(re.search(r"[“「『\"].+?[”」』\"]", str(text or "")))


def _looks_execution_required(text: str, kind: str) -> bool:
    if kind == "review_only":
        return False
    if kind in _AUDIO_KINDS or kind in _VISUAL_KINDS:
        return True
    return _contains_any(text, _EXECUTION_KEYWORDS)


def _parse_review_item(item: Any, idx: int, field_name: str) -> RevisionReviewItem:
    if isinstance(item, str):
        source_text = item
        normalized_source_text = source_text.strip()
        item_id = _extract_review_id(normalized_source_text, f"item_{idx + 1:03d}")
        kind = _classify_review_text(normalized_source_text)
        return RevisionReviewItem(
            item_id=item_id,
            kind=kind,
            source_text=source_text,
            execution_required=_looks_execution_required(normalized_source_text, kind),
            verbatim_status="unverified_source_unavailable",
        )
    if not isinstance(item, dict):
        raise ValueError(f"{field_name}[{idx}] must be an object or string.")

    label = str(
        item.get("label") or item.get("id") or item.get("item_id") or item.get("clip_id") or ""
    ).strip()
    explicit_source_value = item.get("source_text")
    explicit_source_text = "" if explicit_source_value is None else str(explicit_source_value)
    has_explicit_source = "source_text" in item and bool(explicit_source_text.strip())
    if has_explicit_source:
        source_text = explicit_source_text
        default_verbatim_status = "verified"
    else:
        source_text = ""
        for candidate in (
            item.get("text"),
            item.get("detail"),
            item.get("comment"),
            item.get("label"),
        ):
            candidate_text = "" if candidate is None else str(candidate)
            if candidate_text.strip():
                source_text = candidate_text
                break
        default_verbatim_status = "unverified_source_unavailable"
    normalized_source_text = source_text.strip()
    item_id = str(item.get("id") or item.get("item_id") or item.get("clip_id") or "").strip()
    if not item_id:
        item_id = _extract_review_id(f"{label} {normalized_source_text}", f"item_{idx + 1:03d}")
    kind = str(item.get("kind") or item.get("type") or "").strip() or _classify_review_text(
        f"{label} {normalized_source_text}"
    )
    execution_required = (
        _as_bool(item.get("execution_required"), False)
        if "execution_required" in item
        else _looks_execution_required(f"{label} {normalized_source_text}", kind)
    )
    evidence = item.get("evidence") or {}
    validation = item.get("validation") or {}
    if not isinstance(evidence, dict):
        raise ValueError(f"{field_name}[{idx}].evidence must be an object when provided.")
    if not isinstance(validation, dict):
        raise ValueError(f"{field_name}[{idx}].validation must be an object when provided.")
    execution_status = resolve_execution_status(
        item.get("execution_status"),
        evidence,
        validation,
    )
    return RevisionReviewItem(
        item_id=item_id,
        kind=kind,
        source_text=source_text,
        source=str(item.get("source") or "").strip(),
        start=_optional_float(item.get("start"), f"{field_name}[{idx}].start"),
        end=_optional_float(item.get("end"), f"{field_name}[{idx}].end"),
        execution_required=execution_required,
        evidence=evidence,
        validation=validation,
        verbatim_status=(
            str(item.get("verbatim_status") or default_verbatim_status).strip()
            or default_verbatim_status
        ),
        execution_status=execution_status,
    )


def _parse_review_items_payload(payload: Any, field_name: str) -> List[RevisionReviewItem]:
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValueError(f"{field_name} must be a list.")
    return [_parse_review_item(item, idx, field_name) for idx, item in enumerate(payload)]


def _parse_acceptance_rules(payload: Any) -> AcceptanceRules:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("acceptance must be an object.")
    expected_ids_payload = (
        payload.get("expected_review_item_ids") or payload.get("expected_doc_item_ids") or []
    )
    if not isinstance(expected_ids_payload, list):
        raise ValueError("acceptance.expected_review_item_ids must be a list.")
    return AcceptanceRules(
        expected_review_item_count=_optional_int(
            payload.get("expected_review_item_count", payload.get("expected_doc_item_count")),
            "acceptance.expected_review_item_count",
        ),
        expected_review_item_ids=[
            str(item).strip() for item in expected_ids_payload if str(item).strip()
        ],
        require_review_items=_as_bool(payload.get("require_review_items"), False),
        require_execution_evidence=_as_bool(payload.get("require_execution_evidence"), True),
        require_audio_validation=_as_bool(payload.get("require_audio_validation"), False),
        require_visual_evidence=_as_bool(payload.get("require_visual_evidence"), True),
        require_pause_validation=_as_bool(payload.get("require_pause_validation"), False),
        require_subject_pointer_binding=_as_bool(
            payload.get("require_subject_pointer_binding"), False
        ),
        require_pointer_lifecycle_evidence=_as_bool(
            payload.get("require_pointer_lifecycle_evidence"), False
        ),
        require_final_acceptance=_as_bool(payload.get("require_final_acceptance"), False),
        _explicit_require_execution_evidence="require_execution_evidence" in payload,
        _explicit_require_audio_validation="require_audio_validation" in payload,
        _explicit_require_visual_evidence="require_visual_evidence" in payload,
        _explicit_require_pause_validation="require_pause_validation" in payload,
    )


def _parse_pause_adjustments(payload: Any) -> List[PauseAdjustment]:
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValueError("pause_adjustments must be a list.")
    adjustments: List[PauseAdjustment] = []
    for idx, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"pause_adjustments[{idx}] must be an object.")
        source_time = _as_finite_float(
            item.get("source_time", item.get("source_cut_end")),
            f"pause_adjustments[{idx}].source_time",
        )
        requested_source_time = _as_finite_float(
            item.get("requested_source_time", source_time),
            f"pause_adjustments[{idx}].requested_source_time",
        )
        frame_source_time_raw = item.get("frame_source_time", item.get("still_frame_source_time"))
        frame_source_time = (
            None
            if frame_source_time_raw is None
            else _as_finite_float(
                frame_source_time_raw,
                f"pause_adjustments[{idx}].frame_source_time",
            )
        )
        duration = _as_finite_float(item.get("duration"), f"pause_adjustments[{idx}].duration")
        frame_path = str(item.get("frame_path") or item.get("still_frame_path") or "").strip()
        if duration <= 0:
            raise ValueError(f"pause_adjustments[{idx}].duration must be positive.")
        if (
            source_time < 0
            or requested_source_time < 0
            or (frame_source_time is not None and frame_source_time < 0)
        ):
            raise ValueError(f"pause_adjustments[{idx}] timing values must be non-negative.")
        if not frame_path:
            raise ValueError(f"pause_adjustments[{idx}].frame_path is required.")
        adjustments.append(
            PauseAdjustment(
                item_id=str(item.get("item_id") or item.get("doc_item_id") or "").strip(),
                source_time=source_time,
                duration=duration,
                frame_path=frame_path,
                frame_sha256=str(item.get("frame_sha256") or "").strip().casefold(),
                reason=str(item.get("reason") or item.get("semantic_reason") or "").strip(),
                requested_source_time=requested_source_time,
                frame_source_time=(frame_source_time if frame_source_time is not None else None),
                boundary_evidence=(
                    dict(item.get("boundary_evidence"))
                    if isinstance(item.get("boundary_evidence"), dict)
                    else {}
                ),
            )
        )
    return sorted(adjustments, key=lambda item: (item.source_time, item.item_id))


def _parse_audio_delivery_plan(payload: Any) -> AudioDeliveryPlan:
    if payload is None:
        return AudioDeliveryPlan()
    if not isinstance(payload, dict):
        raise ValueError("audio_delivery_plan must be an object.")

    mode = str(payload.get("mode") or "legacy").strip().lower()
    if mode not in {"legacy", "segmented"}:
        raise ValueError(f"Unsupported audio_delivery_plan.mode: {mode!r}")
    max_ratio = _as_finite_float(
        payload.get("max_single_segment_ratio", 0.9),
        "audio_delivery_plan.max_single_segment_ratio",
    )
    if max_ratio <= 0 or max_ratio > 1:
        raise ValueError("audio_delivery_plan.max_single_segment_ratio must be > 0 and <= 1.")

    validation_paths_payload = payload.get("validation_only_audio_paths") or []
    if not isinstance(validation_paths_payload, list):
        raise ValueError("audio_delivery_plan.validation_only_audio_paths must be a list.")
    validation_paths = [str(path).strip() for path in validation_paths_payload if str(path).strip()]

    pending = _as_bool(payload.get("pending"), False)
    segments_payload = payload.get("segments") or []
    if not isinstance(segments_payload, list):
        raise ValueError("audio_delivery_plan.segments must be a list.")
    if mode == "segmented" and not segments_payload and not pending:
        raise ValueError("audio_delivery_plan.segments is required in segmented mode.")
    if pending and (mode != "segmented" or segments_payload):
        raise ValueError(
            "audio_delivery_plan.pending is only valid for an empty segmented compiler plan."
        )

    segments: List[AudioDeliverySegment] = []
    seen_ids: set[str] = set()
    allowed_roles = {"reference", "source", "replacement_video", "repair"}
    for idx, item in enumerate(segments_payload):
        if not isinstance(item, dict):
            raise ValueError(f"audio_delivery_plan.segments[{idx}] must be an object.")
        field_prefix = f"audio_delivery_plan.segments[{idx}]"
        segment_id = str(item.get("id") or item.get("segment_id") or "").strip()
        role = str(item.get("role") or "").strip().lower()
        asset_path = str(item.get("asset_path") or "").strip()
        track_name = str(item.get("track_name") or "").strip()
        if not segment_id:
            raise ValueError(f"{field_prefix}.id is required.")
        if segment_id in seen_ids:
            raise ValueError(f"Duplicate audio delivery segment id: {segment_id}")
        seen_ids.add(segment_id)
        if role not in allowed_roles:
            raise ValueError(f"{field_prefix}.role must be one of {sorted(allowed_roles)}.")
        if not asset_path:
            raise ValueError(f"{field_prefix}.asset_path is required.")
        if not track_name:
            raise ValueError(f"{field_prefix}.track_name is required.")

        source_start = _as_finite_float(
            item.get("source_start", 0.0), f"{field_prefix}.source_start"
        )
        timeline_start = _as_finite_float(
            item.get("timeline_start"), f"{field_prefix}.timeline_start"
        )
        duration = _as_finite_float(item.get("duration"), f"{field_prefix}.duration")
        volume = _as_finite_float(
            item.get("volume", 1.0),
            f"{field_prefix}.volume",
        )
        fade_in = _as_finite_float(item.get("fade_in", 0.0), f"{field_prefix}.fade_in")
        fade_out = _as_finite_float(item.get("fade_out", 0.0), f"{field_prefix}.fade_out")
        if source_start < 0 or timeline_start < 0 or volume < 0:
            raise ValueError(f"{field_prefix} times and volume must be non-negative.")
        # Legacy/full-workflow reference rows are muted by contract. Lite's
        # split-gap A2 lane opts in to an audible reference clip explicitly so
        # older callers remain strict and the saved draft can still be audited.
        if (
            role == "reference"
            and volume != 0.0
            and not _as_bool(payload.get("lite_a2_audible"), False)
        ):
            raise ValueError(f"{field_prefix} reference segment volume must be 0.")
        if duration <= 0:
            raise ValueError(f"{field_prefix}.duration must be positive.")
        if fade_in < 0 or fade_out < 0 or fade_in > duration or fade_out > duration:
            raise ValueError(f"{field_prefix} fades must be between 0 and duration.")

        segments.append(
            AudioDeliverySegment(
                segment_id=segment_id,
                role=role,
                asset_path=asset_path,
                track_name=track_name,
                source_start=source_start,
                timeline_start=timeline_start,
                duration=duration,
                volume=volume,
                fade_in=fade_in,
                fade_out=fade_out,
                doc_item_id=str(item.get("doc_item_id") or item.get("item_id") or "").strip(),
                reason=str(item.get("reason") or "").strip(),
            )
        )

    by_track: Dict[str, List[AudioDeliverySegment]] = {}
    for segment in segments:
        by_track.setdefault(segment.track_name, []).append(segment)
    for track_name, track_segments in by_track.items():
        ordered = sorted(track_segments, key=lambda item: (item.timeline_start, item.segment_id))
        for previous, current in zip(ordered, ordered[1:]):
            previous_end = previous.timeline_start + previous.duration
            if current.timeline_start < previous_end - 0.000001:
                raise ValueError(
                    f"audio_delivery_plan track {track_name!r} has overlapping segments "
                    f"{previous.segment_id!r} and {current.segment_id!r}."
                )

    validation_path_keys = {os.path.normcase(os.path.abspath(path)) for path in validation_paths}
    conflicting_segment_ids = [
        segment.segment_id
        for segment in segments
        if os.path.normcase(os.path.abspath(segment.asset_path)) in validation_path_keys
    ]
    if conflicting_segment_ids:
        raise ValueError(
            "audio_delivery_plan validation-only paths cannot be used as segment assets: "
            + ", ".join(conflicting_segment_ids)
        )

    return AudioDeliveryPlan(
        mode=mode,
        pending=pending,
        forbid_full_length_segments=_as_bool(
            payload.get("forbid_full_length_segments"), mode == "segmented"
        ),
        max_single_segment_ratio=max_ratio,
        validation_only_audio_paths=validation_paths,
        segments=segments,
    )


def _extract_processed_audio_path(payload: Dict[str, Any]) -> str:
    processed_audio = payload.get("processed_audio") or {}
    if not isinstance(processed_audio, dict):
        return ""
    for key in (
        "output_wav",
        "output_audio",
        "audio_path",
        "path",
        "processed_audio",
        "replacement_audio",
        "final_audio",
    ):
        value = str(processed_audio.get(key) or "").strip()
        if value:
            return value
    outputs = processed_audio.get("outputs")
    if isinstance(outputs, dict):
        for key in (
            "output_wav",
            "output_audio",
            "audio",
            "wav",
            "mp3",
            "processed_audio",
            "replacement_audio",
        ):
            value = str(outputs.get(key) or "").strip()
            if value:
                return value
    return ""


def load_review_items_json(path: str) -> List[RevisionReviewItem]:
    payload = _load_json_any(path)
    if isinstance(payload, list):
        return _parse_review_items_payload(payload, "doc_items")
    if not isinstance(payload, dict):
        raise ValueError("doc items JSON root must be an object or list.")
    items_payload = payload.get("review_items")
    if items_payload is None:
        items_payload = payload.get("doc_items")
    if items_payload is None:
        items_payload = payload.get("items")
    if items_payload is None:
        raise ValueError("doc items JSON must contain review_items, doc_items, or items.")
    return _parse_review_items_payload(items_payload, "doc_items")


def load_revision_request(path: str) -> RevisionRequest:
    payload = _load_json(path)
    audio_delivery_plan = _parse_audio_delivery_plan(payload.get("audio_delivery_plan"))

    project_payload = payload.get("project") or {}
    if not isinstance(project_payload, dict):
        raise ValueError("project must be an object.")
    draft_name = str(project_payload.get("draft_name") or "").strip()
    source_video = str(project_payload.get("source_video") or "").strip()
    if not draft_name:
        raise ValueError("project.draft_name is required.")
    if not source_video:
        raise ValueError("project.source_video is required.")

    replacement_audio = str(project_payload.get("replacement_audio") or "").strip()
    if not replacement_audio and audio_delivery_plan.mode != "segmented":
        replacement_audio = _extract_processed_audio_path(payload)

    media_duration_seconds = _as_finite_float(
        project_payload.get("media_duration_seconds", 0.0),
        "project.media_duration_seconds",
    )
    if media_duration_seconds < 0:
        raise ValueError("project.media_duration_seconds must be non-negative.")

    project = RevisionProject(
        draft_name=draft_name,
        source_video=source_video,
        source_audio=str(project_payload.get("source_audio") or "").strip(),
        replacement_audio=replacement_audio,
        project_key=str(project_payload.get("project_key") or "").strip(),
        media_duration_seconds=media_duration_seconds,
    )

    edits_payload = payload.get("edits") or []
    if not isinstance(edits_payload, list):
        raise ValueError("edits must be a list.")
    edits: List[RevisionEdit] = []
    for idx, item in enumerate(edits_payload):
        if not isinstance(item, dict):
            raise ValueError(f"edits[{idx}] must be an object.")
        op_type = str(item.get("type") or "").strip()
        if not op_type:
            raise ValueError(f"edits[{idx}].type is required.")
        audio_path = str(item.get("audio_path") or "").strip()
        if op_type == "replace_audio" and not (
            audio_path or str(project_payload.get("replacement_audio") or "").strip()
        ):
            raise ValueError(
                f"edits[{idx}].audio_path is required when project.replacement_audio is missing."
            )
        edits.append(
            RevisionEdit(
                op_type=op_type,
                start=_as_float(item.get("start"), f"edits[{idx}].start"),
                end=_as_float(item.get("end"), f"edits[{idx}].end"),
                label=_clean_review_label(
                    item.get("label"),
                    doc_item_id=str(
                        item.get("doc_item_id") or item.get("item_id") or item.get("id") or ""
                    ).strip(),
                    detail=str(item.get("detail") or "").strip(),
                    fallback=op_type,
                ),
                detail=str(item.get("detail") or "").strip(),
                audio_path=audio_path,
                doc_item_id=str(
                    item.get("doc_item_id") or item.get("item_id") or item.get("id") or ""
                ).strip(),
                source_kind=str(item.get("source_kind") or item.get("kind") or "").strip(),
                asset_paths=[
                    str(path).strip()
                    for path in (item.get("asset_paths") or item.get("assets") or [])
                    if str(path).strip()
                ],
                visual_plan=(
                    item.get("visual_plan") if isinstance(item.get("visual_plan"), dict) else {}
                ),
                evidence=item.get("evidence") if isinstance(item.get("evidence"), dict) else {},
                validation=(
                    item.get("validation") if isinstance(item.get("validation"), dict) else {}
                ),
            )
        )

    markers_payload = payload.get("markers") or []
    if not isinstance(markers_payload, list):
        raise ValueError("markers must be a list.")
    markers: List[RevisionMarker] = []
    for idx, item in enumerate(markers_payload):
        if not isinstance(item, dict):
            raise ValueError(f"markers[{idx}] must be an object.")
        doc_item_id = str(
            item.get("doc_item_id") or item.get("item_id") or item.get("id") or ""
        ).strip()
        markers.append(
            RevisionMarker(
                label=_clean_review_label(
                    item.get("label"),
                    doc_item_id=doc_item_id,
                    detail=str(item.get("detail") or "").strip(),
                    fallback="marker",
                ),
                start=_as_float(item.get("start"), f"markers[{idx}].start"),
                end=_as_float(item.get("end"), f"markers[{idx}].end"),
                detail=str(item.get("detail") or "").strip(),
                doc_item_id=doc_item_id,
            )
        )

    preserve_payload = payload.get("preserve") or {}
    if not isinstance(preserve_payload, dict):
        raise ValueError("preserve must be an object.")
    preserve = PreservationRules(
        source_video_material=bool(preserve_payload.get("source_video_material", True)),
        separated_audio_material=bool(preserve_payload.get("separated_audio_material", True)),
        replacement_audio_material=bool(preserve_payload.get("replacement_audio_material", True)),
        keep_cut_points=bool(preserve_payload.get("keep_cut_points", True)),
        keep_review_markers_separate=bool(
            preserve_payload.get("keep_review_markers_separate", True)
        ),
    )

    review_items_payload = payload.get("review_items")
    if review_items_payload is None:
        review_items_payload = payload.get("doc_items")
    review_items = _parse_review_items_payload(review_items_payload, "review_items")
    acceptance = _parse_acceptance_rules(payload.get("acceptance"))
    pause_adjustments = _parse_pause_adjustments(payload.get("pause_adjustments"))
    pause_alignment_payload = payload.get("pause_alignment") or {}
    if not isinstance(pause_alignment_payload, dict):
        raise ValueError("pause_alignment must be an object.")

    workflow_mode = str(payload.get("workflow_mode") or "full").strip().lower()
    if workflow_mode not in {"full", "lite"}:
        raise ValueError("workflow_mode must be either 'full' or 'lite'.")
    if workflow_mode == "lite":
        # Lite keeps visual assets executable, but never promotes the full
        # visual/pointer evidence flags into acceptance requirements.  Keep
        # the explicit markers for diagnostics while exposing the effective
        # lite contract in summaries and downstream request consumers.
        acceptance = replace(
            acceptance,
            require_visual_evidence=False,
            require_pause_validation=False,
            require_subject_pointer_binding=False,
            require_pointer_lifecycle_evidence=False,
        )
        review_items = [
            replace(
                item,
                execution_required=lite_review_item_execution_required(item),
                execution_status=lite_review_item_execution_status(item),
            )
            for item in review_items
        ]

    lite_cut_layout = (
        str(
            payload.get("lite_cut_layout")
            or (project_payload.get("lite_cut_layout") if isinstance(project_payload, dict) else "")
            or "split_gap"
        )
        .strip()
        .lower()
    )
    if lite_cut_layout not in {"split_gap", "copy"}:
        raise ValueError("lite_cut_layout must be either 'split_gap' or 'copy'.")

    return RevisionRequest(
        project=project,
        edits=edits,
        markers=markers,
        preserve=preserve,
        review_items=review_items,
        acceptance=acceptance,
        processed_audio=(
            payload.get("processed_audio")
            if isinstance(payload.get("processed_audio"), dict)
            else {}
        ),
        pause_adjustments=pause_adjustments,
        pause_alignment=dict(pause_alignment_payload),
        audio_delivery_plan=audio_delivery_plan,
        workflow_mode=workflow_mode,
        lite_cut_layout=lite_cut_layout,
    )


def build_revision_summary(
    request: RevisionRequest,
    doc_items: Optional[List[RevisionReviewItem]] = None,
) -> Dict[str, Any]:
    from utils.revision_markers import build_marker_plan

    delete_windows: List[List[float]] = []
    replacement_windows: List[List[float]] = []
    marker_plan = build_marker_plan(request, doc_items=doc_items)

    for edit in request.edits:
        if edit.op_type == "delete":
            delete_windows.append([edit.start, edit.end])
        elif edit.op_type == "replace_audio":
            replacement_windows.append([edit.start, edit.end])

    required_materials: List[str] = []
    if request.preserve.source_video_material:
        required_materials.append("source_video")
    if request.preserve.separated_audio_material:
        required_materials.append("source_audio")
    replacement_audio_paths = _replacement_audio_paths_for_request(request)
    if request.preserve.replacement_audio_material and replacement_audio_paths:
        required_materials.append("replacement_audio")

    required_tracks = ["video_track"]
    if request.preserve.separated_audio_material:
        required_tracks.append("source_audio_track")
    if request.audio_delivery_plan.mode == "segmented":
        required_tracks.append("audio_delivery_tracks")
    if request.workflow_mode != "lite" and (
        replacement_windows or _request_uses_full_track_replacement_audio(request)
    ):
        required_tracks.append("replacement_audio_track")
    if marker_plan:
        required_tracks.append("review_marker_tracks")

    required_materials = sorted(required_materials)

    return {
        "draft_name": request.project.draft_name,
        "project_key": request.project.project_key,
        "workflow_mode": request.workflow_mode,
        "edit_count": len(request.edits),
        "review_item_count": len(doc_items if doc_items is not None else request.review_items),
        "review_marker_count": len(marker_plan),
        "delete_windows": delete_windows,
        "replacement_windows": replacement_windows,
        "full_track_replacement_audio": _request_uses_full_track_replacement_audio(request),
        "audio_delivery": {
            "mode": request.audio_delivery_plan.mode,
            "pending": request.audio_delivery_plan.pending,
            "segment_count": len(request.audio_delivery_plan.segments),
            "track_names": sorted(
                {segment.track_name for segment in request.audio_delivery_plan.segments}
            ),
            "validation_only_audio_paths": list(
                request.audio_delivery_plan.validation_only_audio_paths
            ),
            "forbid_full_length_segments": (
                request.audio_delivery_plan.forbid_full_length_segments
            ),
            "max_single_segment_ratio": request.audio_delivery_plan.max_single_segment_ratio,
        },
        "required_materials": required_materials,
        "required_tracks": required_tracks,
        "acceptance": {
            "expected_review_item_count": request.acceptance.expected_review_item_count,
            "expected_review_item_ids": request.acceptance.expected_review_item_ids,
            "require_review_items": request.acceptance.require_review_items,
            "require_execution_evidence": request.acceptance.require_execution_evidence,
            "require_audio_validation": request.acceptance.require_audio_validation,
            "require_visual_evidence": request.acceptance.require_visual_evidence,
            "require_pause_validation": request.acceptance.require_pause_validation,
            "require_subject_pointer_binding": (request.acceptance.require_subject_pointer_binding),
            "require_pointer_lifecycle_evidence": (
                request.acceptance.require_pointer_lifecycle_evidence
            ),
            "require_final_acceptance": request.acceptance.require_final_acceptance,
        },
        "pause_adjustments": {
            "count": (0 if request.workflow_mode == "lite" else len(request.pause_adjustments)),
            "total_duration": (
                0.0
                if request.workflow_mode == "lite"
                else sum(item.duration for item in request.pause_adjustments)
            ),
            "label_only_count": (
                len(request.pause_adjustments) if request.workflow_mode == "lite" else 0
            ),
            "requested_total_duration": sum(item.duration for item in request.pause_adjustments),
            "item_ids": [item.item_id for item in request.pause_adjustments],
        },
        "pause_alignment": {
            "enabled": bool(request.pause_alignment),
            "source_asr_path": str(request.pause_alignment.get("source_asr_path") or ""),
            "semantic_gap_seconds": request.pause_alignment.get("semantic_gap_seconds", 0.8),
            "search_window_seconds": request.pause_alignment.get("search_window_seconds", 3.0),
        },
        "preservation": {
            "source_video_material": request.preserve.source_video_material,
            "separated_audio_material": request.preserve.separated_audio_material,
            "replacement_audio_material": request.preserve.replacement_audio_material,
            "keep_cut_points": request.preserve.keep_cut_points,
            "keep_review_markers_separate": request.preserve.keep_review_markers_separate,
        },
    }


def summarize_revision_request(path: str) -> Dict[str, Any]:
    request = load_revision_request(path)
    return build_revision_summary(request)


def _request_uses_full_track_replacement_audio(request: RevisionRequest) -> bool:
    if request.audio_delivery_plan.mode == "segmented":
        return False
    return bool(
        request.preserve.replacement_audio_material
        and request.project.replacement_audio
        and _collect_delete_windows(request)
        and not _collect_replacement_edits(request)
    )


def _replacement_audio_paths_for_request(request: RevisionRequest) -> List[str]:
    paths = [
        edit.audio_path or request.project.replacement_audio
        for edit in _collect_replacement_edits(request)
        if edit.audio_path or request.project.replacement_audio
    ]
    if _request_uses_full_track_replacement_audio(request):
        paths.append(request.project.replacement_audio)
    unique_paths: List[str] = []
    for path in paths:
        if path and path not in unique_paths:
            unique_paths.append(path)
    return unique_paths


def _normalize_windows(windows: List[List[float]]) -> List[List[float]]:
    normalized: List[List[float]] = []
    for start, end in sorted(windows, key=lambda item: (item[0], item[1])):
        bounded_start = max(0.0, float(start))
        bounded_end = max(bounded_start, float(end))
        if bounded_end - bounded_start <= 0.000001:
            continue
        if not normalized or bounded_start > normalized[-1][1] + 0.000001:
            normalized.append([bounded_start, bounded_end])
            continue
        normalized[-1][1] = max(normalized[-1][1], bounded_end)
    return normalized


def _collect_delete_windows(request: RevisionRequest) -> List[List[float]]:
    return _normalize_windows(
        [[edit.start, edit.end] for edit in request.edits if edit.op_type == "delete"]
    )


def _collect_replacement_edits(request: RevisionRequest) -> List[RevisionEdit]:
    return sorted(
        [
            edit
            for edit in request.edits
            if edit.op_type == "replace_audio" and edit.end > edit.start
        ],
        key=lambda item: (item.start, item.end),
    )


def _interval_is_deleted(start: float, end: float, delete_windows: List[List[float]]) -> bool:
    for delete_start, delete_end in delete_windows:
        if start >= delete_start - 0.000001 and end <= delete_end + 0.000001:
            return True
    return False


def _build_keep_windows(request: RevisionRequest, total_duration: float) -> List[List[float]]:
    delete_windows = _collect_delete_windows(request)
    replacement_edits = _collect_replacement_edits(request)
    split_points = {0.0, max(0.0, total_duration)}

    for start, end in delete_windows:
        split_points.add(min(total_duration, start))
        split_points.add(min(total_duration, end))
    for edit in replacement_edits:
        split_points.add(min(total_duration, max(0.0, edit.start)))
        split_points.add(min(total_duration, max(0.0, edit.end)))
    for adjustment in request.pause_adjustments:
        split_points.add(min(total_duration, max(0.0, adjustment.source_time)))

    ordered_points = sorted(split_points)
    keep_windows: List[List[float]] = []
    for idx in range(len(ordered_points) - 1):
        start = ordered_points[idx]
        end = ordered_points[idx + 1]
        if end - start <= 0.000001:
            continue
        if _interval_is_deleted(start, end, delete_windows):
            continue
        keep_windows.append([start, end])
    return keep_windows


def _visual_kind_for_edit(edit: RevisionEdit) -> str:
    return (edit.source_kind or edit.op_type or "").strip()


def _is_visual_edit(edit: RevisionEdit) -> bool:
    return _visual_kind_for_edit(edit) in _VISUAL_KINDS or edit.op_type in _VISUAL_KINDS


def _edit_review_id(edit: RevisionEdit, fallback_idx: int = 0) -> str:
    if edit.doc_item_id:
        return edit.doc_item_id
    return _extract_review_id(
        f"{edit.label} {edit.detail}",
        f"visual_{fallback_idx + 1:03d}",
    )


def _visual_plan_segments(edit: RevisionEdit) -> List[Dict[str, Any]]:
    plan = edit.visual_plan if isinstance(edit.visual_plan, dict) else {}
    raw_segments = plan.get("segments")
    if isinstance(raw_segments, list) and raw_segments:
        return [segment for segment in raw_segments if isinstance(segment, dict)]

    asset_paths = list(edit.asset_paths)
    if not asset_paths:
        evidence_paths = edit.evidence.get("asset_paths")
        if isinstance(evidence_paths, list):
            asset_paths = [str(path).strip() for path in evidence_paths if str(path).strip()]
        elif edit.evidence.get("asset_path"):
            asset_paths = [str(edit.evidence["asset_path"]).strip()]
    if not asset_paths:
        return []

    kind = _visual_kind_for_edit(edit)
    if kind == "pointer_overlay" and len(asset_paths) > 1:
        asset_path = asset_paths[-1]
        role = "pointer_asset"
        scale_x = scale_y = 0.22
    else:
        asset_path = asset_paths[0]
        role = "visual_overlay"
        scale_x = scale_y = 1.0

    return [
        {
            "role": role,
            "asset_path": asset_path,
            "track_name": _clean_track_name(
                "",
                item_id=_edit_review_id(edit),
                fallback_idx=0,
                role=role,
            ),
            "source_start": edit.start,
            "duration": max(0.8, edit.end - edit.start),
            "scale_x": scale_x,
            "scale_y": scale_y,
            "transform_x": 0.0,
            "transform_y": 0.0,
            "alpha": 1.0,
        }
    ]
