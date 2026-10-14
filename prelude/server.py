"""Phone web UI: server-rendered pages + htmx, localhost only, access-key cookie."""

from __future__ import annotations

import hmac
import json
import logging
import queue
import threading
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import memory_agent
from .config import Config
from .db import Store
from .diffmap import parse_diff
from .discuss import Broker, DiscussionBusy, current_draft, post_message
from .github import parse_pr
from .guard import RepoNotAllowed
from .memory import GLOBAL, STYLE, Memory, split_scope, title
from .review import run_review, start_review
from .textfmt import render_text

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
COOKIE = "prelude_key"


def create_app(cfg: Config, store: Store | None = None, *, review_runner=None, learner=None) -> FastAPI:
    store = store or Store(cfg.db_path)
    store.fail_stale_reviews()
    broker = Broker()
    key = cfg.access_key()
    runner = review_runner or (lambda review_id: run_review(store, cfg, review_id))
    learn = learner or (lambda finding_id, action: memory_agent.learn_async(store, cfg, finding_id, action))

    app = FastAPI(title="PRelude", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["fromjson"] = json.loads
    templates.env.filters["text"] = render_text

    @lru_cache(maxsize=32)
    def diff_files(review_id: int, head_sha: str | None):
        r = store.get_review(review_id)
        return parse_diff(r["diff"] or "") if r else {}

    # ---- access control ----------------------------------------------------

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        allowed_ts = cfg.server.allowed_tailscale_user
        ts_login = request.headers.get("tailscale-user-login")
        if allowed_ts and ts_login is not None and ts_login.lower() != allowed_ts.lower():
            return PlainTextResponse("Forbidden", status_code=403)
        if request.url.path in ("/manifest.webmanifest", "/healthz") or request.url.path.startswith("/static/"):
            return await call_next(request)

        supplied = request.query_params.get("key")
        if supplied is not None:
            if hmac.compare_digest(supplied, key):
                resp = RedirectResponse(request.url.path or "/", status_code=303)
                resp.set_cookie(
                    COOKIE, key, max_age=60 * 60 * 24 * 365, httponly=True, samesite="strict",
                    secure=request.headers.get("x-forwarded-proto") == "https",
                )
                return resp
            return PlainTextResponse("Invalid key", status_code=401)

        cookie = request.cookies.get(COOKIE)
        if not cookie or not hmac.compare_digest(cookie, key):
            return templates.TemplateResponse(request, "locked.html", status_code=401)
        return await call_next(request)

    # ---- helpers -----------------------------------------------------------

    def finding_or_404(finding_id: int):
        f = store.get_finding(finding_id)
        if f is None:
            raise HTTPException(404, "No such finding")
        return f

    def review_or_404(review_id: int):
        r = store.get_review(review_id)
        if r is None:
            raise HTTPException(404, "No such review")
        return r

    def finding_context(f) -> dict:
        r = review_or_404(f["review_id"])
        files = diff_files(r["id"], r["head_sha"])
        fd = files.get(f["path"])
        snippet = fd.snippet(f["line"], f["side"]) if (fd and f["line"]) else []
        ids = [x["id"] for x in store.findings_for(r["id"])]
        idx = ids.index(f["id"])
        thread = store.thread(f["id"])
        return {
            "f": f,
            "r": r,
            "snippet": snippet,
            "draft": current_draft(f),
            "edited": f["user_draft"] is not None and f["user_draft"] != f["claude_comment"],
            "messages": store.messages_for(f["id"]),
            "busy": bool(thread and thread["busy"]),
            "prev_id": ids[idx - 1] if idx > 0 else None,
            "next_id": ids[idx + 1] if idx + 1 < len(ids) else None,
            "position": idx + 1,
            "count": len(ids),
        }

    def partial(request: Request, name: str, ctx: dict) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx)

    # ---- pages -------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/manifest.webmanifest")
    def manifest():
        return Response(
            json.dumps({
                "name": "PRelude", "short_name": "PRelude", "start_url": "/", "display": "standalone",
                "background_color": "#0f1115", "theme_color": "#0f1115",
                "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
            }),
            media_type="application/manifest+json",
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return partial(request, "index.html", {"reviews": store.list_reviews(), "only_repo": cfg.only_repo})

    @app.get("/reviews/rows", response_class=HTMLResponse)
    def review_rows(request: Request):
        return partial(request, "_review_rows.html", {"reviews": store.list_reviews()})

    @app.post("/reviews")
    def create_review(request: Request, pr: str = Form(...)):
        try:
            ref = parse_pr(pr)
            review_id = start_review(store, cfg, ref, trigger_reason="manual (phone)")
        except (ValueError, RepoNotAllowed) as e:
            return partial(
                request, "index.html",
                {"reviews": store.list_reviews(), "only_repo": cfg.only_repo, "error": str(e), "pr": pr},
            )
        threading.Thread(target=runner, args=(review_id,), daemon=True, name=f"review-{review_id}").start()
        return RedirectResponse(f"/reviews/{review_id}", status_code=303)

    @app.get("/reviews/{review_id}", response_class=HTMLResponse)
    def review_page(request: Request, review_id: int, show: str = "pending"):
        r = review_or_404(review_id)
        findings = store.findings_for(review_id)
        shown = [f for f in findings if show == "all" or f["status"] == show]
        counts = {
            "pending": sum(1 for f in findings if f["status"] == "pending"),
            "discarded": sum(1 for f in findings if f["status"] == "discarded"),
            "all": len(findings),
        }
        return partial(request, "review.html", {"r": r, "findings": shown, "show": show, "counts": counts})

    @app.get("/reviews/{review_id}/status", response_class=HTMLResponse)
    def review_status(request: Request, review_id: int):
        r = review_or_404(review_id)
        if r["status"] in ("done", "failed"):
            # Reload the whole page so the findings appear.
            return Response(status_code=200, headers={"HX-Refresh": "true"})
        return partial(request, "_review_status.html", {"r": r})

    @app.get("/findings/{finding_id}", response_class=HTMLResponse)
    def finding_page(request: Request, finding_id: int):
        return partial(request, "finding.html", finding_context(finding_or_404(finding_id)))

    # ---- finding actions ---------------------------------------------------

    @app.get("/findings/{finding_id}/draft/edit", response_class=HTMLResponse)
    def edit_draft(request: Request, finding_id: int):
        return partial(request, "_draft_edit.html", finding_context(finding_or_404(finding_id)))

    @app.get("/findings/{finding_id}/draft", response_class=HTMLResponse)
    def show_draft(request: Request, finding_id: int):
        return partial(request, "_draft.html", finding_context(finding_or_404(finding_id)))

    @app.post("/findings/{finding_id}/draft", response_class=HTMLResponse)
    def save_draft(request: Request, finding_id: int, text: str = Form(...)):
        f = finding_or_404(finding_id)
        text = text.strip()
        store.update_finding(finding_id, user_draft=None if text == f["claude_comment"] else text)
        if text != current_draft(f) and text != f["claude_comment"]:
            learn(finding_id, "edit")
        return partial(request, "_draft.html", finding_context(finding_or_404(finding_id)))

    @app.post("/findings/{finding_id}/draft/reset", response_class=HTMLResponse)
    def reset_draft(request: Request, finding_id: int):
        finding_or_404(finding_id)
        store.update_finding(finding_id, user_draft=None)
        return partial(request, "_draft.html", finding_context(finding_or_404(finding_id)))

    @app.post("/findings/{finding_id}/discard")
    def discard(finding_id: int):
        f = finding_or_404(finding_id)
        store.update_finding(finding_id, status="discarded")
        learn(finding_id, "discard")
        ctx = finding_context(finding_or_404(finding_id))
        target = f"/findings/{ctx['next_id']}" if ctx["next_id"] else f"/reviews/{f['review_id']}"
        return Response(status_code=204, headers={"HX-Redirect": target})

    @app.post("/findings/{finding_id}/restore")
    def restore(finding_id: int):
        finding_or_404(finding_id)
        store.update_finding(finding_id, status="pending")
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    # ---- discussion --------------------------------------------------------

    @app.get("/findings/{finding_id}/thread", response_class=HTMLResponse)
    def thread(request: Request, finding_id: int):
        return partial(request, "_thread.html", finding_context(finding_or_404(finding_id)))

    @app.post("/findings/{finding_id}/messages", response_class=HTMLResponse)
    def send_message(request: Request, finding_id: int, text: str = Form(...)):
        finding_or_404(finding_id)
        error = None
        try:
            post_message(store, cfg, broker, finding_id, text)
        except (ValueError, DiscussionBusy) as e:
            error = str(e)
        ctx = finding_context(finding_or_404(finding_id))
        ctx["error"] = error
        if error:
            ctx["draft_message"] = text
        return partial(request, "_thread.html", ctx)

    @app.post("/messages/{message_id}/use", response_class=HTMLResponse)
    def use_proposal(request: Request, message_id: int):
        m = store.get_message(message_id)
        if m is None or not m["proposal"]:
            raise HTTPException(404, "No proposal on this message")
        f = finding_or_404(m["finding_id"])
        store.update_finding(f["id"], user_draft=None if m["proposal"] == f["claude_comment"] else m["proposal"])
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @app.get("/findings/{finding_id}/stream")
    def stream(finding_id: int):
        finding_or_404(finding_id)
        q = broker.subscribe(finding_id)

        def events():
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        ev = q.get(timeout=15)
                    except queue.Empty:
                        t = store.thread(finding_id)
                        if not (t and t["busy"]):
                            yield "event: done\ndata: {}\n\n"
                            return
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {ev.kind}\ndata: {json.dumps(ev.data)}\n\n"
                    if ev.kind in ("done", "error"):
                        return
            finally:
                broker.unsubscribe(finding_id, q)

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- reviewer memory ---------------------------------------------------

    memory = Memory(cfg)

    def check_scope(scope: str) -> str:
        if scope not in (GLOBAL, STYLE):
            try:
                split_scope(scope)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        return scope

    def memory_ctx(scope: str, **extra) -> dict:
        return {"scope": scope, "title": title(scope), "doc": memory.read(scope),
                "messages": store.memory_messages(scope), **extra}

    @app.get("/memory", response_class=HTMLResponse)
    def memory_index(request: Request):
        repos = sorted({f"{o}/{r}" for o, r in store.reviewed_repos()})
        docs = [{"scope": sc, "title": title(sc), "doc": memory.read(sc)} for sc in (GLOBAL, STYLE, *repos)]
        return partial(request, "memory.html", {"docs": docs, "changes": store.memory_changes()})

    @app.get("/memory/doc", response_class=HTMLResponse)
    def memory_doc(request: Request, scope: str = GLOBAL):
        return partial(request, "memory_doc.html", memory_ctx(check_scope(scope)))

    @app.get("/memory/doc/edit", response_class=HTMLResponse)
    def memory_doc_edit(request: Request, scope: str = GLOBAL):
        return partial(request, "_memory_edit.html", memory_ctx(check_scope(scope)))

    @app.get("/memory/doc/view", response_class=HTMLResponse)
    def memory_doc_view(request: Request, scope: str = GLOBAL):
        return partial(request, "_memory_view.html", memory_ctx(check_scope(scope)))

    @app.post("/memory/doc", response_class=HTMLResponse)
    def memory_doc_save(request: Request, scope: str = Form(...), text: str = Form("")):
        memory.write(check_scope(scope), text)
        return partial(request, "_memory_view.html", memory_ctx(scope))

    @app.post("/memory/chat", response_class=HTMLResponse)
    def memory_chat_send(request: Request, scope: str = Form(...), text: str = Form(...)):
        error = None
        try:
            memory_agent.chat(store, cfg, check_scope(scope), text)
        except ValueError as e:
            error = str(e)
        return partial(request, "_memory_doc_body.html", memory_ctx(scope, error=error))

    @app.post("/memory/changes/{message_id}/undo")
    def memory_undo(message_id: int):
        try:
            memory_agent.undo(store, cfg, message_id)
        except KeyError as e:
            raise HTTPException(404, "Nothing to undo") from e
        except memory_agent.MemoryChanged as e:
            return HTMLResponse(f'<span class="error">{e}</span>')
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    app.state.store = store
    app.state.broker = broker
    return app


def serve(cfg: Config, host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    host = host or cfg.server.host
    port = port or cfg.server.port
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("Binding to %s: PRelude is meant to listen on localhost behind `tailscale serve`.", host)
    app = create_app(cfg)
    print(f"PRelude on http://{host}:{port}  —  open once with: http://{host}:{port}/?key={cfg.access_key()}")
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=False)
