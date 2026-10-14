"""Home page, reviews, and review-wide draft actions."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import drafting, prstate, stats
from ..drafts import DraftError
from ..github import PrRef, parse_pr
from ..guard import RepoNotAllowed
from ..poller import LAST_ERROR, LAST_POLL
from ..review import start_review
from ..workspace import remove_checkout
from .context import Web, WebDep

log = logging.getLogger(__name__)

router = APIRouter()


def _index(request: Request, w: Web, **extra) -> HTMLResponse:
    last_poll = w.store.setting(LAST_POLL, "")
    return w.render(
        request,
        "index.html",
        {
            "reviews": w.store.list_reviews(),
            "only_repo": w.cfg.only_repo,
            "default_reviewers": w.cfg.claude.reviewers,
            "watching": w.cfg.triggers.enabled and hasattr(request.app.state, "poller"),
            "last_poll": float(last_poll) if last_poll else None,
            "poll_error": w.store.setting(LAST_ERROR, ""),
            **extra,
        },
    )


@router.get("/", response_class=HTMLResponse)
def index(w: WebDep, request: Request):
    return _index(request, w)


@router.get("/stats", response_class=HTMLResponse)
def stats_page(w: WebDep, request: Request):
    def pct(x):
        return "–" if x is None else f"{round(x * 100)}%"

    return w.render(request, "stats.html", {"s": stats.overview(w.store), "pct": pct})


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
    _refresh_in_background(w, review_id)
    findings = w.store.findings_for(review_id)
    counts = {s: sum(1 for f in findings if f["status"] == s) for s in ("pending", "drafted", "posted", "discarded")}
    counts["all"] = len(findings)
    return w.render(
        request,
        "review.html",
        {
            "r": r,
            "cards": [w.card_context(f, r) for f in findings if show == "all" or f["status"] == show],
            "show": show,
            "counts": counts,
            "drafts_enabled": w.cfg.drafts.enabled,
            "prefix_on": drafting.prefix_on(w.store),
            "prefix": w.cfg.drafts.prefix,
            "latest": w.store.latest_review_for(r["owner"], r["repo"], r["number"], done_only=False),
            "new_commits": _has_new_commits(w, r),
            "leftovers": prstate.leftovers(findings),
        },
    )


def _refresh_in_background(w: Web, review_id: int) -> None:
    """Notice drafts submitted on GitHub without making the page wait for GitHub."""

    def run():
        try:
            drafting.refresh_submitted(w.store, w.cfg, w.writer, review_id)
            r = w.store.get_review(review_id)
            if r and r["pr_state"] != "merged":
                prstate.refresh(w.store, w.cfg, w.reader, [(r["owner"], r["repo"], r["number"])])
        except Exception:
            log.warning("couldn't refresh review %s from GitHub", review_id, exc_info=True)

    threading.Thread(target=run, daemon=True, name=f"refresh-{review_id}").start()


def _has_new_commits(w: Web, r) -> bool:
    """Known when PRelude is watching the PR (triggers on); otherwise unknown and not shown."""
    seen = w.store.watched(r["owner"], r["repo"], r["number"])
    return bool(seen and r["head_sha"] and seen["head_sha"] and seen["head_sha"] != r["head_sha"])


@router.post("/reviews/{review_id}/more")
def more_reviewers(w: WebDep, review_id: int, n: int = Form(1)):
    w.review_or_404(review_id)
    n = max(1, min(5, n))
    if not w.store.try_mark_busy(review_id, f"Adding {n} reviewer{'s' if n > 1 else ''}"):
        return w.flash("This review is busy; try again when it's finished.")
    threading.Thread(target=w.add_reviewers, args=(review_id, n), daemon=True, name=f"more-{review_id}").start()
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/reviews/{review_id}/again")
def review_again(w: WebDep, review_id: int, mode: str = Form("full")):
    r = w.review_or_404(review_id)
    try:
        new_id = start_review(
            w.store,
            w.cfg,
            PrRef(r["owner"], r["repo"], r["number"]),
            trigger_reason=f"re-review ({mode}) on phone",
            reviewers=r["reviewers"],
            previous_review_id=review_id,
            mode=mode,
        )
    except (ValueError, RepoNotAllowed) as e:
        return w.flash(str(e))
    threading.Thread(target=w.run_review, args=(new_id,), daemon=True, name=f"review-{new_id}").start()
    return Response(status_code=204, headers={"HX-Redirect": f"/reviews/{new_id}"})


@router.post("/reviews/{review_id}/clear-leftovers")
def clear_leftovers(w: WebDep, review_id: int):
    r = w.review_or_404(review_id)
    if (r["pr_state"] or "open") == "open":
        return w.flash("This PR is still open.")
    try:
        prstate.clear_leftovers(w.store, w.cfg, w.writer, w.reader, review_id)
    except prstate.StillOpen as e:
        return w.flash(str(e))
    except DraftError as e:
        return w.flash(f"Couldn't remove a draft comment: {e}")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/reviews/{review_id}/delete")
def delete_review(w: WebDep, review_id: int):
    r = w.review_or_404(review_id)
    if r["status"] in ("queued", "running") or r["progress"]:
        return w.flash("This review is still running; delete it when it's finished.")
    w.store.delete_review(review_id)
    if r["worktree"] and not w.store.worktree_in_use(r["worktree"]):
        try:
            remove_checkout(w.cfg, r["owner"], r["repo"], Path(r["worktree"]))
        except Exception:
            log.warning("couldn't remove checkout %s", r["worktree"], exc_info=True)
    return Response(status_code=204, headers={"HX-Redirect": "/"})


@router.get("/reviews/{review_id}/status", response_class=HTMLResponse)
def review_status(w: WebDep, request: Request, review_id: int):
    r = w.review_or_404(review_id)
    if r["status"] in ("done", "failed", "skipped") and not r["progress"]:
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
