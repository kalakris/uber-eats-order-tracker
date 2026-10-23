"""Offline test of session_refresh against a fake ubereats.com + auth.uber.com.

The fake servers replay the redirect chain a real browser goes through:
ubereats.com -> auth.uber.com (may rotate sid/csid) -> ubereats.com/?_sid=...
(issues the new ubereats sid).

Run: python3 tests/test_session_refresh.py
"""
import asyncio
import functools
import os
import socket
import sys

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractResolver
from yarl import URL

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "uber_eats"))
import session_refresh as sr  # noqa: E402

AUTH_LOGGED_IN = {"csid": "CSID/1="}  # the csid auth.uber.com currently accepts


class LocalResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [{"hostname": host, "host": "127.0.0.1", "port": port,
                 "family": family, "proto": 0, "flags": 0}]

    async def close(self):
        pass


def eats_app(port):
    async def login_redirect(request):
        resp = web.HTTPFound(f"http://auth.uber.com:{port}/v2/?state=S1&next_url=x")
        resp.set_cookie("state", "S1", domain="www.ubereats.com", max_age=900)
        raise resp

    async def root(request):
        if "_sid" in request.query:
            assert request.cookies.get("state") == "S1", "state cookie from hop 1 missing"
            resp = web.HTTPFound(f"http://www.ubereats.com:{port}/feed")
            resp.set_cookie("sid", "EATS-NEW", domain="ubereats.com")
            resp.set_cookie("smeta", "EATS-SMETA-NEW", domain="ubereats.com")
            resp.del_cookie("state", domain="www.ubereats.com")
            raise resp
        if request.cookies.get("sid") == "EATS-EXPIRED":
            resp = web.HTTPFound(f"http://auth.uber.com:{port}/v2/?state=S1&refresh=true")
            resp.set_cookie("state", "S1", domain="www.ubereats.com", max_age=900)
            raise resp
        return web.Response(text="home")

    async def feed(request):
        resp = web.Response(text="feed")
        resp.set_cookie("uev2.id.session", "SESSION-NEW", domain=".ubereats.com")
        return resp

    return [web.get("/login-redirect/", login_redirect), web.get("/", root), web.get("/feed", feed)]


def auth_app(port):
    async def v2(request):
        csid = request.cookies.get("csid")
        if csid != AUTH_LOGGED_IN["csid"]:
            return web.Response(text="login page")
        # One sid per domain: a duplicate .uber.com / host-only sid would be ambiguous.
        assert request.headers["Cookie"].count("sid=") == 2, request.headers["Cookie"]  # sid + csid
        assert '"' not in request.headers["Cookie"], "cookie values were quoted"
        AUTH_LOGGED_IN["csid"] = "CSID-2"
        resp = web.HTTPFound(f"http://www.ubereats.com:{port}/?_sid=X&state=S1")
        resp.set_cookie("sid", "AUTH-SID-2", domain=".uber.com")
        resp.set_cookie("csid", "CSID-2", domain="auth.uber.com")
        resp.set_cookie("smeta", "AUTH-SMETA-2", domain=".uber.com")
        raise resp

    return [web.get("/v2/", v2)]


async def main():
    app = web.Application()
    hits = []
    # Both fake hosts share one server; dispatch on the Host header.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    eats_srv, auth_srv = web.Application(), web.Application()
    eats_srv.add_routes(eats_app(port))
    auth_srv.add_routes(auth_app(port))

    async def dispatch(request):
        hits.append(request.path)
        target = auth_srv if request.host.startswith("auth.") else eats_srv
        match = await target.router.resolve(request)
        return await match.handler(request)

    app.router.add_route("*", "/{tail:.*}", dispatch)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.SockSite(runner, sock)
    await site.start()

    sr.LOGIN_REDIRECT_URL = URL(f"http://www.ubereats.com:{port}/login-redirect/")
    sr.EATS_URL = URL(f"http://www.ubereats.com:{port}/")
    sr.AUTH_URL = URL(f"http://auth.uber.com:{port}/")
    real_session = aiohttp.ClientSession
    sr.aiohttp.ClientSession = functools.partial(
        real_session, connector=aiohttp.TCPConnector(resolver=LocalResolver())
    )
    headers = {"User-Agent": "test"}
    eats_cookie = "sid=EATS-OLD; smeta=EATS-SMETA-OLD; uev2.id.session=SESSION-OLD; _ga=keep"
    auth_cookie = "sid=AUTH/SID+1=; csid=CSID/1=; smeta=AUTH-SMETA-1; udi-id=keep"

    try:
        # 1. Valid auth login: new ubereats session, rotated auth cookies.
        r = await sr.async_refresh_session(eats_cookie, auth_cookie, headers)
        assert r.error is None, r.error
        eats = sr.parse_cookie_header(r.eats_cookie)
        auth = sr.parse_cookie_header(r.auth_cookie)
        assert eats["sid"] == "EATS-NEW" and eats["smeta"] == "EATS-SMETA-NEW", eats
        assert eats["uev2.id.session"] == "SESSION-NEW" and eats["_ga"] == "keep", eats
        assert "state" not in eats, eats
        assert auth == {"sid": "AUTH-SID-2", "csid": "CSID-2", "smeta": "AUTH-SMETA-2", "udi-id": "keep"}, auth
        print("ok: renewal")

        # 2. The old csid was rotated away: login page, error, auth cookies still returned.
        sr.aiohttp.ClientSession = functools.partial(
            real_session, connector=aiohttp.TCPConnector(resolver=LocalResolver())
        )
        r = await sr.async_refresh_session(eats_cookie, auth_cookie, headers)
        assert r.eats_cookie is None and r.login_expired and "login page" in r.error, r
        assert sr.parse_cookie_header(r.auth_cookie)["csid"] == "CSID/1="
        print("ok: expired auth login ->", r.error)

        # 3. An expired sid: the home page itself starts the refresh=true chain.
        AUTH_LOGGED_IN["csid"] = "CSID/1="
        hits.clear()
        sr.aiohttp.ClientSession = functools.partial(
            real_session, connector=aiohttp.TCPConnector(resolver=LocalResolver())
        )
        r = await sr.async_refresh_session(eats_cookie.replace("EATS-OLD", "EATS-EXPIRED"), auth_cookie, headers)
        assert r.error is None, r.error
        assert hits == ["/", "/v2/", "/", "/feed"], hits
        print("ok: home page refresh=true chain")
    finally:
        sr.aiohttp.ClientSession = real_session
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
