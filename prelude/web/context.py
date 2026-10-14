"""Shared state and helpers for the web routes."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache, lru_cache
from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import drafting
from ..codeview import CodeView, build, read_head_file
from ..config import Config, size_label
from ..db import Store
from ..diffmap import FileDiff, parse_diff
from ..discuss import Broker, current_draft
from ..drafts import DraftError, DraftWriter
from ..memory import Memory
from ..textfmt import render_text

PACKAGE = Path(__file__).resolve().parent.parent


def ago(timestamp: float) -> str:
    seconds = max(0, time.time() - timestamp)
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{int(seconds // size)} {unit} ago"
    return "just now"


@cache
def static_url(name: str) -> str:
    """Versioned URL so phones never keep a stale copy after an update."""
    path = PACKAGE / "static" / name
    version = int(path.stat().st_mtime) if path.exists() else 0
    return f"/static/{name}?v={version}"


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=PACKAGE / "templates")
    templates.env.globals["static"] = static_url
    templates.env.filters["fromjson"] = json.loads
    templates.env.filters["text"] = render_text
    templates.env.filters["size_label"] = size_label
    templates.env.filters["ago"] = ago
    return templates


@dataclass
class Web:
    cfg: Config
    store: Store
    writer: DraftWriter
    run_review: Callable[[int], None]
    learn: Callable[[int, str], object]
    broker: Broker = field(default_factory=Broker)
    templates: Jinja2Templates = field(default_factory=make_templates)

    def __post_init__(self) -> None:
        self.memory = Memory(self.cfg)
        self._diff_files = lru_cache(maxsize=32)(self._parse_diff)

    # ---- responses ---------------------------------------------------------

    def render(self, request: Request, name: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
        return self.templates.TemplateResponse(request, name, ctx, status_code=status_code)

    @staticmethod
    def flash(message: str, kind: str = "error") -> HTMLResponse:
        return HTMLResponse(
            f'<div class="flash {kind}">{escape(message)}</div>',
            headers={"HX-Retarget": "#flash", "HX-Reswap": "innerHTML"},
        )

    # ---- lookups -----------------------------------------------------------

    def review_or_404(self, review_id: int):
        r = self.store.get_review(review_id)
        if r is None:
            raise HTTPException(404, "No such review")
        return r

    def finding_or_404(self, finding_id: int):
        f = self.store.get_finding(finding_id)
        if f is None:
            raise HTTPException(404, "No such finding")
        return f

    def _parse_diff(self, review_id: int, head_sha: str | None) -> dict[str, FileDiff]:
        r = self.store.get_review(review_id)
        return parse_diff(r["diff"] or "") if r else {}

    def code_view(self, f, r, start: int | None = None, end: int | None = None) -> CodeView | None:
        """The PR's version of the file around the finding, or None (then the diff snippet is shown)."""
        if f["side"] != "RIGHT" or not f["line"] or not r["worktree"]:
            return None
        lines = read_head_file(r["worktree"], f["path"])
        if lines is None:
            return None
        fd = self._diff_files(r["id"], r["head_sha"]).get(f["path"])
        first = f["start_line"] or f["line"]
        return build(lines, fd, start if start is not None else first - 3, end if end is not None else f["line"] + 3)

    def card_context(self, f, r) -> dict:
        """What a finding card in the review feed needs."""
        return {
            "f": f,
            "r": r,
            "draft": current_draft(f),
            "edited": f["user_draft"] is not None and f["user_draft"] != f["claude_comment"],
            "code": self.code_view(f, r, (f["start_line"] or f["line"] or 1) - 3, (f["line"] or 1) + 3)
            if f["status"] != "discarded" else None,
            "drafts_enabled": self.cfg.drafts.enabled,
        }

    def card(self, request: Request, finding_id: int) -> HTMLResponse:
        f = self.finding_or_404(finding_id)
        return self.render(request, "_finding_card.html", self.card_context(f, self.review_or_404(f["review_id"])))

    @staticmethod
    def wants_card(request: Request, finding_id: int) -> bool:
        return request.headers.get("HX-Target") == f"card-{finding_id}"

    def finding_context(self, finding_id: int, **extra) -> dict:
        f = self.finding_or_404(finding_id)
        r = self.review_or_404(f["review_id"])
        fd = self._diff_files(r["id"], r["head_sha"]).get(f["path"])
        ids = [x["id"] for x in self.store.findings_for(r["id"])]
        idx = ids.index(f["id"])
        thread = self.store.thread(f["id"])
        return {
            "f": f,
            "r": r,
            "snippet": fd.snippet(f["line"], f["side"]) if (fd and f["line"]) else [],
            "code": self.code_view(f, r),
            "draft": current_draft(f),
            "edited": f["user_draft"] is not None and f["user_draft"] != f["claude_comment"],
            "messages": self.store.messages_for(f["id"]),
            "busy": bool(thread and thread["busy"]),
            "prev_id": ids[idx - 1] if idx > 0 else None,
            "next_id": ids[idx + 1] if idx + 1 < len(ids) else None,
            "position": idx + 1,
            "count": len(ids),
            "drafts_enabled": self.cfg.drafts.enabled,
            **extra,
        }

    # ---- actions -----------------------------------------------------------

    def set_draft(self, finding_id: int, text: str | None, *, learn: bool = True) -> str | None:
        """Set the reviewer's wording (None = Claude's), learn from it, and sync any GitHub draft.

        Returns an error message if the GitHub draft couldn't be updated.
        """
        f = self.finding_or_404(finding_id)
        text = text.strip() if text is not None else None
        user_draft = None if text is None or text == f["claude_comment"] else text
        self.store.update_finding(finding_id, user_draft=user_draft)
        if learn and user_draft is not None and user_draft != f["user_draft"]:
            self.learn(finding_id, "edit")
        try:
            drafting.sync(self.store, self.cfg, self.writer, finding_id)
        except DraftError as e:
            return str(e)
        return None


def web(request: Request) -> Web:
    return request.app.state.web


WebDep = Annotated[Web, Depends(web)]
