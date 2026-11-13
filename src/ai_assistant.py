"""AI Transit Intelligence Assistant powered by OpenRouter API.

Integrates real-world transit telemetry, machine learning passenger forecasts,
and operational recommendations into a grounded, conversational AI assistant.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from .auth import InMemoryRateLimiter

logger = logging.getLogger("ai_assistant")

OPENROUTER_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "openrouter/free"
REQUEST_TIMEOUT_SECONDS = 30.0

# Dedicated rate limiter for AI chat: 30 requests per minute per client identifier
_AI_RATE_LIMITER = InMemoryRateLimiter(max_requests=30, window_seconds=60)


def get_ai_rate_limiter() -> InMemoryRateLimiter:
    return _AI_RATE_LIMITER


def get_openrouter_api_key() -> str:
    """Retrieve the OpenRouter API key from the environment. Never exposed to clients."""
    return os.getenv("OPENROUTER_API_KEY", "").strip()


def get_openrouter_model() -> str:
    """Retrieve the target OpenRouter model or fallback to openrouter/free."""
    return os.getenv("OPENROUTER_MODEL", "").strip() or DEFAULT_OPENROUTER_MODEL


def get_ai_status() -> dict[str, Any]:
    """Return the configuration status of the AI assistant without leaking secret keys."""
    key = get_openrouter_api_key()
    return {
        "configured": bool(key),
        "model": get_openrouter_model(),
        "provider": "openrouter",
        "endpoint": "/api/ai/chat",
    }


def extract_system_context(
    data_dir: Path | str | None = None,
    target_route: str | None = None,
) -> dict[str, Any]:
    """Extract and summarize actual passenger demand, model performance, and forecasts.

    Grounded strictly in verified local artifacts and data stores.
    """
    if data_dir is None:
        data_dir = Path(os.getenv("BUS_FORECAST_DATA_DIR", "data"))
    else:
        data_dir = Path(data_dir)

    context: dict[str, Any] = {
        "data_loaded": False,
        "total_records": 0,
        "route_count": 0,
        "available_routes": [],
        "observed_date_range": None,
        "model_trained": False,
        "training_horizon_days": None,
        "model_metrics": None,
        "forecast_available": False,
        "forecast_dates": [],
        "busiest_routes_forecast": [],
        "lowest_routes_forecast": [],
        "next_day_forecast": None,
        "route_specific_detail": None,
    }

    # 1. Inspect daily data
    daily_csv = data_dir / "daily_ridership.csv"
    if not daily_csv.exists():
        daily_csv = data_dir / "sample_daily_ridership.csv"

    if daily_csv.exists():
        try:
            df = pd.read_csv(daily_csv)
            if "service_date" in df.columns and "route_id" in df.columns and "passengers" in df.columns:
                df["service_date"] = pd.to_datetime(df["service_date"])
                context["data_loaded"] = True
                context["total_records"] = int(len(df))
                routes = sorted([str(r) for r in df["route_id"].unique()])
                context["route_count"] = len(routes)
                context["available_routes"] = routes[:20]  # sample if large
                min_d = df["service_date"].min().strftime("%Y-%m-%d")
                max_d = df["service_date"].max().strftime("%Y-%m-%d")
                context["observed_date_range"] = f"{min_d} to {max_d}"
        except Exception as exc:
            logger.warning("Could not read daily ridership data: %s", exc)

    # 2. Inspect model metadata & metrics
    metrics_path = data_dir / "metrics.json"
    if metrics_path.exists():
        try:
            metrics_data = json.loads(metrics_path.read_text(encoding="utf-8"))
            context["model_metrics"] = {
                "model_wape": metrics_data.get("model_wape"),
                "baseline_wape": metrics_data.get("baseline_wape"),
                "better_than_baseline": metrics_data.get("better_than_baseline"),
                "horizon_days": metrics_data.get("horizon"),
            }
            context["model_trained"] = True
            context["training_horizon_days"] = metrics_data.get("horizon")
        except Exception as exc:
            logger.warning("Could not parse metrics.json: %s", exc)

    # 3. Inspect forecast outputs
    forecast_csv = data_dir / "forecast.csv"
    if forecast_csv.exists():
        try:
            fdf = pd.read_csv(forecast_csv)
            if "service_date" in fdf.columns and "predicted_passengers" in fdf.columns and "route_id" in fdf.columns:
                context["forecast_available"] = True
                fdf["service_date"] = fdf["service_date"].astype(str)
                dates = sorted(fdf["service_date"].unique().tolist())
                context["forecast_dates"] = dates

                # Route aggregations over the forecast horizon
                route_totals = (
                    fdf.groupby("route_id")["predicted_passengers"]
                    .mean()
                    .round(1)
                    .sort_values(ascending=False)
                )
                busiest = [
                    {"route_id": str(r), "avg_daily_demand": float(v)}
                    for r, v in route_totals.head(5).items()
                ]
                lowest = [
                    {"route_id": str(r), "avg_daily_demand": float(v)}
                    for r, v in route_totals.tail(5).items()
                ]
                context["busiest_routes_forecast"] = busiest
                context["lowest_routes_forecast"] = lowest

                # Earliest / tomorrow's forecast
                if dates:
                    earliest_date = dates[0]
                    day_slice = fdf[fdf["service_date"] == earliest_date]
                    total_pax = float(day_slice["predicted_passengers"].sum().round(1))
                    top_day_routes = [
                        {
                            "route_id": str(row["route_id"]),
                            "predicted_demand": float(row["predicted_passengers"]),
                            "lower_90": float(row.get("lower_90_approx", row["predicted_passengers"])),
                            "upper_90": float(row.get("upper_90_approx", row["predicted_passengers"])),
                        }
                        for _, row in day_slice.sort_values(by="predicted_passengers", ascending=False).head(5).iterrows()
                    ]
                    context["next_day_forecast"] = {
                        "date": earliest_date,
                        "total_system_predicted_demand": total_pax,
                        "top_routes": top_day_routes,
                    }

                # Specific route detail if requested
                if target_route:
                    matched = fdf[fdf["route_id"].astype(str) == str(target_route)]
                    if not matched.empty:
                        context["route_specific_detail"] = {
                            "route_id": str(target_route),
                            "daily_predictions": [
                                {
                                    "date": str(row["service_date"]),
                                    "predicted": float(row["predicted_passengers"]),
                                    "lower_90": float(row.get("lower_90_approx", row["predicted_passengers"])),
                                    "upper_90": float(row.get("upper_90_approx", row["predicted_passengers"])),
                                }
                                for _, row in matched.iterrows()
                            ],
                        }
        except Exception as exc:
            logger.warning("Could not parse forecast.csv: %s", exc)

    return context


def format_system_prompt(context: dict[str, Any]) -> str:
    """Construct a grounding system prompt containing actual system telemetry and constraints."""
    context_json = json.dumps(context, indent=2)
    return f"""You are the AI Transit Intelligence Assistant for the Smart Bus Demand Intelligence platform.
