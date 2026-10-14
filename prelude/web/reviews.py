"""Home page, reviews, and review-wide draft actions."""

from __future__ import annotations

import threading

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import drafting
from ..drafts import DraftError
from ..github import parse_pr
from ..guard import RepoNotAllowed
from ..review import start_review
from .context import Web, WebDep

router = APIRouter()


def _index(request: Request, w: Web, **extra) -> HTMLResponse:
    return w.render(request, "index.html", {
        "reviews": w.store.list_reviews(), "only_repo": w.cfg.only_repo,
        "default_reviewers": w.cfg.claude.reviewers, **extra,
    })


@router.get("/", response_class=HTMLResponse)
def index(w: WebDep, request: Request):
    return _index(request, w)


@router.get("/reviews/rows", response_class=HTMLResponse)
def review_rows(w: WebDep, request: Request):
    return w.render(request, "_review_rows.html", {"reviews": w.store.list_reviews()})


@router.post("/reviews")
def create_review(w: WebDep, request: Request, pr: str = Form(...), reviewers: int | None = Form(None)):
    try:
        review_id = start_review(w.store, w.cfg, parse_pr(pr), trigger_reason="manual (phone)", reviewers=reviewers)
    except (ValueError, RepoNotAllowed) as e:
        return _index(request, w, error=str(e), pr=pr)
    threading.Thread(target=w.run_review, args=(review_id,), daemon=True, name=f"review-{review_id}").start()
    return RedirectResponse(f"/reviews/{review_id}", status_code=303)


@router.get("/reviews/{review_id}", response_class=HTMLResponse)
def review_page(w: WebDep, request: Request, review_id: int, show: str = "pending"):
    r = w.review_or_404(review_id)
    findings = w.store.findings_for(review_id)
    counts = {s: sum(1 for f in findings if f["status"] == s) for s in ("pending", "drafted", "discarded")}
    counts["all"] = len(findings)
    return w.render(request, "review.html", {
        "r": r,
        "findings": [f for f in findings if show == "all" or f["status"] == show],
        "show": show,
        "counts": counts,
        "drafts_enabled": w.cfg.drafts.enabled,
        "prefix_on": drafting.prefix_on(w.store),
        "prefix": w.cfg.drafts.prefix,
    })


@router.get("/reviews/{review_id}/status", response_class=HTMLResponse)
def review_status(w: WebDep, request: Request, review_id: int):
    r = w.review_or_404(review_id)
    if r["status"] in ("done", "failed"):
        return Response(headers={"HX-Refresh": "true"})  # reload so the findings appear
    return w.render(request, "_review_status.html", {"r": r})


@router.post("/reviews/{review_id}/github/add-all")
def github_add_all(w: WebDep, review_id: int):
    w.review_or_404(review_id)
    try:
        added, failed = drafting.add_all(w.store, w.cfg, w.writer, review_id)
    except DraftError as e:
        return w.flash(str(e))
    if failed:
        return w.flash(f"Added {added}; {len(failed)} failed: " + " | ".join(failed))
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/settings/prefix")
def set_prefix(w: WebDep, on: str = Form("0")):
    w.store.set_setting(drafting.PREFIX_SETTING, "1" if on == "1" else "0")
    return Response(status_code=204, headers={"HX-Refresh": "true"})
