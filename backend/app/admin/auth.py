from __future__ import annotations

import hashlib
import hmac
import sqlite3
from typing import Optional
import unicodedata
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, status

from backend.app.security import read_signed_session, sign_session_payload
from backend.app.settings import Settings


ADMIN_SESSION_SCOPE = "admin"
_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)
LOGIN_RESERVATION_TTL_SECONDS = 60
LOGIN_RESERVATION_HEARTBEAT_SECONDS = 10.0
COUNT_USERNAME_LOGIN_ATTEMPTS_SQL = """
SELECT COUNT(*) AS attempt_count
FROM admin_login_attempts
WHERE username_key = ?
  AND expires_at > strftime('%Y-%m-%d %H:%M:%f', 'now')
"""
COUNT_ADDRESS_LOGIN_ATTEMPTS_SQL = """
SELECT COUNT(*) AS attempt_count
FROM admin_login_attempts
WHERE client_address = ?
  AND expires_at > strftime('%Y-%m-%d %H:%M:%f', 'now')
"""
DELETE_EXPIRED_LOGIN_ATTEMPTS_SQL = """
DELETE FROM admin_login_attempts
WHERE expires_at <= strftime('%Y-%m-%d %H:%M:%f', 'now')
"""


def _configured_admin_user(settings: Settings) -> str:
    return settings.admin_user.strip()


def _configured_admin_hash(settings: Settings) -> str | None:
    if settings.admin_password_hash is None:
        return None
    value = settings.admin_password_hash.strip()
    return value or None


def _configured_admin_salt(settings: Settings) -> str | None:
    if settings.admin_password_salt is None:
        return None
    value = settings.admin_password_salt.strip()
    return value or None


def _session_secret(settings: Settings) -> str | None:
    if settings.app_secret_key is None:
        return None
    value = settings.app_secret_key.strip()
    return value or None


def is_admin_auth_configured(
    settings: Settings,
    *,
    persisted_password_hash: str | None = None,
) -> bool:
    configured_hash = _configured_admin_hash(settings)
    if not _configured_admin_user(settings) or not _session_secret(settings):
        return False
    if persisted_password_hash is not None:
        return is_modern_admin_password_hash(persisted_password_hash)
    if not configured_hash:
        return False
    return is_modern_admin_password_hash(configured_hash) or bool(
        _configured_admin_salt(settings)
    )


