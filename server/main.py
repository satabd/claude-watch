"""FastAPI entry point. Run with:
    uvicorn server.main:app --reload --port 8765
"""
from __future__ import annotations

# Logging must be configured BEFORE any submodule that emits log lines at
# module-import time (notably server.providers.__init__ which logs the
# concurrency limit, and server.remote_watcher which logs the tunables).
# Override the level with WATCHER_LOG_LEVEL=DEBUG / WARNING / etc.
from .log_config import configure_logging

configure_logging()

import asyncio  # noqa: E402  (must follow configure_logging())
from contextlib import asynccontextmanager  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import db  # noqa: E402
from .remote_watcher import RemoteWatcherManager  # noqa: E402
from .routes import (  # noqa: E402
    actions,
    projects,
    prompt_writer,
    remotes as remotes_routes,
    reviews as reviews_routes,
    runtime as runtime_routes,
    settings as settings_routes,
)
from .routes import stream as stream_routes  # noqa: E402
from .watcher import WatcherService  # noqa: E402

watcher_service = WatcherService()
remote_watcher_manager = RemoteWatcherManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    loop = asyncio.get_running_loop()
    watcher_service.start(loop)
    # Start one background tailer per enabled remote host. Each maintains a
    # persistent SSH/SFTP session and polls every ~2s, append-fetching new
    # bytes into the local mirror dir; the local watchdog observer above
    # then emits SSE events the same way it does for native local sessions.
    await remote_watcher_manager.start_all()
    try:
        yield
    finally:
        await remote_watcher_manager.stop_all()
        watcher_service.stop()


app = FastAPI(title="Claude Watcher", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(actions.router)
app.include_router(settings_routes.router)
app.include_router(prompt_writer.router)
app.include_router(remotes_routes.router)
app.include_router(reviews_routes.router)
app.include_router(runtime_routes.router)
app.include_router(stream_routes.make_router(watcher_service))


@app.get("/api/health")
def health():
    return {"ok": True}


# In production, serve the built web app from ./web/dist
WEB_DIST = Path(__file__).parent.parent / "web" / "dist"
if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="assets")

    @app.get("/")
    def index():
        return FileResponse(str(WEB_DIST / "index.html"))

    # Unknown /api and /sse paths must 404 as JSON for EVERY method. Without
    # this, a POST to an endpoint the running build doesn't have (stale
    # server, older deploy) matched only the GET-only SPA fallback below and
    # surfaced in the UI as a baffling "405 Method Not Allowed" instead of a
    # clear 404. Registered before the catch-all so it wins.
    @app.api_route(
        "/{api_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    def api_not_found(api_path: str):
        if api_path.startswith(("api/", "sse/")):
            return JSONResponse(
                {"detail": {"reason": f"No such endpoint: /{api_path}"}},
                status_code=404,
            )
        # SPA fallback: any other path → index.html
        return FileResponse(str(WEB_DIST / "index.html"))
