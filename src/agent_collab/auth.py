from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


class AuthenticationError(ValueError):
    pass


def canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def seal(payload: dict[str, Any], key: bytes) -> dict[str, Any]:
    mac = hmac.new(key, canonical_json(payload), hashlib.sha256).hexdigest()
    return {"payload": payload, "mac": mac}


def open_sealed(document: object, key: bytes) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise AuthenticationError("sealed document must be an object")
    payload = document.get("payload")
    mac = document.get("mac")
    if not isinstance(payload, dict) or not isinstance(mac, str):
        raise AuthenticationError("sealed document is incomplete")
    expected = hmac.new(key, canonical_json(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise AuthenticationError("invalid message authentication code")
    return payload
