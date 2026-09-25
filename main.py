import os
import re
import asyncio
import logging
import signal
import string
import secrets
import threading
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
# Every inline button goes through one factory (same design as Melody_music):
# premium custom-emoji icon + colour style where the library supports them,
# plain button everywhere else. Shadowing the name keeps all call sites intact.
from premium_buttons import ikb as InlineKeyboardButton  # noqa: F811
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import emailnator_service
import outlook_service
import mail_options
import gmail_alias
import outlook_alias

load_dotenv()

# ── CONFIG ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.getenv("OWNER_ID", "8098146730"))
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@TheY_CaIl_mE_OG")
SUPPORT_CHANNEL = os.getenv("SUPPORT_CHANNEL", "https://t.me/ApexAssociation")
# Only real provider mailboxes are supported (see mail_options.ALLOWED_DOMAINS):
#   @gmail.com / @googlemail.com  and  @outlook.com / @hotmail.com

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("TempMailBot")

# ── SHUTDOWN / TIMEOUT SAFETY ──────────────────────────────────────────────────
# ROOT CAUSE of `heroku[worker.1]: Error R12 (Exit timeout)`:
# on SIGTERM python-telegram-bot waits for every in-flight handler and job to
# finish before the process can exit.  The mailbox builders retry for minutes
# (multiple rounds x domains x sleeps), so a single /new in flight kept the
# worker alive past Heroku's 30s grace period -> SIGKILL (status 137) while the
# replacement dyno was already polling (double getUpdates).
# Two guards below: hard per-operation deadlines, and a shutdown watchdog that
# force-exits well before Heroku's SIGKILL.
MAILBOX_TIMEOUT = float(os.getenv("MAILBOX_TIMEOUT", "120"))
INBOX_TIMEOUT = float(os.getenv("INBOX_TIMEOUT", "30"))
# Reduced from 12s to 4s: the old process actually stops polling ~3s after
# SIGTERM, so 4s is enough. 12s kept the old process alive long enough for
# the new process to start polling -> 409 Conflict crash.
SHUTDOWN_GRACE = float(os.getenv("SHUTDOWN_GRACE", "4"))
# Per-option sub-timeout so one slow upstream can't eat the whole budget.
OPTION_TIMEOUT = float(os.getenv("OPTION_TIMEOUT", "15"))
# Seconds to wait before starting to poll. Gives the old dyno time to release
# the getUpdates lock after SIGTERM, preventing 409 Conflict.
STARTUP_DELAY = float(os.getenv("STARTUP_DELAY", "5"))


async def _deadline(coro, seconds: float, label: str = "operation"):
    """Run `coro` with a hard timeout so no handler can outlive shutdown."""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"{label} timed out after {int(seconds)}s") from e


def _install_shutdown_watchdog(app) -> None:
    """Stop polling on SIGTERM/SIGINT and force-exit if cleanup drags on."""
    fired = threading.Event()

    def _handler(signum, _frame):
        if fired.is_set():
            os._exit(0)
        fired.set()
        logger.info("Signal %s received - shutting down (grace %ss)", signum, SHUTDOWN_GRACE)
        timer = threading.Timer(SHUTDOWN_GRACE, lambda: os._exit(0))
        timer.daemon = True
        timer.start()
        try:
            app.stop_running()
        except Exception:  # pragma: no cover - best effort
            os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGABRT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


async def _post_shutdown(_app) -> None:
    try:
        await asyncio.wait_for(emailnator_service.aclose(), timeout=5)
    except Exception:
        pass

# ── SESSION STORE ──────────────────────────────────────────────────────────────
SESSIONS: dict[int, dict] = {}
# The user's OWN Gmail (set via /gmail). Aliases of it are the most reliable
# signup mailbox: the verification mail always lands in their real inbox.
OWN_BASES: dict[int, str] = {}


def generate_strong_password(length: int = 18) -> str:
    upper = [secrets.choice(string.ascii_uppercase) for _ in range(3)]
    lower = [secrets.choice(string.ascii_lowercase) for _ in range(3)]
    digits = [secrets.choice(string.digits) for _ in range(3)]
    special = [secrets.choice("@#$!%&*?") for _ in range(3)]
    rest = [secrets.choice(string.ascii_letters + string.digits + "@#$!%&*?")
            for _ in range(length - 12)]
    pool = upper + lower + digits + special + rest
    secrets.SystemRandom().shuffle(pool)
    return "".join(pool)


# ── KEYBOARD BUILDERS ──────────────────────────────────────────────────────────
def _owner_row() -> list:
    return [
        InlineKeyboardButton("👑 Owner", url=f"https://t.me/{OWNER_USERNAME.lstrip('@')}"),
        InlineKeyboardButton("📢 Support", url=SUPPORT_CHANNEL),
    ]


def email_created_kb(email: str, acc_password: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy Email", switch_inline_query=email)],
        [InlineKeyboardButton("🔑 Copy Password", switch_inline_query=acc_password)],
        [
            InlineKeyboardButton("📥 Inbox", callback_data="inbox"),
            InlineKeyboardButton("🔄 New Email", callback_data="new_email"),
        ],
        [
            InlineKeyboardButton("👁 Watch OTP", callback_data="watch"),
            InlineKeyboardButton("🗑 Delete", callback_data="delete_account"),
        ],
        _owner_row(),
    ])


def main_menu_kb(email: str | None = None, acc_password: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if email:
        rows.append([InlineKeyboardButton("📋 Copy Email", switch_inline_query=email)])
    if acc_password:
        rows.append([InlineKeyboardButton("🔑 Copy Password", switch_inline_query=acc_password)])
        rows.append([
            InlineKeyboardButton("📥 Inbox", callback_data="inbox"),
            InlineKeyboardButton("🔄 New Email", callback_data="new_email"),
        ])
        rows.append([
            InlineKeyboardButton("👁 Watch OTP", callback_data="watch"),
            InlineKeyboardButton("🗑 Delete", callback_data="delete_account"),
        ])
    else:
        rows.append([InlineKeyboardButton("📧 Gmail / GoogleMail", callback_data="new_email")])
        rows.append([InlineKeyboardButton("🔵 Outlook Mail", callback_data="new_outlook")])
        rows.append([
            InlineKeyboardButton("💜 Lovable Mail", callback_data="new_lovable"),
            InlineKeyboardButton("🐙 GitHub Mail", callback_data="new_github"),
        ])
        rows.append([InlineKeyboardButton("⚡ Bolt.new Mail", callback_data="new_bolt")])
        rows.append([
            InlineKeyboardButton("💜 Lovable options", callback_data="opts:lovable"),
            InlineKeyboardButton("🐙 GitHub options", callback_data="opts:github"),
        ])
        rows.append([InlineKeyboardButton("⚡ Bolt.new options", callback_data="opts:bolt")])
        rows.append([InlineKeyboardButton("📮 Custom Gmail (multi)", callback_data="gmail_menu")])
        rows.append([InlineKeyboardButton("📬 Custom Outlook (multi)", callback_data="outlook_menu")])
        rows.append(_owner_row())
    return InlineKeyboardMarkup(rows)


def inbox_keyboard(messages: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for msg in messages[:8]:
        subject = (msg.get("subject") or "(No Subject)")[:38]
        rows.append([InlineKeyboardButton(
            f"📧 {subject}", callback_data=f"read:{msg['id']}",
        )])
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="inbox"),
        InlineKeyboardButton("◀️ Back", callback_data="main_menu"),
    ])
    rows.append(_owner_row())
    return InlineKeyboardMarkup(rows)


