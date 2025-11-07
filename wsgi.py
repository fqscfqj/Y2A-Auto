"""
Deprecated stub: WSGI entry is no longer supported by default.

This module intentionally raises at import time to prevent accidental
use in server deployments. Please start the app via `python app.py`
or Docker `CMD ["python", "app.py"]`.
"""

raise RuntimeError(
	"WSGI entry has been removed. Start the application with 'python app.py' instead."
)
