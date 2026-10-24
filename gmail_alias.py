"""Custom Gmail alias generator.

User apna asli Gmail deta hai -> hum uske multiple working aliases banate hain.
Har alias ka mail SEEDHA user ke asli inbox me hi aata hai (Google ka
dot-trick + googlemail.com mirror domain).
Plus (+) tags poori tarah hata diye — GitHub / Bolt / Lovable jaisi bahut sites
unhe reject karti hain.
"""
from __future__ import annotations

import random
import re

GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
_EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._%+-]{4,}@([a-z0-9.-]+\.[a-z]{2,})$")



def normalize_gmail(raw: str) -> tuple[str, str] | None:
    """Return (clean_local_without_dots, domain) or None if not a Gmail address."""
    if not raw:
        return None
    raw = raw.strip().strip("<>").lower()
    m = _EMAIL_RE.match(raw)
    if not m:
        return None
    local, domain = raw.split("@", 1)
    if domain not in GMAIL_DOMAINS:
        return None
    local = local.split("+", 1)[0].replace(".", "")
    if len(local) < 6 or len(local) > 30:
        return None
    return local, domain


def dot_variants(local: str, limit: int) -> list[str]:
    """Dot-trick: gaps me dots daal ke unique addresses."""
    gaps = len(local) - 1
    if gaps <= 0 or limit <= 0:
        return []
    total = 1 << gaps
    masks = list(range(1, total)) if total <= 4096 else random.sample(range(1, total), 4096)
    random.shuffle(masks)
    out: list[str] = []
    seen: set[str] = set()
    for mask in masks:
        # 3 se zyada dots natural nahi lagte
        if bin(mask).count("1") > 3:
            continue
        buf = []
        for i, ch in enumerate(local):
            buf.append(ch)
            if i < gaps and (mask >> i) & 1:
                buf.append(".")
        cand = "".join(buf)
        if ".." in cand or cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
        if len(out) >= limit:
            break
    return out


def generate_aliases(
    email: str,
    count: int = 10,
    include_mirror: bool = True,
) -> list[str]:
    """Multiple aliases for one real Gmail. Sab same inbox me deliver hote hain."""
    parsed = normalize_gmail(email)
    if not parsed:
        return []
    local, _ = parsed
    count = max(1, min(count, 50))

    locals_: list[str] = dot_variants(local, count)
    if not locals_:
        locals_ = [local]

    out: list[str] = []
    for i, lp in enumerate(locals_[:count]):
        domain = "googlemail.com" if include_mirror and i % 4 == 3 else "gmail.com"
        out.append(f"{lp}@{domain}")
    return out


def generate_sets(email: str, count: int = 10) -> dict[str, list[str]]:
    """Dot-trick addresses (sab same real inbox me). Plus (+) kabhi nahi."""
    parsed = normalize_gmail(email)
    if not parsed:
        return {"dot": []}
    local, _ = parsed
    count = max(1, min(count, 50))
    dots = generate_aliases(email, count) or [f"{local}@gmail.com"]
    return {"dot": dots[:count]}
