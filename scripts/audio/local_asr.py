"""Command-line entry point for diagnostic local ASR."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audio_sound.local_asr import transcribe_local  # noqa: E402


def _configure_utf8_output() -> None:
    """Keep JSON diagnostics printable on Windows locales such as GBK."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, help="absolute or relative mono PCM16 16 kHz WAV")
    parser.add_argument("--model", required=True, help="local model directory")
    parser.add_argument("--backend", choices=("faster-whisper", "funasr"), default="faster-whisper")
    parser.add_argument("--python", dest="python_executable", help="model runtime Python executable")
    parser.add_argument("--cache-dir", default=str(PROJECT_ROOT / "tmp" / "local-asr-cache"))
    parser.add_argument("--query", help="optional exact text candidate query")
    parser.add_argument("--must-keep", action="append", default=[], help="phrase that must remain protected")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--compute-type", default=None)
    parser.add_argument("--language", default=None)
    parser.add_argument("--cpu-threads", type=int, default=None)
    parser.add_argument("--chunk-seconds", type=float, default=18.0)
    parser.add_argument("--timeout-seconds", type=float, default=3600.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_output()
    args = build_parser().parse_args(argv)
    parameters = {"device": args.device, "device_index": args.device_index,
                  "chunk_seconds": args.chunk_seconds}
    if args.compute_type is not None:
        parameters["compute_type"] = args.compute_type
    if args.language is not None:
        parameters["language"] = args.language
    if args.cpu_threads is not None:
        parameters["cpu_threads"] = args.cpu_threads
    try:
        result = transcribe_local(
            args.audio, args.model, backend=args.backend, parameters=parameters,
            cache_dir=args.cache_dir, python_executable=args.python_executable,
            query=args.query, must_keep=tuple(args.must_keep), timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("status") in {"completed", "cache_hit"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
