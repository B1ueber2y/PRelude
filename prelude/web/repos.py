"""Which repos PRelude watches, and how: the repos part of `triggers` in config.yaml."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import repoconfig
from ..poller import cap_usage, next_cap_slot
from ..triggers import MODES
from .context import Web, WebDep

router = APIRouter()


def _page(w: Web, request: Request, **extra) -> HTMLResponse:
    t = w.cfg.triggers
    activity = w.store.repo_activity()
    usage = cap_usage(w.store, t)  # by repo, last 24 hours
    rows = repoconfig.configured(w.cfg)
    for row in rows:
        slugs = [slug for slug in activity if t.repo_pattern(slug) == row["slug"].lower()]
        row["reviews"] = sum(activity[s][0] for s in slugs)
        row["last"] = max((activity[s][1] for s in slugs), default=None)
        row["capped"] = sum(usage[s] for s in slugs)
    others = [
        {"slug": slug, "reviews": n, "last": last, "capped": usage[slug]}
        for slug, (n, last) in sorted(activity.items())
        if t.repo_pattern(slug) is None
    ]
    return w.render(
        request,
        "repos.html",
        {
            "t": t,
            "rows": rows,
            "others": others,
            "modes": MODES,
            "cap_used": sum(usage.values()),
            "next_slot": next_cap_slot(w.store, t),
            **extra,
        },
    )


@router.get("/repos", response_class=HTMLResponse)
def repos_page(w: WebDep, request: Request):
    return _page(w, request)


@router.post("/repos", response_class=HTMLResponse)
def save_repo(
    w: WebDep,
    request: Request,
    slug: str = Form(...),
    action: str = Form("mode"),
    mode: str = Form("on_request"),
    since: str = Form(""),
    exempt: str = Form("0"),
    post: str = Form("0"),
):
    """One change to a repo: set its mode (and since), toggle cap exemption or auto-posting, or remove it."""
    try:
        if action == "remove":
            repoconfig.remove_repo(w.cfg, slug)
        elif action == "exempt":
            repoconfig.set_cap_exempt(w.cfg, slug, exempt == "1")
        elif action == "post":
            repoconfig.set_auto_post(w.cfg, slug, post == "1")
        else:
            repoconfig.set_repo(w.cfg, slug, mode, since.strip() or None)
    except repoconfig.RepoConfigError as e:
        return _page(w, request, error=str(e), slug=slug)
    return _page(w, request, saved=slug)
