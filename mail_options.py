"""
mail_options.py — real-provider mailboxes only (Gmail / GoogleMail / Outlook).

Why this module is so small now
───────────────────────────────
Every temp-mail provider (mail.tm, mail.gw, temp-mail.io, TempMail Plus,
1secmail, mailsac, relay aliases …) is removed. Lovable, GitHub and
Bolt.new all reject those domains outright, so the bot only offers mailboxes
on genuine provider domains:

  • @gmail.com / @googlemail.com  — real Google mailbox (Emailnator pool)
  • @outlook.com / @hotmail.com   — real Microsoft mailbox

Plus the user's OWN Gmail / Outlook via /gmail and /outlookmail aliases.

Every address is still verified live before it is handed over:
  1. domain must be on the allow-list of real providers
  2. domain must have a real MX record (DNS-over-HTTPS)
  3. the inbox must be readable end-to-end through the provider API
  4. for Bolt.new the exact address is probed against its signup endpoint
"""
from __future__ import annotations

import asyncio
import logging
import random
import secrets
import string
import time

import aiohttp

import emailnator_service
import gmail_alias
import outlook_service

logger = logging.getLogger("TempMailBot.options")

TIMEOUT = aiohttp.ClientTimeout(total=15)

# The ONLY domains this bot will ever hand out.
GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
OUTLOOK_DOMAINS = ("outlook.com", "hotmail.com", "live.com", "msn.com")
ALLOWED_DOMAINS = GMAIL_DOMAINS + OUTLOOK_DOMAINS

BLOCKLIST_URLS = [
    "https://raw.githubusercontent.com/disposable-email-domains/"
    "disposable-email-domains/main/disposable_email_blocklist.conf",
]
_BLOCKLIST: set[str] = set()
_BLOCKLIST_AT = 0.0
_BLOCKLIST_TTL = 6 * 3600

_MX_CACHE: dict[str, tuple[bool, float]] = {}
_MX_TTL = 3600

_DISPOSABLE_CACHE: dict[str, tuple[bool, float]] = {}
_DISPOSABLE_TTL = 3600


# ── DOMAIN VERIFICATION ────────────────────────────────────────────────────────
async def get_blocklist() -> set[str]:
    global _BLOCKLIST, _BLOCKLIST_AT
    if _BLOCKLIST and (time.time() - _BLOCKLIST_AT) < _BLOCKLIST_TTL:
        return _BLOCKLIST
    collected: set[str] = set()
    for url in BLOCKLIST_URLS:
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
                async with s.get(url) as r:
                    if r.status == 200:
                        text = await r.text()
                        collected.update(
                            ln.strip().lower()
                            for ln in text.splitlines()
                            if ln.strip() and not ln.startswith("#")
                        )
        except Exception as e:
            logger.warning("blocklist %s: %s", url, e)
    if collected:
        _BLOCKLIST, _BLOCKLIST_AT = collected, time.time()
    return _BLOCKLIST


async def has_mx(domain: str) -> bool:
    domain = (domain or "").lower().strip().lstrip("@")
    cached = _MX_CACHE.get(domain)
    if cached and (time.time() - cached[1]) < _MX_TTL:
        return cached[0]
    ok = False
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(
                "https://dns.google/resolve",
                params={"name": domain, "type": "MX"},
            ) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    ok = bool(data.get("Answer"))
    except Exception as e:
        logger.warning("mx %s: %s", domain, e)
    _MX_CACHE[domain] = (ok, time.time())
    return ok


async def kickbox_disposable(domain: str) -> bool | None:
    """True = live disposable detector flags it, None = unknown."""
    domain = (domain or "").lower().strip().lstrip("@")
    cached = _DISPOSABLE_CACHE.get(domain)
    if cached and (time.time() - cached[1]) < _DISPOSABLE_TTL:
        return cached[0]
    val: bool | None = None
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(f"https://open.kickbox.com/v1/disposable/{domain}") as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    val = bool(data.get("disposable"))
    except Exception as e:
        logger.warning("kickbox %s: %s", domain, e)
        return None
    _DISPOSABLE_CACHE[domain] = (val, time.time())
    return val


def is_allowed_domain(domain: str) -> bool:
    return (domain or "").lower().strip().lstrip("@") in ALLOWED_DOMAINS


