import argparse


def _add_project_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", required=True, help="Draft name")
    parser.add_argument("--drafts-root", default=None, help="Override drafts root")
    parser.add_argument(
        "--recipient-drafts-root",
        default=None,
        help="First-use guard: exact currentCustomDraftPath expected on the recipient computer",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON response")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="High-level JianYing wrapper and draft mutation CLI."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_detect_env = sub.add_parser(
        "detect-env", help="Detect local JianYing/CapCut install and draft environment"
    )
    p_detect_env.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_root_check = sub.add_parser(
        "draft-root-check",
        help="Check that the recipient JianYing custom draft root matches the source path",
    )
    p_root_check.add_argument(
        "--expected-root",
        required=True,
        help="Exact source-machine currentCustomDraftPath to use on the recipient computer",
    )
    p_root_check.add_argument(
        "--configured-root",
        default=None,
        help="Optional already-read recipient path (otherwise read JianYing globalSetting)",
    )
    p_root_check.add_argument(
        "--local-app-data",
        default=None,
        help="Optional LOCALAPPDATA override for the recipient setting file",
    )
    p_root_check.add_argument("--json", action="store_true")

    p_mirror = sub.add_parser(
        "draft-mirror-deliver",
        help="Validate and copy one editable JianYing draft tree unchanged",
    )
    p_mirror.add_argument("--name", required=True, help="Exact draft directory name")
    p_mirror.add_argument(
        "--drafts-root",
        "--source-drafts-root",
        dest="drafts_root",
        default=None,
        help="Source JianYing custom draft root; defaults to strict currentCustomDraftPath",
    )
    p_mirror.add_argument(
        "--desktop-root",
        required=True,
        help="Existing destination root (normally the recipient-transfer Desktop folder)",
    )
    p_mirror.add_argument("--receipt-json", required=True)
    p_mirror.add_argument("--job-input-digest", required=True)
    p_mirror.add_argument(
        "--recipient-drafts-root",
        default=None,
        help="Expected identical currentCustomDraftPath on the recipient computer",
    )
    p_mirror.add_argument("--configured-recipient-root", default=None)
    p_mirror.add_argument("--local-app-data", default=None)
    p_mirror.add_argument("--quiet-window-seconds", type=float, default=6.0)
    p_mirror.add_argument("--poll-interval-seconds", type=float, default=1.0)
    p_mirror.add_argument("--stability-timeout-seconds", type=float, default=120.0)
    p_mirror.add_argument("--stability-retries", type=int, default=2)
    p_mirror.add_argument("--json", action="store_true")

    p_smoke = sub.add_parser("smoke-test", help="Create a draft and probe the live JianYing window")
    p_smoke.add_argument("--name", default=None, help="Draft name to use for smoke test")
    p_smoke.add_argument("--drafts-root", default=None, help="Override drafts root")
    p_smoke.add_argument(
        "--cleanup", action="store_true", help="Remove the temporary draft after probing"
    )
    p_smoke.add_argument(
        "--allow-launch", action="store_true", help="Reserved for future app launch support"
    )
    p_smoke.add_argument("--json", action="store_true", help="Print machine-readable JSON response")

    p_self_check = sub.add_parser(
        "self-check", help="Run environment detection plus smoke test and summarize usability"
    )
    p_self_check.add_argument("--name", default=None, help="Draft name to use for smoke test")
    p_self_check.add_argument("--drafts-root", default=None, help="Override drafts root")
    p_self_check.add_argument(
        "--cleanup", action="store_true", help="Remove the temporary draft after probing"
    )
    p_self_check.add_argument(
        "--allow-launch", action="store_true", help="Reserved for future app launch support"
    )
    p_self_check.add_argument(
        "--refresh", action="store_true", help="Refresh cached environment detection first"
    )
    p_self_check.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_search = sub.add_parser("search-assets", help="Search indexed draft assets")
    p_search.add_argument("query", nargs="?", default=None, help="Search query")
    p_search.add_argument("--category", default=None, help="Asset category filter")
    p_search.add_argument("--limit", type=int, default=20, help="Max results to return")
    p_search.add_argument(
        "--list-categories", action="store_true", help="List indexed asset categories"
    )
    p_search.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_sync_favorites = sub.add_parser(
        "sync-favorite-text-assets",
        help="Sync local JianYing favorite/cached text templates and flower text assets into local CSV indexes",
    )
    p_sync_favorites.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_resolve = sub.add_parser(
        "resolve-asset", help="Resolve one best-match asset for direct writing"
    )
    p_resolve.add_argument("query", help="Search query")
    p_resolve.add_argument("--category", default=None, help="Asset category filter")
    p_resolve.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_revision_summary = sub.add_parser(
        "revision-summary",
        help="Summarize a standardized review-revision request JSON",
    )
    p_revision_summary.add_argument(
        "--request-json",
        required=True,
        help="Path to a standardized revision request JSON file",
    )
    p_revision_summary.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_revision_bind_audio = sub.add_parser(
        "revision-bind-audio-report",
        help="Bind a reverse-ASR report to the normalized segmented audio delivery plan",
    )
    p_revision_bind_audio.add_argument(
        "--request-json",
        required=True,
        help="Path to the normalized revision request JSON file",
    )
    p_revision_bind_audio.add_argument(
        "--report-json",
        required=True,
        help="Path to the raw reverse-ASR report JSON file",
    )
    p_revision_bind_audio.add_argument(
        "--output-json",
        required=True,
        help="Path for the plan-bound reverse-ASR report JSON file",
    )
    p_revision_bind_audio.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_revision_validate = sub.add_parser(
        "revision-validate",
        help="Validate a standardized review-revision request JSON",
    )
    p_revision_validate.add_argument(
        "--request-json",
        required=True,
        help="Path to a standardized revision request JSON file",
    )
    p_revision_validate.add_argument(
        "--doc-items-json",
        default=None,
        help="Optional JSON file containing review_items/doc_items parsed directly from the source document",
    )
    p_revision_validate.add_argument(
        "--strict",
        action="store_true",
        help="Run item-level acceptance validation in addition to editable-draft structure checks",
    )
    p_revision_validate.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_revision_run = sub.add_parser(
        "revision-run",
        help="Execute a standardized review-revision request and build a reviewable draft",
    )
    p_revision_run.add_argument(
        "--request-json",
        required=True,
        help="Path to a standardized revision request JSON file",
    )
    p_revision_run.add_argument("--drafts-root", default=None, help="Override drafts root")
    p_revision_run.add_argument(
        "--recipient-drafts-root",
        default=None,
        help="First-use guard: exact currentCustomDraftPath expected on the recipient computer",
    )
    p_revision_run.add_argument(
        "--doc-items-json",
        default=None,
        help="Optional JSON file containing review_items/doc_items parsed directly from the source document",
    )
    p_revision_run.add_argument(
        "--strict",
        action="store_true",
        help="Run item-level acceptance validation after writing the draft",
    )
    p_revision_run.add_argument(
        "--mock-media",
        action="store_true",
        help="Use mock media material objects for dry-run or tests",
    )
    p_revision_run.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_review_job_compile = sub.add_parser(
        "review-job-compile",
        help="Compile canonical review-job inputs and a local evidence-window plan",
    )
    p_review_job_compile.add_argument("--snapshot-json", required=True)
    p_review_job_compile.add_argument("--project-json", required=True)
    p_review_job_compile.add_argument("--output-dir", required=True)
    p_review_job_compile.add_argument("--context-before", type=float, default=5.0)
    p_review_job_compile.add_argument("--context-after", type=float, default=5.0)
    p_review_job_compile.add_argument("--full-preview", action="store_true")
    p_review_job_compile.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_review_job_status = sub.add_parser(
        "review-job-status", help="Read resumable review-job state without mutating it"
    )
    p_review_job_status.add_argument("--state-json", required=True)
    p_review_job_status.add_argument("--input-digest", default=None)
    p_review_job_status.add_argument("--tool-version", default=None)
    p_review_job_status.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_review_job_wait_open = sub.add_parser(
        "review-job-wait-open",
        help="Persist a resumable subject-pointer user-action wait",
    )
    p_review_job_wait_open.add_argument("--state-json", required=True)
    p_review_job_wait_open.add_argument("--wait-json", required=True)
    p_review_job_wait_open.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_review_job_wait_resolve = sub.add_parser(
        "review-job-wait-resolve",
        help="Resolve a subject-pointer user-action wait for the current project",
    )
    p_review_job_wait_resolve.add_argument("--state-json", required=True)
    p_review_job_wait_resolve.add_argument("--input-digest", required=True)
    p_review_job_wait_resolve.add_argument("--artifact-sha256", required=True)
    p_review_job_wait_resolve.add_argument("--project-key", required=True)
    p_review_job_wait_resolve.add_argument("--draft-path", required=True)
    p_review_job_wait_resolve.add_argument("--draft-root", required=True)
    p_review_job_wait_resolve.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_review_job_cache = sub.add_parser(
        "review-job-cache-inspect", help="Read-only validation of one review-job cache entry"
    )
    p_review_job_cache.add_argument("--cache-root", required=True)
    p_review_job_cache.add_argument("--namespace", required=True)
    p_review_job_cache.add_argument("--digest", required=True)
    p_review_job_cache.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON response"
    )

    p_oral_validate = sub.add_parser(
        "validate-oral-video-draft",
        help="Run the repository oral-video post-save validation against a saved draft",
    )
    _add_project_args(p_oral_validate)
    p_oral_validate.add_argument(
        "--style",
        default="basic-oral-video",
        help="Validation preset to use; currently supports basic-oral-video",
    )

    p_list_segments = sub.add_parser("list-segments", help="List all segment ids in a draft")
    _add_project_args(p_list_segments)

    p_draft_info = sub.add_parser(
        "draft-info", help="Show draft overview, track counts, and material counts"
    )
    _add_project_args(p_draft_info)

    p_list_materials = sub.add_parser(
        "list-materials", help="List material buckets or the items in one bucket"
    )
    _add_project_args(p_list_materials)
    p_list_materials.add_argument(
        "--type", dest="bucket", default=None, help="Optional material bucket name"
    )

    p_show_material = sub.add_parser("show-material", help="Show one material by id")
    _add_project_args(p_show_material)
    p_show_material.add_argument("--material-id", required=True, help="Material id")

    p_list_tracks = sub.add_parser("list-tracks", help="List tracks in a draft")
    _add_project_args(p_list_tracks)

    p_list_texts = sub.add_parser("list-texts", help="List text segments and resolved caption text")
    _add_project_args(p_list_texts)

    p_show_segment = sub.add_parser(
        "show-segment", help="Show one segment with track and material context"
    )
    _add_project_args(p_show_segment)
    p_show_segment.add_argument("--segment-id", required=True, help="Segment id")

    p_sticker = sub.add_parser("add-sticker", help="Add a sticker segment to a draft")
    _add_project_args(p_sticker)
    p_sticker.add_argument("--sticker-id", default=None, help="Sticker resource id")
    p_sticker.add_argument(
        "--sticker-query", default=None, help="Resolve sticker id from asset search"
    )
    p_sticker.add_argument("--start-time", default=None, help="Segment start time")
    p_sticker.add_argument("--duration", default="3s", help="Segment duration")
    p_sticker.add_argument("--track-name", default="StickerTrack", help="Target track name")
    p_sticker.add_argument("--scale", type=float, default=1.0, help="Uniform scale")
    p_sticker.add_argument("--transform-x", type=float, default=0.0, help="Horizontal offset in px")
    p_sticker.add_argument("--transform-y", type=float, default=0.0, help="Vertical offset in px")
    p_sticker.add_argument("--rotation", type=float, default=0.0, help="Rotation in degrees")
    p_sticker.add_argument("--alpha", type=float, default=1.0, help="Opacity from 0 to 1")

    p_mask = sub.add_parser("add-mask", help="Attach a mask to an existing segment")
    _add_project_args(p_mask)
    p_mask.add_argument("--segment-id", required=True, help="Target segment id")
    p_mask.add_argument("--mask-type", default=None, help="Mask type, such as circle or rectangle")
    p_mask.add_argument("--mask-query", default=None, help="Resolve mask type from asset search")
    p_mask.add_argument("--center-x", type=float, default=0.0, help="Mask center X")
    p_mask.add_argument("--center-y", type=float, default=0.0, help="Mask center Y")
    p_mask.add_argument("--size", type=float, default=0.5, help="Mask size")
    p_mask.add_argument("--rotation", type=float, default=0.0, help="Mask rotation")
    p_mask.add_argument("--feather", type=float, default=0.0, help="Mask feather")
    p_mask.add_argument("--invert", action="store_true", help="Invert mask")
    p_mask.add_argument("--rect-width", type=float, default=None, help="Rectangle mask width")
    p_mask.add_argument(
        "--round-corner", type=float, default=None, help="Rectangle mask corner radius"
    )

    p_rich = sub.add_parser("add-rich-text", help="Add rich text with keyword highlighting")
    _add_project_args(p_rich)
    p_rich.add_argument("--text", required=True, help="Caption text")
    p_rich.add_argument("--start-time", default=None, help="Segment start time")
    p_rich.add_argument("--duration", default="3s", help="Segment duration")
    p_rich.add_argument("--track-name", default="Subtitles", help="Target track name")
    p_rich.add_argument("--text-color", default="#ffffff", help="Base text color")
    p_rich.add_argument("--border-color", default=None, help="Base border color")
    p_rich.add_argument("--font", default=None, help="Font enum or synonym")
    p_rich.add_argument("--font-query", default=None, help="Resolve font from asset search")
    p_rich.add_argument("--font-size", type=float, default=5.0, help="Base font size")
    p_rich.add_argument("--bold", action="store_true", help="Use bold text")
    p_rich.add_argument("--italic", action="store_true", help="Use italic text")
    p_rich.add_argument("--underline", action="store_true", help="Underline the text")
    p_rich.add_argument("--transform-x", type=float, default=0.0, help="Horizontal offset in px")
    p_rich.add_argument("--transform-y", type=float, default=-0.8, help="Vertical transform ratio")
    p_rich.add_argument("--scale-x", type=float, default=1.0, help="Horizontal scale")
    p_rich.add_argument("--scale-y", type=float, default=1.0, help="Vertical scale")
    p_rich.add_argument(
        "--keyword", default=None, help="Keyword or pipe-delimited keywords to highlight"
    )
    p_rich.add_argument("--keyword-color", default="#ff7100", help="Highlight color")
    p_rich.add_argument("--keyword-font-size", type=float, default=None, help="Highlight font size")
    p_rich.add_argument("--keyword-border-color", default=None, help="Highlight border color")
    p_rich.add_argument("--shadow-json", default=None, help="JSON object for shadow settings")
    p_rich.add_argument("--text-effect", default=None, help="Optional text effect")
    p_rich.add_argument(
        "--text-effect-query", default=None, help="Resolve text effect from asset search"
    )
    p_rich.add_argument("--anim-in", default=None, help="Intro animation")
    p_rich.add_argument(
        "--anim-in-query", default=None, help="Resolve intro animation from asset search"
    )
    p_rich.add_argument("--anim-out", default=None, help="Outro animation")
    p_rich.add_argument(
        "--anim-out-query", default=None, help="Resolve outro animation from asset search"
    )
    p_rich.add_argument("--anim-loop", default=None, help="Loop animation")
    p_rich.add_argument(
        "--anim-loop-query", default=None, help="Resolve loop animation from asset search"
    )

    p_keyframes = sub.add_parser("add-keyframes", help="Apply high-level keyframes to a segment")
    _add_project_args(p_keyframes)
    p_keyframes.add_argument("--segment-id", required=True, help="Target segment id")
    p_keyframes.add_argument(
        "--keyframes-json",
        required=True,
        help='JSON array, e.g. [{"property":"position_x","offset":"0.5s","value":0.2}]',
    )

    p_filter = sub.add_parser("add-filter", help="Add a filter track segment to a draft")
    _add_project_args(p_filter)
    p_filter.add_argument("--filter-name", default=None, help="Filter enum name")
    p_filter.add_argument("--filter-query", default=None, help="Resolve filter from asset search")
    p_filter.add_argument("--start-time", default=None, help="Segment start time")
    p_filter.add_argument("--duration", default="3s", help="Segment duration")
    p_filter.add_argument("--track-name", default="FilterTrack", help="Target track name")
    p_filter.add_argument(
        "--intensity", type=float, default=100.0, help="Filter intensity from 0 to 100"
    )

    p_image = sub.add_parser("add-image", help="Add an image segment to a draft")
    _add_project_args(p_image)
    p_image.add_argument("--image-path", required=True, help="Local image path")
    p_image.add_argument("--start-time", default=None, help="Segment start time")
    p_image.add_argument("--duration", default="3s", help="Segment duration")
    p_image.add_argument("--track-name", default="ImageTrack", help="Target track name")
    p_image.add_argument("--scale-x", type=float, default=1.0, help="Horizontal scale")
    p_image.add_argument("--scale-y", type=float, default=1.0, help="Vertical scale")
    p_image.add_argument("--transform-x", type=float, default=0.0, help="Horizontal offset in px")
    p_image.add_argument("--transform-y", type=float, default=0.0, help="Vertical offset in px")
    p_image.add_argument("--rotation", type=float, default=0.0, help="Rotation in degrees")
    p_image.add_argument("--alpha", type=float, default=1.0, help="Opacity from 0 to 1")
    p_image.add_argument(
        "--background-blur", type=int, default=None, help="Background blur level 1-4"
    )

    p_effect = sub.add_parser(
        "add-effect", help="Add a scene or character effect segment to a draft"
    )
    _add_project_args(p_effect)
    p_effect.add_argument("--effect-name", default=None, help="Effect enum name")
    p_effect.add_argument("--effect-query", default=None, help="Resolve effect from asset search")
    p_effect.add_argument(
        "--effect-category",
        default="scene",
        choices=["scene", "character"],
        help="Effect category",
    )
    p_effect.add_argument("--start-time", default=None, help="Segment start time")
    p_effect.add_argument("--duration", default="3s", help="Segment duration")
    p_effect.add_argument("--track-name", default="EffectTrack", help="Target track name")

    p_video_effect = sub.add_parser(
        "add-video-effect", help="Add a scene video effect segment to a draft"
    )
    _add_project_args(p_video_effect)
    p_video_effect.add_argument("--effect-name", default=None, help="Effect enum name")
    p_video_effect.add_argument(
        "--effect-query", default=None, help="Resolve effect from asset search"
    )
    p_video_effect.add_argument("--start-time", default=None, help="Segment start time")
    p_video_effect.add_argument("--duration", default="3s", help="Segment duration")
    p_video_effect.add_argument("--track-name", default="EffectTrack", help="Target track name")

    p_face_effect = sub.add_parser("add-face-effect", help="Add a face effect segment to a draft")
    _add_project_args(p_face_effect)
    p_face_effect.add_argument("--effect-name", default=None, help="Effect enum name")
    p_face_effect.add_argument(
        "--effect-query", default=None, help="Resolve effect from asset search"
    )
    p_face_effect.add_argument("--start-time", default=None, help="Segment start time")
    p_face_effect.add_argument("--duration", default="3s", help="Segment duration")
    p_face_effect.add_argument("--track-name", default="EffectTrack", help="Target track name")

    p_audio_effect = sub.add_parser(
        "add-audio-effect", help="Attach an audio effect to an existing audio segment"
    )
    _add_project_args(p_audio_effect)
    p_audio_effect.add_argument("--segment-id", required=True, help="Target audio segment id")
    p_audio_effect.add_argument("--effect-name", default=None, help="Audio effect enum name")
    p_audio_effect.add_argument(
        "--effect-query", default=None, help="Resolve audio effect from asset search"
    )
    p_audio_effect.add_argument(
        "--effect-category",
        default="scene",
        choices=["scene", "tone", "speech_to_song"],
        help="Audio effect category",
    )
    p_audio_effect.add_argument(
        "--params-json", default=None, help="Optional JSON list of effect parameters"
    )

    p_audio_fade = sub.add_parser(
        "add-audio-fade", help="Attach fade-in/out to an existing audio segment"
    )
    _add_project_args(p_audio_fade)
    p_audio_fade.add_argument("--segment-id", required=True, help="Target audio segment id")
    p_audio_fade.add_argument("--fade-in", default="0s", help="Fade-in duration")
    p_audio_fade.add_argument("--fade-out", default="0s", help="Fade-out duration")

    p_attach = sub.add_parser(
        "attach-material", help="Attach transition or animation material to a segment"
    )
    _add_project_args(p_attach)
    p_attach.add_argument("--segment-id", required=True, help="Target segment id")
    p_attach.add_argument(
        "--kind", required=True, choices=["transition", "animation"], help="Material kind"
    )
    p_attach.add_argument("--material-name", default=None, help="Resolved enum name")
    p_attach.add_argument("--query", default=None, help="Resolve material from asset search")
    p_attach.add_argument("--duration", default=None, help="Optional material duration")
    p_attach.add_argument(
        "--animation-role",
        default="in",
        choices=["in", "out", "group", "loop"],
        help="Animation role when kind=animation",
    )

    p_complex = sub.add_parser(
        "add-complex-text", help="Add rich text with bubble, flower, and animation materials"
    )
    _add_project_args(p_complex)
    p_complex.add_argument("--text", required=True, help="Caption text")
    p_complex.add_argument("--start-time", default=None, help="Segment start time")
    p_complex.add_argument("--duration", default="3s", help="Segment duration")
    p_complex.add_argument("--track-name", default="Subtitles", help="Target track name")
    p_complex.add_argument("--text-color", default="#ffffff", help="Base text color")
    p_complex.add_argument("--border-color", default=None, help="Base border color")
    p_complex.add_argument("--font", default=None, help="Font enum or synonym")
    p_complex.add_argument("--font-query", default=None, help="Resolve font from asset search")
    p_complex.add_argument("--font-size", type=float, default=5.0, help="Base font size")
    p_complex.add_argument("--transform-x", type=float, default=0.0, help="Horizontal offset in px")
    p_complex.add_argument(
        "--transform-y", type=float, default=-0.8, help="Vertical transform ratio"
    )
    p_complex.add_argument("--scale-x", type=float, default=1.0, help="Horizontal scale")
    p_complex.add_argument("--scale-y", type=float, default=1.0, help="Vertical scale")
    p_complex.add_argument("--shadow-json", default=None, help="JSON object for shadow settings")
    p_complex.add_argument("--bubble-resource-id", default=None, help="Bubble resource id")
    p_complex.add_argument("--bubble-effect-id", default=None, help="Bubble effect id")
    p_complex.add_argument(
        "--bubble-query", default=None, help="Resolve bubble text material from asset search"
    )
    p_complex.add_argument("--flower-id", default=None, help="Flower text resource id")
    p_complex.add_argument(
        "--flower-query", default=None, help="Resolve flower text material from asset search"
    )
    p_complex.add_argument("--text-effect", default=None, help="Optional text effect")
    p_complex.add_argument(
        "--text-effect-query", default=None, help="Resolve text effect from asset search"
    )
    p_complex.add_argument("--anim-in", default=None, help="Intro animation")
    p_complex.add_argument(
        "--anim-in-query", default=None, help="Resolve intro animation from asset search"
    )
    p_complex.add_argument("--anim-out", default=None, help="Outro animation")
    p_complex.add_argument(
        "--anim-out-query", default=None, help="Resolve outro animation from asset search"
    )
    p_complex.add_argument("--anim-loop", default=None, help="Loop animation")
    p_complex.add_argument(
        "--anim-loop-query", default=None, help="Resolve loop animation from asset search"
    )

    p_green = sub.add_parser(
        "add-green-screen", help="Attach green screen chroma settings and add a background segment"
    )
    _add_project_args(p_green)
    p_green.add_argument("--segment-id", required=True, help="Target video segment id")
    p_green.add_argument("--background-path", required=True, help="Local background media path")
    p_green.add_argument("--chroma-color", default="#00ff00", help="Base chroma color")
    p_green.add_argument("--chroma-strength", type=int, default=20, help="Chroma intensity 0-100")
    p_green.add_argument("--edge-feather", type=int, default=10, help="Edge smooth 0-100")
    p_green.add_argument("--edge-cleanup", type=int, default=10, help="Spill cleanup 0-100")

    p_import_srt = sub.add_parser(
        "import-srt", help="Import subtitles from an SRT file into a text track"
    )
    _add_project_args(p_import_srt)
    p_import_srt.add_argument("--srt-path", required=True, help="SRT file path")
    p_import_srt.add_argument("--track-name", default="Subtitles", help="Target text track name")
    p_import_srt.add_argument(
        "--style-ref-segment-id", default=None, help="Existing text segment id to mirror style from"
    )
    p_import_srt.add_argument("--time-offset", default=None, help="Optional cue time offset")
    p_import_srt.add_argument("--text-color", default="#ffffff", help="Base text color")
    p_import_srt.add_argument("--border-color", default=None, help="Base border color")
    p_import_srt.add_argument("--font", default=None, help="Font enum or synonym")
    p_import_srt.add_argument("--font-size", type=float, default=5.0, help="Base font size")
    p_import_srt.add_argument("--bold", action="store_true", help="Use bold text")
    p_import_srt.add_argument("--italic", action="store_true", help="Use italic text")
    p_import_srt.add_argument("--underline", action="store_true", help="Underline the text")
    p_import_srt.add_argument(
        "--transform-x", type=float, default=0.0, help="Horizontal offset in px"
    )
    p_import_srt.add_argument(
        "--transform-y", type=float, default=-0.8, help="Vertical transform ratio"
    )
    p_import_srt.add_argument("--scale-x", type=float, default=1.0, help="Horizontal scale")
    p_import_srt.add_argument("--scale-y", type=float, default=1.0, help="Vertical scale")
    p_import_srt.add_argument(
        "--keyword", default=None, help="Keyword or pipe-delimited keywords to highlight"
    )
    p_import_srt.add_argument("--keyword-color", default="#ff7100", help="Highlight color")
    p_import_srt.add_argument(
        "--keyword-font-size", type=float, default=None, help="Highlight font size"
    )
    p_import_srt.add_argument("--keyword-border-color", default=None, help="Highlight border color")
    p_import_srt.add_argument("--shadow-json", default=None, help="JSON object for shadow settings")
    p_import_srt.add_argument("--text-effect", default=None, help="Optional text effect")
    p_import_srt.add_argument("--anim-in", default=None, help="Intro animation")
    p_import_srt.add_argument("--anim-out", default=None, help="Outro animation")
    p_import_srt.add_argument("--anim-loop", default=None, help="Loop animation")

    p_export_srt = sub.add_parser("export-srt", help="Export text track segments as SRT")
    _add_project_args(p_export_srt)

    p_text_ranges = sub.add_parser(
        "text-ranges", help="Apply inline style ranges to an existing text segment"
    )
    _add_project_args(p_text_ranges)
    p_text_ranges.add_argument("--segment-id", required=True, help="Target text segment id")
    p_text_ranges.add_argument("--styles", required=True, help="JSON array of style ranges")

    p_set_text = sub.add_parser(
        "set-text", help="Set the text content for one existing text segment"
    )
    _add_project_args(p_set_text)
    p_set_text.add_argument("--segment-id", required=True, help="Target text segment id")
    p_set_text.add_argument("--text", default=None, help="New caption text")
    p_set_text.add_argument(
        "--texts-json", default=None, help="JSON array of caption texts for multi-slot templates"
    )

    p_shift = sub.add_parser("shift", help="Shift one segment start time by an offset")
    _add_project_args(p_shift)
    p_shift.add_argument("--segment-id", required=True, help="Target segment id")
    p_shift.add_argument("--offset", required=True, help="Signed time offset")

    p_trim = sub.add_parser("trim", help="Adjust one segment source range and target duration")
    _add_project_args(p_trim)
    p_trim.add_argument("--segment-id", required=True, help="Target segment id")
    p_trim.add_argument("--start-time", required=True, help="New source start time")
    p_trim.add_argument("--duration", required=True, help="New source duration")

    p_speed = sub.add_parser("speed", help="Set one segment playback speed")
    _add_project_args(p_speed)
    p_speed.add_argument("--segment-id", required=True, help="Target segment id")
    p_speed.add_argument(
        "--multiplier", type=float, required=True, help="Playback speed multiplier"
    )

    p_volume = sub.add_parser("volume", help="Set one segment volume")
    _add_project_args(p_volume)
    p_volume.add_argument("--segment-id", required=True, help="Target segment id")
    p_volume.add_argument("--level", type=float, required=True, help="Volume level, >= 0")

    p_opacity = sub.add_parser("opacity", help="Set one segment clip opacity")
    _add_project_args(p_opacity)
    p_opacity.add_argument("--segment-id", required=True, help="Target segment id")
    p_opacity.add_argument("--alpha", type=float, required=True, help="Opacity from 0.0 to 1.0")

    p_shift_all = sub.add_parser(
        "shift-all", help="Shift all segment start times, optionally filtered by track type"
    )
    _add_project_args(p_shift_all)
    p_shift_all.add_argument("--offset", required=True, help="Signed time offset")
    p_shift_all.add_argument("--track-type", default=None, help="Optional track type filter")

    p_batch = sub.add_parser("batch", help="Apply a batch of saved-draft patch operations")
    _add_project_args(p_batch)
    p_batch.add_argument("--ops-json", required=True, help="JSON array of batch operations")

    p_cut = sub.add_parser("cut", help="Keep only a timeline range and rebase the draft to zero")
    _add_project_args(p_cut)
    p_cut.add_argument("--start-time", required=True, help="Cut range start time")
    p_cut.add_argument("--end-time", required=True, help="Cut range end time")

    p_save_template = sub.add_parser(
        "save-template", help="Save one segment and its materials as a reusable template"
    )
    _add_project_args(p_save_template)
    p_save_template.add_argument("--segment-id", required=True, help="Source segment id")
    p_save_template.add_argument("--template-name", required=True, help="Template name")
    p_save_template.add_argument("--out", required=True, help="Output template JSON path")

    p_apply_template = sub.add_parser(
        "apply-template", help="Apply a saved segment template into a draft"
    )
    _add_project_args(p_apply_template)
    p_apply_template.add_argument("--template-path", required=True, help="Template JSON path")
    p_apply_template.add_argument("--start-time", required=True, help="Target segment start time")
    p_apply_template.add_argument("--duration", required=True, help="Target segment duration")
    p_apply_template.add_argument(
        "--text", default=None, help="Optional text override for text templates"
    )

    p_add_text_template = sub.add_parser(
        "add-text-template", help="Add an official text template segment to a draft"
    )
    _add_project_args(p_add_text_template)
    p_add_text_template.add_argument(
        "--template-id", default=None, help="Official text template resource id"
    )
    p_add_text_template.add_argument(
        "--template-query", default=None, help="Resolve text template from asset search"
    )
    p_add_text_template.add_argument("--text", default=None, help="Single text value")
    p_add_text_template.add_argument("--texts-json", default=None, help="JSON array of text values")
    p_add_text_template.add_argument("--start-time", required=True, help="Segment start time")
    p_add_text_template.add_argument("--duration", required=True, help="Segment duration")
    p_add_text_template.add_argument(
        "--track-name", default="TextTemplateTrack", help="Target track name"
    )
    p_add_text_template.add_argument(
        "--template-payload-path", default=None, help="Pre-expanded template payload JSON path"
    )

    p_apply_zoom = sub.add_parser(
        "apply-zoom", help="Create a draft from a recording and apply smart zoom"
    )
    p_apply_zoom.add_argument("--name", required=True, help="Draft name")
    p_apply_zoom.add_argument("--drafts-root", default=None, help="Override drafts root")
    p_apply_zoom.add_argument(
        "--recipient-drafts-root",
        default=None,
        help="First-use guard: exact currentCustomDraftPath expected on the recipient computer",
    )
    p_apply_zoom.add_argument("--video", required=True, help="Recording video path")
    p_apply_zoom.add_argument(
        "--json", dest="events_json", required=True, help="Recording events JSON path"
    )
    p_apply_zoom.add_argument("--scale", type=int, default=150, help="Zoom scale percentage")
    return parser
