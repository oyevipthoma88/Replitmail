"""
Outlook service — real @outlook.com / @hotmail.com temp addresses via 22.do.

22.do's free API returns a random provider address when you POST a domain.
Passing one of the Outlook-capable domains makes it return a genuine
@outlook.com or @hotmail.com address — but 22.do randomly rotates the
result, so we retry until we get an Outlook-family address.

Flow:
  1. GET  /inbox/                   -> establish Cloudflare cookies + XSRF
  2. POST /action/mailbox/create   {"type":"microsoft","domain":...}  -> {email,...}
  3. POST /action/mailbox/applyToken {email, uuid}                    -> {token (JWT)}
  4. POST /action/mailbox/message   {email, lastime}                   -> [ {messageId,...} ]
  5. GET  /content/{messageId}      (cookie: email=...)                -> HTML page with body
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import random
import urllib.parse

import asyncio

import aiohttp

BASE_URL = "https://22.do"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Realistic browser headers to survive Cloudflare's bot-fight mode.
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",  # no brotli: aiohttp cannot decode br without the Brotli package
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/inbox/",
    "X-Requested-With": "XMLHttpRequest",
}

# All known 22.do domains that can yield @outlook.com / @hotmail.com.
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

# Accepted Outlook-family domains (both are real Microsoft Outlook addresses).
ACCEPTED_DOMAINS = ("outlook.com", "hotmail.com")

MAX_RETRIES_PER_DOMAIN = 4


def _uuid_for(email: str) -> str:
    """Stable 32-char hex uuid (mimics FingerprintJS visitorId)."""
    return hashlib.md5(("replitmail:" + email).encode()).hexdigest()


async def _warmup(session: aiohttp.ClientSession) -> None:
    """Prime the session cookies by loading /inbox/ once — some Cloudflare
    checks fail if the API is hit cold."""
    try:
        async with session.get(BASE_URL + "/inbox/", headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }, timeout=aiohttp.ClientTimeout(total=15)) as r:
            await r.read()
    except Exception:
        pass


async def _post_json(session: aiohttp.ClientSession, path: str, payload: dict, *, token: str | None = None) -> dict:
    headers = dict(BROWSER_HEADERS)
    headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with session.post(
            BASE_URL + path,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            text = await r.text()
            if r.status >= 400:
                return {"status": False, "msg": f"HTTP {r.status}"}
            try:
                return json.loads(text)
            except Exception:
                return {"status": False, "msg": text[:200]}
    except Exception as e:
        return {"status": False, "msg": f"{type(e).__name__}: {str(e)[:120]}"}


def _is_outlook_family(email: str) -> bool:
    """True if email ends with an accepted Outlook-family domain."""
    lowered = (email or "").lower()
    return any(lowered.endswith("@" + d) or lowered.endswith("." + d) for d in ACCEPTED_DOMAINS)


# Address families 22.do can hand out, per mailbox type.
FAMILY = {
    "microsoft": ("outlook.com", "hotmail.com"),
    "gmail": ("gmail.com", "googlemail.com"),
}

MAX_ROUNDS = 3


def base_address(email: str) -> str:
    """Strip a "+tag" sub-address: 22.do keys every inbox by the BASE account,
    so mail to "abc+xy@hotmail.com" is only ever listed under "abc@hotmail.com".
    Polling the alias form returns an empty list forever — that was the
    "Outlook pe mail receive nahi hota" bug."""
    email = (email or "").strip()
    if "+" not in email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    return local.split("+", 1)[0] + "@" + domain


def _in_family(email: str, kind: str) -> bool:
    lowered = (email or "").lower()
    return any(lowered.endswith("@" + d) for d in FAMILY.get(kind, ()))


def _is_clean_base_address(email: str) -> bool:
    """Accept only a plain local part (no dot, plus, or other separators)."""
    email = base_address(email)
    local, separator, _domain = email.partition("@")
    return bool(separator and re.fullmatch(r"[A-Za-z0-9]+", local))


async def create_mailbox(
    kind: str = "microsoft",
    allow_alias: bool = False,
    source_domain: str | None = None,
    clean_only: bool = False,
) -> dict:
    """Create a real provider mailbox on 22.do.

    kind="microsoft" -> @outlook.com / @hotmail.com
    kind="gmail"     -> @gmail.com / @googlemail.com

    22.do rotates its result and usually returns a "+tag" sub-address. That
    tag is cosmetic: the mailbox is the base account, signup sites normalise
    it away, and 22.do only ever lists mail under the base form. So the tag is
    stripped and the base address is what the bot hands out and polls.
    """
    domains = [source_domain] if source_domain else OUTLOOK_DOMAINS[:]
    random.shuffle(domains)
    last_err = "unknown"
    for _round in range(MAX_ROUNDS):
        connector = aiohttp.TCPConnector(ssl=False)
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as s:
            await _warmup(s)
            for domain in domains:
                for attempt in range(MAX_RETRIES_PER_DOMAIN):
                    data = await _post_json(
                        s, "/action/mailbox/create",
                        {"type": kind, "domain": domain},
                    )
                    if not data.get("status") or not data.get("data"):
                        last_err = str(data.get("msg", data))[:120]
                        if "403" in last_err or "Cloudflare" in last_err:
                            await asyncio.sleep(3)
                        else:
                            await asyncio.sleep(1)
                        continue
                    email = data["data"].get("email")
                    if not _in_family(email or "", kind):
                        last_err = f"got out-of-family address: {email}"
                        continue
                    address = base_address(email)
                    if clean_only and not _is_clean_base_address(address):
                        last_err = f"got non-clean address: {address}"
                        continue
                    return {
                        "email": address,
                        "source_domain": domain,
                    }
        if _round < MAX_ROUNDS - 1:
            await asyncio.sleep(2)
    label = "@outlook.com / @hotmail.com" if kind == "microsoft" else "@gmail.com / @googlemail.com"
    raise RuntimeError(f"22.do abhi {label} nahi de raha ({last_err}).")


async def create_outlook(
    allow_alias: bool = False,
    source_domain: str | None = None,
    clean_only: bool = False,
) -> dict:
    """Real @outlook.com / @hotmail.com mailbox."""
    return await create_mailbox(
        "microsoft",
        allow_alias=allow_alias,
        source_domain=source_domain,
        clean_only=clean_only,
    )


async def create_outlook_batch(count: int = 5) -> list[dict]:
    """Create clean Outlook mailboxes through different 22.do source routes.

    The source domain is only the upstream route used to request a mailbox;
    the returned address is always a real @outlook.com or @hotmail.com address.
    """
    count = max(1, min(count, len(OUTLOOK_DOMAINS)))
    sources = OUTLOOK_DOMAINS[:]
    random.shuffle(sources)
    results: list[dict] = []

    # Run each source route independently, then use the next route if a
    # particular pool is unavailable or returns a duplicate address.
    for start in range(0, len(sources), count):
        batch_sources = sources[start:start + count]
        attempts = await asyncio.gather(
            *(
                create_mailbox(
                    "microsoft",
                    source_domain=source,
                    clean_only=True,
                )
                for source in batch_sources
            ),
            return_exceptions=True,
        )
        for source, result in zip(batch_sources, attempts):
            if isinstance(result, Exception):
                continue
            if any(item["email"] == result["email"] for item in results):
                continue
            result["source_domain"] = source
            results.append(result)
            if len(results) >= count:
                return results

    raise RuntimeError(
        f"Sirf {len(results)}/{count} clean Outlook mailboxes mile."
    )


async def create_gmail(allow_alias: bool = False) -> dict:
    """Real @gmail.com / @googlemail.com mailbox (fresh 22.do base account)."""
    return await create_mailbox("gmail", allow_alias=allow_alias)


async def get_token(email: str) -> str:
    """Fetch a JWT token for reading the inbox of `email`."""
    email = base_address(email)
    uuid = _uuid_for(email)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as s:
        await _warmup(s)
        data = await _post_json(s, "/action/mailbox/applyToken", {"email": email, "uuid": uuid})
        if not data.get("status") or not data.get("data"):
            raise RuntimeError(f"get_token failed: {data.get('msg', data)}")
        return data["data"]["token"]


def undotted_address(email: str) -> str:
    """`j.oh.n@outlook.com` -> `john@outlook.com`.

    Custom-dot address ka mail base inbox me tabhi aata hai jab wo Microsoft
    account par alias ke roop me registered ho — us case me 22.do use base
    account ke neeche list karta hai. Isliye dot-wale address par kuch na mile
    to hum dots hata kar dobara poll karte hain."""
    email = base_address(email)
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    return local.replace(".", "") + "@" + domain


async def get_messages(email: str) -> list[dict]:
    """
    Return list of message metadata: [{messageId, subject, from, time}].
    Empty list if no messages.
    """
    msgs = await _get_messages_exact(email)
    if msgs:
        return msgs
    alt = undotted_address(email)
    if alt != base_address(email):
        return await _get_messages_exact(alt)
    return msgs


async def _get_messages_exact(email: str) -> list[dict]:
    email = base_address(email)
    try:
        token = await get_token(email)
    except Exception:
        return []
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as s:
        await _warmup(s)
        data = await _post_json(
            s, "/action/mailbox/message", {"email": email, "lastime": 0}, token=token
        )
        if not data.get("status"):
            return []
        msgs = data.get("data")
        if not msgs or msgs is True:
            return []
        return msgs


async def get_message(email: str, message_id: str) -> str:
    """
    Fetch a single message's HTML body via the server-rendered
    /content/{messageId} page (authenticated by the email cookie).
    Returns raw HTML string.
    """
    cookie_email = urllib.parse.quote(base_address(email), safe="")
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": BASE_URL + "/inbox/",
        "Cookie": f"email={cookie_email}",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{BASE_URL}/content/{message_id}", headers=headers, timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                return await r.text()
    except Exception:
        return ""


async def delete_message(email: str, message_id: str) -> bool:
    """Delete a single message. Returns True on success."""
    email = base_address(email)
    try:
        token = await get_token(email)
    except Exception:
        return False
    async with aiohttp.ClientSession() as s:
        data = await _post_json(
            s, "/action/mailbox/deleteMessage", {"messageId": message_id}, token=token
        )
        return bool(data.get("status"))


def _cf_decode(hex_str: str) -> str:
    """Decode Cloudflare's email-obfuscation payload (data-cfemail)."""
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


