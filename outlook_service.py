"""
Outlook service — real @outlook.com / @hotmail.com temp addresses via 22.do.

Uses curl_cffi with a persistent session (shared Cloudflare cookies) so
the warmup happens once, not before every single API call.
"""
from __future__ import annotations

import hashlib
import html
import re
import random
import threading
import urllib.parse

import asyncio

from curl_cffi import requests as cffi_requests

BASE_URL = "https://22.do"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
IMPERSONATE = "chrome131"

OUTLOOK_DOMAINS = [
    "colabeta.com",
    "colaname.com",
    "colabeta.net",
    "colabeta.dev",
    "colaname.io",
    "colaname.org",
    "colaname.net",
    "colabeta.org",
]

ACCEPTED_DOMAINS = ("outlook.com", "hotmail.com")

FAMILY = {
    "microsoft": ("outlook.com", "hotmail.com"),
    "gmail": ("gmail.com", "googlemail.com"),
}

# One global session — cookies persist across all calls so Cloudflare
# challenge is solved once, not on every request.
_session: cffi_requests.Session | None = None
_session_lock = threading.Lock()
_warmed = False


def _get_session() -> cffi_requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            _session = cffi_requests.Session(impersonate=IMPERSONATE)
        return _session


def _warmup_once() -> None:
    """Load /inbox/ once to establish Cloudflare cookies. Idempotent + thread-safe."""
    global _warmed
    with _session_lock:
        if _warmed:
            return
    s = _get_session()
    try:
        s.get(
            BASE_URL + "/inbox/",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=10,
        )
        with _session_lock:
            _warmed = True
    except Exception:
        pass


def _post_json(path: str, payload: dict, *, token: str | None = None) -> dict:
    s = _get_session()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/inbox/",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = s.post(BASE_URL + path, json=payload, headers=headers, timeout=12)
        if r.status_code >= 400:
            return {"status": False, "msg": f"HTTP {r.status_code}"}
        try:
            return r.json()
        except Exception:
            return {"status": False, "msg": r.text[:200]}
    except Exception as e:
        return {"status": False, "msg": f"{type(e).__name__}: {str(e)[:120]}"}


def _uuid_for(email: str) -> str:
    return hashlib.md5(("replitmail:" + email).encode()).hexdigest()


def base_address(email: str) -> str:
    email = (email or "").strip()
    if "+" not in email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    return local.split("+", 1)[0] + "@" + domain


def _in_family(email: str, kind: str) -> bool:
    lowered = (email or "").lower()
    return any(lowered.endswith("@" + d) for d in FAMILY.get(kind, ()))


def _is_clean_base_address(email: str) -> bool:
    email = base_address(email)
    local, separator, _domain = email.partition("@")
    return bool(separator and re.fullmatch(r"[A-Za-z0-9]+", local))


def _sync_create_mailbox(
    kind: str = "microsoft",
    source_domain: str | None = None,
    clean_only: bool = False,
) -> dict:
    """Create a mailbox. Tries each domain once, max 2 rounds."""
    domains = [source_domain] if source_domain else OUTLOOK_DOMAINS[:]
    random.shuffle(domains)
    last_err = "unknown"

    for _round in range(2):
        _warmup_once()
        for domain in domains:
            data = _post_json(
                "/action/mailbox/create",
                {"type": kind, "domain": domain},
            )
            if not data.get("status") or not data.get("data"):
                last_err = str(data.get("msg", data))[:120]
                continue
            email = data["data"].get("email")
            if not _in_family(email or "", kind):
                last_err = f"got out-of-family: {email}"
                continue
            address = base_address(email)
            if clean_only and not _is_clean_base_address(address):
                last_err = f"non-clean: {address}"
                continue
            return {"email": address, "source_domain": domain}
        global _warmed
        with _session_lock:
            _warmed = False

    label = "@outlook.com / @hotmail.com" if kind == "microsoft" else "@gmail.com / @googlemail.com"
    raise RuntimeError(f"22.do abhi {label} nahi de raha ({last_err}).")


async def create_mailbox(
    kind: str = "microsoft",
    allow_alias: bool = False,
    source_domain: str | None = None,
    clean_only: bool = False,
) -> dict:
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: _sync_create_mailbox(kind, source_domain=source_domain, clean_only=clean_only),
    )


async def create_outlook(
    allow_alias: bool = False,
    source_domain: str | None = None,
    clean_only: bool = False,
) -> dict:
    return await create_mailbox(
        "microsoft",
        allow_alias=allow_alias,
        source_domain=source_domain,
        clean_only=clean_only,
    )


async def create_outlook_batch(count: int = 5) -> list[dict]:
    count = max(1, min(count, len(OUTLOOK_DOMAINS)))
    sources = OUTLOOK_DOMAINS[:]
    random.shuffle(sources)
    results: list[dict] = []

    for start in range(0, len(sources), count):
        batch_sources = sources[start:start + count]
        attempts = await asyncio.gather(
            *(
                create_mailbox("microsoft", source_domain=src, clean_only=True)
                for src in batch_sources
            ),
            return_exceptions=True,
        )
        for src, result in zip(batch_sources, attempts):
            if isinstance(result, Exception):
                continue
            if any(item["email"] == result["email"] for item in results):
                continue
            result["source_domain"] = src
            results.append(result)
            if len(results) >= count:
                return results
    raise RuntimeError(f"Sirf {len(results)}/{count} clean Outlook mailboxes mile.")


