"""Response helpers shared by the domain API modules."""

from __future__ import annotations

from typing import Callable

from fastapi.responses import JSONResponse

from .idempotency import IdempotencyStore


def idempotent_json(
    store: IdempotencyStore,
    scope: str,
    key: str | None,
    produce: Callable[[], tuple[int, object]],
    *,
    status_code: int = 200,
) -> JSONResponse:
    """Return the first result for ``key`` and replay it on retries.

    ``produce`` returns ``(status_code, body)`` and must be free of side effects
    that cannot be replayed, because it only runs once per key.
    """

    def guarded() -> tuple[int, object]:
        code, body = produce()
        return code or status_code, body

    status, body, replayed = store.execute(scope, key, guarded)
    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    return JSONResponse(content=body, status_code=status, headers=headers)
