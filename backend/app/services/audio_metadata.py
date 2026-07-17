from __future__ import annotations

import math
from pathlib import Path

from pymediainfo import MediaInfo


EXPECTED_CONTAINER_FORMATS = {
    "audio/webm": {"WebM"},
    "audio/mp4": {"MPEG-4"},
    "audio/ogg": {"Ogg"},
}


class AudioDurationError(ValueError):
    pass


def read_audio_duration_seconds(audio_path: Path, *, media_type: str) -> float:
    expected_formats = EXPECTED_CONTAINER_FORMATS.get(media_type)
    if expected_formats is None:
        raise AudioDurationError("Unsupported audio media type.")

    try:
        with audio_path.open("rb") as audio_file:
            media_info = MediaInfo.parse(audio_file, buffer_size=64 * 1024)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AudioDurationError("Audio duration could not be determined.") from exc

    general_tracks = [track for track in media_info.tracks if track.track_type == "General"]
    audio_tracks = [track for track in media_info.tracks if track.track_type == "Audio"]
    if (
        not general_tracks
        or general_tracks[0].format not in expected_formats
        or not audio_tracks
    ):
        raise AudioDurationError("Audio duration could not be determined.")

    duration_candidates: list[float] = []
    for track in media_info.tracks:
        for field_name in ("duration", "source_duration"):
            raw_duration = getattr(track, field_name, None)
            if raw_duration is None:
                continue
            try:
                duration_ms = float(raw_duration)
            except (TypeError, ValueError):
                continue
            if math.isfinite(duration_ms) and duration_ms > 0:
                duration_candidates.append(duration_ms)

    if not duration_candidates:
        raise AudioDurationError("Audio duration could not be determined.")
    return max(duration_candidates) / 1_000
