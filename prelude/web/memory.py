"""Reviewer memory pages: read, edit by hand, or tell Claude what to change."""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .. import memory_agent
from ..memory import GLOBAL, STYLE, split_scope, title
from .context import Web, WebDep

router = APIRouter()


def _scope(scope: str) -> str:
    if scope not in (GLOBAL, STYLE):
        try:
            split_scope(scope)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    return scope


def _ctx(w: Web, scope: str, **extra) -> dict:
    return {"scope": scope, "title": title(scope), "doc": w.memory.read(scope),
            "messages": w.store.memory_messages(scope), **extra}


@router.get("/memory", response_class=HTMLResponse)
def memory_index(w: WebDep, request: Request):
    repos = sorted({f"{o}/{r}" for o, r in w.store.reviewed_repos()})
    docs = [{"scope": s, "title": title(s), "doc": w.memory.read(s)} for s in (GLOBAL, STYLE, *repos)]
    return w.render(request, "memory.html", {"docs": docs, "changes": w.store.memory_changes()})


@router.get("/memory/doc", response_class=HTMLResponse)
def memory_doc(w: WebDep, request: Request, scope: str = GLOBAL):
    return w.render(request, "memory_doc.html", _ctx(w, _scope(scope)))


@router.get("/memory/doc/edit", response_class=HTMLResponse)
def memory_doc_edit(w: WebDep, request: Request, scope: str = GLOBAL):
    return w.render(request, "_memory_edit.html", _ctx(w, _scope(scope)))


@router.get("/memory/doc/view", response_class=HTMLResponse)
def memory_doc_view(w: WebDep, request: Request, scope: str = GLOBAL):
    return w.render(request, "_memory_view.html", _ctx(w, _scope(scope)))


@router.post("/memory/doc", response_class=HTMLResponse)
def memory_doc_save(w: WebDep, request: Request, scope: str = Form(...), text: str = Form("")):
    w.memory.write(_scope(scope), text)
    return w.render(request, "_memory_view.html", _ctx(w, scope))


@router.post("/memory/chat", response_class=HTMLResponse)
def memory_chat(w: WebDep, request: Request, scope: str = Form(...), text: str = Form(...)):
    error = None
    try:
        memory_agent.chat(w.store, w.cfg, _scope(scope), text)
    except ValueError as e:
        error = str(e)
    return w.render(request, "_memory_doc_body.html", _ctx(w, scope, error=error))


@router.post("/memory/changes/{message_id}/undo")
def memory_undo(w: WebDep, message_id: int):
    try:
        memory_agent.undo(w.store, w.cfg, message_id)
    except KeyError as e:
        raise HTTPException(404, "Nothing to undo") from e
    except memory_agent.MemoryChanged as e:
        return HTMLResponse(f'<span class="error">{escape(str(e))}</span>')
    return Response(status_code=204, headers={"HX-Refresh": "true"})
