"""Authentication gate for the OpenManus Web UI.

Threat model: the server usually sits behind a Cloudflare Tunnel and is
reachable from the public internet, while it can execute arbitrary bash through
the agent's tools. Every ``/api/*`` route except ``/api/health`` therefore
requires a valid session:

* ``OPENMANUS_WEB_TOKEN`` (required) — shared bearer token. If unset, the
  server refuses to start (:func:`require_web_token`).
* ``POST /api/login`` accepts ``{"token": "..."}`` and, on success, sets an
  **HttpOnly**, **SameSite=Strict** cookie (``Secure`` added automatically when
  the request arrived over HTTPS / via ``X-Forwarded-Proto``, which is exactly
  the Cloudflare-Tunnel case).
* All other ``/api/*`` routes return **401 JSON** without that cookie.
* ``Cf-Access-Jwt-Assertion`` (Cloudflare Access in front of the tunnel) is
  honoured **only** when ``OPENMANUS_TRUST_CF_ACCESS=true`` — presence of the
  header alone is not enough; operators must opt in explicitly because the
  header can be spoofed by anything that terminates traffic before cloudflared
  or by origins that bypass Access.

The comparison uses :func:`hmac.compare_digest` to avoid trivial timing leaks.
"""

import hmac
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

#: Name of the session cookie set by /api/login.
AUTH_COOKIE_NAME = "openmanus_session"

#: Header injected by Cloudflare Access after successful IdP authentication.
CF_ACCESS_HEADER = "cf-access-jwt-assertion"


def get_web_token() -> Optional[str]:
    """Return the configured web token, or ``None`` when unset/blank."""
    token = os.getenv("OPENMANUS_WEB_TOKEN", "").strip()
    return token or None


def require_web_token() -> str:
    """Fetch the web token or raise a clear startup error.

    Called from the FastAPI startup hook so ``python -m app.web.server``
    refuses to boot with no auth configured.
    """
    token = get_web_token()
    if not token:
        raise RuntimeError(
            "OPENMANUS_WEB_TOKEN is not set — refusing to start the Web UI "
            "without authentication. Export a strong secret first, e.g.:  "
            "OPENMANUS_WEB_TOKEN=\"$(openssl rand -hex 32)\" python -m app.web.server"
        )
    return token


def trust_cf_access_enabled() -> bool:
    """True only when the operator explicitly opted into CF Access headers."""
    return os.getenv("OPENMANUS_TRUST_CF_ACCESS", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _token_matches(candidate: str) -> bool:
    expected = get_web_token()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def authenticate_request(request: Request) -> bool:
    """Decide whether an incoming request may access protected /api routes."""
    # 1) Session cookie issued by POST /api/login.
    cookie = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie and _token_matches(cookie):
        return True

    # 2) Direct bearer token (handy for curl/scripts): Authorization header.
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        if _token_matches(auth_header[7:].strip()):
            return True

    # 3) Cloudflare Access assertion — ONLY when explicitly trusted.
    if trust_cf_access_enabled():
        cf_jwt = request.headers.get(CF_ACCESS_HEADER)
        if cf_jwt and cf_jwt.strip():
            # Presence check by design: CF Access already validated the JWT at
            # the edge. Operators who cannot guarantee that every request passes
            # through Access must leave OPENMANUS_TRUST_CF_ACCESS unset/false.
            return True

    return False


class LoginRequest(BaseModel):
    token: str = Field(..., min_length=1, description="Value of OPENMANUS_WEB_TOKEN")


def install_auth(app: FastAPI) -> None:
    """Attach the auth middleware + login route to a FastAPI app."""

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        path = request.url.path
        # Public surface: everything non-API plus the health probe and login.
        if not path.startswith("/api"):
            return await call_next(request)
        if path in ("/api/health", "/api/login"):
            return await call_next(request)
        if request.method == "OPTIONS":  # CORS pre-flight carries no cookies anyway
            return await call_next(request)
        if not authenticate_request(request):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized: log in at /api/login or provide the web token.",
            )
        return await call_next(request)

    @app.post("/api/login")
    async def login(body: LoginRequest, request: Request):
        """Validate the shared token and set the HttpOnly session cookie."""
        secure = request.url.scheme == "https" or request.headers.get(
            "x-forwarded-proto", ""
        ).split(",")[0].strip().lower() == "https"
        if not _token_matches(body.token):
            raise HTTPException(status_code=401, detail="Invalid token")
        from starlette.responses import JSONResponse

        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=body.token,
            httponly=True,
            samesite="strict",
            secure=secure,
            max_age=60 * 60 * 24 * 7,  # one week
            path="/",
        )
        return response


__all__ = [
    "AUTH_COOKIE_NAME",
    "CF_ACCESS_HEADER",
    "LoginRequest",
    "authenticate_request",
    "get_web_token",
    "install_auth",
    "require_web_token",
    "trust_cf_access_enabled",
]
