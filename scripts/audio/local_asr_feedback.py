"""Import or reverify a saved human ASR boundary reference; never create cuts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audio_sound.local_asr_review import verify_review_receipt, write_review_receipt  # noqa: E402
from scripts.audio.local_asr import _configure_utf8_output  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("import", help="record a chat-confirmed reference from saved evidence")
    record.add_argument("--feedback", required=True)
    record.add_argument("--output", required=True)
    verify = commands.add_parser("verify", help="recompute all evidence checks for a saved receipt")
    verify.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            receipt = write_review_receipt(args.feedback, args.output)
            output = args.output
        else:
            receipt = verify_review_receipt(args.receipt)
            output = args.receipt
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "recorded_and_verified" if args.command == "import" else "verified_existing",
                      "receipt": str(Path(output).resolve()), "item_id": receipt["item_id"],
                      "model_candidate_range": receipt["model_candidate_range"],
                      "human_reference_range": receipt["human_reference_range"],
                      "difference_ms": receipt["difference_ms"], "production_eligible": False,
                      "authoritative_cut_boundary": False, "source_media_modified": False},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