You analyze bus passenger demand, forecast projections, capacity bottlenecks, and transit operations.

CRITICAL OPERATIONAL RULES:
1. GROUNDING RULE: You must ONLY state facts, numbers, dates, and routes that exist in the TRANSIT SYSTEM CONTEXT below.
2. DO NOT INVENT or extrapolate passenger numbers, accuracy metrics, routes, or historical counts.
3. If the user asks for a prediction, route, or date that is not available or not loaded, explicitly state:
   "That data is currently unavailable in the system. Please ensure daily route counts are imported and model training is run on the dashboard."
4. If asked about model accuracy, refer strictly to the recorded WAPE (Weighted Absolute Percentage Error) and baseline comparison in the context.
5. When recommending scheduling or operational adjustments (e.g. additional buses, headway changes):
   - Cite the specific route ID and its forecasted demand numbers.
   - Explain how high predicted volume risks crowding or bunching and recommend adjusting headways or dispatching reserve vehicles.
6. Provide concise, clear, and professional markdown responses with bullet points, bold labels, or tables when comparing routes.

--- TRANSIT SYSTEM CONTEXT (VERIFIED REAL-TIME DATA) ---
{context_json}
-------------------------------------------------------
"""


async def call_openrouter_api(
    messages: list[dict[str, str]],
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute asynchronous chat completion request to OpenRouter API with graceful error handling."""
    key = api_key or get_openrouter_api_key()
    if not key:
        return {
            "status": "error",
            "error_code": "API_KEY_MISSING",
            "message": "OpenRouter API key is not configured. Please add OPENROUTER_API_KEY to your environment or .env file.",
        }

    target_model = model or get_openrouter_model()

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Vital1506/bus-passenger-demand-forecasting",
        "X-Title": "Smart Bus Demand Intelligence AI",
    }

    payload = {
        "model": target_model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1000,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                OPENROUTER_COMPLETIONS_URL,
                headers=headers,
                json=payload,
            )

        if response.status_code == 200:
            data = response.json()
            choices = data.get("choices", [])
            if choices and "message" in choices[0]:
                reply_content = choices[0]["message"].get("content", "").strip()
                return {
                    "status": "ok",
                    "reply": reply_content,
                    "model": data.get("model", target_model),
                    "usage": data.get("usage"),
                }
            return {
                "status": "error",
                "error_code": "EMPTY_RESPONSE",
                "message": "The AI model returned an empty response.",
            }

        elif response.status_code == 401:
            logger.warning("OpenRouter API returned 401 Unauthorized")
            return {
                "status": "error",
                "error_code": "INVALID_API_KEY",
                "message": "Invalid or expired OpenRouter API key. Please check your OPENROUTER_API_KEY configuration.",
            }

        elif response.status_code == 429:
            logger.warning("OpenRouter API returned 429 Rate Limit")
            return {
                "status": "error",
                "error_code": "RATE_LIMIT_EXCEEDED",
                "message": "OpenRouter rate limit or model quota exceeded. Please wait a moment or try another model.",
            }

        elif response.status_code in {400, 404}:
            err_msg = response.text[:200]
            logger.warning("OpenRouter API returned %s: %s", response.status_code, err_msg)
            return {
                "status": "error",
                "error_code": "MODEL_UNAVAILABLE",
                "message": f"The model '{target_model}' is unavailable on OpenRouter or received an invalid request: {err_msg}",
            }

        else:
            logger.error("OpenRouter API error %s: %s", response.status_code, response.text[:300])
            return {
                "status": "error",
                "error_code": f"HTTP_{response.status_code}",
                "message": f"OpenRouter API returned error code {response.status_code}.",
            }

    except httpx.TimeoutException:
        logger.error("OpenRouter request timed out after %.1f seconds", timeout)
        return {
            "status": "error",
            "error_code": "TIMEOUT",
            "message": f"Request to OpenRouter AI timed out after {int(timeout)} seconds. Please try again.",
        }

    except httpx.RequestError as exc:
        logger.error("OpenRouter connection error: %s", exc)
        return {
            "status": "error",
            "error_code": "NETWORK_ERROR",
            "message": "Network error communicating with OpenRouter AI service. Please verify connectivity.",
        }

    except Exception as exc:
        logger.exception("Unexpected error in OpenRouter call: %s", exc)
        return {
            "status": "error",
            "error_code": "INTERNAL_ERROR",
            "message": "An internal error occurred while communicating with the AI service.",
        }


