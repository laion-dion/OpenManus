"""Per-request token stream bridge between the agent's LLM calls and the SSE UI.

While a Web request is active, ``LLM.ask`` pushes every streamed chunk into the
current ``TokenStream`` (a contextvar-scoped queue). The FastAPI SSE endpoint
drains that queue and relays deltas to the browser character group by character
group, giving real-time typing without blocking the agent loop.
"""

import asyncio
from contextvars import ContextVar
from typing import List, Optional


class TokenStream:
    """Thread/asyncio-safe conduit for one chat run."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2048)
        self.deltas: List[str] = []
        self.done = False

    def emit(self, text: str) -> None:
        """Publish one raw LLM chunk (non-blocking; drops on backpressure)."""
        if not text or self.done:
            return
        self.deltas.append(text)
        try:
            self.queue.put_nowait({"type": "delta", "text": text})
        except asyncio.QueueFull:
            # Consumer too slow: drop oldest pending delta so we never stall the agent.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait({"type": "delta", "text": text})
            except Exception:
                pass

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        try:
            self.queue.put_nowait({"type": "stream_end", "text": "".join(self.deltas)})
        except asyncio.QueueFull:
            pass


# Context variable holding the stream of the currently-running web request.
current_stream: ContextVar[Optional[TokenStream]] = ContextVar(
    "openmanus_token_stream", default=None
)


def activate() -> TokenStream:
    """Bind a fresh TokenStream to this task context and return it."""
    stream = TokenStream()
    current_stream.set(stream)
    return stream


def publish_delta(text: str) -> None:
    """Called from app.llm streaming paths: forward a chunk to the active stream, if any."""
    stream = current_stream.get()
    if stream is not None:
        stream.emit(text)


def is_stream_active() -> bool:
    """True when a Web SSE TokenStream is bound to the current task context.

    Used by ``LLM.ask_tool`` to decide whether to switch the tool-call request
    to streaming mode (CLI runs keep the classic non-streaming path).
    """
    stream = current_stream.get()
    return stream is not None and not stream.done


__all__ = ["TokenStream", "current_stream", "activate", "publish_delta", "is_stream_active"]
