"""CORS handling for the OpenManus Web UI (secure-by-default).

Because every authenticated ``/api`` route now rides on a cookie, a permissive
CORS policy would let any website read a logged-in user's agent streams, so the
previous "trust any Cloudflare-Tunnel subdomain" default was removed.

Default policy (no configuration needed)
----------------------------------------
* Allow only local development origins: ``http(s)://localhost:<any-port>``,
  ``http(s)://127.0.0.1:<any-port>`` and ``http(s)://[::1]:<port>``.
* Every other origin is rejected.
* Credentials (cookies) are **never** sent cross-origin by default.

Exposing the app through a Cloudflare Tunnel hostname
-----------------------------------------------------
Set an explicit allow-regex that matches *your* domain only, e.g.::

    OPENMANUS_CORS_ALLOW_REGEX='^https://([a-z0-9-]+\\.)*example\\.com$'

which allows any subdomain of ``example.com`` (the shape cloudflared maps,
like ``https://openmanus.example.com`` or ``*.trycloudflare.com`` hosts) while
rejecting everything else. Only when this regex is configured may operators
also opt into cookies across origins with::

    OPENMANUS_CORS_ALLOW_CREDENTIALS=true

Environment variables
---------------------
* ``OPENMANUS_CORS_ALLOW_REGEX``      regex an Origin must fully match (unset =
                                      localhost-only).
* ``OPENMANUS_CORS_ALLOW_LOCALHOST``  keep the localhost allowance
                                      (default: true).
* ``OPENMANUS_CORS_ALLOW_CREDENTIALS`` send cookies/auth cross-origin; honoured
                                      ONLY when ``OPENMANUS_CORS_ALLOW_REGEX``
                                      is set (default: false).
"""

import os
import re
from typing import List, Optional

from fastapi.middleware.cors import CORSMiddleware

# Local development origins: http(s)://localhost|127.0.0.1|[::1] on any port.
_LOCALHOST_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$")


def _compile_allow_regex() -> Optional[re.Pattern]:
    """Compile the operator-provided origin regex (no open default anymore)."""
    pattern = os.getenv("OPENMANUS_CORS_ALLOW_REGEX", "").strip()
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        # Fail closed on a bad operator-provided regex: fall back to the
        # safest possible policy (nothing but localhost allowed).
        return None


class TunnelCORSMiddleware(CORSMiddleware):
    """CORSMiddleware with a strict, env-driven origin allow-list.

    Allowed origins = localhost (unless disabled) ∪ ``OPENMANUS_CORS_ALLOW_REGEX``
    matches ∪ explicit ``allow_origins`` passed at construction.
    """

    def __init__(self, app, allow_regex: Optional[re.Pattern] = None, **kwargs):
        kwargs.setdefault("allow_origin_regex", None)
        super().__init__(app, **kwargs)
        self._allow_regex = allow_regex if allow_regex is not None else _compile_allow_regex()
        self._allow_localhost = os.getenv("OPENMANUS_CORS_ALLOW_LOCALHOST", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    def is_allowed_origin(self, origin: str) -> bool:
        if not origin:
            return False
        if self._allow_localhost and _LOCALHOST_RE.match(origin):
            return True
        if self._allow_regex is not None and self._allow_regex.match(origin):
            return True
        return super().is_allowed_origin(origin)


def _credentials_allowed(allow_regex_set: bool) -> bool:
    """Credentials stay off unless explicitly requested AND a custom regex is set."""
    wants = os.getenv("OPENMANUS_CORS_ALLOW_CREDENTIALS", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    return bool(wants and allow_regex_set)


def install_cors(app, extra_origins: Optional[List[str]] = None) -> None:
    """Attach the strict CORS middleware to a FastAPI app."""
    allow_regex = _compile_allow_regex()
    app.add_middleware(
        TunnelCORSMiddleware,
        allow_origins=extra_origins or [],
        allow_regex=allow_regex,
        allow_credentials=_credentials_allowed(allow_regex is not None),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Type", "Cache-Control", "X-Accel-Buffering"],
        max_age=86400,  # cache pre-flight for a day: tunnels add latency
    )


__all__ = ["TunnelCORSMiddleware", "install_cors"]
