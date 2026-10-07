"""
Microsoft (Outlook / Hotmail) mail providers.

Pehle /outlook, /outlook1../outlook5 sab ek hi backend (22.do recycled pool)
se address lete the, isliye har command same type ke purane mail dikhata tha.
Ab har route alag service par jaata hai (round-robin) aur har naye mailbox ke
purane (recycled) mail hide kar diye jaate hain.

Providers (free wale bina key ke chalte hain):
  0. HioMail     — free, har baar naya unique alias (100% fresh inbox)
  0. EmailMux    — free, outlook.com / hotmail.com, naya +tag har baar
  0. temp.tf     — free, outlook.com / hotmail.com pool
  1. 22.do       — free, koi key nahi
  2. Kopeechka   — KOPEECHKA_API_KEY  (fresh real outlook/hotmail)
  3. HotmailBox  — HOTMAILBOX_API_KEY (fresh real outlook/hotmail, Graph API)
Key na ho to provider auto-skip ho jaata hai.
"""
from __future__ import annotations

import os
import time
import html
import logging

import httpx

import outlook_service

logger = logging.getLogger(__name__)

FRESH_RETRIES = 3
GRACE_SECONDS = 300
TIMEOUT = httpx.Timeout(20.0)


def _ts(value) -> float:
    """Best-effort parse of a provider timestamp into epoch seconds (0 = unknown)."""
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e12 else 1)
    from datetime import datetime
    s = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return dt.timestamp()
        except Exception:
            continue
    return 0.0


# ── 1. 22.do ──────────────────────────────────────────────────────────────────
class TwentyTwoDo:
    name = "22do"
    label = "22.do"

    @staticmethod
    def configured() -> bool:
        return True

    def routes(self) -> list[dict]:
        return [
            {"provider": self.name, "label": f"22.do · {d}", "arg": d}
            for d in outlook_service.OUTLOOK_DOMAINS
        ]

    async def create(self, arg=None) -> dict:
        r = await outlook_service.create_outlook(source_domain=arg, clean_only=True)
        return {"email": r["email"], "handle": {"source_domain": r.get("source_domain")}}

    async def messages(self, email: str, handle: dict) -> list[dict]:
        raw = await outlook_service.get_messages(email)
        return [
            {
                "id": m.get("messageId"),
                "subject": m.get("subject") or "(No Subject)",
                "from": m.get("from") or "unknown",
                "time": m.get("time") or "",
                "ts": _ts(m.get("time")),
            }
            for m in raw if m.get("messageId")
        ]

    async def read(self, email: str, handle: dict, msg_id: str) -> dict:
        d = await outlook_service.read_message(email, msg_id)
        return {"subject": d.get("subject"), "from": d.get("from"), "html": d.get("html") or ""}

    async def delete(self, email: str, handle: dict, msg_id: str) -> bool:
        return await outlook_service.delete_message(email, msg_id)


# ── 2. Kopeechka ──────────────────────────────────────────────────────────────
class Kopeechka:
    name = "kopeechka"
    label = "Kopeechka"
    BASE = "https://api.kopeechka.store"

    @staticmethod
    def configured() -> bool:
        return bool(os.getenv("KOPEECHKA_API_KEY"))

    def routes(self) -> list[dict]:
        return [
            {"provider": self.name, "label": "Kopeechka · outlook.com", "arg": "outlook.com"},
            {"provider": self.name, "label": "Kopeechka · hotmail.com", "arg": "hotmail.com"},
        ]

    async def _get(self, path: str, **params) -> dict:
        params.update(token=os.getenv("KOPEECHKA_API_KEY"), type="json", api="2.0")
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(self.BASE + path, params=params)
            return r.json()

    async def create(self, arg=None) -> dict:
        d = await self._get(
            "/mailbox-get-email",
            site=os.getenv("KOPEECHKA_SITE", "lovable.dev"),
            mail_type=arg or "outlook.com",
            clear=1,
        )
        if d.get("status") != "OK":
            raise RuntimeError(f"Kopeechka: {d.get('value') or d}")
        return {"email": d["mail"], "handle": {"task_id": d["id"]}}

    async def messages(self, email: str, handle: dict) -> list[dict]:
        d = await self._get("/mailbox-get-message", id=handle["task_id"], full=1)
        if d.get("status") != "OK":
            return []  # WAIT_LINK = abhi mail nahi aaya
        body = d.get("fullmessage") or d.get("value") or ""
        handle["last_body"] = body
        return [{
            "id": f"kp-{handle['task_id']}",
            "subject": "Verification mail",
            "from": "kopeechka",
            "time": "",
            "ts": time.time(),
        }]

    async def read(self, email: str, handle: dict, msg_id: str) -> dict:
        if not handle.get("last_body"):
            await self.messages(email, handle)
        return {"subject": "Verification mail", "from": "kopeechka",
                "html": handle.get("last_body") or ""}

    async def delete(self, email: str, handle: dict, msg_id: str) -> bool:
        await self._get("/mailbox-cancel", id=handle["task_id"])
        return True


