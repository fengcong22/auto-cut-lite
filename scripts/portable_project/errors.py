from __future__ import annotations

from typing import Any


class PortableProjectError(RuntimeError):
    """A closed, user-actionable portable-project failure."""

    def __init__(
        self,
        code: str,
        reason: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.reason = str(reason)
        self.data = dict(data or {})
        super().__init__(self.reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "reason": self.reason,
            "data": dict(self.data),
        }