def _safe_url(u: str) -> str:
    if u.startswith("http://") or u.startswith("https://"):
        return u
    return "https://" + u


def message_action_kb(msg_id: str, otp: str | None = None, links: list[str] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if otp:
        rows.append([
            InlineKeyboardButton("🔑 Copy OTP", switch_inline_query_current_chat=otp),
            InlineKeyboardButton("👁 Show OTP", callback_data=f"show_otp:{otp}"),
        ])
    if links:
        for i, link in enumerate(links):
            if is_verify_link(link):
                label = "✅ Verify / Confirm Email" if i == 0 else "✅ Verification Link"
            else:
                label = link if len(link) <= 40 else link[:37] + "..."
                label = f"🔗 {label}"
            rows.append([InlineKeyboardButton(label, url=_safe_url(link))])
    rows.append([
        InlineKeyboardButton("🗑 Delete Mail", callback_data=f"del_msg:{msg_id}"),
        InlineKeyboardButton("◀️ Back to Inbox", callback_data="inbox"),
    ])
    rows.append(_owner_row())
    return InlineKeyboardMarkup(rows)


def watch_kb(email: str, acc_password: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy Email", switch_inline_query=email)],
        [InlineKeyboardButton("🔑 Copy Password", switch_inline_query=acc_password)],
        [
            InlineKeyboardButton("📥 Inbox", callback_data="inbox"),
            InlineKeyboardButton("🛑 Stop Watch", callback_data="stop_watch"),
        ],
        _owner_row(),
    ])


def generic_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        _owner_row(),
    ])


# ── DISPOSABLE-DOMAIN BLOCKLIST (why lovable.dev logins failed) ────────────────
BLOCKLIST_URLS = [
    "https://raw.githubusercontent.com/disposable-email-domains/"
    "disposable-email-domains/main/disposable_email_blocklist.conf",
]
_BLOCKLIST: set[str] = set()
_BLOCKLIST_AT: float = 0.0
_BLOCKLIST_TTL = 6 * 3600


# ── SERVICE-AGNOSTIC MESSAGE ACCESS ────────────────────────────────────────────
async def service_get_messages(session: dict) -> list[dict]:
    if session["service"] == "outlook":
        raw = await outlook_service.get_messages(session["email"])
        items = [
            {
                "id": m.get("messageId"),
                "subject": m.get("subject") or "(No Subject)",
                "from": m.get("from") or "unknown",
                "time": m.get("time") or "",
            }
            for m in raw
        ]
        meta = session.setdefault("meta", {})
        for it in items:
            if it["id"]:
                meta[it["id"]] = it
        return items
    if session["service"] == "emailnator":
        # Poll EVERY form of the address (@gmail.com AND @googlemail.com).
        # It is the same Google inbox, but the provider keys its message list
        # per address form, so a Bolt.new / Lovable verification mail sent to
        # the other form used to look like it never arrived.
        raw: list[dict] = []
        seen_ids: set[str] = set()
        variants = mail_options.address_variants(session["email"])
        errors: list[str] = []
        for variant in variants:
            try:
                part = await emailnator_service.get_messages(variant)
            except Exception as e:
                errors.append(f"{variant}: {e}")
                continue
            for m in part:
                mid = m.get("messageID")
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    m["_variant"] = variant
                    raw.append(m)
        if errors and not raw:
            logger.warning("emailnator inbox: %s", "; ".join(errors))
        items = [
            {
                "id": m.get("messageID"),
                "subject": m.get("subject") or "(No Subject)",
                "from": m.get("from") or "unknown",
                "time": m.get("time") or "",
                "variant": m.get("_variant") or session["email"],
            }
            for m in raw
            # Emailnator injects a permanent ad row; it is never a real mail.
            if m.get("messageID") and m.get("messageID") != "ADSVPN"
        ]
        # Emailnator's message endpoint returns only the body, so keep the
        # list metadata around for the read view (otherwise every GitHub OTP
        # mail showed "(No Subject)" / "unknown" sender).
        meta = session.setdefault("meta", {})
        for it in items:
            meta[it["id"]] = it
        return items
    return []


async def service_get_message(session: dict, msg_id: str) -> dict | None:
    if session["service"] == "outlook":
        # /content/{id} is only a shell page; outlook_service.read_message
        # follows the iframe so the real mail body (and OTP) actually shows up.
        data = await outlook_service.read_message(session["email"], msg_id)
        meta = (session.get("meta") or {}).get(msg_id, {})
        body_html = data.get("html") or ""
        return {
            "subject": data.get("subject") or meta.get("subject") or "(No Subject)",
            "from": {"address": data.get("from") or meta.get("from") or "unknown"},
            "text": clean_html(body_html),
            "html": body_html,
        }
    if session["service"] == "emailnator":
        meta = (session.get("meta") or {}).get(msg_id, {})
        # Read the mail through the SAME address form it was listed under.
        body_html = await emailnator_service.get_message(
            meta.get("variant") or session["email"], msg_id
        )
        body_text = clean_html(body_html)
        return {
            "subject": meta.get("subject") or "(No Subject)",
            "from": {"address": meta.get("from") or "unknown"},
            "text": body_text,
            "html": body_html,
        }
    return None


async def service_delete_message(session: dict, msg_id: str) -> bool:
    if session["service"] == "outlook":
        return await outlook_service.delete_message(session["email"], msg_id)
    return False


