from backend.app.services.file_naming import (
    canonical_audio_relative_path,
    canonical_participant_stem,
)


def test_canonical_participant_stem_preserves_identity_and_day_turn_session():
    stem = canonical_participant_stem(
        name="张 三/A",
        phone=" 138-0000-0001 ",
        participant_type="long",
        day_index=2,
        turn_index=3,
        session_id="session-abc_123",
    )

    assert stem == "张_三_A_13800000001_long_day_2_turn_3_session-abc_123"


def test_canonical_audio_relative_path_uses_safe_suffix():
    path = canonical_audio_relative_path(
        name="Participant",
        phone="phone-asr-0001",
        participant_type="short",
        day_index=1,
        turn_index=5,
        session_id="session/unsafe",
        suffix=".webm",
    )

    assert path == "audio/Participant_0001_short_day_1_turn_5_session_unsafe.webm"