# ── 3. HotmailBox (Microsoft Graph) ───────────────────────────────────────────
class HotmailBox:
    name = "hotmailbox"
    label = "HotmailBox"
    BASE = "https://api.hotmailbox.me"
    TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    GRAPH = "https://graph.microsoft.com/v1.0/me"

    @staticmethod
    def configured() -> bool:
        return bool(os.getenv("HOTMAILBOX_API_KEY"))

    def routes(self) -> list[dict]:
        return [
            {"provider": self.name, "label": "HotmailBox · outlook", "arg": "OUTLOOK"},
            {"provider": self.name, "label": "HotmailBox · hotmail", "arg": "HOTMAIL"},
        ]

    async def create(self, arg=None) -> dict:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{self.BASE}/mail/buy", params={
                "apikey": os.getenv("HOTMAILBOX_API_KEY"),
                "mailcode": arg or "OUTLOOK", "quantity": 1,
            })
            d = r.json()
        emails = (d.get("Data") or {}).get("Emails") or []
        if not emails:
            raise RuntimeError(f"HotmailBox: {d.get('Message') or d}")
        e = emails[0]
        return {"email": e["Email"], "handle": {
            "refresh_token": e.get("RefreshToken"),
            "client_id": e.get("ClientId") or "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        }}

    async def _token(self, handle: dict) -> str:
        if handle.get("access_token") and handle.get("exp", 0) > time.time() + 60:
            return handle["access_token"]
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(self.TOKEN_URL, data={
                "client_id": handle["client_id"],
                "grant_type": "refresh_token",
                "refresh_token": handle["refresh_token"],
                "scope": "https://graph.microsoft.com/.default offline_access",
            })
            d = r.json()
        if "access_token" not in d:
            raise RuntimeError(f"Graph token: {d.get('error_description') or d}")
        handle["access_token"] = d["access_token"]
        handle["exp"] = time.time() + int(d.get("expires_in", 3600))
        if d.get("refresh_token"):
            handle["refresh_token"] = d["refresh_token"]
        return d["access_token"]

    async def _graph(self, method: str, path: str, handle: dict, **kw):
        tok = await self._token(handle)
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            return await c.request(method, self.GRAPH + path,
                                   headers={"Authorization": f"Bearer {tok}"}, **kw)

    async def messages(self, email: str, handle: dict) -> list[dict]:
        r = await self._graph("GET", "/messages", handle, params={
            "$top": 25, "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,receivedDateTime",
        })
        out = []
        for m in r.json().get("value", []):
            out.append({
                "id": m["id"],
                "subject": m.get("subject") or "(No Subject)",
                "from": ((m.get("from") or {}).get("emailAddress") or {}).get("address", "unknown"),
                "time": m.get("receivedDateTime", ""),
                "ts": _ts(m.get("receivedDateTime")),
            })
        return out

    async def read(self, email: str, handle: dict, msg_id: str) -> dict:
        m = (await self._graph("GET", f"/messages/{msg_id}", handle)).json()
        body = (m.get("body") or {}).get("content") or ""
        if (m.get("body") or {}).get("contentType") == "text":
            body = html.escape(body)
        return {
            "subject": m.get("subject"),
            "from": ((m.get("from") or {}).get("emailAddress") or {}).get("address"),
            "html": body,
        }

    async def delete(self, email: str, handle: dict, msg_id: str) -> bool:
        r = await self._graph("DELETE", f"/messages/{msg_id}", handle)
        return r.status_code in (200, 204)


# ── 4. HioMail (free, public) ─────────────────────────────────────────────────
class HioMail:
    """hiomail.com — real @outlook/@hotmail/@live mailbox pool, har baar
    naya unique +tag milta hai, isliye inbox hamesha 100% fresh hota hai."""
    name = "hiomail"
    label = "HioMail"
    BASE = "https://hiomail.com"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/136.0 Safari/537.36",
        "Origin": "https://hiomail.com",
        "Referer": "https://hiomail.com/en/",
        "Accept": "application/json",
    }

    @staticmethod
    def configured() -> bool:
        return os.getenv("DISABLE_HIOMAIL") != "1"

    def routes(self) -> list[dict]:
        return [{"provider": self.name, "label": "HioMail · outlook/hotmail", "arg": "outlook"}]

    async def create(self, arg=None) -> dict:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=self.HEADERS) as c:
            r = await c.post(f"{self.BASE}/api/alias/new", params={"provider": arg or "outlook"})
        if r.status_code != 200:
            raise RuntimeError(f"HioMail HTTP {r.status_code}")
        d = r.json()
        if not d.get("alias") or not d.get("token"):
            raise RuntimeError(f"HioMail: {d}")
        return {"email": d["alias"], "handle": {"token": d["token"], "cache": {}}}

    async def _inbox(self, email: str, handle: dict) -> list[dict]:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=self.HEADERS) as c:
            r = await c.post(f"{self.BASE}/api/inbox", json={"alias": email},
                             headers={"Authorization": f"Bearer {handle['token']}"})
        if r.status_code != 200:
            return []
        return r.json().get("messages") or []

    async def messages(self, email: str, handle: dict) -> list[dict]:
        out = []
        cache = handle.setdefault("cache", {})
        for i, m in enumerate(await self._inbox(email, handle)):
            mid = str(m.get("id") or i)
            cache[mid] = m
            out.append({
                "id": mid,
                "subject": m.get("subject") or "(No Subject)",
                "from": m.get("sender") or "unknown",
                "time": m.get("time") or "",
                "ts": _ts(m.get("timestamp")),
            })
        return out

    async def read(self, email: str, handle: dict, msg_id: str) -> dict:
        m = handle.get("cache", {}).get(msg_id)
        if not m:
            await self.messages(email, handle)
            m = handle.get("cache", {}).get(msg_id) or {}
        body = m.get("html") or ("<pre>" + html.escape(m.get("text") or m.get("snippet") or "") + "</pre>")
        return {"subject": m.get("subject"), "from": m.get("sender"), "html": body}

    async def delete(self, email: str, handle: dict, msg_id: str) -> bool:
        handle.get("cache", {}).pop(msg_id, None)
        return True