# ── UTILS ──────────────────────────────────────────────────────────────────────
def clean_html(html_text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_text or "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_outlook_subject(page_html: str) -> str:
    m = re.search(r'<title>\s*([^<]+?)\s*</title>', page_html or "", re.IGNORECASE)
    if m and m.group(1).strip() and m.group(1).strip() != "Not Found":
        return m.group(1).strip()
    m = re.search(r'class="[^"]*subject[^"]*"[^>]*>\s*([^<]+)', page_html or "", re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return "(No Subject)"


def _parse_outlook_from(page_html: str) -> str:
    m = re.search(r'class="[^"]*from[^"]*"[^>]*>\s*([^<]+)', page_html or "", re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return "unknown"


def _parse_outlook_body(page_html: str) -> str:
    html_str = page_html or ""
    for sel in [
        r'<div[^>]*class="[^"]*email-content[^"]*"[^>]*>(.*?)\s*</div>',
        r'<div[^>]*class="[^"]*message-body[^"]*"[^>]*>(.*?)\s*</div>',
        r'<div[^>]*class="[^"]*content-body[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]*id="content"[^>]*>(.*?)</div>',
    ]:
        m = re.search(sel, html_str, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    m = re.search(r'<body[^>]*>(.*?)</body>', html_str, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return html_str


def extract_otp(text: str) -> str | None:
    labelled = re.search(
        r"(?:code|otp|verification|pin)[^\d]{0,12}(\d{4,8})",
        text or "",
        re.IGNORECASE,
    )
    if labelled:
        return labelled.group(1)
    matches = re.findall(r"\b\d{4,8}\b", text or "")
    return matches[0] if matches else None


_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


_JUNK_HINTS = (
    "unsubscribe", "opt-out", "optout", "privacy", "/terms", "twitter.com",
    "x.com/", "facebook.com", "linkedin.com", "instagram.com", "youtube.com",
    "emailnator", "temp-mail", "tempmail", "w3.org", "gravatar",
    "list-manage.com/track", "/track/open", "googleusercontent.com",
)
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".ico")
_VERIFY_HINTS = (
    "verify", "verification", "confirm", "activate", "activation", "magic",
    "magiclink", "signup", "sign-up", "sign_up", "auth/v1/verify", "validate",
    "confirmation", "token=", "confirmation_token", "invite", "set-password",
    "reset", "login-link", "otp",
)


def _clean_link(raw: str) -> str | None:
    link = (raw or "").strip().replace("&amp;", "&")
    if link.startswith("//"):
        link = "https:" + link
    if not (link.startswith("http://") or link.startswith("https://")):
        return None
    link = link.rstrip(".,);]'\"")
    low = link.lower()
    if any(low.split("?")[0].endswith(e) for e in _IMG_EXT):
        return None
    if any(h in low for h in _JUNK_HINTS):
        return None
    return link


def is_verify_link(link: str) -> bool:
    return any(h in (link or "").lower() for h in _VERIFY_HINTS)


def extract_links(text: str, html_body: str = "", max_links: int = 6) -> list[str]:
    """Return usable links, verification/magic links FIRST.

    Signup mails put logo, help-centre and social links before the real
    confirm button, so plain document order used to bury (or push out) the
    only link the user actually needs.
    """
    found: list[str] = []
    for raw in _HREF_RE.findall(html_body or "") + _URL_RE.findall(text or ""):
        link = _clean_link(raw)
        if link and link not in found:
            found.append(link)
    verify = [l for l in found if is_verify_link(l)]
    other = [l for l in found if l not in verify]
    return (verify + other)[:max_links]


def link_notice(links: list[str]) -> str:
    """One honest status line about the verification link."""
    if not links:
        return ""
    if is_verify_link(links[0]):
        return "\n✅ *Verification link mil gaya* — neeche wala button dabao."
    return "\n🔗 *Link mila* (verification button nahi mila — mail ka pura text neeche dekho)."


def _md_escape(s: str) -> str:
    return re.sub(r"([_*`\[])", r"\\\1", s or "")


# ── CORE: CREATE EMAIL ─────────────────────────────────────────────────────────
async def _create_email(
    uid: int,
    username: str | None = None,
    outlook_only: bool = False,
    provider: str | None = None,
) -> tuple[bool, str, str]:
    """Create a mailbox on a real provider domain only.

    Gmail / GoogleMail by default, Outlook / Hotmail with `outlook_only`.
    All temp-mail providers were removed — every target site blocks them.
    """
    if outlook_only:
        return await _create_outlook(uid)
    return await _create_gmail(uid)


async def _create_gmail(uid: int) -> tuple[bool, str, str]:
    try:
        fields = await _deadline(mail_options.build_gmail(), MAILBOX_TIMEOUT, "Gmail create")
    except Exception as e:
        logger.error("gmail create: %s", e)
        return False, (
            "❌ Gmail service abhi respond nahi kar raha (Emailnator temporary block).\n"
            "Thodi der baad try karo, ya /outlook (real Outlook) use karo.\n"
            "Apna khud ka Gmail chahiye to /gmail `you@gmail.com`."
        ), ""
    acc_password = generate_strong_password()
    SESSIONS[uid] = {
        "acc_password": acc_password,
        "token": None,
        "api_base": None,
        "seen_ids": set(),
        **fields,
    }
    return True, fields["email"], acc_password


_FIRST_NAMES = [
    "daniel", "marcus", "olivia", "ethan", "sophia", "liam", "nathan", "chloe",
    "julia", "aaron", "hannah", "victor", "elena", "peter", "mia", "adrian",
    "laura", "simon", "clara", "kevin", "nora", "felix", "irene", "oscar",
]
_LAST_NAMES = [
    "harper", "bennett", "morris", "walsh", "keller", "hayes", "porter",
    "grant", "wallace", "fisher", "reyes", "novak", "lambert", "duncan",
    "sawyer", "mercer", "holt", "barrett", "ellis", "kramer", "shaw",
]


async def _create_outlook(uid: int) -> tuple[bool, str, str]:
    try:
        result = await _deadline(outlook_service.create_outlook(), MAILBOX_TIMEOUT, "Outlook create")
        address = result["email"]
    except Exception as e:
        logger.error("outlook create: %s", e)
        return False, (
            "❌ Outlook service abhi respond nahi kar raha (22.do temporary block).\n"
            "Thodi der baad try karo, ya /new (Gmail) use karo.\n"
            "Apna khud ka Outlook chahiye to /outlookmail `you@outlook.com`."
        ), ""
    acc_password = generate_strong_password()
    SESSIONS[uid] = {
        "service": "outlook",
        "email": address,
        "acc_password": acc_password,
        "token": None,
        "seen_ids": set(),
        "api_base": None,
    }
    return True, address, acc_password


async def _create_outlook_variant(uid: int, variant: int) -> tuple[bool, str, str]:
    """Create one clean Outlook mailbox on a dedicated source route."""
    source_domain = outlook_service.OUTLOOK_DOMAINS[variant - 1]
    try:
        result = await _deadline(
            outlook_service.create_outlook(
                source_domain=source_domain,
                clean_only=True,
            ),
            MAILBOX_TIMEOUT,
            f"Outlook {variant} create",
        )
        address = result["email"]
    except Exception as e:
        logger.error("outlook%s create: %s", variant, e)
        return False, (
            f"❌ Outlook {variant} abhi available nahi hai. "
            "Thodi der baad dobara try karo."
        ), ""

    acc_password = generate_strong_password()
    SESSIONS[uid] = {
        "service": "outlook",
        "email": address,
        "acc_password": acc_password,
        "token": None,
        "seen_ids": set(),
        "api_base": None,
        "source_domain": source_domain,
    }
    return True, address, acc_password


def _email_password_text(email: str, acc_password: str) -> str:
    return (
        f"✅ *Temp Email Ready!*\n\n"
        f"📧 *Email:*\n`{email}`\n\n"
        f"🔑 *Password:*\n`{acc_password}`\n\n"
        f"_Tap email or password above to copy._\n"
        f"_Works on Replit, Discord, GitHub & more._"
    )


# ── COMMAND HANDLERS ───────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    session = SESSIONS.get(uid)
    name = update.effective_user.first_name or "User"
    if session:
        creds = (
            f"📧 *Email:*\n`{session['email']}`\n\n"
            f"🔑 *Password:*\n`{session['acc_password']}`"
        )
        kb = main_menu_kb(session["email"], session["acc_password"])
    else:
        creds = "❌ No active email — tap *Generate Email* below."
        kb = main_menu_kb()
    text = (
        f"👋 Welcome, *{name}*!\n\n"
        f"🔧 *Replit Temp Mail Bot*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{creds}\n\n"
        f"📌 *Commands:*\n"
        f"• /new — Real Gmail / GoogleMail inbox\n"
        f"• /outlook — Real @outlook.com / @hotmail.com inbox\n"
        f"• /Outlook1 … /Outlook5 — 5 clean Outlook routes (no dot/+)\n"
        f"• /lovable — Verified email for lovable.dev\n"
        f"• /github — Verified email for github.com\n"
        f"• /bolt — Verified email for bolt.new\n"
        f"• /lovable1-2, /github1-2, /bolt1-2 — Outlook / Gmail option chuno\n"
        f"• /options — Options + kis ke liye best\n"
        f"• /check `domain.com` — Domain verify karo\n"
        f"• /gmail `you@gmail.com` — Apne Gmail ke multiple addresses\n"
        f"• /outlookmail `you@outlook.com` — Apne Outlook ke multiple addresses\n"
        f"• /inbox — Check messages\n"
        f"• /watch — Auto OTP forward\n"
        f"• /delete — Delete email\n"
        f"• /help — Full guide\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = await update.message.reply_text("⏳ Creating email + generating password...")
    ok, email_or_err, acc_pw = await _create_email(uid)
    if ok:
        await msg.edit_text(
            _email_password_text(email_or_err, acc_pw),
            parse_mode="Markdown",
            reply_markup=email_created_kb(email_or_err, acc_pw),
        )
    else:
        await msg.edit_text(email_or_err, parse_mode="Markdown", reply_markup=generic_kb())


async def cmd_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    session = SESSIONS.get(uid)
    if not session:
        await update.message.reply_text(
            "❌ No active email. Use /new first.", reply_markup=generic_kb()
        )
        return
    msg = await update.message.reply_text("⏳ Checking inbox...")
    await _render_inbox(msg, session, edit=True)


async def _render_inbox(msg, session: dict, edit: bool = False):
    if session.get("service") == "own":
        text = (
            f"📬 *Apna Gmail alias*\n\n"
            f"📧 `{session['email']}`\n\n"
            f"_Ye aapke khud ke Gmail ka alias hai — verification mail aapke asli "
            f"Gmail inbox me aata hai, isliye bot yahan mail nahi padh sakta._"
        )
        if edit:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=generic_kb())
        else:
            await msg.reply_text(text, parse_mode="Markdown", reply_markup=generic_kb())
        return
    try:
        messages = await _deadline(service_get_messages(session), INBOX_TIMEOUT, "inbox poll")
    except Exception as e:
        logger.warning("watch job inbox: %s", e)
        return
    if session["service"] == "emailnator":
        messages = [m for m in messages if m.get("id") != "ADSVPN"]
    if not messages:
        text = (
            f"📭 *Inbox Empty*\n\n"
            f"📧 `{session['email']}`\n\n"
            f"_No messages yet. Use /watch for auto-check._"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="inbox"),
                InlineKeyboardButton("◀️ Back", callback_data="main_menu"),
            ],
            _owner_row(),
        ])
    else:
        text = (
            f"📬 *Inbox — {len(messages)} message(s)*\n\n"
            f"📧 `{session['email']}`\n\n"
            f"_Tap a message to read:_"
        )
        kb = inbox_keyboard(messages)
    if edit:
        try:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            pass
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    session = SESSIONS.get(uid)
    if not session:
        await update.message.reply_text(
            "❌ No active email. Use /new first.", reply_markup=generic_kb()
        )
        return
    _stop_watch(ctx, uid)
    ctx.job_queue.run_repeating(
        _watch_job,
        interval=15,
        first=3,
        name=f"watch_{uid}",
        data={"uid": uid, "chat_id": update.effective_chat.id},
        job_kwargs={"misfire_grace_time": 30},
    )
    await update.message.reply_text(
        f"👁 *Auto-Watch Active!*\n\n"
        f"📧 *Email:*\n`{session['email']}`\n\n"
        f"🔑 *Password:*\n`{session['acc_password']}`\n\n"
        f"⚡ Checking every *15 seconds* for new emails.\n"
        f"OTP will be sent here instantly!\n\n"
        f"➡️ Now open Replit, enter this email & click signup.\n"
        f"OTP will arrive here automatically.\n\n"
        f"Use /stopwatch to stop.",
        parse_mode="Markdown",
        reply_markup=watch_kb(session["email"], session["acc_password"]),
    )


def _stop_watch(ctx: ContextTypes.DEFAULT_TYPE, uid: int) -> bool:
    jobs = ctx.job_queue.get_jobs_by_name(f"watch_{uid}")
    for j in jobs:
        j.schedule_removal()
    return bool(jobs)


async def cmd_stopwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if _stop_watch(ctx, uid):
        await update.message.reply_text("🛑 Auto-watch stopped.", reply_markup=generic_kb())
    else:
        await update.message.reply_text("❌ No active watch running.", reply_markup=generic_kb())


async def _watch_job(ctx: ContextTypes.DEFAULT_TYPE):
    data = ctx.job.data
    uid = data["uid"]
    chat_id = data["chat_id"]
    session = SESSIONS.get(uid)
    if not session:
        ctx.job.schedule_removal()
        return
    messages = await service_get_messages(session)
    if session["service"] == "emailnator":
        messages = [m for m in messages if m.get("id") != "ADSVPN"]
    for msg in messages:
        mid = msg["id"]
        if mid in session["seen_ids"]:
            continue
        session["seen_ids"].add(mid)
        full = await service_get_message(session, mid)
        if not full:
            continue
        subject = full.get("subject") or "(No Subject)"
        sender = full.get("from", {}).get("address", "unknown")
        body_text = full.get("text") or ""
        body_html = full.get("html") or ""
        body = body_text or clean_html(body_html)
        body_short = body[:800]
        otp = extract_otp(body_short)
        links = extract_links(body_text, body_html)
        otp_line = f"\n\n🔑 *OTP Code: `{otp}`* ← Copy this!" if otp else ""
        link_line = link_notice(links)
        text = (
            f"📨 *New Email!*{otp_line}{link_line}\n\n"
            f"*From:* `{_md_escape(sender)}`\n"
            f"*Subject:* {_md_escape(subject)}\n\n"
            f"```\n{body_short}\n```"
        )
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=text[:4000],
            parse_mode="Markdown",
            reply_markup=message_action_kb(mid, otp, links),
        )


