"""Server-Sent Events broadcast for dashboard real-time updates."""
import asyncio
import json
import time
from typing import AsyncGenerator


class SSEManager:
    """Manages SSE client connections and broadcasting."""

    def __init__(self):
        self._clients: list[asyncio.Queue] = []

    def connect(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._clients.append(q)
        return q

    def disconnect(self, q: asyncio.Queue):
        if q in self._clients:
            self._clients.remove(q)

    async def broadcast(self, event: str, data: dict):
        payload = {"event": event, "data": data, "ts": int(time.time())}
        dead = []
        for q in self._clients:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._clients.remove(q)

    async def stream(self, q: asyncio.Queue) -> AsyncGenerator[str, None]:
        """Yield SSE-formatted strings for StreamingResponse."""
        yield "event: connected\ndata: {}\n\n"
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # heartbeat
        except asyncio.CancelledError:
            pass


# Global instances — one per messenger
tg_sse = SSEManager()
max_sse = SSEManager()
