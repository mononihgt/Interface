from __future__ import annotations

import re
from pathlib import Path


UNSAFE_FILENAME_CHARS = re.compile(r"[^\w.-]+", re.UNICODE)


def safe_filename_component(value: object, *, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    safe = UNSAFE_FILENAME_CHARS.sub("_", text).strip("._-")
    return safe or fallback


def safe_phone_component(phone: str) -> str:
    digits = "".join(character for character in str(phone or "") if character.isdigit())
    if digits:
        return digits
    return safe_filename_component(phone, fallback="phone")


def canonical_participant_stem(
    *,
    name: str,
    phone: str,
    participant_type: str,
    day_index: int,
    turn_index: int,
    session_id: str,
) -> str:
    type_component = "long" if str(participant_type) == "long" else "short"
    return "_".join(
        [
            safe_filename_component(name, fallback="participant"),
            safe_phone_component(phone),
            type_component,
            "day",
            str(int(day_index)),
            "turn",
            str(int(turn_index)),
            safe_filename_component(session_id, fallback="session"),
        ]
    )


def canonical_audio_relative_path(
    *,
    name: str,
    phone: str,
    participant_type: str,
    day_index: int,
    turn_index: int,
    session_id: str,
    suffix: str,
) -> str:
    safe_suffix = Path(f"audio{suffix}").suffix.lower()
    if not safe_suffix or len(safe_suffix) > 10:
        safe_suffix = ".bin"
    stem = canonical_participant_stem(
        name=name,
        phone=phone,
        participant_type=participant_type,
        day_index=day_index,
        turn_index=turn_index,
        session_id=session_id,
    )
    return str(Path("audio") / f"{stem}{safe_suffix}")