async def verify_domain(domain: str) -> tuple[bool, str]:
    """/check — is this domain usable for a real signup?"""
    domain = (domain or "").lower().strip().lstrip("@")
    mx = await has_mx(domain)
    if not mx:
        return False, f"❌ `{domain}` — koi MX record nahi (mail receive hi nahi hoga)."
    if is_allowed_domain(domain):
        return True, (
            f"✅ `{domain}` — real provider domain (Gmail / Outlook family). "
            f"Signup-safe."
        )
    if await kickbox_disposable(domain) is True:
        return False, (
            f"⚠️ `{domain}` — live disposable-detector isse temp-mail bata raha hai. "
            f"Signup reject hoga — sirf Gmail / GoogleMail / Outlook use karo."
        )
    blocked = await get_blocklist()
    if domain in blocked:
        return False, (
            f"⚠️ `{domain}` — public disposable blocklist par hai. "
            f"Sirf Gmail / GoogleMail / Outlook use karo."
        )
    return False, (
        f"ℹ️ `{domain}` — MX ok hai, par ye bot sirf real providers "
        f"(Gmail / GoogleMail / Outlook) handle karta hai."
    )


def _rand(n: int = 11) -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(n))


# ── TARGET-SIDE LIVE PROBE (Bolt.new) ─────────────────────────────────────────
# Bolt.new's registration endpoint validates the e-mail field BEFORE the
# captcha, so a throwaway captcha token tells us whether an address is usable
# without ever creating an account (and without burning a verification mail).
_BOLT_URL = "https://bolt.new/api/auth/registration"
_BOLT_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://bolt.new",
    "Referer": "https://bolt.new/login",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}
_BOLT_CACHE: dict[str, tuple[bool, float]] = {}
_BOLT_TTL = 12 * 3600


async def _bolt_probe(email: str) -> str | None:
    payload = {
        "email": email,
        "username": _rand(9),
        "password": "Qx7!" + _rand(10),
        "captcha_verification_token": "probe",
    }
    for _attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
                async with s.post(_BOLT_URL, json=payload, headers=_BOLT_HEADERS) as r:
                    data = await r.json(content_type=None)
        except Exception as e:
            logger.warning("bolt probe %s: %s", email, e)
            return None
        if not isinstance(data, dict):
            return None
        if data.get("error") == "rate_limited" or data.get("retryable") is True:
            await asyncio.sleep(5)
            continue
        return (data.get("fieldErrors") or {}).get("email") or ""
    return None


def _verdict(err: str) -> bool | None:
    low = err.lower()
    if "block" in low or "not allowed" in low or "disposable" in low:
        return False
    if "already" in low or "taken" in low or "used" in low:
        return None
    return True


async def bolt_domain_ok(domain: str) -> bool | None:
    domain = (domain or "").lower().strip().lstrip("@")
    if not domain:
        return None
    cached = _BOLT_CACHE.get(domain)
    if cached and (time.time() - cached[1]) < _BOLT_TTL:
        return cached[0]
    err = await _bolt_probe(f"{_rand(9)}@{domain}")
    if err is None:
        return None
    verdict = _verdict(err)
    if verdict is None:
        return True
    _BOLT_CACHE[domain] = (verdict, time.time())
    return verdict


