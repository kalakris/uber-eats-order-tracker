"""Run one real session renewal against Uber and check the result.

Usage: python3 tests/live_refresh.py COOKIES.json [TIME_ZONE]
COOKIES.json holds {"eats_cookie": "...", "auth_cookie": "..."} and is
rewritten with the rotated cookies, because the old csid may stop working.
Keep that file outside the repo. Prints cookie names only, never values.
"""
import asyncio
import json
import os
import sys

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "uber_eats"))
import session_refresh as sr  # noqa: E402
from const import ENDPOINT, HEADERS_TEMPLATE  # noqa: E402


TIME_ZONE = sys.argv[2] if len(sys.argv) > 2 else "UTC"


async def active_orders(eats_cookie):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{ENDPOINT}?localeCode=us",
            json={"orderUuid": None, "timezone": TIME_ZONE},
            headers={**HEADERS_TEMPLATE, "Cookie": eats_cookie},
        ) as resp:
            data = await resp.json(content_type=None)
            return resp.status, data.get("status"), (data.get("data") or {}).get("message")


async def main(path):
    with open(path) as f:
        cookies = json.load(f)
    print("before:", await active_orders(cookies["eats_cookie"]))
    result = await sr.async_refresh_session(cookies["eats_cookie"], cookies["auth_cookie"], HEADERS_TEMPLATE)
    cookies["auth_cookie"] = result.auth_cookie
    if result.eats_cookie:
        cookies["eats_cookie"] = result.eats_cookie
    with open(path, "w") as f:
        json.dump(cookies, f)
    os.chmod(path, 0o600)
    print("renewal error:", result.error)
    print("auth cookies:", sorted(sr.parse_cookie_header(result.auth_cookie)))
    if result.eats_cookie:
        print("after:", await active_orders(result.eats_cookie))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
