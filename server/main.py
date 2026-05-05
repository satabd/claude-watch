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
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import db  # noqa: E402
from .remote_watcher import RemoteWatcherManager  # noqa: E402
from .routes import actions, projects, prompt_writer, remotes as remotes_routes, settings as settings_routes  # noqa: E402
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

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        # SPA fallback: anything not /api or /sse → index.html
        if full_path.startswith(("api/", "sse/")):
            return {"error": "not found"}
        return FileResponse(str(WEB_DIST / "index.html"))
