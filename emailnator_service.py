"""
Emailnator service — real @gmail.com / @googlemail.com temp addresses.
"""
from __future__ import annotations

import asyncio
import urllib.parse

import httpx

BASE_URL = "https://www.emailnator.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_OPTIONS = ["domain", "plusGmail", "dotGmail", "googleMail"]

_client: httpx.AsyncClient | None = None
_token: str | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            http2=False,
            timeout=httpx.Timeout(30.0),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
            follow_redirects=True,
        )
    return _client


async def _ensure_token() -> str:
    global _token
    if _token:
        return _token
    client = await _get_client()
    for attempt in range(3):
        await client.get(BASE_URL)
        await asyncio.sleep(1)
        raw = client.cookies.get("XSRF-TOKEN")
        if raw:
            _token = urllib.parse.unquote(raw)
            return _token
    raise RuntimeError("Emailnator: XSRF-TOKEN not found in cookies")


async def _refresh_token() -> None:
    global _token
    _token = None
    client = await _get_client()
    client.cookies.clear()
    await _ensure_token()


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-XSRF-TOKEN": _token or "",
        "Referer": BASE_URL + "/",
        "Origin": BASE_URL,
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


async def _post(path: str, payload: dict, *, _retry: bool = True) -> httpx.Response:
    await _ensure_token()
    client = await _get_client()
    resp = await client.post(BASE_URL + path, json=payload, headers=_headers())
    if resp.status_code in (401, 403, 419) and _retry:
        await asyncio.sleep(2)
        await _refresh_token()
        return await _post(path, payload, _retry=False)
    return resp


async def generate_email(options: list[str] | None = None) -> str:
    opts = options or DEFAULT_OPTIONS
    resp = await _post("/generate-email", {"email": opts})
    if resp.status_code >= 400:
        raise RuntimeError(f"generate-email failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    emails = data.get("email")
    if isinstance(emails, str):
        return emails
    if isinstance(emails, list) and emails:
        return emails[0]
    raise RuntimeError(f"generate-email: unexpected response {resp.text[:200]}")


async def get_messages(email: str) -> list[dict]:
    resp = await _post("/message-list", {"email": email})
    if resp.status_code >= 400:
        raise RuntimeError(f"message-list failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    return data.get("messageData", []) or []


async def get_message(email: str, message_id: str) -> str:
    resp = await _post("/message-list", {"email": email, "messageID": message_id})
    if resp.status_code >= 400:
        raise RuntimeError(f"get_message failed ({resp.status_code}): {resp.text[:200]}")
    return resp.text


async def aclose() -> None:
    global _client, _token
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
    _token = None