def hash_admin_password(*, password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def is_modern_admin_password_hash(password_hash: str) -> bool:
    return password_hash.startswith("$argon2id$")


def _hash_legacy_admin_password(*, salt: str, password: str) -> str:
    return hashlib.sha256(f"{salt}{password}".encode("utf-8")).hexdigest()


def normalize_admin_username(username: str) -> str:
    return unicodedata.normalize("NFKC", username).strip().casefold()


def admin_username_throttle_key(username: str) -> str:
    normalized_username = normalize_admin_username(username)
    return hashlib.sha256(normalized_username.encode("utf-8")).hexdigest()


def verify_admin_password(*, password: str, password_hash: str) -> bool:
    if not is_modern_admin_password_hash(password_hash):
        return False
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def verify_admin_credentials(
    *,
    username: str,
    password: str,
    settings: Settings,
    persisted_password_hash: str | None = None,
) -> bool:
    configured_user = _configured_admin_user(settings)
    effective_hash = persisted_password_hash or _configured_admin_hash(settings)
    if not configured_user or effective_hash is None:
        return False

    if is_modern_admin_password_hash(effective_hash):
        password_matches = verify_admin_password(
            password=password,
            password_hash=effective_hash,
        )
    else:
        configured_salt = _configured_admin_salt(settings)
        if configured_salt is None:
            return False
        candidate_hash = _hash_legacy_admin_password(
            salt=configured_salt,
            password=password,
        )
        password_matches = hmac.compare_digest(candidate_hash, effective_hash)
    username_matches = hmac.compare_digest(
        normalize_admin_username(username).encode("utf-8"),
        normalize_admin_username(configured_user).encode("utf-8"),
    )
    return username_matches and password_matches


def admin_password_needs_migration(
    *,
    settings: Settings,
    persisted_password_hash: str | None,
) -> bool:
    return persisted_password_hash is None and not is_modern_admin_password_hash(
        _configured_admin_hash(settings) or ""
    )


def get_persisted_admin_password_hash(
    conn: sqlite3.Connection,
    *,
    admin_user: str,
) -> str | None:
    row = conn.execute(
        "SELECT password_hash FROM admin_credentials WHERE admin_user = ?",
        (admin_user,),
    ).fetchone()
    return None if row is None else str(row["password_hash"])


def claim_admin_password_hash(
    conn: sqlite3.Connection,
    *,
    admin_user: str,
    password_hash: str,
) -> bool:
    cursor = conn.execute(
        """
        INSERT INTO admin_credentials (admin_user, password_hash)
        VALUES (?, ?)
        ON CONFLICT(admin_user) DO NOTHING
        """,
        (admin_user, password_hash),
    )
    return cursor.rowcount == 1


def reserve_admin_login_attempt(
    conn: sqlite3.Connection,
    *,
    username_key: str,
    client_address: str,
    max_failures: int,
    reservation_ttl_seconds: int,
) -> str | None:
    conn.execute(DELETE_EXPIRED_LOGIN_ATTEMPTS_SQL)
    username_count = int(
        conn.execute(
            COUNT_USERNAME_LOGIN_ATTEMPTS_SQL,
            (username_key,),
        ).fetchone()["attempt_count"]
    )
    if username_count >= max_failures:
        return None
    address_count = int(
        conn.execute(
            COUNT_ADDRESS_LOGIN_ATTEMPTS_SQL,
            (client_address,),
        ).fetchone()["attempt_count"]
    )
    if address_count >= max_failures:
        return None

    reservation_token = uuid4().hex
    conn.execute(
        """
        INSERT INTO admin_login_attempts (
            reservation_token,
            username_key,
            client_address,
            state,
            expires_at
        ) VALUES (?, ?, ?, 'pending', strftime('%Y-%m-%d %H:%M:%f', 'now', ?))
        """,
        (
            reservation_token,
            username_key,
            client_address,
            f"+{reservation_ttl_seconds} seconds",
        ),
    )
    return reservation_token


def renew_admin_login_attempt(
    conn: sqlite3.Connection,
    *,
    reservation_token: str,
    reservation_ttl_seconds: int,
) -> bool:
    cursor = conn.execute(
        """
        UPDATE admin_login_attempts
        SET expires_at = strftime('%Y-%m-%d %H:%M:%f', 'now', ?)
        WHERE reservation_token = ?
          AND state = 'pending'
          AND expires_at > strftime('%Y-%m-%d %H:%M:%f', 'now')
        """,
        (f"+{reservation_ttl_seconds} seconds", reservation_token),
    )
    return cursor.rowcount == 1


def fail_admin_login_attempt(
    conn: sqlite3.Connection,
    *,
    reservation_token: str,
    window_seconds: int,
) -> bool:
    cursor = conn.execute(
        """
        UPDATE admin_login_attempts
        SET state = 'failed',
            expires_at = strftime('%Y-%m-%d %H:%M:%f', 'now', ?)
        WHERE reservation_token = ?
          AND state = 'pending'
          AND expires_at > strftime('%Y-%m-%d %H:%M:%f', 'now')
        """,
        (f"+{window_seconds} seconds", reservation_token),
    )
    return cursor.rowcount == 1


def release_admin_login_attempt(
    conn: sqlite3.Connection,
    *,
    reservation_token: str,
) -> bool:
    cursor = conn.execute(
        """
        DELETE FROM admin_login_attempts
        WHERE reservation_token = ?
          AND state = 'pending'
          AND expires_at > strftime('%Y-%m-%d %H:%M:%f', 'now')
        """,
        (reservation_token,),
    )
    return cursor.rowcount == 1


def issue_admin_session_token(*, settings: Settings) -> str:
    session_secret = _session_secret(settings)
    if session_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is not configured.",
        )
    return sign_session_payload(
        {
            "scope": ADMIN_SESSION_SCOPE,
            "admin_user": _configured_admin_user(settings),
        },
        session_secret,
    )


def read_admin_session(
    *,
    session_token: Optional[str],
    settings: Settings,
) -> str | None:
    session_secret = _session_secret(settings)
    if session_secret is None or not session_token:
        return None
    payload = read_signed_session(session_token, session_secret)
    if payload is None:
        return None
    if payload.get("scope") != ADMIN_SESSION_SCOPE:
        return None
    admin_user = payload.get("admin_user")
    if not isinstance(admin_user, str):
        return None
    if admin_user != _configured_admin_user(settings):
        return None
    return admin_user


def require_admin_session(
    *,
    session_token: Optional[str],
    settings: Settings,
) -> str:
    admin_user = read_admin_session(session_token=session_token, settings=settings)
    if admin_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin login required.",
        )
    return admin_user
