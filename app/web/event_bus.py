"""In-process pub/sub event bus powering the Web UI's Server-Sent Events streams.

Two independent channels are exposed to the browser:

* ``chat``   - assistant text deltas, streamed token by token (SSE).
* ``logs``   - agentic runtime logs (tool activations, bash commands being
               executed, results, errors), captured live from loguru.

The bus is intentionally non-blocking: publishers drop events when a consumer
falls behind instead of stalling the agent loop, which keeps the UI rendering
smooth even under bursty output.
"""

import asyncio
from typing import Any, Dict, List, Optional

MAX_CHANNEL_HISTORY = 500


class EventBus:
    """A tiny fan-out pub/sub hub with per-channel replay history."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._history: Dict[str, List[Dict[str, Any]]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the running loop so thread-based loggers can publish safely."""
        self._loop = loop

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------
    def publish(self, channel: str, event: Dict[str, Any]) -> None:
        """Push an event to every subscriber of ``channel`` (fire and forget).

        Safe to call from the bound event loop or from another thread.
        """
        history = self._history.setdefault(channel, [])
        history.append(event)
        if len(history) > MAX_CHANNEL_HISTORY:
            del history[: len(history) - MAX_CHANNEL_HISTORY]

        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None

        if current is not None and (self._loop is None or current is self._loop):
            self._put_nowait(channel, event)
        elif self._loop is not None:
            # Called from a worker thread: hop onto the main loop.
            self._loop.call_soon_threadsafe(self._put_nowait, channel, event)
        # Otherwise there are no listeners yet; history still holds the event.

    def _put_nowait(self, channel: str, event: Dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(channel, [])):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop the oldest pending event to stay real-time.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Subscribing
    # ------------------------------------------------------------------
    def subscribe(self, channel: str, replay: bool = True, maxsize: int = 1000):
        return _Subscription(self, channel, replay=replay, maxsize=maxsize)

    def _register(self, channel: str, queue: asyncio.Queue) -> None:
        self._subscribers.setdefault(channel, []).append(queue)

    def _unregister(self, channel: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(channel, [])
        if queue in subs:
            subs.remove(queue)

    def get_history(self, channel: str) -> List[Dict[str, Any]]:
        return list(self._history.get(channel, []))

    def clear(self, channel: Optional[str] = None) -> None:
        if channel is None:
            self._history.clear()
        else:
            self._history.pop(channel, None)


class _Subscription:
    """Async context manager yielding SSE-ready events on a channel."""

    def __init__(self, bus: EventBus, channel: str, replay: bool, maxsize: int):
        self._bus = bus
        self._channel = channel
        self._replay = replay
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._pending_history: List[Dict[str, Any]] = []

    async def __aenter__(self) -> "_Subscription":
        if self._replay:
            self._pending_history = self._bus.get_history(self._channel)
        self._bus._register(self._channel, self._queue)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._bus._unregister(self._channel, self._queue)

    def __aiter__(self):
        return self

    async def __anext__(self) -> Dict[str, Any]:
        if self._pending_history:
            return self._pending_history.pop(0)
        try:
            return await self._queue.get()
        except asyncio.CancelledError:
            raise StopAsyncIteration


# Singleton shared by the FastAPI server and the loguru sink.
event_bus = EventBus()
