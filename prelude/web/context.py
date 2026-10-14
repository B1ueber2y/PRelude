"""Shared state and helpers for the web routes."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import drafting
from ..config import Config, size_label
from ..db import Store
from ..diffmap import FileDiff, parse_diff
from ..discuss import Broker, current_draft
from ..drafts import DraftError, DraftWriter
from ..memory import Memory
from ..textfmt import render_text

PACKAGE = Path(__file__).resolve().parent.parent


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=PACKAGE / "templates")
    templates.env.filters["fromjson"] = json.loads
    templates.env.filters["text"] = render_text
    templates.env.filters["size_label"] = size_label
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
