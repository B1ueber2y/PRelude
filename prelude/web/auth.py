"""Access control: an access-key cookie, plus the Tailscale identity when proxied by Tailscale."""

from __future__ import annotations

import hmac

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from .context import Web

COOKIE = "prelude_key"
PUBLIC = ("/manifest.webmanifest", "/healthz")


def install(app: FastAPI, w: Web) -> None:
    key = w.cfg.access_key()

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        allowed_ts = w.cfg.server.allowed_tailscale_user
        ts_login = request.headers.get("tailscale-user-login")
        if allowed_ts and ts_login is not None and ts_login.lower() != allowed_ts.lower():
            return PlainTextResponse("Forbidden", status_code=403)
        if request.url.path in PUBLIC or request.url.path.startswith("/static/"):
            return await call_next(request)

        supplied = request.query_params.get("key")
        if supplied is not None:
            if not hmac.compare_digest(supplied, key):
                return PlainTextResponse("Invalid key", status_code=401)
            resp = RedirectResponse(request.url.path or "/", status_code=303)
            resp.set_cookie(
                COOKIE,
                key,
                max_age=60 * 60 * 24 * 365,
                httponly=True,
                samesite="strict",
                secure=request.headers.get("x-forwarded-proto") == "https",
            )
            return resp

        cookie = request.cookies.get(COOKIE)
        if not cookie or not hmac.compare_digest(cookie, key):
            return w.render(request, "locked.html", {}, status_code=401)
        return await call_next(request)
