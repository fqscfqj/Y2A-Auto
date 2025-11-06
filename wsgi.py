"""WSGI entrypoint for production deployments.

This module exposes a pre-initialised Flask application instance that
production-grade WSGI servers (e.g. Gunicorn, Waitress) can import.
"""

from app import initialize_runtime

# Gunicorn and other WSGI servers look for a module-level variable named
# ``app`` by default. ``initialize_runtime`` makes sure all background
# services, schedulers, and configuration are ready before serving
# requests.
app = initialize_runtime()