# ── 5. temp.tf (free, public) ─────────────────────────────────────────────────
class TempTf:
    """temp.tf — apne outlook.com / hotmail.com accounts ka pool (+alias)."""
    name = "temptf"
    label = "temp.tf"
    BASE = "https://temp.tf"
    HEADERS = {
        "User-Agent": HioMail.HEADERS["User-Agent"],
        "Referer": "https://temp.tf/",
        "Origin": "https://temp.tf",
    }

    @staticmethod
    def configured() -> bool:
        return os.getenv("DISABLE_TEMPTF") != "1"

    def routes(self) -> list[dict]:
        return [
            {"provider": self.name, "label": "temp.tf · hotmail.com", "arg": "hotmail.com"},
            {"provider": self.name, "label": "temp.tf · outlook.com", "arg": "outlook.com"},
        ]

    async def create(self, arg=None) -> dict:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=self.HEADERS) as c:
            r = await c.get(f"{self.BASE}/api/account",
                            params={"providers": arg or "outlook.com", "dot": "0", "plus": "0"})
        d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not d.get("email"):
            raise RuntimeError(f"temp.tf: {d.get('error') or 'pool khali hai'}")
        return {"email": d["email"], "handle": {"cache": {}}}

    async def messages(self, email: str, handle: dict) -> list[dict]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), headers=self.HEADERS) as c:
            r = await c.post(f"{self.BASE}/api/check", json={"email": email, "wait": False})
        if r.status_code != 200:
            return []
        d = r.json()
        items = d if isinstance(d, list) else (d.get("data") or [])
        cache = handle.setdefault("cache", {})
        out = []
        for i, m in enumerate(items):
            mid = str(m.get("id") or i)
            cache[mid] = m
            frm = m.get("from")
            if isinstance(frm, dict):
                frm = frm.get("address") or frm.get("name")
            out.append({
                "id": mid,
                "subject": m.get("subject") or "(No Subject)",
                "from": frm or "unknown",
                "time": m.get("date") or "",
                "ts": _ts(m.get("date")),
            })
        return out

    async def read(self, email: str, handle: dict, msg_id: str) -> dict:
        m = handle.get("cache", {}).get(msg_id)
        if not m:
            await self.messages(email, handle)
            m = handle.get("cache", {}).get(msg_id) or {}
        body = (m.get("html") or m.get("body") or "")
        if not body:
            body = "<pre>" + html.escape(m.get("text") or m.get("snippet") or "") + "</pre>"
        frm = m.get("from")
        if isinstance(frm, dict):
            frm = frm.get("address") or frm.get("name")
        return {"subject": m.get("subject"), "from": frm, "html": body}

    async def delete(self, email: str, handle: dict, msg_id: str) -> bool:
        handle.get("cache", {}).pop(msg_id, None)
        return True