async def cmd_outlook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = await update.message.reply_text("⏳ Creating real @outlook.com email...")
    ok, email_or_err, acc_pw = await _create_email(uid, outlook_only=True)
    if ok:
        await msg.edit_text(
            _email_password_text(email_or_err, acc_pw),
            parse_mode="Markdown",
            reply_markup=email_created_kb(email_or_err, acc_pw),
        )
    else:
        await msg.edit_text(email_or_err, parse_mode="Markdown", reply_markup=generic_kb())



# ── TARGET-AWARE CREATION (Lovable / GitHub) ──────────────────────────────────
async def _create_for_target(uid: int, target: str, option: str | None = None) -> tuple[bool, str, str]:
    """Build a mailbox verified for `target` ("lovable" | "github" | "bolt")."""
    acc_password = generate_strong_password()
    own_base = OWN_BASES.get(uid)
    if option:
        try:
            fields = await _deadline(
                mail_options.build(option, acc_password, target, own_base),
                MAILBOX_TIMEOUT, "mailbox build")
            fields["option"] = option
        except Exception as e:
            label = mail_options.OPTIONS[option]["label"]
            return False, f"❌ *{label}* abhi fail hua:\n`{e}`\n\nDoosra option try karo — /{target}", ""
    else:
        try:
            fields, errors = await _deadline(
                mail_options.build_best(target, acc_password, own_base,
                                        option_timeout=OPTION_TIMEOUT),
                MAILBOX_TIMEOUT, "mailbox build")
        except Exception as e:
            logger.error("build_best %s: %s", target, e)
            fields, errors = None, [str(e)]
        if not fields:
            detail = "\n".join(f"• {e}" for e in errors[:5])
            hint = ""
            if not own_base:
                hint = (
                    "\n\n💡 *Bolt.new har temp-mail domain block karta hai* aur shared "
                    "Gmail pool ke accounts wahan pehle se registered hain — isi liye "
                    "verification mail nahi aata tha.\n"
                    "Sabse pakka tareeka: `/outlookmail you@outlook.com` ya "
                    "`/gmail you@gmail.com` se apne *khud ke* account ka naya address "
                    "banao, phir usi se signup karo."
                )
            return False, f"❌ Abhi koi verified mailbox nahi mila.\n{detail}{hint}", ""
    SESSIONS[uid] = {
        "acc_password": acc_password,
        "seen_ids": set(),
        "token": None,
        "api_base": None,
        **fields,
    }
    return True, fields["email"], acc_password