async def process_chat_message(
    user_message: str,
    history: list[dict[str, str]] | None = None,
    route_id: str | None = None,
    client_id: str = "default",
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Process incoming chat query with rate limiting, data extraction, and OpenRouter completion."""
    # 1. Validate request
    cleaned_message = (user_message or "").strip()
    if not cleaned_message:
        return {
            "status": "error",
            "error_code": "EMPTY_MESSAGE",
            "message": "Message text cannot be empty.",
        }

    # 2. Check rate limit
    if not _AI_RATE_LIMITER.check(client_id):
        return {
            "status": "error",
            "error_code": "CLIENT_RATE_LIMIT",
            "message": "Too many requests to AI Assistant. Please wait a minute before asking again.",
        }

    # 3. Check API key presence before doing heavy context loading
    key = get_openrouter_api_key()
    if not key:
        return {
            "status": "error",
            "error_code": "API_KEY_MISSING",
            "message": "OpenRouter API key is not configured. Add OPENROUTER_API_KEY to your environment variables or .env file to enable the AI assistant.",
        }

    # 4. Extract data context
    context = extract_system_context(data_dir=data_dir, target_route=route_id)
    system_prompt = format_system_prompt(context)

    # 5. Build messages array with sanitized history
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

    if history:
        # Keep up to the last 6 turns to maintain context without token bloat
        for turn in history[-6:]:
            role = turn.get("role")
            content = str(turn.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": cleaned_message})

    # 6. Call OpenRouter
    api_result = await call_openrouter_api(messages=messages, api_key=key)

    # Attach context metadata (safe summary)
    if api_result.get("status") == "ok":
        api_result["context_summary"] = {
            "data_loaded": context.get("data_loaded", False),
            "forecast_available": context.get("forecast_available", False),
            "forecast_dates_count": len(context.get("forecast_dates", [])),
            "routes_count": context.get("route_count", 0),
        }

    return api_result
