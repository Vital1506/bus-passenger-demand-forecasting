FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BUS_FORECAST_DATA_DIR=/var/lib/bus-forecast \
    BUS_FORECAST_REQUIRE_AUTH=1

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY data/sample_daily_ridership.csv ./data/sample_daily_ridership.csv
COPY web ./web
EXPOSE 8000
CMD ["sh", "-c", "uvicorn src.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
