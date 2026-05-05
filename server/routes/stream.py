"""SSE stream of live events from the watcher."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from ..watcher import WatcherService

router = APIRouter()


def make_router(watcher: WatcherService) -> APIRouter:
    @router.get("/sse/live")
    async def live(request: Request):
        queue = watcher.broadcaster.subscribe()

        async def event_gen():
            try:
                # Initial "hello" so the client knows the stream is up
                yield {"event": "hello", "data": json.dumps({"ok": True})}
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        # Heartbeat to keep proxies happy
                        yield {"event": "ping", "data": "{}"}
                        continue
                    yield {
                        "event": payload.get("kind", "message"),
                        "data": json.dumps(payload, default=str),
                    }
            finally:
                watcher.broadcaster.unsubscribe(queue)

        return EventSourceResponse(event_gen())

    return router
