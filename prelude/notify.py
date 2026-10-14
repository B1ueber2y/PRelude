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
    include_details: bool = False   # repo name and PR title in the message
    open_url: str = "http://localhost:8787"


def new_topic() -> str:
    return "prelude-" + secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:24].lower()


Sender = Callable[[str, bytes, dict[str, str]], None]


def _http_send(url: str, body: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    urllib.request.urlopen(req, timeout=15).close()


def send(cfg: NotifyConfig, title: str, message: str, *, path: str = "/", tags: str = "",
         sender: Sender | None = None) -> bool:
    if not cfg.ntfy_topic:
        return False
    headers = {"Title": title, "Click": cfg.open_url.rstrip("/") + path}
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
    where = f" · {review['owner']}/{review['repo']}#{review['number']} {review['title'] or ''}".rstrip()
    detail = where if cfg.include_details else ""
    if review["status"] == "done":
        high = sum(1 for f in findings if f["severity"] == "high")
        n = len(findings)
        msg = f"{n} finding{'' if n == 1 else 's'}" + (f" ({high} high)" if high else "") + detail
        return send(cfg, "Review ready", msg, path=f"/reviews/{review['id']}", tags="mag", sender=sender)
    return send(cfg, "Review failed", "Open PRelude for details" + detail, path=f"/reviews/{review['id']}",
                tags="warning", sender=sender)
