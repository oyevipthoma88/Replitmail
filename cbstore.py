"""Short inline-button payloads.

ROOT CAUSE of `telegram.error.BadRequest: Button_data_invalid`:
Telegram allows at most 64 **bytes** of `callback_data` per inline button.
Emailnator message IDs look like
`gp1.V1LMUjLgMEz3kzlShqFjLLqAQ_uftY5DojbRvsD7rZ4uQNKWXVeCV3B792CsMwFZjPy_...`
(100+ chars), so `callback_data=f"read:{msg_id}"` always exceeded the limit and
every inbox / new-mail message with such a button failed to send.

Fix: keep the long value in memory and put a short, stable hash token in the
button instead.  `put()` returns the plain `prefix:value` form whenever it
already fits, so existing short payloads are unchanged.
"""
from __future__ import annotations

import hashlib
import threading

MAX_BYTES = 64
_MAX_ENTRIES = 5000

_lock = threading.Lock()
_values: dict[str, str] = {}
_order: list[str] = []


def _token(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "ignore")).hexdigest()[:16]


def put(prefix: str, value: str) -> str:
    """Return a callback_data string for `prefix` + `value`, always <= 64 bytes."""
    value = "" if value is None else str(value)
    plain = f"{prefix}:{value}"
    if len(plain.encode("utf-8")) <= MAX_BYTES:
        return plain
    key = _token(value)
    with _lock:
        if key not in _values:
            _values[key] = value
            _order.append(key)
            while len(_order) > _MAX_ENTRIES:
                _values.pop(_order.pop(0), None)
    return f"{prefix}:#{key}"


def resolve(payload: str) -> str | None:
    """Turn the part after `prefix:` back into the original value."""
    if payload is None:
        return None
    if not payload.startswith("#"):
        return payload
    with _lock:
        return _values.get(payload[1:])


def is_safe(data: str) -> bool:
    return len(data.encode("utf-8")) <= MAX_BYTES
