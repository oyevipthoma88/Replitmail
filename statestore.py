"""Tiny JSON state store that survives process restarts.

ROOT CAUSE of "repeat mails":
every bit of state lived in plain Python dicts.  Heroku restarts the worker
constantly (see the logs), and after each restart `seen_ids` was empty again,
so the very next watch poll re-sent every mail that was still sitting in the
mailbox.  Starting /watch did the same thing: `seen_ids` began empty, so the
whole existing inbox was pushed out as "New Email!".

Everything written here is keyed per mailbox, bounded in size, and flushed to
disk immediately so a restart cannot replay old mail.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

STATE_DIR = os.getenv("STATE_DIR", "/tmp/mailbot-state")
_MAX_PER_BOX = 400
_MAX_BOXES = 500
_BOX_TTL = 7 * 24 * 3600

_lock = threading.Lock()
_cache: dict[str, dict] = {}


def _path(name: str) -> str:
    return os.path.join(STATE_DIR, f"{name}.json")


def _load(name: str) -> dict:
    if name in _cache:
        return _cache[name]
    data: dict = {}
    try:
        with open(_path(name), "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
    except Exception:
        data = {}
    _cache[name] = data
    return data


def _save(name: str, data: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = _path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, _path(name))
    except Exception:
        pass


# ── delivered-mail dedupe ─────────────────────────────────────────────────────
def _box_key(chat_id: int, email: str) -> str:
    return f"{chat_id}:{(email or '').lower()}"


def mail_key(mid: str, sender: str = "", subject: str = "", body: str = "") -> str:
    """Stable key for one mail.

    Uses the provider id when present, plus a content hash so providers that
    hand out a fresh id for the same mail (Emailnator does this after a pool
    rotation) still cannot produce a duplicate notification.
    """
    digest = hashlib.sha1(
        f"{sender}|{subject}|{(body or '')[:300]}".encode("utf-8", "ignore")
    ).hexdigest()[:16]
    return f"{mid or ''}|{digest}"


def already_delivered(chat_id: int, email: str, key: str) -> bool:
    with _lock:
        boxes = _load("delivered")
        box = boxes.get(_box_key(chat_id, email))
        return bool(box) and key in box.get("keys", [])


def mark_delivered(chat_id: int, email: str, keys: list[str] | str) -> None:
    if isinstance(keys, str):
        keys = [keys]
    if not keys:
        return
    with _lock:
        boxes = _load("delivered")
        bk = _box_key(chat_id, email)
        box = boxes.setdefault(bk, {"keys": [], "ts": 0})
        existing = box["keys"]
        for key in keys:
            if key not in existing:
                existing.append(key)
        box["keys"] = existing[-_MAX_PER_BOX:]
        box["ts"] = int(time.time())
        now = time.time()
        stale = [k for k, v in boxes.items() if now - v.get("ts", 0) > _BOX_TTL]
        for k in stale:
            boxes.pop(k, None)
        if len(boxes) > _MAX_BOXES:
            for k in sorted(boxes, key=lambda k: boxes[k].get("ts", 0))[:-_MAX_BOXES]:
                boxes.pop(k, None)
        _save("delivered", boxes)


# ── used Gmail bases (so every /new hands out a fresh address) ────────────────
def used_bases() -> set[str]:
    with _lock:
        data = _load("used_bases")
        now = time.time()
        return {b for b, ts in data.items() if now - ts < _BOX_TTL}


def add_used_base(base: str) -> None:
    if not base:
        return
    with _lock:
        data = _load("used_bases")
        data[base] = time.time()
        now = time.time()
        for b in [b for b, ts in data.items() if now - ts > _BOX_TTL]:
            data.pop(b, None)
        if len(data) > 2000:
            for b in sorted(data, key=data.get)[:-2000]:
                data.pop(b, None)
        _save("used_bases", data)


# ── daily fresh-mailbox stock ────────────────────────────────────────────────
# Background job roz naye, sabse khaali inbox wale Outlook/Hotmail mailbox yahan bharta hai,
# taaki /outlook turant ekdam fresh address de sake.
def _pool_clean(items: list, max_age: float) -> list:
    now = time.time()
    return [i for i in items if now - i.get("ts", 0) < max_age]


def pool_push(kind: str, email: str, source: str | None = None, mails: int = 0) -> None:
    with _lock:
        data = _load("fresh_pool")
        items = data.setdefault(kind, [])
        if any(i["email"].lower() == email.lower() for i in items):
            return
        items.append({"email": email, "source": source, "mails": mails, "ts": time.time()})
        data[kind] = items[-200:]
        _save("fresh_pool", data)


def pool_pop(kind: str, max_age: float) -> dict | None:
    with _lock:
        data = _load("fresh_pool")
        items = _pool_clean(data.get(kind, []), max_age)
        item = items.pop(0) if items else None
        data[kind] = items
        _save("fresh_pool", data)
        return item


def pool_size(kind: str, max_age: float) -> int:
    with _lock:
        return len(_pool_clean(_load("fresh_pool").get(kind, []), max_age))


def pool_emails(kind: str) -> list[str]:
    with _lock:
        return [i["email"] for i in _load("fresh_pool").get(kind, [])]
