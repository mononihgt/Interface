from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")


def current_shanghai_date() -> str:
    return datetime.now(tz=ASIA_SHANGHAI).date().isoformat()


def parse_stored_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise ValueError("Timestamp must not be empty.")
        if len(normalized) <= 10 or normalized[10] not in {"T", " "}:
            raise ValueError("Timestamp must include an explicit time component.")
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("Timestamp must be a valid ISO timestamp.") from exc
    else:
        raise ValueError("Timestamp must be a string or datetime.")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def shanghai_date_from_timestamp(value: str | datetime) -> str:
    return parse_stored_timestamp(value).astimezone(ASIA_SHANGHAI).date().isoformat()
