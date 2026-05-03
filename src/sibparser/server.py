"""FastAPI server + minimal HTML UI for configuring and running the parser.

Endpoints:

* ``GET  /``                 - serves the static index.html UI.
* ``GET  /api/status``       - check Drive auth and config status.
* ``POST /api/auth/drive``   - run interactive OAuth flow (synchronously).
* ``POST /api/discover``     - discover the catalog tree (synchronously).
* ``GET  /api/categories``   - return last-discovered tree (cached in memory).
* ``POST /api/run``          - start a run on a worker thread.
* ``POST /api/cancel``       - cancel the current run.
* ``WS   /ws/progress``      - live progress events from the runner.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .drive import DriveClient, open_drive
from .runner import ProgressEvent, Runner, RunRequest
from .site import CategoryNode, category_node_to_dict
from .state import State

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = State(settings.state_db)
        self.drive: DriveClient | None = None
        self.tree: list[CategoryNode] = []
        self._tree_lock = threading.Lock()
        self.runner: Runner | None = None
        self.runner_thread: threading.Thread | None = None
        self.queue: asyncio.Queue[ProgressEvent] = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None

    def emit(self, event: ProgressEvent) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    state = AppState(settings)
    state.loop = asyncio.get_running_loop()
    app.state.app_state = state
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Siberian Health Parser", lifespan=lifespan)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        index_html = STATIC_DIR / "index.html"
        if not index_html.exists():
            raise HTTPException(status_code=500, detail="UI assets missing")
        return FileResponse(index_html)

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        s: AppState = app.state.app_state
        cfg = s.settings
        creds_present = cfg.google_credentials.exists()
        token_present = cfg.google_token.exists()
        return {
            "credentials_present": creds_present,
            "token_present": token_present,
            "drive_authorized": s.drive is not None,
            "tree_loaded": bool(s.tree),
            "headful": cfg.headful,
            "downloads_dir": str(cfg.downloads_dir),
            "state_db": str(cfg.state_db),
            "drive_root_folder": cfg.drive_root_folder,
        }

    class DriveAuthBody(BaseModel):
        credentials_path: str | None = None

    @app.post("/api/auth/drive")
    def auth_drive(body: DriveAuthBody) -> dict[str, Any]:
        s: AppState = app.state.app_state
        cfg = s.settings
        cred_path = Path(body.credentials_path) if body.credentials_path else cfg.google_credentials
        if not cred_path.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Файл credentials.json не найден по пути {cred_path}",
            )
        try:
            s.drive = open_drive(
                client_secret_path=cred_path,
                token_path=cfg.google_token,
                state=s.state,
                root_folder_name=cfg.drive_root_folder,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "root_folder": cfg.drive_root_folder}

    @app.post("/api/discover")
    def discover() -> dict[str, Any]:
        s: AppState = app.state.app_state
        runner = Runner(settings=s.settings, state=s.state, drive=s.drive, progress=s.emit)
        with s._tree_lock:
            s.tree = runner.discover_tree()
        return {"tree": [category_node_to_dict(n) for n in s.tree]}

    @app.get("/api/categories")
    def categories() -> dict[str, Any]:
        s: AppState = app.state.app_state
        return {"tree": [category_node_to_dict(n) for n in s.tree]}

    class RunBody(BaseModel):
        selected_category_paths: list[str] = Field(default_factory=list)
        single_product_url: str | None = None
        products_per_category_limit: int = 0
        upload_to_drive: bool = True

    @app.post("/api/run")
    def run(body: RunBody) -> dict[str, Any]:
        s: AppState = app.state.app_state
        if s.runner_thread and s.runner_thread.is_alive():
            raise HTTPException(status_code=409, detail="Уже запущено")
        if body.upload_to_drive and s.drive is None:
            raise HTTPException(
                status_code=400,
                detail="Не авторизован Google Drive. Нажми «Войти в Google» или сними галку «Загружать на Диск».",
            )

        request = RunRequest(
            selected_category_paths=body.selected_category_paths,
            single_product_url=body.single_product_url,
            products_per_category_limit=body.products_per_category_limit,
            upload_to_drive=body.upload_to_drive,
        )
        runner = Runner(
            settings=s.settings,
            state=s.state,
            drive=s.drive if body.upload_to_drive else None,
            progress=s.emit,
        )
        s.runner = runner

        def _target() -> None:
            try:
                runner.run(request)
            except Exception:
                log.exception("runner crashed")

        thread = threading.Thread(target=_target, daemon=True, name="sibparser-runner")
        s.runner_thread = thread
        thread.start()
        return {"started": True}

    @app.post("/api/cancel")
    def cancel() -> dict[str, Any]:
        s: AppState = app.state.app_state
        if s.runner:
            s.runner.cancel()
        return {"ok": True}

    @app.websocket("/ws/progress")
    async def ws_progress(ws: WebSocket) -> None:
        await ws.accept()
        s: AppState = app.state.app_state
        try:
            while True:
                event = await s.queue.get()
                await ws.send_text(
                    json.dumps(
                        {"kind": event.kind, "message": event.message, "data": event.data},
                        ensure_ascii=False,
                    )
                )
        except WebSocketDisconnect:
            return
        except Exception:
            log.exception("websocket error")
            await ws.close()

    return app


def serve() -> None:
    """Convenience entrypoint used by the CLI."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "sibparser.server:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level="info",
        reload=False,
    )
