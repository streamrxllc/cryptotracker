"""Production WSGI entry point for the Flask dashboard."""
import os
import threading

from . import tracker, web

app = web.app

# The production VM uses one Gunicorn worker so there is exactly one tracker
# loop per application process. Allow smoke tests to serve the UI without
# starting a second Helius polling loop.
if os.environ.get("SMARTMONEY_DISABLE_TRACKER", "").strip().lower() not in {
    "1",
    "true",
    "yes",
    "on",
}:
    _tracker_thread = threading.Thread(
        target=tracker.loop,
        name="smartmoney-tracker",
        daemon=True,
    )
    _tracker_thread.start()
