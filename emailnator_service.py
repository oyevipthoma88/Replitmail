"""
Emailnator service — real @gmail.com / @googlemail.com temp addresses.
"""
from __future__ import annotations

import asyncio
import httpx

BASE_URL = "https://www.emailnator.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

EMAIL_TYPE_IDS = {"domain": 1, "plusGmail": 2, "dotGmail": 3, "googleMail": 8}
DEFAULT_OPTIONS = ["domain", "plusGmail", "dotGmail", "googleMail"]

_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            http2=False,
            timeout=httpx.Timeout(30.0),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
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


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
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
    client = await _get_client()
    resp = await client.post(BASE_URL + path, json=payload, headers=_headers())
    if resp.status_code in (429, 502, 503, 504) and _retry:
        await asyncio.sleep(2)
        return await _post(path, payload, _retry=False)
    return resp


async def generate_email(options: list[str] | None = None) -> str:
    opts = options or DEFAULT_OPTIONS
    ids = [EMAIL_TYPE_IDS[name] for name in opts if name in EMAIL_TYPE_IDS]
    if not ids:
        raise RuntimeError("generate-email: no valid email type selected")
    resp = await _post("/api/generate-email", {"ids": ids})
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
    resp = await _post("/api/message-list", {"email": email, "limit": 20})
    if resp.status_code >= 400:
        raise RuntimeError(f"message-list failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    messages = data.get("messages", data.get("messageData", [])) or []
    # Keep the shape expected by the bot while accepting Emailnator's new API.
    return [
        {
            **message,
            "messageID": message.get("messageID") or message.get("id"),
            "time": message.get("time") or message.get("time_ago") or "",
        }
        for message in messages
        if not message.get("locked")
    ]


async def get_message(email: str, message_id: str) -> str:
    client = await _get_client()
    resp = await client.get(
        BASE_URL + "/api/message/" + httpx.URL(message_id).raw_path.decode(),
        headers=_headers(),
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"get_message failed ({resp.status_code}): {resp.text[:200]}")
    return resp.text


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
