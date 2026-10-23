"""Renew the ubereats.com session from the long-lived auth.uber.com login.

The ubereats.com `sid` stops working after about 30 days. A browser renews it
silently: ubereats.com redirects to auth.uber.com, which still holds a login
(`sid` on .uber.com plus `csid` on auth.uber.com, 180 days, extended on every
use), and auth.uber.com redirects back with a fresh ubereats.com session.
This module replays that redirect chain with a real cookie jar.

auth.uber.com rotates `csid` on every renewal, so the caller must persist the
returned auth cookies even when the renewal itself fails.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from http.cookies import SimpleCookie

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

EATS_URL = URL("https://www.ubereats.com/")
AUTH_URL = URL("https://auth.uber.com/")
# Starts the auth.uber.com hop whenever the sid is no longer valid.
LOGIN_REDIRECT_URL = URL("https://www.ubereats.com/login-redirect/")

# Cookies auth.uber.com scopes to its own host; the rest of the login lives on .uber.com.
_AUTH_HOST_ONLY = ("csid",)


class SessionRefreshError(Exception):
    """The redirect chain did not produce a new ubereats.com session."""


@dataclass
class RefreshResult:
    eats_cookie: str | None  # None when the renewal failed
    auth_cookie: str  # always returned: csid may have rotated either way
    error: str | None = None
    login_expired: bool = False  # auth.uber.com wants a password: retrying will not help


def parse_cookie_header(header: str | None) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in (header or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key:
            jar[key] = value
    return jar


def format_cookie_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in jar.items())


def auth_cookie_problem(header: str | None) -> str | None:
    """Return a config-flow error key if the auth.uber.com cookie is unusable."""
    jar = parse_cookie_header(header)
    if not jar.get("csid"):
        return "auth_csid_not_found"
    if not jar.get("sid"):
        return "auth_sid_not_found"
    return None


def _seed(jar: aiohttp.CookieJar, cookies: dict[str, str], domain_for) -> None:
    morsels = SimpleCookie()
    for name, value in cookies.items():
        try:
            morsels[name] = value
        except Exception:  # names SimpleCookie rejects (rare analytics cookies)
            continue
        morsels[name]["domain"] = domain_for(name)
        morsels[name]["path"] = "/"
    jar.update_cookies(morsels)


def _read(jar: aiohttp.CookieJar, url: URL) -> dict[str, str]:
    return {name: morsel.value for name, morsel in jar.filter_cookies(url).items()}


async def async_refresh_session(
    eats_cookie: str, auth_cookie: str, headers: dict[str, str]
) -> RefreshResult:
    """Follow ubereats.com -> auth.uber.com -> ubereats.com and return both cookie sets."""
    eats_in = parse_cookie_header(eats_cookie)
    auth_in = parse_cookie_header(auth_cookie)

    # Uber's values contain '/' and '='; the default jar would send them quoted.
    jar = aiohttp.CookieJar(quote_cookie=False)
    _seed(jar, eats_in, lambda _name: "ubereats.com")
    _seed(jar, auth_in, lambda name: "auth.uber.com" if name in _AUTH_HOST_ONLY else "uber.com")

    request_headers = {
        "User-Agent": headers.get("User-Agent", ""),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    error = None
    login_expired = False
    try:
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            # The home page first: with an expired sid it sends a browser through
            # auth.uber.com with refresh=true, which also extends the 180-day login.
            # /login-redirect/ reaches auth.uber.com in every case but does not extend it.
            for start in (EATS_URL, LOGIN_REDIRECT_URL):
                async with session.get(
                    start,
                    headers=request_headers,
                    allow_redirects=True,
                    max_redirects=10,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    hops = [str(r.url.host) for r in resp.history] + [str(resp.url.host)]
                    _LOGGER.debug("Session refresh from %s: %s -> %s", start.path, hops, resp.status)
                    if resp.url.host == AUTH_URL.host:
                        login_expired = True
                        error = "auth.uber.com showed its login page: the auth login has expired"
                        break
                    if any(r.url.host == AUTH_URL.host for r in resp.history):
                        error = None
                        break
                    error = f"never reached auth.uber.com (hops {hops}, status {resp.status})"
    except Exception as err:  # network errors, too many redirects
        error = f"{type(err).__name__}: {err}"

    auth_out = {**auth_in, **_read(jar, AUTH_URL)}
    eats_out = {**eats_in, **_read(jar, EATS_URL)}

    # The new sid is the success test: a non-200 after the `_sid` hop does not undo it.
    if eats_out.get("sid") != eats_in.get("sid"):
        error = None
        login_expired = False
    elif error is None:
        error = "the redirect chain did not issue a new ubereats.com sid"

    return RefreshResult(
        eats_cookie=None if error else format_cookie_header(eats_out),
        auth_cookie=format_cookie_header(auth_out),
        error=error,
        login_expired=login_expired,
    )