def _target_note(target: str, option_key: str, warn: bool = False) -> str:
    opt = mail_options.OPTIONS[option_key]
    site = mail_options.site_for(target)
    flag = "✅ signup-safe" if opt["safe"] and not warn else "⚠️ fallback (may get flagged)"
    if opt.get("external_inbox"):
        warn_line = (
            "\n📬 *Note:* Ye aapke *apne Gmail* ka alias hai — verification mail "
            "aapke asli Gmail inbox me aayega (bot yahan inbox nahi dikhata). "
            "Address har baar naya hota hai, isliye 'already used' error nahi aayega."
        )
    elif opt.get("shared_pool"):
        warn_line = (
            "\n⚠️ *Dhyan do:* Ye Gmail ek public shared pool ka account hai — "
            "dusre log bhi isme login kar sakte hain aur unke mail bhi dikh sakte hain. "
            "Permanent account ke liye /outlook ya `/gmail you@gmail.com` use karo."
        )
    else:
        warn_line = ""
    return (
        f"\n\n🎯 *Target:* {site}\n"
        f"🧩 *Option:* {opt['label']} ({flag})\n"
        f"_{opt['note']}_"
        f"{warn_line}\n"
        f"_Sirf real provider domains (Gmail / GoogleMail / Outlook) diye jate hain — "
        f"MX aur target-site acceptance dono live check hote hain._"
    )


def options_kb(target: str, own_base: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    for i, key in enumerate(mail_options.order_for(target, own_base), start=1):
        opt = mail_options.OPTIONS[key]
        rows.append([InlineKeyboardButton(
            f"{i}. {opt['label']}", callback_data=f"opt:{target}:{key}",
        )])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data="main_menu")])
    rows.append(_owner_row())
    return InlineKeyboardMarkup(rows)


def options_text(target: str, own_base: str | None = None) -> str:
    site = mail_options.site_for(target)
    lines = [f"🧩 *Working Options — {site}*\n"]
    for i, key in enumerate(mail_options.order_for(target, own_base), start=1):
        opt = mail_options.OPTIONS[key]
        mark = "✅" if opt["safe"] else "⚠️"
        lines.append(f"{mark} *{i}. {opt['label']}*\n_{opt['note']}_\n`/{target}{i}`\n")
    lines.append("_Sirf real providers: Gmail / GoogleMail / Outlook. Har address dene se pehle MX + inbox + target-site acceptance live verify hota hai._")
    return "\n".join(lines)


async def _reply_created(msg, ok: bool, email_or_err: str, acc_pw: str, target: str, uid: int, edit_obj=None):
    sender = msg.edit_text if edit_obj is None else edit_obj
    if ok:
        option_key = SESSIONS[uid].get("option", "")
        warn = bool(SESSIONS[uid].get("warn"))
        note = _target_note(target, option_key, warn=warn) if option_key else ""
        await sender(
            _email_password_text(email_or_err, acc_pw) + note,
            parse_mode="Markdown",
            reply_markup=email_created_kb(email_or_err, acc_pw),
        )
    else:
        await sender(email_or_err, parse_mode="Markdown", reply_markup=generic_kb())


def _make_target_cmd(target: str, option: str | None = None):
    async def _cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        label = mail_options.OPTIONS[option]["label"] if option else "best verified"
        msg = await update.message.reply_text(f"⏳ {target.title()} ke liye {label} mailbox bana raha hoon...")
        ok, email_or_err, acc_pw = await _create_for_target(uid, target, option)
        await _reply_created(msg, ok, email_or_err, acc_pw, target, uid)
    return _cmd


cmd_lovable = _make_target_cmd("lovable")
cmd_github = _make_target_cmd("github")
cmd_bolt = _make_target_cmd("bolt")
TARGET_CMDS: dict[str, callable] = {}
for _t in ("lovable", "github", "bolt"):
    for _i, _key in enumerate(mail_options.ORDERS[_t], start=1):
        TARGET_CMDS[f"{_t}{_i}"] = _make_target_cmd(_t, _key)


def _make_outlook_variant_cmd(variant: int):
    async def _cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        msg = await update.message.reply_text(
            f"⏳ Clean Outlook {variant} mailbox bana raha hoon..."
        )
        ok, email_or_err, acc_pw = await _create_outlook_variant(uid, variant)
        if ok:
            await msg.edit_text(
                _email_password_text(email_or_err, acc_pw)
                + f"\n\n🧭 Outlook route: `{variant}/5`"
                + "\n✅ Address me dot (.) ya plus (+) nahi hai.",
                parse_mode="Markdown",
                reply_markup=email_created_kb(email_or_err, acc_pw),
            )
        else:
            await msg.edit_text(
                email_or_err,
                parse_mode="Markdown",
                reply_markup=generic_kb(),
            )

    return _cmd


