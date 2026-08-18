import sys
from typing import Optional, TextIO


def configure_utf8_stdio(
    *,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> None:
    """Use UTF-8 for user-facing CLI output when the stream supports reconfiguration."""

    for stream in (
        stdout if stdout is not None else sys.stdout,
        stderr if stderr is not None else sys.stderr,
    ):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
