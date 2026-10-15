"""A finding: its wording, discard/restore, GitHub draft, and discussion with Claude."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .. import drafting
from ..discuss import DiscussionBusy, current_draft, post_message
from ..drafts import DraftError
from .context import WebDep, event_stream
from .reviews import _pick

router = APIRouter()

REFRESH = {"HX-Refresh": "true"}


@router.get("/findings/{finding_id}", response_class=HTMLResponse)
def finding_page(w: WebDep, request: Request, finding_id: int):
    ctx = w.finding_context(finding_id)
    w.store.mark_thread_seen(finding_id)
    return w.render(request, "finding.html", ctx)


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
    if refused := w.refuse_if_posted(finding_id):
        return refused
    f = w.finding_or_404(finding_id)
    try:
        drafting.remove(w.store, w.cfg, w.writer, finding_id, new_status="discarded")
    except DraftError as e:
        return w.flash(f"Couldn't remove the GitHub draft comment: {e}")
    w.learn(finding_id, "discard")
    if w.wants_card(request, finding_id):
        return w.card(request, finding_id)
    if request.headers.get("X-Pane"):  # laptop split view: move on to the next finding
        return Response(status_code=204, headers={"HX-Redirect": _next_in_pane(w, request, f)})
    next_id = w.finding_context(finding_id)["next_id"]
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": f"/findings/{next_id}" if next_id else f"/reviews/{f['review_id']}",
        },
    )


def _next_in_pane(w, request: Request, f) -> str:
    """The review page showing the finding after `f` in the tab the pane was on."""
    show = parse_qs(urlparse(request.headers.get("HX-Current-URL", "")).query).get("show", [None])[0]
    findings = w.store.findings_for(f["review_id"])
    tab = [x["id"] for x in findings if x["id"] != f["id"] and (show in (None, "all") or x["status"] == show)]
    nxt = _pick(tab, [x["id"] for x in findings], f["id"])
    query = "&".join(p for p in (f"show={show}" if show else "", f"f={nxt}" if nxt else "") if p)
    return f"/reviews/{f['review_id']}" + (f"?{query}" if query else "")


@router.post("/findings/{finding_id}/restore")
def restore(w: WebDep, request: Request, finding_id: int):
    if refused := w.refuse_if_posted(finding_id):
        return refused
    w.finding_or_404(finding_id)
    w.store.update_finding(finding_id, status="pending")
    if w.wants_card(request, finding_id):
        return w.card(request, finding_id)
    return Response(status_code=204, headers=REFRESH)


@router.post("/findings/{finding_id}/github/add")
def github_add(w: WebDep, request: Request, finding_id: int):
    if refused := w.refuse_if_posted(finding_id):
        return refused
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
    if refused := w.refuse_if_posted(finding_id):
        return refused
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
def thread(w: WebDep, request: Request, finding_id: int, open: bool = True):
    """`open` is false for a folded thread on the PR's discussion page: not seen yet."""
    if open:
        w.store.mark_thread_seen(finding_id)
    if request.query_params.get("open") is not None:  # folded or not, it's on the PR's discussion page
        return _inline_thread(w, request, finding_id, open=open)
    return w.render(request, "_thread.html", w.finding_context(finding_id))


def _inline_thread(w, request: Request, finding_id: int, *, open: bool, **extra) -> HTMLResponse:
    f = w.finding_or_404(finding_id)
    ctx = {
        "f": w.store.comment_thread(finding_id),
        "messages": w.store.messages_for(finding_id),
        "draft": current_draft(f),
        "open": open,
        **extra,
    }
    return w.render(request, "_finding_thread.html", ctx)


@router.post("/findings/{finding_id}/seen")
def seen(w: WebDep, finding_id: int):
    w.finding_or_404(finding_id)
    w.store.mark_thread_seen(finding_id)
    return Response(status_code=204)


@router.post("/findings/{finding_id}/messages", response_class=HTMLResponse)
def send_message(w: WebDep, request: Request, finding_id: int, text: str = Form(...)):
    w.finding_or_404(finding_id)
    try:
        post_message(w.store, w.cfg, w.broker, finding_id, text)
        extra = {}
    except (ValueError, DiscussionBusy) as e:
        extra = {"error": str(e), "draft_message": text}
    w.store.mark_thread_seen(finding_id)
    if request.headers.get("X-Inline") == "1":  # on the PR's discussion page
        return _inline_thread(w, request, finding_id, open=True, **extra)
    return w.render(request, "_thread.html", w.finding_context(finding_id, **extra))


@router.get("/findings/{finding_id}/busy")
def busy(w: WebDep, finding_id: int):
    """Fallback for when a stream's final event is lost (e.g. through an SSH tunnel)."""
    w.finding_or_404(finding_id)
    t = w.store.thread(finding_id)
    return {"busy": bool(t and t["busy"])}


@router.get("/findings/{finding_id}/stream")
def stream(w: WebDep, finding_id: int):
    w.finding_or_404(finding_id)

    def busy() -> bool:
        t = w.store.thread(finding_id)
        return bool(t and t["busy"])

    return event_stream(w.broker, finding_id, busy)