OUTLOOK_CMDS: dict[str, callable] = {
    f"outlook{i}": _make_outlook_variant_cmd(i)
    for i in range(1, 6)
}


async def cmd_options(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    arg = (ctx.args[0].lower() if ctx.args else "lovable")
    if arg.startswith("g"):
        target = "github"
    elif arg.startswith("b"):
        target = "bolt"
    else:
        target = "lovable"
    own_base = OWN_BASES.get(update.effective_user.id)
    await update.message.reply_text(
        options_text(target, own_base), parse_mode="Markdown",
        reply_markup=options_kb(target, own_base),
    )


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/check mailto.plus`\nDomain ya poora email dono chalega.",
            parse_mode="Markdown", reply_markup=generic_kb(),
        )
        return
    raw = ctx.args[0].strip().lower()
    domain = raw.split("@")[-1]
    msg = await update.message.reply_text(f"⏳ `{domain}` verify kar raha hoon...", parse_mode="Markdown")
    ok, reason = await mail_options.verify_domain(domain)
    tip = "" if ok else "\n\n👉 Safe address ke liye /lovable ya /github use karo."
    await msg.edit_text(reason + tip, parse_mode="Markdown", reply_markup=generic_kb())




async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in SESSIONS:
        _stop_watch(ctx, uid)
        email = SESSIONS.pop(uid)["email"]
        await update.message.reply_text(
            f"🗑 Email `{email}` deleted.\nUse /new to create a fresh one.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
    else:
        await update.message.reply_text(
            "❌ No active email to delete.", reply_markup=generic_kb()
        )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Temp Mail Bot — Help*\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🟢 *How to use for Replit:*\n"
        "1️⃣ /new — Generate email + password\n"
        "2️⃣ /watch — Start auto OTP mode\n"
        "3️⃣ Go to replit.com → Sign Up\n"
        "4️⃣ Enter the email & password\n"
        "5️⃣ OTP arrives here automatically ⚡\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📌 *Commands:*\n"
        "• /new — Real Gmail / GoogleMail inbox + strong password\n"
        "• /outlook — Real @outlook.com / @hotmail.com inbox\n"
        "• /Outlook1 … /Outlook5 — 5 clean Outlook routes (no dot/+)\n"
        "• /lovable — Verified email for lovable.dev\n"
        "• /github — Verified email for github.com\n"
        "• /bolt — Verified email for bolt.new\n"
        "• /lovable1 / /lovable2 — Outlook ya Gmail option\n"
        "• /github1 / /github2, /bolt1 / /bolt2 — same\n"
        "• /options — Option list with details\n"
        "• /check `domain.com` — MX + provider verify\n"
        "• /gmail `you@gmail.com` — Apne asli Gmail ke multiple addresses\n"
        "• /outlookmail `you@outlook.com` — Apne asli Outlook ke multiple addresses\n"

        "• /inbox — Check messages manually\n"
        "• /watch — Auto-check every 15s + forward OTP\n"
        "• /stopwatch — Stop auto-watch\n"
        "• /delete — Delete current email\n"
        "• /start — Main menu\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "💡 *Tips:*\n"
        "• Sirf real provider domains milte hain: Gmail / GoogleMail / Outlook\n"
        "• Bolt.new par 'already used' aaye to /outlook ya apna khud ka Gmail "
        "(`/gmail you@gmail.com`) use karo\n"
        "• /watch is the fastest way to get OTP codes\n"
        "• Sessions reset on bot restart — save your password!\n"
    )
    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=generic_kb()
    )



# ── CUSTOM GMAIL (MULTI ALIAS) ────────────────────────────────────────────────
GMAIL_PROMPT = (
    "📮 *Custom Gmail — Multi Address*\n\n"
    "Apna asli Gmail bhejo, main usi ke *multiple working addresses* bana dunga.\n"
    "Har address ka mail **isi ek inbox** me aayega (Google dot-trick).\n\n"
    "Example: `johndoe123@gmail.com`\n"
    "Ya command: `/gmail johndoe123@gmail.com 15`"
)


def gmail_result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 More addresses", callback_data="gmail_more"),
            InlineKeyboardButton("➕ Plus tags", callback_data="gmail_plus"),
        ],
        [InlineKeyboardButton("📮 Naya Gmail", callback_data="gmail_menu")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])


def _gmail_text(base: str, sets: dict) -> str:
    def block(items):
        return "```\n" + "\n".join(f"{i}. {a}" for i, a in enumerate(items, 1)) + "\n```"

    dots, plus = sets.get("dot", []), sets.get("plus", [])
    return (
        f"📮 *Gmail addresses* — base `{base}`\n\n"
        f"*🔹 Dot (.) addresses — {len(dots)}*\n{block(dots)}\n"
        f"*🔸 Plus (+) addresses — {len(plus)}*\n{block(plus)}\n"
        "✅ Dono set ke mails aapke *isi asli Gmail inbox* me aayenge.\n"
        "💡 Signup ke liye dot-wale best hain (plus `+` ko GitHub/Bolt reject karte hain)."
    )


async def _send_gmail_aliases(msg, ctx, base: str, count: int, plus: bool, edit: bool = False):
    sets = gmail_alias.generate_sets(base, count=count)
    if not (sets["dot"] or sets["plus"]):
        text = "❌ Ye valid Gmail nahi lagta. Sirf `@gmail.com` / `@googlemail.com` chalega."
        kb = generic_kb()
    else:
        ctx.user_data["gmail_base"] = base
        uid = getattr(getattr(msg, "chat", None), "id", None)
        if uid and gmail_alias.normalize_gmail(base):
            OWN_BASES[uid] = base
        ctx.user_data["gmail_count"] = count
        ctx.user_data["gmail_plus"] = plus
        text = _gmail_text(base, sets)
        kb = gmail_result_kb()
    if edit:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def cmd_gmail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if not args:
        ctx.user_data["awaiting_gmail"] = True
        await update.message.reply_text(GMAIL_PROMPT, parse_mode="Markdown", reply_markup=generic_kb())
        return
    base = args[0]
    count = 10
    if len(args) > 1 and args[1].isdigit():
        count = max(1, min(int(args[1]), 50))
    ctx.user_data.pop("awaiting_gmail", None)
    await _send_gmail_aliases(update.message, ctx, base, count, plus=False)


# ── CUSTOM OUTLOOK (MULTI ALIAS) ──────────────────────────────────────────────
OUTLOOK_PROMPT = (
    "📬 *Custom Outlook — Multi Address*\n\n"
    "Apna asli Outlook/Hotmail bhejo, main usi ke *multiple addresses* bana dunga.\n"
    "Har address ka mail aur OTP **isi ek inbox** me aayega (Microsoft plus-tag).\n\n"
    "Example: `johndoe123@outlook.com`\n"
    "Ya command: `/outlookmail johndoe123@outlook.com 15`"
)


def outlook_result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 More addresses", callback_data="outlook_more")],
        [InlineKeyboardButton("📬 Naya Outlook", callback_data="outlook_menu")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])


def _outlook_alias_text(base: str, sets: dict) -> str:
    dots = sets.get("dot", [])

    def block(items):
        return "```\n" + "\n".join(f"{i}. {a}" for i, a in enumerate(items, 1)) + "\n```"

    parts = [
        f"\U0001F4EC *Outlook addresses* — base `{base}`\n",
        "*\u2705 OTP 100% yahin aayega*\n```\n" + base + "\n```",
    ]
    if dots:
        parts.append(f"*\U0001F539 Dot (.) — {len(dots)}*\n{block(dots)}")
    parts.append(
        "\u2139\ufe0f Plus (+) band hai. Dot (.) wale addresses unique hain, par "
        "Microsoft dots ko ignore nahi karta \u2014 inka OTP tabhi milega jab wo "
        "address account me alias ho. Bot dono jagah (dot + base) inbox check "
        "karta hai, isliye OTP miss nahi hoga; kabhi na aaye to upar wala base "
        "address use karo."
    )
    return "\n\n".join(parts)


async def _send_outlook_aliases(msg, ctx, base: str, count: int, edit: bool = False):
    sets = outlook_alias.generate_sets(base, count=count)
    if not sets.get("dot"):
        text = (
            "❌ Ye valid Outlook address nahi lagta. "
            "`@outlook.com` / `@hotmail.com` / `@live.com` / `@msn.com` chalega."
        )
        kb = generic_kb()
    else:
        ctx.user_data["outlook_base"] = base
        ctx.user_data["outlook_count"] = count
        text = _outlook_alias_text(base, sets)
        kb = outlook_result_kb()
    if edit:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def cmd_outlookmail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if not args:
        ctx.user_data["awaiting_outlook"] = True
        await update.message.reply_text(
            OUTLOOK_PROMPT, parse_mode="Markdown", reply_markup=generic_kb()
        )
        return
    base = args[0]
    count = 10
    if len(args) > 1 and args[1].isdigit():
        count = max(1, min(int(args[1]), 50))
    ctx.user_data.pop("awaiting_outlook", None)
    await _send_outlook_aliases(update.message, ctx, base, count)


# ── RELAY ALIASES (Bolt.new-safe, always unique) ──────────────────────────────
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    waiting_gmail = ctx.user_data.pop("awaiting_gmail", False)
    waiting_outlook = ctx.user_data.pop("awaiting_outlook", False)

    is_gmail = bool(gmail_alias.normalize_gmail(text))
    is_outlook = bool(outlook_alias.normalize_outlook(text))

    if is_outlook and (waiting_outlook or not waiting_gmail):
        await _send_outlook_aliases(update.message, ctx, text, 10)
        return
    if is_gmail:
        await _send_gmail_aliases(update.message, ctx, text, 10, plus=False)
        return

    if waiting_outlook:
        ctx.user_data["awaiting_outlook"] = True
        await update.message.reply_text(
            "❌ Valid Outlook/Hotmail address bhejo, jaise `johndoe123@outlook.com`",
            parse_mode="Markdown",
            reply_markup=generic_kb(),
        )
        return
    if waiting_gmail:
        ctx.user_data["awaiting_gmail"] = True
        await update.message.reply_text(
            "❌ Valid Gmail bhejo, jaise `johndoe123@gmail.com`",
            parse_mode="Markdown",
            reply_markup=generic_kb(),
        )


# ── CALLBACK HANDLER ───────────────────────────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = update.effective_user.id
    session = SESSIONS.get(uid)
    data = query.data

    if not (data.startswith("show_otp:") or (
        data.startswith("del_msg:") and not session
    )):
        await query.answer()

    if data == "outlook_menu":
        ctx.user_data["awaiting_outlook"] = True
        ctx.user_data.pop("awaiting_gmail", None)
        await query.edit_message_text(OUTLOOK_PROMPT, parse_mode="Markdown", reply_markup=generic_kb())

    elif data == "outlook_more":
        base = ctx.user_data.get("outlook_base")
        if not base:
            ctx.user_data["awaiting_outlook"] = True
            await query.edit_message_text(OUTLOOK_PROMPT, parse_mode="Markdown", reply_markup=generic_kb())
        else:
            count = min(ctx.user_data.get("outlook_count", 10) + 10, 50)
            await _send_outlook_aliases(query.message, ctx, base, count, edit=True)

    elif data == "gmail_menu":
        ctx.user_data["awaiting_gmail"] = True
        ctx.user_data.pop("awaiting_outlook", None)
        await query.edit_message_text(GMAIL_PROMPT, parse_mode="Markdown", reply_markup=generic_kb())

    elif data in ("gmail_more", "gmail_plus"):
        base = ctx.user_data.get("gmail_base")
        if not base:
            ctx.user_data["awaiting_gmail"] = True
            await query.edit_message_text(GMAIL_PROMPT, parse_mode="Markdown", reply_markup=generic_kb())
        else:
            plus = data == "gmail_plus" or ctx.user_data.get("gmail_plus", False)
            count = ctx.user_data.get("gmail_count", 10)
            if data == "gmail_more":
                count = min(count + 10, 50)
            await _send_gmail_aliases(query.message, ctx, base, count, plus=plus, edit=True)

    elif data == "new_email":
        await query.edit_message_text("⏳ Creating email + generating password...")
        ok, email_or_err, acc_pw = await _create_email(uid)
        if ok:
            await query.edit_message_text(
                _email_password_text(email_or_err, acc_pw),
                parse_mode="Markdown",
                reply_markup=email_created_kb(email_or_err, acc_pw),
            )
        else:
            await query.edit_message_text(email_or_err, reply_markup=generic_kb())

    elif data == "new_outlook":
        await query.edit_message_text("⏳ Creating real @outlook.com email...")
        ok, email_or_err, acc_pw = await _create_email(uid, outlook_only=True)
        if ok:
            await query.edit_message_text(
                _email_password_text(email_or_err, acc_pw),
                parse_mode="Markdown",
                reply_markup=email_created_kb(email_or_err, acc_pw),
            )
        else:
            await query.edit_message_text(email_or_err, reply_markup=generic_kb())

    elif data in ("new_lovable", "new_github", "new_bolt"):
        target = data.removeprefix("new_")
        await query.edit_message_text(f"⏳ {target.title()} ke liye verified mailbox bana raha hoon...")
        ok, email_or_err, acc_pw = await _create_for_target(uid, target)
        await _reply_created(query.message, ok, email_or_err, acc_pw, target, uid,
                             edit_obj=query.edit_message_text)

    elif data.startswith("opts:"):
        target = data.split(":", 1)[1]
        own_base = OWN_BASES.get(uid)
        await query.edit_message_text(
            options_text(target, own_base), parse_mode="Markdown",
            reply_markup=options_kb(target, own_base),
        )

    elif data.startswith("opt:"):
        _, target, key = data.split(":", 2)
        if key not in mail_options.OPTIONS:
            await query.edit_message_text("❌ Unknown option.", reply_markup=generic_kb())
            return
        await query.edit_message_text(f"⏳ {mail_options.OPTIONS[key]['label']} bana raha hoon...")
        ok, email_or_err, acc_pw = await _create_for_target(uid, target, key)
        await _reply_created(query.message, ok, email_or_err, acc_pw, target, uid,
                             edit_obj=query.edit_message_text)

    elif data == "main_menu":
        s = SESSIONS.get(uid)
        if s:
            text = (
                f"🏠 *Main Menu*\n\n"
                f"📧 *Email:*\n`{s['email']}`\n\n"
                f"🔑 *Password:*\n`{s['acc_password']}`"
            )
            kb = main_menu_kb(s["email"], s["acc_password"])
        else:
            text = "🏠 *Main Menu*\n\n❌ No active email."
            kb = main_menu_kb()
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif data == "inbox":
        if not session:
            await query.edit_message_text("❌ No active email. Use /new first.", reply_markup=generic_kb())
            return
        await query.edit_message_text("⏳ Refreshing inbox...")
        await _render_inbox(query.message, session, edit=True)

    elif data == "watch":
        if not session:
            await query.edit_message_text("❌ No active email. Use /new first.", reply_markup=generic_kb())
            return
        _stop_watch(ctx, uid)
        ctx.job_queue.run_repeating(
            _watch_job,
            interval=15,
            first=3,
            name=f"watch_{uid}",
            data={"uid": uid, "chat_id": update.effective_chat.id},
            job_kwargs={"misfire_grace_time": 30},
        )
        await query.edit_message_text(
            f"👁 *Auto-Watch Active!*\n\n"
            f"📧 *Email:*\n`{session['email']}`\n\n"
            f"🔑 *Password:*\n`{session['acc_password']}`\n\n"
            f"⚡ OTP will arrive instantly!\n"
            f"Use /stopwatch to stop.",
            parse_mode="Markdown",
            reply_markup=watch_kb(session["email"], session["acc_password"]),
        )

    elif data == "stop_watch":
        _stop_watch(ctx, uid)
        await query.edit_message_text("🛑 Auto-watch stopped.", reply_markup=generic_kb())

    elif data == "delete_account":
        if uid in SESSIONS:
            _stop_watch(ctx, uid)
            email = SESSIONS.pop(uid)["email"]
            await query.edit_message_text(
                f"🗑 Email `{email}` deleted.\nUse /new to create a fresh one.",
                parse_mode="Markdown",
                reply_markup=main_menu_kb(),
            )
        else:
            await query.edit_message_text("❌ No active email.", reply_markup=generic_kb())

    elif data.startswith("read:"):
        if not session:
            await query.edit_message_text("❌ No active email.", reply_markup=generic_kb())
            return
        mid = data.split(":", 1)[1]
        full = await service_get_message(session, mid)
        if not full:
            await query.edit_message_text("❌ Could not load message.", reply_markup=generic_kb())
            return
        subject = full.get("subject") or "(No Subject)"
        sender = full.get("from", {}).get("address", "unknown")
        body_text = full.get("text") or ""
        body_html = full.get("html") or ""
        body = body_text or clean_html(body_html)
        body_short = body[:1200]
        otp = extract_otp(body_short)
        links = extract_links(body_text, body_html)
        otp_line = f"\n\n🔑 *OTP: `{otp}`* ← Copy this!" if otp else ""
        link_line = link_notice(links)
        text = (
            f"📧 *Message*{otp_line}{link_line}\n\n"
            f"*From:* `{_md_escape(sender)}`\n"
            f"*Subject:* {_md_escape(subject)}\n\n"
            f"```\n{body_short}\n```"
        )
        await query.edit_message_text(
            text[:4000], parse_mode="Markdown", reply_markup=message_action_kb(mid, otp, links)
        )

    elif data.startswith("show_otp:"):
        otp = data.split(":", 1)[1]
        await query.answer(f"OTP: {otp}", show_alert=True)
        return

    elif data.startswith("del_msg:"):
        if not session:
            await query.edit_message_text("❌ No active email.", reply_markup=generic_kb())
            return
        mid = data.split(":", 1)[1]
        deleted = await service_delete_message(session, mid)
        if deleted:
            await query.edit_message_text(
                "🗑 Message deleted.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📥 Back to Inbox", callback_data="inbox")],
                    _owner_row(),
                ]),
            )
        else:
            await query.edit_message_text("❌ Could not delete message.", reply_markup=generic_kb())


# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("outlook", cmd_outlook))
    app.add_handler(CommandHandler("lovable", cmd_lovable))
    app.add_handler(CommandHandler("github", cmd_github))
    app.add_handler(CommandHandler("bolt", cmd_bolt))
    for _name, _handler in TARGET_CMDS.items():
        app.add_handler(CommandHandler(_name, _handler))
    app.add_handler(CommandHandler("options", cmd_options))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("gmail", cmd_gmail))
    app.add_handler(CommandHandler("outlookmail", cmd_outlookmail))
    app.add_handler(CommandHandler("inbox", cmd_inbox))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("stopwatch", cmd_stopwatch))
    app.add_handler(CommandHandler("help", cmd_help))
    for _name, _handler in OUTLOOK_CMDS.items():
        # Telegram command matching is normally case-insensitive, but keeping
        # the capitalized form explicit also supports clients that preserve it.
        app.add_handler(CommandHandler([_name, _name.capitalize()], _handler))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    # Wait for any old dyno to release the Telegram getUpdates lock.
    # Without this, the new process polls while the old one is still shutting
    # down -> 409 Conflict -> crash (the root cause of the restart loop).
    if STARTUP_DELAY > 0:
        logger.info("⏳ Waiting %ss before polling (old dyno shutdown)...", int(STARTUP_DELAY))
        import time as _time
        _time.sleep(STARTUP_DELAY)
    logger.info("✅ Bot started. Polling...")
    # stop_signals=None -> our watchdog owns SIGTERM so the dyno always exits
    # inside Heroku's 30s window instead of being SIGKILLed (Error R12).
    _install_shutdown_watchdog(app)
    # Retry loop: if the old dyno hasn't released the getUpdates lock yet,
    # Telegram returns 409 Conflict. We wait and retry instead of crashing.
    for _attempt in range(4):
        try:
            app.run_polling(drop_pending_updates=True, stop_signals=None)
            break
        except Exception as e:
            if "Conflict" in str(e) and _attempt < 3:
                logger.warning("409 Conflict on attempt %d, retrying in 5s...", _attempt + 1)
                import time as _time
                _time.sleep(5)
            else:
                raise


if __name__ == "__main__":
    main()
