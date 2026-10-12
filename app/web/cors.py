"""CORS handling tuned for Cloudflare Tunnel deployments.

When OpenManus sits behind a Cloudflare Tunnel the browser never talks to the
origin directly; requests arrive from whatever public hostname/subdomain the
tunnel maps (``https://<any-sub>.<your-domain>.com``, workers.dev prefixes,
trycloudflare.com ephemeral hosts, ...). A static allow-list breaks every time
a subdomain changes, so we validate the ``Origin`` header dynamically instead.

Rules (all overridable via environment variables):

* ``OPENMANUS_CORS_ALLOW_REGEX``   regex a request Origin must match to be
                                   allowed (default: any http/https origin).
* ``OPENMANUS_CORS_ALLOW_LOCALHOST`` add plain localhost origins for dev
                                   (default: true).
* ``OPENMANUS_CORS_ALLOW_CREDENTIALS`` send cookies/auth across tunnels
                                   (default: false - keep it off unless needed,
                                   since "*" + credentials is unsafe).
"""

import os
import re
from typing import List, Optional

from fastapi.middleware.cors import CORSMiddleware

# Default: trust any well-formed http(s) origin coming through the tunnel.
DEFAULT_ORIGIN_REGEX = r"^https?://[A-Za-z0-9.-]+(?::\d{1,5})?$"

_LOCALHOST_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")


def _compile_allow_regex() -> Optional[re.Pattern]:
    pattern = os.getenv("OPENMANUS_CORS_ALLOW_REGEX", DEFAULT_ORIGIN_REGEX)
    if pattern in ("", "*none*"):
        return None
    try:
        return re.compile(pattern)
    except re.error:
        # Fail closed on a bad operator-provided regex.
        return re.compile(DEFAULT_ORIGIN_REGEX)


class CloudflareTunnelCORSMiddleware(CORSMiddleware):
    """CORSMiddleware whose allow_origins check accepts any tunnel subdomain."""

    def __init__(self, app, allow_regex: Optional[re.Pattern] = None, **kwargs):
        kwargs.setdefault("allow_origin_regex", None)
        super().__init__(app, **kwargs)
        self._allow_regex = allow_regex if allow_regex is not None else _compile_allow_regex()
        self._allow_localhost = os.getenv("OPENMANUS_CORS_ALLOW_LOCALHOST", "true").lower() in (
            "1",
            "true",
            "yes",
        )

    def is_allowed_origin(self, origin: str) -> bool:
        if not origin:
            return False
        if self._allow_localhost and _LOCALHOST_RE.match(origin):
            return True
        if self._allow_regex is not None and self._allow_regex.match(origin):
            return True
        return super().is_allowed_origin(origin)


def install_cors(app, extra_origins: Optional[List[str]] = None) -> None:
    """Attach tunnel-friendly CORS middleware to a FastAPI app."""
    credentials = os.getenv("OPENMANUS_CORS_ALLOW_CREDENTIALS", "false").lower() in (
        "1",
        "true",
        "yes",
    )
    app.add_middleware(
        CloudflareTunnelCORSMiddleware,
        allow_origins=extra_origins or [],
        allow_credentials=credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Type", "Cache-Control", "X-Accel-Buffering"],
        max_age=86400,  # cache pre-flight for a day: tunnels add latency
    )
