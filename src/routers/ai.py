"""AI Transit Intelligence conversational copilot router."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..ai_assistant import get_ai_status, process_chat_message
from ..auth import AuthenticatedPrincipal, UserRole, authorize_ai_chat

ai_router = APIRouter(tags=["ai"])


class AIChatRequest(BaseModel):
    message: str = Field(..., max_length=2000)
    history: list[dict[str, str]] | None = Field(default=None)
    route_id: str | None = Field(default=None, max_length=64)


@ai_router.get("/api/ai/status")
def ai_status() -> dict[str, Any]:
    """Check AI assistant configuration and backend readiness."""
    return get_ai_status()


@ai_router.post("/api/ai/chat")
async def ai_chat_endpoint(
    payload: AIChatRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Any:
    """Conversational AI assistant grounded in real transit demand telemetry."""
    principal = authorize_ai_chat(authorization)
    role = principal.role

    client_id = request.client.host if request and request.client else "unknown"
    data_dir = Path(os.getenv("BUS_FORECAST_DATA_DIR", "data"))
    result = await process_chat_message(
        user_message=payload.message,
        history=payload.history,
        route_id=payload.route_id,
        client_id=f"{role.value}:{client_id}",
        data_dir=data_dir,
    )

    if result.get("status") == "error":
        status_code_map = {
            "API_KEY_MISSING": 503,
            "OPENROUTER_KEY_MISSING": 503,
            "INVALID_API_KEY": 401,
            "CLIENT_RATE_LIMIT": 429,
            "RATE_LIMIT_EXCEEDED": 429,
            "TIMEOUT": 504,
            "EMPTY_MESSAGE": 422,
            "MODEL_UNAVAILABLE": 502,
            "NETWORK_ERROR": 502,
            "PROVIDER_BUSY": 503,
            "PROVIDER_ERROR": 502,
            "INVALID_RESPONSE": 502,
            "EMPTY_RESPONSE": 502,
        }
        code = status_code_map.get(result.get("error_code"), 500)
        return JSONResponse(status_code=code, content=result)

    result["role"] = role.value
    return result
