"""A finding: its wording, discard/restore, GitHub draft, and discussion with Claude."""

from __future__ import annotations

import json
import queue

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from .. import drafting
from ..discuss import DiscussionBusy, post_message
from ..drafts import DraftError
from .context import WebDep

router = APIRouter()

REFRESH = {"HX-Refresh": "true"}


@router.get("/findings/{finding_id}", response_class=HTMLResponse)
def finding_page(w: WebDep, request: Request, finding_id: int):
    return w.render(request, "finding.html", w.finding_context(finding_id))


@router.get("/findings/{finding_id}/code", response_class=HTMLResponse)
def code(w: WebDep, request: Request, finding_id: int, start: int, end: int):
    f = w.finding_or_404(finding_id)
    r = w.review_or_404(f["review_id"])
    view = w.code_view(f, r, start, end)
    if view is None:
        raise HTTPException(404, "File not available")
    return w.render(request, "_code.html", {"f": f, "code": view})


# ---- wording ---------------------------------------------------------------

@router.get("/findings/{finding_id}/draft", response_class=HTMLResponse)
def show_draft(w: WebDep, request: Request, finding_id: int, compact: bool = False):
    return w.render(request, "_draft.html", w.finding_context(finding_id, compact=compact))


@router.get("/findings/{finding_id}/draft/edit", response_class=HTMLResponse)
def edit_draft(w: WebDep, request: Request, finding_id: int, compact: bool = False):
    return w.render(request, "_draft_edit.html", w.finding_context(finding_id, compact=compact))


@router.post("/findings/{finding_id}/draft", response_class=HTMLResponse)
def save_draft(w: WebDep, request: Request, finding_id: int, text: str = Form(...), compact: bool = False):
    error = w.set_draft(finding_id, text)
    return w.render(request, "_draft.html", w.finding_context(finding_id, sync_error=error, compact=compact))


@router.post("/findings/{finding_id}/draft/reset", response_class=HTMLResponse)
def reset_draft(w: WebDep, request: Request, finding_id: int, compact: bool = False):
    error = w.set_draft(finding_id, None)
    return w.render(request, "_draft.html", w.finding_context(finding_id, sync_error=error, compact=compact))


@router.post("/messages/{message_id}/use")
def use_proposal(w: WebDep, message_id: int):
    m = w.store.get_message(message_id)
    if m is None or not m["proposal"]:
        raise HTTPException(404, "No proposal on this message")
    if error := w.set_draft(m["finding_id"], m["proposal"], learn=False):  # Claude's words, not the reviewer's
        return w.flash(error)
    return Response(status_code=204, headers=REFRESH)


# ---- status ----------------------------------------------------------------

@router.post("/findings/{finding_id}/discard")
def discard(w: WebDep, request: Request, finding_id: int):
    f = w.finding_or_404(finding_id)
    try:
        drafting.remove(w.store, w.cfg, w.writer, finding_id, new_status="discarded")
    except DraftError as e:
        return w.flash(f"Couldn't remove the GitHub draft comment: {e}")
    w.learn(finding_id, "discard")
    if w.wants_card(request, finding_id):
        return w.card(request, finding_id)
    next_id = w.finding_context(finding_id)["next_id"]
    return Response(status_code=204, headers={
        "HX-Redirect": f"/findings/{next_id}" if next_id else f"/reviews/{f['review_id']}",
    })


@router.post("/findings/{finding_id}/restore")
def restore(w: WebDep, request: Request, finding_id: int):
    w.finding_or_404(finding_id)
    w.store.update_finding(finding_id, status="pending")
    if w.wants_card(request, finding_id):
        return w.card(request, finding_id)
    return Response(status_code=204, headers=REFRESH)


@router.post("/findings/{finding_id}/github/add")
def github_add(w: WebDep, request: Request, finding_id: int):
    w.finding_or_404(finding_id)
    try:
        drafting.add(w.store, w.cfg, w.writer, finding_id)
    except DraftError as e:
        return w.flash(str(e))
    if w.wants_card(request, finding_id):
        return w.card(request, finding_id)
    return Response(status_code=204, headers=REFRESH)


@router.post("/findings/{finding_id}/github/remove")
def github_remove(w: WebDep, request: Request, finding_id: int):
    w.finding_or_404(finding_id)
    try:
        drafting.remove(w.store, w.cfg, w.writer, finding_id)
    except DraftError as e:
        return w.flash(str(e))
    if w.wants_card(request, finding_id):
        return w.card(request, finding_id)
    return Response(status_code=204, headers=REFRESH)


# ---- discussion ------------------------------------------------------------

@router.get("/findings/{finding_id}/thread", response_class=HTMLResponse)
def thread(w: WebDep, request: Request, finding_id: int):
    return w.render(request, "_thread.html", w.finding_context(finding_id))


@router.post("/findings/{finding_id}/messages", response_class=HTMLResponse)
def send_message(w: WebDep, request: Request, finding_id: int, text: str = Form(...)):
    w.finding_or_404(finding_id)
    try:
        post_message(w.store, w.cfg, w.broker, finding_id, text)
        extra = {}
    except (ValueError, DiscussionBusy) as e:
        extra = {"error": str(e), "draft_message": text}
    return w.render(request, "_thread.html", w.finding_context(finding_id, **extra))


@router.get("/findings/{finding_id}/stream")
def stream(w: WebDep, finding_id: int):
    w.finding_or_404(finding_id)
    q = w.broker.subscribe(finding_id)

    def events():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    t = w.store.thread(finding_id)
                    if not (t and t["busy"]):
                        yield "event: done\ndata: {}\n\n"
                        return
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {ev.kind}\ndata: {json.dumps(ev.data)}\n\n"
                if ev.kind in ("done", "error"):
                    return
        finally:
            w.broker.unsubscribe(finding_id, q)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
