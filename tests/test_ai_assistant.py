"""Tests for OpenRouter AI Transit Intelligence Assistant."""

import asyncio
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.ai_assistant import (
    call_openrouter_api,
    extract_system_context,
    format_system_prompt,
    get_ai_status,
    process_chat_message,
)
from src.app import AIChatRequest, ai_chat, ai_status


def run_async(coro):
    """Run an async coroutine synchronously using a dedicated event loop."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class DummyClient:
    host = "127.0.0.1"


class DummyRequest:
    client = DummyClient()


def test_ai_status_unconfigured():
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
        status = get_ai_status()
        assert status["configured"] is False
        assert status["provider"] == "openrouter"
        assert "key" not in status
        assert "OPENROUTER_API_KEY" not in str(status)


def test_ai_status_configured():
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test-secret-12345", "OPENROUTER_MODEL": "openrouter/free"}):
        status = get_ai_status()
        assert status["configured"] is True
        assert status["model"] == "openrouter/free"
        assert "sk-or-test-secret-12345" not in str(status)


def test_extract_system_context_empty_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = extract_system_context(data_dir=tmpdir)
        assert ctx["data_loaded"] is False
        assert ctx["total_records"] == 0
        assert ctx["forecast_available"] is False
        assert ctx["model_trained"] is False


def test_extract_system_context_with_data():
    ctx = extract_system_context(data_dir="data")
    assert isinstance(ctx, dict)
    assert "data_loaded" in ctx
    assert "route_count" in ctx
    prompt = format_system_prompt(ctx)
    assert "TRANSIT SYSTEM CONTEXT" in prompt
    assert "GROUNDING RULE" in prompt


def test_process_chat_message_empty():
    res = run_async(process_chat_message(""))
    assert res["status"] == "error"
    assert res["error_code"] == "EMPTY_MESSAGE"


def test_process_chat_message_missing_key():
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
        res = run_async(process_chat_message("What is the forecast?"))
        assert res["status"] == "error"
        assert res["error_code"] == "API_KEY_MISSING"
        assert "OPENROUTER_API_KEY" in res["message"]


def test_call_openrouter_api_success():
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "id": "gen-123",
        "model": "openrouter/free",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Tomorrow's predicted system passenger demand is approximately 4,200 boardings.",
                }
            }
        ],
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = fake_response
        result = run_async(
            call_openrouter_api(
                messages=[{"role": "user", "content": "Forecast tomorrow?"}],
                api_key="sk-or-valid-test-key",
                model="openrouter/free",
            )
        )
        assert result["status"] == "ok"
        assert "Tomorrow's predicted system" in result["reply"]
        assert result["model"] == "openrouter/free"
        assert mock_post.called
        headers_sent = mock_post.call_args[1]["headers"]
        assert headers_sent["Authorization"] == "Bearer sk-or-valid-test-key"


def test_call_openrouter_api_401_invalid_key():
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.text = "Unauthorized: Invalid API Key"

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = fake_response
        result = run_async(
            call_openrouter_api(
                messages=[{"role": "user", "content": "Hi"}],
                api_key="sk-or-invalid-key",
            )
        )
        assert result["status"] == "error"
        assert result["error_code"] == "INVALID_API_KEY"
        assert "Invalid or expired OpenRouter API key" in result["message"]


def test_call_openrouter_api_429_rate_limit():
    fake_response = MagicMock()
    fake_response.status_code = 429
    fake_response.text = "Rate limit reached"

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = fake_response
        result = run_async(
            call_openrouter_api(
                messages=[{"role": "user", "content": "Hi"}],
                api_key="sk-or-valid-key",
            )
        )
        assert result["status"] == "error"
        assert result["error_code"] == "RATE_LIMIT_EXCEEDED"


def test_call_openrouter_api_timeout():
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.TimeoutException("Timed out")
        result = run_async(
            call_openrouter_api(
                messages=[{"role": "user", "content": "Hi"}],
                api_key="sk-or-valid-key",
            )
        )
        assert result["status"] == "error"
        assert result["error_code"] == "TIMEOUT"


def test_call_openrouter_api_network_error():
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("Connection refused")
        result = run_async(
            call_openrouter_api(
                messages=[{"role": "user", "content": "Hi"}],
                api_key="sk-or-valid-key",
            )
        )
        assert result["status"] == "error"
        assert result["error_code"] == "NETWORK_ERROR"


def test_api_ai_status_endpoint():
    status = ai_status()
    assert "configured" in status
    assert "model" in status
    assert status["provider"] == "openrouter"


def test_api_ai_chat_missing_key_returns_503():
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
        payload = AIChatRequest(message="What is the demand?")
        response = run_async(ai_chat(payload, DummyRequest(), authorization=None))
        assert response.status_code == 503
        body = json.loads(response.body.decode())
        assert body["status"] == "error"
        assert body["error_code"] == "API_KEY_MISSING"


def test_api_ai_chat_success_mocked():
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "model": "openrouter/free",
        "choices": [{"message": {"role": "assistant", "content": "Route 9 has the highest demand with 1,250 boardings."}}],
    }

    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-mock-valid-key"}):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = fake_response
            payload = AIChatRequest(
                message="Which bus route has the highest passenger demand?",
                history=[],
            )
            result = run_async(ai_chat(payload, DummyRequest(), authorization=None))
            assert isinstance(result, dict)
            assert result["status"] == "ok"
            assert "Route 9 has the highest demand" in result["reply"]
            assert result["model"] == "openrouter/free"
            assert "sk-or-mock-valid-key" not in str(result)
