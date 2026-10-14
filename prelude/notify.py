"""Phone notifications through ntfy (https://ntfy.sh). Messages say little by default:
the topic lives on a public server, so repo names and titles are only sent if enabled."""

from __future__ import annotations

import logging
import secrets
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class NotifyConfig:
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    details: str = "ref"  # none | ref (owner/repo#number) | full (also the PR title)
    open_url: str = "http://localhost:8787"


def new_topic() -> str:
    return "prelude-" + secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:24].lower()


Sender = Callable[[str, bytes, dict[str, str]], None]


def _http_send(url: str, body: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    urllib.request.urlopen(req, timeout=15).close()


def send(
    cfg: NotifyConfig,
    title: str,
    message: str,
    *,
    path: str = "/",
    tags: str = "",
    link: str | None = None,
    sender: Sender | None = None,
) -> bool:
    """Tapping opens `link` if given, else PRelude at `path`."""
    if not cfg.ntfy_topic:
        return False
    if cfg.details == "none":
        link = None  # the link names the repo
    headers = {"Title": title, "Click": link or cfg.open_url.rstrip("/") + path}
    if tags:
        headers["Tags"] = tags
    try:
        (sender or _http_send)(f"{cfg.ntfy_server.rstrip('/')}/{cfg.ntfy_topic}", message.encode(), headers)
        return True
    except Exception:
        log.warning("notification failed", exc_info=True)
        return False


def review_finished(cfg: NotifyConfig, review, findings, sender: Sender | None = None) -> bool:
    """`review` and `findings` are database rows."""
    ref = f"{review['owner']}/{review['repo']}#{review['number']}"
    prefix = ""
    if cfg.details == "full":
        prefix = f"{ref} {review['title'] or ''}".rstrip() + " · "
    elif cfg.details == "ref":
        prefix = ref + " · "
    repo_tag = "" if cfg.details == "none" else f",{review['repo']}"
    if review["status"] == "done":
        high = sum(1 for f in findings if f["severity"] == "high")
        n = len(findings)
        drafted = sum(1 for f in findings if f["status"] == "drafted")
        msg = prefix + f"{n} finding{'' if n == 1 else 's'}" + (f" ({high} high)" if high else "")
        if drafted:
            msg += ", all in your draft" if drafted == n else f", {drafted} in your draft"
        return send(
            cfg,
            "Review ready",
            msg,
            path=f"/reviews/{review['id']}",
            tags="mag" + repo_tag,
            link=f"{review['url']}/files",
            sender=sender,
        )
    why = (review["error"] or "").split(":", 1)[0] or "see PRelude for details"
    return send(
        cfg,
        "Review failed",
        prefix + why,
        path=f"/reviews/{review['id']}",
        tags="warning" + repo_tag,
        sender=sender,
    )
