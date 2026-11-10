"""Launch the local Windows desktop app in an embedded native window."""

from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request

import uvicorn
import webview

from src.app import app


def wait_for_server(url: str, server: uvicorn.Server, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            return
        if not server.should_exit:
            try:
                with urllib.request.urlopen(url, timeout=1):
                    return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.2)
        else:
            break
    raise RuntimeError("The local forecasting service did not start. Check whether port 8765 is already in use.")


def main() -> None:
    host = os.getenv("BUS_FORECAST_HOST", "127.0.0.1")
    port = int(os.getenv("BUS_FORECAST_PORT", "8765"))
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("The Windows desktop app is local only by default. Set up authentication before exposing it to a network.")

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="bus-forecast-api", daemon=True)
    thread.start()
    app_url = f"http://{host}:{port}/"
    wait_for_server(app_url, server)

    window = webview.create_window(
        "Bus Passenger Demand Forecast",
        app_url,
        width=1440,
        height=950,
        min_size=(800, 650),
        background_color="#f5f7fa",
    )
    window.events.closed += lambda: setattr(server, "should_exit", True)
    webview.start(debug=False)
    server.should_exit = True
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
