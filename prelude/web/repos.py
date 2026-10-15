"""Which repos PRelude watches, and how: the repos part of `triggers` in config.yaml."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import repoconfig
from ..triggers import MODES
from .context import Web, WebDep

router = APIRouter()


def _page(w: Web, request: Request, **extra) -> HTMLResponse:
    t = w.cfg.triggers
    activity = w.store.repo_activity()
    rows = repoconfig.configured(w.cfg)
    for row in rows:
        matches = [a for slug, a in activity.items() if t.repo_pattern(slug) == row["slug"].lower()]
        row["reviews"] = sum(n for n, _ in matches)
        row["last"] = max((last for _, last in matches), default=None)
    others = [
        {"slug": slug, "reviews": n, "last": last}
        for slug, (n, last) in sorted(activity.items())
        if t.repo_pattern(slug) is None
    ]
    return w.render(request, "repos.html", {"t": t, "rows": rows, "others": others, "modes": MODES, **extra})


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
):
    """One change to a repo: set its mode (and since), toggle cap exemption, or remove it."""
    try:
        if action == "remove":
            repoconfig.remove_repo(w.cfg, slug)
        elif action == "exempt":
            repoconfig.set_cap_exempt(w.cfg, slug, exempt == "1")
        else:
            repoconfig.set_repo(w.cfg, slug, mode, since.strip() or None)
    except repoconfig.RepoConfigError as e:
        return _page(w, request, error=str(e), slug=slug)
    return _page(w, request, saved=slug)
