import argparse
import json

from utils.draft_retention import retain_latest_project_drafts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retain only the latest JianYing drafts for one project family."
    )
    parser.add_argument(
        "--draft-name", default=None, help="A draft name used to infer the project family"
    )
    parser.add_argument("--family", default=None, help="Explicit project family prefix")
    parser.add_argument("--drafts-root", default=None, help="Override JianYing drafts root")
    parser.add_argument("--keep", type=int, default=3, help="Number of latest drafts to keep")
    parser.add_argument(
        "--keep-fallbacks",
        type=int,
        default=1,
        help="Maximum fallback drafts to keep within the retained set",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report without deleting drafts")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = retain_latest_project_drafts(
            draft_name=args.draft_name,
            family=args.family,
            drafts_root=args.drafts_root,
            keep_count=args.keep,
            max_fallback_count=args.keep_fallbacks,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        payload = {"ok": False, "code": "runtime_error", "reason": str(exc)}
        print(json.dumps(payload, ensure_ascii=False) if args.json else f"Error: {exc}")
        return 1

    payload = {"ok": True, "code": "ok", "reason": "", "data": result}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"Family: {result['family']}")
        print("Kept:")
        for item in result["kept"]:
            print(f" - {item['name']}")
        print("Deleted:")
        for item in result["deleted"]:
            print(f" - {item['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
