"""Custom Outlook alias generator — Gmail wale /gmail command ka Outlook version.

User apna asli @outlook.com / @hotmail.com / @live.com / @msn.com address deta
hai, hum uske multiple addresses banate hain jinka mail *usi ek inbox* me
aata hai.

IMPORTANT (bug fix):
Microsoft ke saath sirf **plus sub-addressing** hi guaranteed same-inbox
delivery deta hai:  john.doe+gh@outlook.com  ->  john.doe@outlook.com

Ye do cheezein mail ko DUSRE mailbox me bhej deti hain, isliye ab generate
nahi hoti (yahi OTP na milne ka asli reason tha):
  * dot variants  — Gmail ke ulat Microsoft dots ko ignore NAHI karta, so
    j.ohn@outlook.com ek alag (ya non-existent) mailbox hai -> OTP kabhi nahi
    aayega.
  * domain mirror — hotmail.com / live.com / msn.com tabhi kaam karte hain jab
    wo alias account par khud registered ho; blind mirror karne se mail bounce
    ho jaata hai.
"""
from __future__ import annotations

import random
import re

OUTLOOK_DOMAINS = (
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "outlook.in",
    "hotmail.co.uk",
    "live.co.uk",
    "outlook.co.uk",
    "hotmail.fr",
    "live.in",
)
_EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._%+-]{2,}@([a-z0-9.-]+\.[a-z]{2,})$")

DEFAULT_TAGS = (
    "gh", "dev", "app", "web", "acc", "one", "two", "pro", "x1", "np",
    "bolt", "lov", "shop", "news", "beta", "test", "id", "sign", "n1", "n2",
    "mail", "box", "run", "go", "up", "kit", "lab", "hub", "net", "zone",
)


def normalize_outlook(raw: str) -> tuple[str, str] | None:
    """Return (base_local, domain) or None if it is not a Microsoft address."""
    if not raw:
        return None
    raw = raw.strip().strip("<>").lower()
    m = _EMAIL_RE.match(raw)
    if not m:
        return None
    local, domain = raw.split("@", 1)
    if domain not in OUTLOOK_DOMAINS:
        return None
    # '+tag' hata do, par dots waise ke waise rakho — Microsoft me dots
    # address ka hissa hain.
    local = local.split("+", 1)[0]
    if not (3 <= len(local) <= 64):
        return None
    return local, domain


def tag_variants(local: str, count: int) -> list[str]:
    """Plus sub-addresses — Microsoft delivers all of these to one inbox."""
    tags = list(DEFAULT_TAGS)
    random.shuffle(tags)
    out = [f"{local}+{t}" for t in tags[:count]]
    seen = set(out)
    i = 1
    while len(out) < count:
        cand = f"{local}+n{i}{random.randint(10, 99)}"
        if cand not in seen:
            seen.add(cand)
            out.append(cand)
        i += 1
    return out[:count]


def generate_aliases(email: str, count: int = 10) -> list[str]:
    """Multiple Outlook addresses that all land in the same real inbox.

    Sirf plus sub-addressing — same local part, same domain.
    """
    parsed = normalize_outlook(email)
    if not parsed:
        return []
    local, domain = parsed
    count = max(1, min(count, 50))
    return [f"{lp}@{domain}" for lp in tag_variants(local, count)]


def dot_variants(local: str, limit: int) -> list[str]:
    """Custom dot (.) addresses — local part me dots inject karke.

    NOTE: Microsoft, Gmail ke ulat, dots ko *ignore nahi karta*. Isliye ye
    addresses tabhi same inbox me deliver honge jab wo alias account par
    khud add/registered ho. Agar OTP na aaye to plus (+) wale use karo —
    wo 100% deliver hote hain.
    """
    base = local.replace(".", "")
    gaps = len(base) - 1
    if gaps <= 0 or limit <= 0:
        return []
    total = 1 << gaps
    masks = list(range(1, total)) if total <= 4096 else random.sample(range(1, total), 4096)
    random.shuffle(masks)
    out: list[str] = []
    seen: set[str] = set()
    for mask in masks:
        if bin(mask).count("1") > 3:  # 3 se zyada dots natural nahi lagte
            continue
        buf = []
        for i, ch in enumerate(base):
            buf.append(ch)
            if i < gaps and (mask >> i) & 1:
                buf.append(".")
        cand = "".join(buf)
        if ".." in cand or cand.startswith(".") or cand.endswith(".") or cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
        if len(out) >= limit:
            break
    return out


def dotted_tag_variants(local: str, count: int) -> list[str]:
    """Dot *dikhne* wale addresses jo 100% deliver hote hain.

    Dot ko plus-tag ke andar rakhte hain: john+g.h1@outlook.com — Microsoft
    isko base inbox me hi deliver karta hai, par site ke liye ye ek naya,
    dot-wala unique address hai.
    """
    tags = list(DEFAULT_TAGS)
    random.shuffle(tags)
    out: list[str] = []
    seen: set[str] = set()
    i = 0
    while len(out) < count:
        t = tags[i % len(tags)]
        cand = f"{local}+{t[0]}.{t[1:] or 'x'}{random.randint(1, 99)}"
        i += 1
        if cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
        if i > count * 20:
            break
    return out[:count]


def generate_dot_aliases(email: str, count: int = 10) -> list[str]:
    parsed = normalize_outlook(email)
    if not parsed:
        return []
    local, domain = parsed
    count = max(1, min(count, 50))
    return [f"{lp}@{domain}" for lp in dot_variants(local, count)]


def generate_sets(email: str, count: int = 10) -> dict[str, list[str]]:
    """Sirf DOT (.) addresses — plus (+) wale band.

    User ne bola ki `john+k.it47@outlook.com` jaise plus addresses nahi
    chahiye, isliye ab sirf dot variants (`j.ohn.doe@outlook.com`) bante hain.
    """
    parsed = normalize_outlook(email)
    if not parsed:
        return {"plus": [], "dotplus": [], "dot": []}
    local, domain = parsed
    count = max(1, min(count, 50))
    return {
        "plus": [],
        "dotplus": [],
        "dot": generate_dot_aliases(email, count),
    }
