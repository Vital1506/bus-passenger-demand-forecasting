"""FastAPI APIRouter modules for Enterprise Transit Intelligence."""

from .auth import auth_router
from .data import data_router
from .forecast import forecast_router
from .gtfs import gtfs_router
from .realtime import realtime_router
from .operations import operations_router
from .reports import reports_router
from .ai import ai_router

__all__ = [
    "auth_router",
    "data_router",
    "forecast_router",
    "gtfs_router",
    "realtime_router",
    "operations_router",
    "reports_router",
    "ai_router",
]
