"""Web panel: overview, token check, narrative analysis, bundler groups, trades.

A small aiohttp server with a JSON API and one static page. It never places orders.
Bound to 127.0.0.1 by default; on any other address BH_WEB_PASSWORD is required
(HTTP basic auth, any user name). The page builds its DOM from text only, and a strict
Content-Security-Policy blocks injected scripts, because token names and links are
written by whoever launched the token.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
from importlib import resources
from typing import Any, Awaitable, Callable

from aiohttp import web

from .service import Service

log = logging.getLogger(__name__)

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
STATIC = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def check_bind(host: str, password: str | None) -> None:
    if host not in LOOPBACK and not password:
        raise SystemExit(
            f"web panel on {host} would be open to the network: set BH_WEB_PASSWORD in .env "
            "(or use host 127.0.0.1 and an SSH tunnel)"
        )


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


def create_app(service: Service, password: str | None = None) -> web.Application:
    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        if password:
            ok = False
            header = request.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                    ok = hmac.compare_digest(given.encode(), password.encode())
                except (ValueError, UnicodeDecodeError):
                    ok = False
            if not ok:
                return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="bundlehunter"'})
        try:
            resp = await handler(request)
        except ValueError as exc:
            resp = _json({"error": str(exc)}, 400)
        except web.HTTPException:
            raise
        except Exception as exc:
            log.exception("web %s failed", request.path)
            resp = _json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    async def static(request: web.Request) -> web.Response:
        name = request.match_info.get("name") or "index.html"
        if name not in STATIC:
            raise web.HTTPNotFound()
        body = resources.files("bundlehunter.webui").joinpath(name).read_text(encoding="utf-8")
        return web.Response(text=body, content_type=STATIC[name], charset="utf-8")

    async def status(request: web.Request) -> web.Response:
        return _json(service.status())

    async def clusters(request: web.Request) -> web.Response:
        black_only = request.query.get("all") != "1"
        rows = service.clusters(black_only=black_only, top=int(request.query.get("top", 50)))
        sel = service.cfg.selection
        return _json([{**c.to_dict(sel), "tokens": None} for c in rows])

    async def cluster(request: web.Request) -> web.Response:
        c = service.cluster(request.match_info["key"])
        if not c:
            return _json({"error": "немає такої групи чи гаманця"}, 404)
        return _json(c.to_dict(service.cfg.selection))

    async def check(request: web.Request) -> web.Response:
        res = await service.check(request.match_info["mint"], ai=request.query.get("ai") == "1")
        return _json(res.to_dict(service.cfg))

    async def narrative(request: web.Request) -> web.Response:
        res = await service.narrative_search(request.query.get("q", ""))
        return _json(res.to_dict())

    async def traders(request: web.Request) -> web.Response:
        return _json([t.to_dict() for t in service.traders()])

    def require_intent(request: web.Request) -> None:
        # a custom header: plain cross-site forms cannot send it, so other sites cannot press our buttons
        if request.headers.get("X-BH-Action") != "1":
            raise web.HTTPForbidden()

    async def paper(request: web.Request) -> web.Response:
        require_intent(request)
        body = await request.json()
        action = str(body.get("action", ""))
        if action not in ("", "deposit", "withdraw", "reset"):
            raise ValueError("невідома дія")
        amount = float(body.get("amount") or 0)
        if action and not (0 < amount < 1e6):
            raise ValueError("вкажи суму більше 0")
        return _json({"balance_sol": service.paper(action, amount)})

    async def scalp_eval(request: web.Request) -> web.Response:
        require_intent(request)
        text = service.scalp_eval()
        return _json({"text": text, "status": service.status().get("scalp")})

    async def learned(request: web.Request) -> web.Response:
        return _json(service.learned())

    async def learn_now(request: web.Request) -> web.Response:
        require_intent(request)
        text = service.learn()  # always the export/ folder next to the bot: the page cannot pick a path
        return _json({"text": text, "learned": service.learned()})

    async def positions(request: web.Request) -> web.Response:
        return _json(service.positions())

    app = web.Application(middlewares=[guard])
    app.add_routes([
        web.get("/", static),
        web.get("/static/{name}", static),
        web.get("/api/status", status),
        web.get("/api/clusters", clusters),
        web.get("/api/cluster/{key}", cluster),
        web.get("/api/check/{mint}", check),
        web.get("/api/narrative", narrative),
        web.get("/api/positions", positions),
        web.get("/api/traders", traders),
        web.post("/api/paper", paper),
        web.post("/api/scalp/eval", scalp_eval),
        web.get("/api/learned", learned),
        web.post("/api/learn", learn_now),
    ])
    return app


async def serve_web(service: Service, host: str, port: int, password: str | None) -> None:
    check_bind(host, password)
    runner = web.AppRunner(create_app(service, password), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("web panel on http://%s:%d", "localhost" if host in LOOPBACK else host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
