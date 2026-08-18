import argparse
import os
from typing import Any, Dict, List, Optional

from utils.asset_index import AssetIndex
from utils.cli_protocol import emit_result, make_result
from utils.errors import InfraError
from utils.logging_utils import setup_logger

logger = setup_logger("asset_search")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def search_assets(
    query: str, category: Optional[str] = None, limit: int = 20
) -> List[Dict[str, Any]]:
    return AssetIndex(skill_root=BASE_DIR).search(query, category=category, limit=limit)


def list_categories() -> List[Dict[str, Any]]:
    return AssetIndex(skill_root=BASE_DIR).list_categories()


def format_results(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "No matching assets found."

    header = f"{'Identifier':<25} | {'Category':<22} | {'Name':<24} | {'Source'}"
    output = [header, "-" * len(header)]

    for item in results:
        identifier = (item.get("identifier") or "N/A")[:25]
        category = (item.get("category") or "N/A")[:22]
        name = (item.get("name") or item.get("title") or "N/A")[:24]
        source = item.get("source", "unknown")
        output.append(f"{identifier:<25} | {category:<22} | {name:<24} | {source}")

    return "\n".join(output)


def format_categories(categories: List[Dict[str, Any]]) -> str:
    if not categories:
        return "No asset categories found."

    header = f"{'Category':<22} | {'Count':<8} | {'Sources'}"
    output = [header, "-" * len(header)]
    for item in categories:
        sources = ",".join(item.get("sources", []))
        output.append(f"{item['category']:<22} | {item['count']:<8} | {sources}")
    return "\n".join(output)


def main() -> int:
    parser = argparse.ArgumentParser(description="Search indexed CapCut / JianYing assets.")
    parser.add_argument("query", nargs="?", default=None, help="Search query")
    parser.add_argument("-c", "--category", default=None, help="Asset category filter")
    parser.add_argument("-l", "--limit", type=int, default=20, help="Maximum results to return")
    parser.add_argument("--list", action="store_true", help="List available categories")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON response")
    args = parser.parse_args()

    try:
        if args.list:
            categories = list_categories()
            if args.json:
                emit_result(
                    make_result(
                        True, "ok", "", {"count": len(categories), "categories": categories}
                    ),
                    True,
                )
            else:
                print(format_categories(categories))
            return 0

        if not args.query:
            parser.print_help()
            return 0

        logger.info("Searching '%s'...", args.query)
        results = search_assets(args.query, args.category, args.limit)
        if args.json:
            emit_result(
                make_result(
                    True,
                    "ok",
                    "",
                    {
                        "query": args.query,
                        "category": args.category,
                        "count": len(results),
                        "results": results,
                    },
                ),
                True,
            )
        else:
            print(format_results(results))
        return 0
    except InfraError as exc:
        logger.error(str(exc))
        emit_result(make_result(False, "infra_error", str(exc)), args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
