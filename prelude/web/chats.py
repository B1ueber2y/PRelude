"""A PR's discussion space: threads with Claude, like threads in a chat room."""

from __future__ import annotations

import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import chat
from ..discuss import DiscussionBusy
from .context import Web, WebDep, event_stream

router = APIRouter()


def discuss_url(owner: str, repo: str, number: int) -> str:
    return f"/prs/{owner}/{repo}/{number}/discuss"


def _chat_or_404(w: Web, chat_id: int):
    c = w.store.get_chat(chat_id)
    if c is None:
        raise HTTPException(404, "No such thread")
    return c


def _thread(w: Web, request: Request, chat_id: int, *, open: bool, **extra) -> HTMLResponse:
    row = _chat_or_404(w, chat_id)
    c = next(x for x in w.store.chats_for(row["owner"], row["repo"], row["number"]) if x["id"] == chat_id)
    if open:
        w.store.update_chat(chat_id, seen_at=time.time())
    return w.render(request, "_chat.html", {"c": c, "messages": w.store.chat_messages(chat_id), "open": open, **extra})


@router.get("/prs/{owner}/{repo}/{number}/discuss", response_class=HTMLResponse)
def page(w: WebDep, request: Request, owner: str, repo: str, number: int, open: int | None = None):
    review = w.store.latest_review_for(owner, repo, number, done_only=False)
    chats = w.store.chats_for(owner, repo, number)
    if review is None and not chats:
        raise HTTPException(404, "PRelude hasn't reviewed this PR")
    source = chat.source_review(w.store, owner, repo, number)
    shown = {c["id"] for c in chats if c["busy"] or c["id"] == open}
    for c in chats:
        if c["id"] == open:
            w.store.update_chat(c["id"], seen_at=time.time())
    return w.render(
        request,
        "discuss.html",
        {
            "owner": owner,
            "repo": repo,
            "number": number,
            "review": review,
            "source": source,
            "threads": [{"c": c, "messages": w.store.chat_messages(c["id"]), "open": c["id"] in shown} for c in chats],
            "findings": w.store.finding_discussions(owner, repo, number),
        },
    )


@router.post("/prs/{owner}/{repo}/{number}/discuss", response_class=HTMLResponse)
def new_thread(w: WebDep, request: Request, owner: str, repo: str, number: int, text: str = Form(...)):
    try:
        chat_id = chat.start_thread(w.store, w.cfg, w.chat_broker, owner, repo, number, text)
    except ValueError as e:
        return w.flash(str(e))
    return _thread(w, request, chat_id, open=True)


@router.get("/chats/{chat_id}")
def go_to_thread(w: WebDep, chat_id: int):
    c = _chat_or_404(w, chat_id)
    return RedirectResponse(f"{discuss_url(c['owner'], c['repo'], c['number'])}?open={chat_id}#chat-{chat_id}")


@router.get("/chats/{chat_id}/thread", response_class=HTMLResponse)
def thread(w: WebDep, request: Request, chat_id: int, open: bool = True):
    return _thread(w, request, chat_id, open=open)


@router.post("/chats/{chat_id}/seen")
def seen(w: WebDep, chat_id: int):
    _chat_or_404(w, chat_id)
    w.store.update_chat(chat_id, seen_at=time.time())
    return Response(status_code=204)


@router.post("/chats/{chat_id}/messages", response_class=HTMLResponse)
def reply(w: WebDep, request: Request, chat_id: int, text: str = Form(...)):
    _chat_or_404(w, chat_id)
    try:
        chat.post_message(w.store, w.cfg, w.chat_broker, chat_id, text)
        extra = {}
    except (ValueError, DiscussionBusy) as e:
        extra = {"error": str(e), "draft_message": text}
    return _thread(w, request, chat_id, open=True, **extra)


@router.post("/chats/{chat_id}/delete", response_class=HTMLResponse)
def delete(w: WebDep, chat_id: int):
    if _chat_or_404(w, chat_id)["busy"]:
        return w.flash("Claude is still replying in this thread; delete it when it's done.")
    w.store.delete_chat(chat_id)
    return HTMLResponse("")


@router.get("/chats/{chat_id}/busy")
def busy(w: WebDep, chat_id: int):
    return {"busy": bool(_chat_or_404(w, chat_id)["busy"])}


@router.get("/chats/{chat_id}/stream")
def stream(w: WebDep, chat_id: int):
    _chat_or_404(w, chat_id)

    def is_busy() -> bool:
        c = w.store.get_chat(chat_id)
        return bool(c and c["busy"])

    return event_stream(w.chat_broker, chat_id, is_busy)