async def bolt_address_ok(email: str) -> bool | None:
    """False = Bolt already has this address (base account burned)."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    err = await _bolt_probe(email)
    if err is None:
        return None
    low = err.lower()
    if "already" in low or "taken" in low or "used" in low:
        return False
    return _verdict(err)


async def target_domain_ok(target: str, domain: str) -> bool | None:
    if target == "bolt":
        return await bolt_domain_ok(domain)
    return None


async def target_address_ok(target: str, email: str) -> bool | None:
    if target == "bolt":
        return await bolt_address_ok(email)
    return None


# ── ADDRESS HELPERS ────────────────────────────────────────────────────────────
_USED_BASES: set[str] = set()


def gmail_base(address: str) -> str:
    """Google normalises dots and '+' tags — this is the real account."""
    local, _, domain = (address or "").lower().partition("@")
    if domain not in GMAIL_DOMAINS:
        return ""
    return local.split("+")[0].replace(".", "") + "@gmail.com"


def mark_address_taken(address: str) -> None:
    base = gmail_base(address)
    if base:
        _USED_BASES.add(base)


def address_variants(address: str) -> list[str]:
    """Every form the SAME mailbox can be addressed as.

    Bolt.new / Lovable send the verification mail to whichever form was typed
    into the signup box, and Emailnator's message list is keyed per form. A
    mail sent to `x@googlemail.com` therefore does NOT show up when only
    `x@gmail.com` is polled (and vice-versa) — that is exactly why Bolt
    verification mails looked like they "never arrived". So the bot polls
    every variant of the address and merges the results.
    """
    addr = (address or "").strip().lower()
    local, _, domain = addr.partition("@")
    if not local or domain not in GMAIL_DOMAINS:
        return [addr] if addr else []
    return [f"{local}@{d}" for d in GMAIL_DOMAINS]


def is_signup_usable(address: str) -> tuple[bool, str]:
    """Reject addresses that GitHub / Lovable / Bolt.new refuse outright."""
    addr = (address or "").strip().lower()
    if "@" not in addr:
        return False, "invalid address"
    local, _, domain = addr.partition("@")
    if "+" in local:
        return False, "'+' alias addresses signup par reject hote hain"
    if not local or not domain or "." not in domain:
        return False, "malformed address"
    if not is_allowed_domain(domain):
        return False, (
            f"{domain} allowed nahi hai — bot sirf Gmail / GoogleMail / Outlook deta hai"
        )
    return True, ""


# ── BUILDERS ───────────────────────────────────────────────────────────────────
async def build_gmail() -> dict:
    """Real @gmail.com / @googlemail.com mailbox via Emailnator.

    @googlemail.com is tried first: it is the same Google inbox but survives
    `is_disposable` checks more often than the well-known dot-alias form.
    """
    strategies = [["googleMail"], ["googleMail", "dotGmail"], ["dotGmail"]]
    last_err = "Gmail service respond nahi kar raha"
    for opts in strategies:
        for _ in range(3):
            try:
                address = await emailnator_service.generate_email(opts)
            except Exception as e:
                logger.warning("emailnator %s: %s", opts, e)
                last_err = str(e)
                await asyncio.sleep(1)
                continue
            if "+" in address:
                continue
            if not address.lower().endswith(tuple(f"@{d}" for d in GMAIL_DOMAINS)):
                continue
            if gmail_base(address) in _USED_BASES:
                continue
            try:
                await emailnator_service.get_messages(address)
            except Exception as e:
                logger.warning("emailnator inbox: %s", e)
                last_err = str(e)
                continue
            return {
                "service": "emailnator",
                "email": address,
                # Shared Google pool: mail arrives, but the account is public.
                "warn": False,
                "shared_pool": True,
            }
    raise RuntimeError(last_err)


async def build_gmail_fresh() -> dict:
    """Real @gmail.com / @googlemail.com mailbox from 22.do's own pool.

    Different pool than Emailnator, so when one pool's base accounts are
    already registered on the target site the other one can still work.
    """
    result = await outlook_service.create_gmail()
    return {"service": "outlook", "email": result["email"], "shared_pool": True}


async def build_own(base: str) -> dict:
    """A brand-new address of the USER'S OWN Gmail (dot-alias trick).

    This is the only option that can never fail on "already used" and where
    the verification mail is guaranteed to arrive: it lands in the user's real
    Gmail inbox. Bolt.new's verification mails were "missing" precisely
    because the shared pools' base accounts were already registered, so no
    mail was ever sent.
    """
    if not gmail_alias.normalize_gmail(base or ""):
        raise RuntimeError(
            "apna Gmail set nahi hai ya valid nahi — `/gmail you@gmail.com` chalao"
        )
    aliases = gmail_alias.generate_aliases(base, count=25, include_plus=False)
    if not aliases:
        raise RuntimeError("is Gmail ke aliases nahi ban paye")
    return {
        "service": "own",
        "email": random.choice(aliases),
        "external_inbox": True,
    }


async def build_outlook() -> dict:
    """Real @outlook.com / @hotmail.com mailbox (never a '+' alias)."""
    result = await outlook_service.create_outlook(allow_alias=False)
    address = result["email"]
    return {
        "service": "outlook",
        "email": address,
        **{k: v for k, v in result.items() if k != "email"},
    }


# ── OPTION TABLE ───────────────────────────────────────────────────────────────
OPTIONS: dict[str, dict] = {
    "outlook": {
        "label": "Real Outlook / Hotmail",
        "note": "Genuine Microsoft mailbox — Lovable, GitHub aur Bolt.new teeno accept karte hain.",
        "build": lambda pw, allow=None: build_outlook(),
        "safe": True,
    },
    "own": {
        "label": "Apna khud ka Gmail (naya alias)",
        "note": (
            "Aapke asli Gmail ka naya address (dot-trick). Verification mail "
            "seedha aapke apne inbox me aata hai — 'already used' kabhi nahi aayega. "
            "Ek baar `/gmail you@gmail.com` set karo."
        ),
        "build": lambda pw, allow=None: build_own(""),
        "safe": True,
        "external_inbox": True,
    },
    "gmail_fresh": {
        "label": "Real Gmail (22.do pool)",
        "note": "Doosra real Gmail pool — jab Emailnator pool ka account already used ho.",
        "build": lambda pw, allow=None: build_gmail_fresh(),
        "safe": True,
        "shared_pool": True,
    },
    "gmail": {
        "label": "Real Gmail / GoogleMail",
        "note": (
            "Genuine @gmail.com / @googlemail.com inbox. Ye ek public shared "
            "Google pool hai — agar 'already used' aaye to /gmail se apna khud "
            "ka Gmail use karo."
        ),
        "build": lambda pw, allow=None: build_gmail(),
        "safe": True,
        "shared_pool": True,
    },
}

# Outlook first everywhere: the Emailnator Gmail pool is shared worldwide, so
# its base accounts are often already registered on the target site.
LOVABLE_ORDER = ["own", "outlook", "gmail", "gmail_fresh"]
GITHUB_ORDER = ["own", "outlook", "gmail", "gmail_fresh"]
# Bolt.new blocks every temp domain and both shared Gmail pools are already
# registered there, so the user's own Gmail alias leads for Bolt.
BOLT_ORDER = ["own", "outlook", "gmail_fresh", "gmail"]

ORDERS = {"lovable": LOVABLE_ORDER, "github": GITHUB_ORDER, "bolt": BOLT_ORDER}
TARGET_SITES = {"lovable": "lovable.dev", "github": "github.com", "bolt": "bolt.new"}


def order_for(target: str, own_base: str | None = None) -> list[str]:
    order = ORDERS.get(target, LOVABLE_ORDER)
    if not own_base:
        order = [k for k in order if k != "own"]
    return order


def site_for(target: str) -> str:
    return TARGET_SITES.get(target, "lovable.dev")


async def _build_once(option_key: str, password: str, target: str | None,
                      own_base: str | None = None) -> dict:
    if option_key == "own":
        fields = await build_own(own_base or "")
    else:
        fields = await OPTIONS[option_key]["build"](password)
    address = fields.get("email", "")
    ok, reason = is_signup_usable(address)
    if not ok:
        raise RuntimeError(f"{address or '?'}: {reason}")
    domain = address.partition("@")[2].lower()
    if not await has_mx(domain):
        raise RuntimeError(f"{domain}: MX record nahi mila")
    if await target_domain_ok(target or "", domain) is False:
        raise RuntimeError(f"{domain}: {site_for(target or '')} is domain ko block karta hai")
    return fields


async def build(option_key: str, password: str, target: str | None = None,
                own_base: str | None = None) -> dict:
    """Build one verified mailbox on a real provider domain.

    For Bolt.new the exact address is probed first, so the old
    "email has already been used" dead-end turns into a retry with a fresh
    address instead of a signup that never sends a verification mail.
    """
    last_err = ""
    for _attempt in range(2):
        fields = await _build_once(option_key, password, target, own_base)
        address = fields.get("email", "")
        if await target_address_ok(target or "", address) is False:
            mark_address_taken(address)
            last_err = (
                f"{site_for(target or '')} par is address ka account pehle se hai "
                f"(shared pool burn ho chuka hai). Apna khud ka Gmail set karo: "
                f"`/gmail you@gmail.com`, phir dobara try karo."
            )
            logger.warning("address already registered, retrying: %s", address)
            continue
        return fields
    raise RuntimeError(last_err or "koi usable address nahi mila")


async def build_best(target: str, password: str,
                     own_base: str | None = None,
                     option_timeout: float = 15.0) -> tuple[dict | None, list[str]]:
    """Try every option for `target`, best one first.

    Each option gets at most `option_timeout` seconds so one slow upstream
    (22.do under Cloudflare challenge, Emailnator rate-limited) cannot eat
    the entire MAILBOX_TIMEOUT budget and cause a 60s+ timeout error.
    """
    errors: list[str] = []
    for key in order_for(target, own_base):
        try:
            fields = await asyncio.wait_for(
                build(key, password, target, own_base),
                timeout=option_timeout,
            )
            fields["option"] = key
            return fields, errors
        except asyncio.TimeoutError:
            label = OPTIONS.get(key, {}).get("label", key)
            errors.append(f"{label}: timed out after {int(option_timeout)}s")
            logger.warning("option %s timed out for %s after %ss", key, target, option_timeout)
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            label = OPTIONS.get(key, {}).get("label", key)
            errors.append(f"{label}: {e}")
            logger.warning("option %s failed for %s: %s", key, target, e)
    return None, errors
