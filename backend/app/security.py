from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any


def normalize_phone(phone: str) -> str:
    return phone.strip()


def hash_phone(phone: str) -> str:
    normalized_phone = normalize_phone(phone)
    return hashlib.sha256(normalized_phone.encode("utf-8")).hexdigest()


def mask_phone(phone: str) -> str:
    normalized_phone = normalize_phone(phone)
    if len(normalized_phone) < 7:
        return "*" * len(normalized_phone)
    return f"{normalized_phone[:3]}****{normalized_phone[-4:]}"


def sign_session_payload(payload: dict[str, Any], secret_key: str) -> str:
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_bytes = payload_json.encode("utf-8")
    signature = hmac.new(
        secret_key.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    encoded_payload = base64.urlsafe_b64encode(payload_bytes).decode("ascii")
    return f"{encoded_payload}.{signature}"


def read_signed_session(token: str, secret_key: str) -> dict[str, Any] | None:
    try:
        encoded_payload, provided_signature = token.split(".", maxsplit=1)
        payload_bytes = base64.urlsafe_b64decode(encoded_payload.encode("ascii"))
    except (ValueError, TypeError, binascii.Error):
        return None

    expected_signature = hmac.new(
        secret_key.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(provided_signature, expected_signature):
        return None

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    return payload
