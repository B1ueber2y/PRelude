"""Phone web UI: server-rendered pages + htmx, served on localhost only."""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .. import memory_agent
from ..config import Config
from ..db import Store
from ..drafts import DraftWriter
from ..review import run_review
from . import auth, findings, memory, reviews
from .auth import COOKIE
from .context import PACKAGE, Web

__all__ = ["COOKIE", "create_app", "serve"]

log = logging.getLogger(__name__)

MANIFEST = json.dumps({
    "name": "PRelude", "short_name": "PRelude", "start_url": "/", "display": "standalone",
    "background_color": "#0f1115", "theme_color": "#0f1115",
    "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
})


def create_app(
    cfg: Config, store: Store | None = None, *, review_runner=None, learner=None, writer: DraftWriter | None = None,
    watch: bool = False,
) -> FastAPI:
    store = store or Store(cfg.db_path)
    store.fail_stale_reviews()
    w = Web(
        cfg=cfg,
        store=store,
        writer=writer or DraftWriter(cfg),
        run_review=review_runner or (lambda review_id: run_review(store, cfg, review_id)),
        learn=learner or (lambda finding_id, action: memory_agent.learn_async(store, cfg, finding_id, action)),
    )

    app = FastAPI(title="PRelude", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.web = w
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")
    auth.install(app, w)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/manifest.webmanifest")
    def manifest():
        return Response(MANIFEST, media_type="application/manifest+json")

    for module in (reviews, findings, memory):
        app.include_router(module.router)

    if watch and cfg.triggers.enabled:
        from ..poller import Poller

        app.state.poller = Poller(store, cfg, run=w.run_review)
        app.state.poller.start()
    return app


def serve(cfg: Config, host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    host = host or cfg.server.host
    port = port or cfg.server.port
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("Binding to %s: PRelude is meant to listen on localhost behind `tailscale serve`.", host)
    app = create_app(cfg, watch=True)
    print(f"PRelude on http://{host}:{port}  —  open once with: http://{host}:{port}/?key={cfg.access_key()}")
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=False)