# ── 6. EmailMux (free, public) ────────────────────────────────────────────────
class EmailMux:
    """emailmux.com — real @outlook.com / @hotmail.com pool, har baar naya
    random +tag, isliye inbox fresh rehta hai. Requests MD5 se signed hote hain."""
    name = "emailmux"
    label = "EmailMux"
    BASE = "https://emailmux.com"
    SALT = "yjd683c@47"
    HEADERS = {
        "User-Agent": HioMail.HEADERS["User-Agent"],
        "Origin": "https://emailmux.com",
        "Referer": "https://emailmux.com/",
    }

    @staticmethod
    def configured() -> bool:
        return os.getenv("DISABLE_EMAILMUX") != "1"

    def routes(self) -> list[dict]:
        return [
            {"provider": self.name, "label": "EmailMux · outlook.com", "arg": "outlook"},
            {"provider": self.name, "label": "EmailMux · hotmail.com", "arg": "hotmail"},
        ]

    def _sig(self, email: str) -> dict:
        import hashlib
        ts = str(int(time.time() * 1000))
        sig = hashlib.md5((self.SALT + email + ts).encode()).hexdigest()
        return {"X-API-Timestamp": ts, "X-API-Signature": sig, "Content-Type": "application/json"}

    def _client(self, handle: dict | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT, headers=self.HEADERS,
                                 cookies=(handle or {}).get("cookies") or None,
                                 follow_redirects=True)

    async def create(self, arg=None) -> dict:
        async with self._client() as c:
            await c.get(self.BASE + "/")
            r = await c.post(self.BASE + "/generate-email", json={"domains": [arg or "outlook"]})
            d = r.json()
            email = d.get("email")
            if d.get("status") != "success" or not email:
                raise RuntimeError(f"EmailMux: {d.get('msg') or d}")
            await c.get(self.BASE + "/use-email", params={"email": email}, headers=self._sig(email))
            return {"email": email, "handle": {"cookies": dict(c.cookies)}}

    async def messages(self, email: str, handle: dict) -> list[dict]:
        async with self._client(handle) as c:
            r = await c.get(self.BASE + "/emails", params={"email": email}, headers=self._sig(email))
            if r.status_code == 401:  # session reset — dobara attach karo
                await c.get(self.BASE + "/use-email", params={"email": email}, headers=self._sig(email))
                r = await c.get(self.BASE + "/emails", params={"email": email}, headers=self._sig(email))
            handle["cookies"] = dict(c.cookies)
        if r.status_code != 200:
            return []
        items = r.json()
        if not isinstance(items, list):
            return []
        out = []
        for m in items:
            uid = m.get("uuid")
            if not uid or uid == "WelcomeToEmailMux":
                continue
            out.append({
                "id": uid,
                "subject": m.get("subject") or "(No Subject)",
                "from": m.get("sender") or m.get("email_address") or "unknown",
                "time": m.get("timestamp") or "",
                "ts": _ts(m.get("timestamp")),
            })
        return out

    async def read(self, email: str, handle: dict, msg_id: str) -> dict:
        import json, re
        async with self._client(handle) as c:
            r = await c.get(f"{self.BASE}/en/email/{msg_id}")
        body = ""
        m = re.search(r'<script id="email-html-data" type="application/json">\s*(.*?)\s*</script>', r.text, re.S)
        if m:
            try:
                body = json.loads(m.group(1))
            except Exception:
                body = ""
        subj = re.search(r'<span class="text-blue-600">(.*?)</span>', r.text, re.S)
        return {"subject": html.unescape(subj.group(1)) if subj else None,
                "from": None, "html": body or ""}

    async def delete(self, email: str, handle: dict, msg_id: str) -> bool:
        async with self._client(handle) as c:
            r = await c.get(self.BASE + "/del-email", params={"emailuuid": msg_id},
                            headers=self._sig(email))
        return r.status_code == 200


