"""Shared primitives used by both the review and the write-off services."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class ApiProblem(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {"code": self.code, "message": self.message, **self.extra},
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
