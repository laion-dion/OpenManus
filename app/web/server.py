"""OpenManus Web UI server.

FastAPI application that:

* serves the chat interface (static SPA in ``app/web/static``),
* streams assistant replies token-by-token over Server-Sent Events,
* streams every agentic tool execution (bash scripts included, secrets
  redacted) over a second SSE channel consumed by the retractable
  terminal-style logs panel,
* requires authentication on every /api route except /api/health
  (shared-token login cookie or optional Cloudflare Access header).

Run (defaults to localhost only — safe for a local Ubuntu server):

    OPENMANUS_WEB_TOKEN="$(openssl rand -hex 32)" python -m app.web.server

The server REFUSES TO START without OPENMANUS_WEB_TOKEN set, and binds
127.0.0.1 unless you explicitly pass --host / set OPENMANUS_WEB_HOST.
To expose it through a Cloudflare Tunnel, point the tunnel at
http://localhost:8000 (never widen the bind address) and put Cloudflare
Access in front of the public hostname, or rely on the login token.

Optional environment:
    OPENMANUS_WEB_HOST=127.0.0.1      bind address (default, keep as-is)
    OPENMANUS_WEB_PORT=8000           bind port
    OPENMANUS_TRUST_CF_ACCESS=true    accept Cf-Access-Jwt-Assertion header
    OPENMANUS_CORS_ALLOW_REGEX='^https://([a-z0-9-]+\\.)*example\\.com$'
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import config
from app.logger import logger
from app.web.auth import install_auth, require_web_token
from app.web.cors import install_cors
from app.web.event_bus import event_bus
from app.web.log_sink import install_log_sink
from app.web.redaction import redact_and_truncate
from app.web.streaming import activate

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="OpenManus Web UI", version="0.3.0")

# CORS: strict by default (localhost origins only); see app/web/cors.py.
install_cors(app)

# Auth gate: all /api/* except /api/health (+ /api/login) need the session cookie.
install_auth(app)


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="User message")
    session_id: str = Field(default="default", description="Per-tab agent session")


class _Session:
    """One Manus agent instance kept warm across turns of a conversation."""

    def __init__(self):
        self.agent = None
        self.lock = asyncio.Lock()
        self.last_used = time.time()

    async def get_agent(self):
        if self.agent is None:
            from app.agent.manus import Manus

            self.agent = await Manus.create()
        return self.agent

    async def cleanup(self):
        if self.agent is not None:
            try:
                await self.agent.cleanup()
            except Exception as exc:
                logger.debug(f"Agent cleanup failed: {exc}")
            self.agent = None


_sessions: Dict[str, _Session] = {}


def _get_session(session_id: str) -> _Session:
    session = _sessions.get(session_id)
    if session is None:
        session = _sessions[session_id] = _Session()
    return session


def _sse(event: dict) -> str:
    """Serialise one event in Server-Sent Events wire format."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.on_event("startup")
async def _startup():
    # Fail hard and early: no web token -> no server. (Also enforced per-request
    # by the auth gate; this makes misconfiguration obvious at boot.)
    require_web_token()
    event_bus.bind_loop(asyncio.get_running_loop())
    # Mirror all loguru records (tool activations, bash output, errors...) into
    # the "logs" channel so the browser terminal shows them live. Records are
    # secret-redacted + truncated inside the sink (app/web/log_sink.py).
    install_log_sink("logs")
    logger.info("OpenManus Web UI ready — SSE channels: /api/stream/chat, /api/stream/logs")


@app.on_event("shutdown")
async def _shutdown():
    for session in _sessions.values():
        await session.cleanup()
    _sessions.clear()


@app.post("/api/chat")
async def chat(request: Request, body: ChatRequest):
    """Kick off an agent turn; deltas arrive on the /api/stream/chat SSE feed."""
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")

    session = _get_session(body.session_id)
    stream = activate()  # binds a TokenStream to this task's contextvar scope

    async def run_agent():
        async with session.lock:
            session.last_used = time.time()
            event_bus.publish(
                "chat", {"type": "turn_start", "prompt": prompt, "session": body.session_id}
            )
            try:
                agent = await session.get_agent()
                result = await agent.run(prompt)
                # The final result can echo secrets (tool output, config dumps);
                # redact + truncate before it reaches the browser.
                event_bus.publish(
                    "chat", {"type": "turn_result", "result": redact_and_truncate(str(result or ""))}
                )
            except asyncio.CancelledError:
                stream.finish()
                raise
            except Exception as exc:
                logger.exception(f"Agent run failed: {exc}")
                event_bus.publish("chat", {"type": "error", "message": str(exc)})
            finally:
                stream.finish()
                event_bus.publish("chat", {"type": "turn_end"})

    # Detached task: the POST returns immediately, tokens flow via SSE. The
    # contextvar copy made by create_task carries our TokenStream into the run.
    asyncio.create_task(run_agent())
    return {"status": "started", "session_id": body.session_id}


@app.get("/api/stream/chat")
async def stream_chat():
    """SSE feed of assistant tokens (delta events rendered char-by-char)."""

    async def gen():
        yield _sse({"type": "hello", "channel": "chat"})
        async with event_bus.subscribe("chat", replay=False) as sub:
            async for event in sub:
                yield _sse(event)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # cloudflared/nginx: never buffer the stream.
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/stream/logs")
async def stream_logs():
    """SSE feed of agentic runtime logs + structured tool events."""

    async def gen():
        yield _sse({"kind": "hello", "channel": "logs", "ts": time.strftime("%H:%M:%S")})
        async with event_bus.subscribe("logs", replay=True) as sub:
            async for event in sub:
                yield _sse(event)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/health")
async def health() -> dict:
    llm_settings = config.llm["default"] if isinstance(config.llm, dict) else config.llm
    return {
        "status": "ok",
        "model": llm_settings.model,
        "active_sessions": len(_sessions),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = STATIC_DIR / "index.html"
    if not html_file.exists():
        raise HTTPException(status_code=404, detail="Web UI assets missing")
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main():
    import uvicorn

    # Default bind: loopback only. This app can execute bash through the
    # agent's tools, so it must never listen on 0.0.0.0 by default; expose it
    # via a Cloudflare Tunnel pointing at http://localhost:8000 instead.
    host = os.getenv("OPENMANUS_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("OPENMANUS_WEB_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