# HioMail / EmailMux "+" / temp.tf "+" wale address dete the — hata diye.
PROVIDERS = {p.name: p for p in (TwentyTwoDo(), Kopeechka(), HotmailBox())}


def build_routes() -> list[dict]:
    """Interleave routes so /outlook1, /outlook2, /outlook3 hit different services."""
    lists = [p.routes() for p in PROVIDERS.values() if p.configured()]
    out: list[dict] = []
    i = 0
    while any(i < len(l) for l in lists):
        for l in lists:
            if i < len(l):
                out.append(l[i])
        i += 1
    return out


ROUTES = build_routes()


def route_by_index(n: int) -> dict:
    return ROUTES[(n - 1) % len(ROUTES)]


async def create_for_variant(n: int) -> dict:
    """/outlookN: pehle apni service, fail ho to agli service (kabhi khali na lauto)."""
    errors = []
    order = [ROUTES[(n - 1 + k) % len(ROUTES)] for k in range(len(ROUTES))]
    # Primary fail ho to sabse reliable free service (HioMail) ko turant try karo
    for k, route in enumerate(order):
        try:
            r = await create(route)
            if k:
                r["route_label"] += " (fallback)"
            return r
        except Exception as e:
            logger.warning("outlook%s %s failed: %s", n, route["label"], e)
            errors.append(f"{route['label']}: {e}")
    raise RuntimeError("; ".join(errors)[:300])


async def create(route: dict) -> dict:
    """Create a FRESH mailbox. Recycled inboxes are retried; leftovers are hidden."""
    p = PROVIDERS[route["provider"]]
    last = None
    for _ in range(FRESH_RETRIES):
        r = await p.create(route.get("arg"))
        if "+" in r["email"].split("@")[0]:
            last = None
            continue  # "+" wala address nahi chahiye
        created = time.time()
        try:
            old = await p.messages(r["email"], r["handle"])
        except Exception:
            old = []
        last = {
            "email": r["email"], "handle": r["handle"], "provider": p.name,
            "provider_label": p.label, "route_label": route["label"],
            "created_at": created, "ignore_ids": {m["id"] for m in old},
        }
        if not old:
            return last
        logger.info("%s gave recycled inbox %s (%d old mails) — retrying",
                    p.label, r["email"], len(old))
    if not last:
        raise RuntimeError(f"{p.label}: sirf + wale address mile")
    return last


async def create_any(start: int = 0) -> dict:
    errors = []
    for k in range(len(ROUTES)):
        route = ROUTES[(start + k) % len(ROUTES)]
        try:
            return await create(route)
        except Exception as e:
            errors.append(f"{route['label']}: {e}")
    raise RuntimeError("; ".join(errors)[:300])


async def messages(session: dict) -> list[dict]:
    p = PROVIDERS[session.get("provider", "22do")]
    items = await p.messages(session["email"], session.setdefault("handle", {}))
    ignore = session.get("ignore_ids") or set()
    cutoff = session.get("created_at", 0) - GRACE_SECONDS
    return [m for m in items
            if m["id"] not in ignore and (not m.get("ts") or m["ts"] >= cutoff)]


async def read(session: dict, msg_id: str) -> dict:
    p = PROVIDERS[session.get("provider", "22do")]
    return await p.read(session["email"], session.setdefault("handle", {}), msg_id)


async def delete(session: dict, msg_id: str) -> bool:
    p = PROVIDERS[session.get("provider", "22do")]
    return await p.delete(session["email"], session.setdefault("handle", {}), msg_id)


def summary() -> str:
    lines = []
    for i, r in enumerate(ROUTES, 1):
        lines.append(f"/outlook{i} → {r['label']}")
    off = [p.label for p in PROVIDERS.values() if not p.configured()]
    if off:
        lines.append("\nBand (API key nahi): " + ", ".join(off))
    return "\n".join(lines)