async def read_message(email: str, message_id: str) -> dict:
    """Fetch one message as {subject, from, html}.

    /content/{id} is only a shell page: the mail body lives in an <iframe>
    pointing at /view/{blob}. Reading the shell alone gave "(No Subject)" from
    the site's <title> and no body at all, so the iframe is followed here.
    """
    page = await get_message(email, message_id)
    if not page or "<iframe" not in page.lower():
        alt = undotted_address(email)
        if alt != base_address(email):
            page = await get_message(alt, message_id) or page
    if not page:
        return {"subject": "", "from": "", "html": ""}

    subject = _detail_field(page, "Rubject") or _detail_field(page, "Subject")
    sender = _detail_field(page, "From")

    body = ""
    m = re.search(r'<iframe[^>]*src="([^"]+)"', page, re.IGNORECASE)
    if m:
        src = html.unescape(m.group(1))
        if src.startswith("/"):
            src = BASE_URL + src
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": f"{BASE_URL}/content/{message_id}",
            "Cookie": f"email={urllib.parse.quote(base_address(email), safe='')}",
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(src, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    body = await r.text()
        except Exception:
            body = ""
    if not body:
        m = re.search(r'<div class="content-iframe">(.*?)</div>', page, re.DOTALL | re.IGNORECASE)
        body = m.group(1) if m else ""
    return {"subject": subject, "from": sender, "html": _undo_cf_email(body)}