async def create_gmail(allow_alias: bool = False) -> dict:
    return await create_mailbox("gmail", allow_alias=allow_alias)


def _sync_get_token(email: str) -> str:
    email = base_address(email)
    uuid = _uuid_for(email)
    _warmup_once()
    data = _post_json("/action/mailbox/applyToken", {"email": email, "uuid": uuid})
    if not data.get("status") or not data.get("data"):
        raise RuntimeError(f"get_token failed: {data.get('msg', data)}")
    return data["data"]["token"]


async def get_token(email: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(None, lambda: _sync_get_token(email))


def undotted_address(email: str) -> str:
    email = base_address(email)
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    return local.replace(".", "") + "@" + domain


def _sync_get_messages_exact(email: str) -> list[dict]:
    email = base_address(email)
    try:
        token = _sync_get_token(email)
    except Exception:
        return []
    data = _post_json(
        "/action/mailbox/message", {"email": email, "lastime": 0}, token=token
    )
    if not data.get("status"):
        return []
    msgs = data.get("data")
    if not msgs or msgs is True:
        return []
    return msgs


async def get_messages(email: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    msgs = await loop.run_in_executor(None, lambda: _sync_get_messages_exact(email))
    if msgs:
        return msgs
    alt = undotted_address(email)
    if alt != base_address(email):
        return await loop.run_in_executor(None, lambda: _sync_get_messages_exact(alt))
    return msgs


def _sync_get_message_page(email: str, message_id: str) -> str:
    s = _get_session()
    cookie_email = urllib.parse.quote(base_address(email), safe="")
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": BASE_URL + "/inbox/",
        "Cookie": f"email={cookie_email}",
    }
    try:
        r = s.get(f"{BASE_URL}/content/{message_id}", headers=headers, timeout=12)
        return r.text
    except Exception:
        return ""


async def get_message(email: str, message_id: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: _sync_get_message_page(email, message_id)
    )


def _sync_delete_message(email: str, message_id: str) -> bool:
    email = base_address(email)
    try:
        token = _sync_get_token(email)
    except Exception:
        return False
    data = _post_json(
        "/action/mailbox/deleteMessage", {"messageId": message_id}, token=token
    )
    return bool(data.get("status"))


async def delete_message(email: str, message_id: str) -> bool:
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: _sync_delete_message(email, message_id)
    )


def _cf_decode(hex_str: str) -> str:
    try:
        key = int(hex_str[:2], 16)
        return "".join(
            chr(int(hex_str[i:i + 2], 16) ^ key) for i in range(2, len(hex_str), 2)
        )
    except Exception:
        return ""


def _undo_cf_email(html_str: str) -> str:
    return re.sub(
        r'<a[^>]*class="__cf_email__"[^>]*data-cfemail="([0-9a-fA-F]+)"[^>]*>.*?</a>',
        lambda m: _cf_decode(m.group(1)),
        html_str or "",
        flags=re.DOTALL,
    )


def _detail_field(page_html: str, label: str) -> str:
    m = re.search(
        r'<span class="label">' + label + r'[^<]*</span>\s*<span class="con">(.*?)</span>',
        page_html or "",
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", _undo_cf_email(m.group(1)))).strip()


def _sync_read_message_body(email: str, message_id: str, page: str) -> str:
    m = re.search(r'<iframe[^>]*src="([^"]+)"', page, re.IGNORECASE)
    if not m:
        return ""
    src = html.unescape(m.group(1))
    if src.startswith("/"):
        src = BASE_URL + src
    s = _get_session()
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"{BASE_URL}/content/{message_id}",
        "Cookie": f"email={urllib.parse.quote(base_address(email), safe='')}",
    }
    try:
        r = s.get(src, headers=headers, timeout=12)
        return r.text
    except Exception:
        return ""


async def read_message(email: str, message_id: str) -> dict:
    loop = asyncio.get_running_loop()
    page = await loop.run_in_executor(
        None, lambda: _sync_get_message_page(email, message_id)
    )
    if not page or "<iframe" not in page.lower():
        alt = undotted_address(email)
        if alt != base_address(email):
            page_alt = await loop.run_in_executor(
                None, lambda: _sync_get_message_page(alt, message_id)
            )
            page = page_alt or page
    if not page:
        return {"subject": "", "from": "", "html": ""}

    subject = _detail_field(page, "Rubject") or _detail_field(page, "Subject")
    sender = _detail_field(page, "From")

    body = ""
    m = re.search(r'<iframe[^>]*src="([^"]+)"', page, re.IGNORECASE)
    if m:
        body = await loop.run_in_executor(
            None, lambda: _sync_read_message_body(email, message_id, page)
        )
    if not body:
        m = re.search(r'<div class="content-iframe">(.*?)</div>', page, re.DOTALL | re.IGNORECASE)
        body = m.group(1) if m else ""
    return {"subject": subject, "from": sender, "html": _undo_cf_email(body)}
