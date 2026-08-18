import argparse
import os
from typing import Dict, List, Optional

from _bootstrap import ensure_skill_scripts_on_path

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
JY_EDITOR_ROOT, JY_SCRIPT_DIR = ensure_skill_scripts_on_path(PROJECT_ROOT)
SKILLS_ROOT = os.path.dirname(JY_EDITOR_ROOT)
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(SKILLS_ROOT))

from jy_wrapper import JyProject, draft  # noqa: E402


def parse_srt_time(time_str: str) -> float:
    parts = time_str.replace(",", ".").split(":")
    if len(parts) != 3:
        return 0.0
    h, m, s = map(float, parts)
    return h * 3600 + m * 60 + s


def parse_srt_content(content: str) -> List[Dict]:
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in content.split("\n")]
    items: List[Dict] = []
    i = 0
    while i < len(lines):
        if lines[i].isdigit() and i + 1 < len(lines) and "-->" in lines[i + 1]:
            idx = int(lines[i])
            t1, t2 = [x.strip() for x in lines[i + 1].split("-->")]
            start = parse_srt_time(t1)
            end = parse_srt_time(t2)
            j = i + 2
            text_parts: List[str] = []
            while j < len(lines) and lines[j] and not lines[j].isdigit():
                text_parts.append(lines[j])
                j += 1
            items.append(
                {
                    "index": idx,
                    "start": start,
                    "end": end,
                    "duration": max(0.1, end - start),
                    "text": " ".join(text_parts).strip(),
                }
            )
            i = j
        else:
            i += 1
    return items


def collect_materials(material_inputs: List[str]) -> List[Dict]:
    paths: List[str] = []
    for source in material_inputs:
        p = os.path.abspath(source)
        if os.path.isfile(p) and p.lower().endswith((".mp4", ".mov")):
            paths.append(p)
        elif os.path.isdir(p):
            for root, _, files in os.walk(p):
                for name in files:
                    if name.lower().endswith((".mp4", ".mov")):
                        paths.append(os.path.join(root, name))

    out: List[Dict] = []
    for p in paths:
        try:
            dur = draft.VideoMaterial(p).duration / 1_000_000.0
        except Exception:
            dur = 0.0
        out.append({"path": p, "filename": os.path.basename(p), "duration": max(0.1, dur)})
    return out


def _deterministic_matches(subs: List[Dict], materials: List[Dict]) -> List[Dict]:
    if not subs or not materials:
        return []
    return [
        {"srt_idx": item["index"], "id": index % len(materials)} for index, item in enumerate(subs)
    ]


def match_subtitles(
    subs: List[Dict],
    materials: List[Dict],
    *,
    provider: str | None = None,
) -> dict[str, object]:
    """Match subtitles while exposing whether an external provider actually ran."""
    requested_provider = (
        provider or os.environ.get("AUTO_CUT_MATCH_PROVIDER") or "deterministic"
    ).strip()
    matches = _deterministic_matches(subs, materials)
    if requested_provider.casefold() in {"", "deterministic", "round_robin"}:
        return {
            "status": "degraded",
            "provider": "deterministic_round_robin",
            "matches": matches,
            "reason": "No attributable external subtitle-matching provider is bundled.",
        }
    return {
        "status": "degraded",
        "provider": "deterministic_round_robin",
        "requested_provider": requested_provider,
        "matches": matches,
        "reason": "Requested provider is not installed or verified on this computer.",
    }


def ai_match_subtitles(subs: List[Dict], materials: List[Dict]) -> List[Dict]:
    """Backward-compatible list API; callers should prefer match_subtitles()."""
    return list(match_subtitles(subs, materials)["matches"])


def assemble_project(
    project_name: str,
    main_video: Optional[str],
    subtitles: List[Dict],
    materials: List[Dict],
    matches: List[Dict],
) -> None:
    project = JyProject(project_name, overwrite=True)
    if main_video and os.path.exists(main_video):
        project.add_media_safe(main_video, start_time="0s", track_name="VideoTrack")

    sub_map = {s["index"]: s for s in subtitles}
    for item in matches:
        sidx = item.get("srt_idx")
        mid = item.get("id")
        if sidx not in sub_map:
            continue
        if not isinstance(mid, int) or mid < 0 or mid >= len(materials):
            continue
        sub = sub_map[sidx]
        mat = materials[mid]
        project.add_media_safe(
            mat["path"],
            start_time=f'{sub["start"]}s',
            duration=f'{min(sub["duration"], mat["duration"])}s',
            track_name="B_Roll",
        )

    project.save()


def load_srt(srt_path: str) -> List[Dict]:
    with open(srt_path, "r", encoding="utf-8") as f:
        return parse_srt_content(f.read())


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe+match demo for JianYing.")
    parser.add_argument("--video", type=str, default=None, help="Main video path")
    parser.add_argument("--srt", type=str, required=True, help="Subtitle SRT path")
    parser.add_argument(
        "--materials", type=str, nargs="+", required=True, help="Material folders/files"
    )
    parser.add_argument(
        "--project", type=str, default="AI_Auto_Edit_Project", help="Draft project name"
    )
    args = parser.parse_args()

    srt_path = os.path.abspath(args.srt)
    if not os.path.exists(srt_path):
        raise FileNotFoundError(f"SRT not found: {srt_path}")

    subtitles = load_srt(srt_path)
    materials = collect_materials(args.materials)
    match_result = match_subtitles(subtitles, materials)
    matches = match_result["matches"]
    main_video = os.path.abspath(args.video) if args.video else None
    assemble_project(args.project, main_video, subtitles, materials, matches)
    print(f"Done. Draft created: {args.project}")
    print(f"Subtitle matching: {match_result['status']} ({match_result['provider']})")
    print(f"Workspace root: {WORKSPACE_ROOT}")


if __name__ == "__main__":
    main()
