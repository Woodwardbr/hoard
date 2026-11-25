"""Keep other websites out of the local dashboard.

Hoard has no login, so the browser is the security boundary. Two checks:

* **Host**: every request must be addressed to a local name (127.0.0.1,
  localhost, ::1, or APP_HOST). This defeats DNS rebinding, where an attacker's
  domain is re-pointed at 127.0.0.1 so their page can *read* the API.
* **Origin**: requests that change something (POST/PUT/PATCH/DELETE) must come
  from the dashboard's own page. A cross-site page can still fire a "simple"
  POST at 127.0.0.1 without a CORS preflight; this rejects it. Requests with no
  Origin and no Sec-Fetch-Site (curl, scripts) are allowed.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import settings

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", settings.app_host.strip("[]")}


def _hostname(netloc: str) -> str:
    return (urlsplit(f"//{netloc}").hostname or "").lower()


def check(request: Request) -> str | None:
    """Return a reason to reject ``request``, or None if it's allowed."""
    host_header = request.headers.get("host", "")
    if _hostname(host_header) not in ALLOWED_HOSTS:
        return "Requests must be addressed to localhost."
    if request.method not in UNSAFE_METHODS:
        return None
    origin = request.headers.get("origin")
    if origin is not None:
        if origin == "null" or urlsplit(origin).netloc.lower() != host_header.lower():
            return "Cross-site request blocked."
    elif request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return "Cross-site request blocked."
    return None


async def middleware(request: Request, call_next):
    reason = check(request)
    if reason:
        return JSONResponse(status_code=403, content={"detail": reason, "error_code": "FORBIDDEN"})
    return await call_next(request)
