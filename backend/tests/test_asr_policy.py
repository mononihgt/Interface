from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.tests.audio_fixtures import (
    AUDIO_CONTAINERS,
    build_mp4_audio,
    build_webm_audio,
)
from backend.app.services.providers import ProviderAttempt, ProviderResponse
from backend.app.settings import Settings


TEST_DATE = "2026-07-02"


def build_pretest_payload(*, trust_score: int = 4) -> dict[str, object]:
    scales = {f"q{index}": 3 for index in range(1, 27)}
    scales.update({f"q{index}": 50 for index in range(27, 48)})
    scales.update({f"confidence_q{index}": 75 for index in range(27, 47)})
    scales["q21"] = trust_score
    scales["q48"] = "B"
    scales["q49"] = "C"

    slider_touch_state = {f"q{index}": True for index in range(27, 48)}
    slider_touch_state.update(
        {f"confidence_q{index}": True for index in range(27, 47)}
    )

    return {
        "demographics": {
            "birthDate": "2000-01-01",
            "gender": "男",
            "idNumber": "ID1234567",
        },
        "scales": scales,
        "slider_touch_state": slider_touch_state,
        "page_progress": {
            "section": "save",
            "current_step": "save",
            "completed_steps": ["intro", "demographics", "scales"],
        },
        "client_timestamp": "2026-07-02T09:30:00+08:00",
    }


class FakeAsrClient:
    def __init__(self, *results: SimpleNamespace) -> None:
        self._results = list(results)
        self.calls: list[dict[str, object]] = []

    def transcribe(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str | None,
        request_id: str,
    ) -> SimpleNamespace:
        self.calls.append(
            {
                "audio_bytes": audio_bytes,
                "filename": filename,
                "content_type": content_type,
                "request_id": request_id,
            }
        )
        if not self._results:
            raise AssertionError("fake ASR client exhausted")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "asr-policy.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="test-secret-key",
        asr_max_retry_per_turn=3,
        asr_max_upload_bytes=2_048,
        asr_max_request_bytes=4_096,
        asr_allowed_media_types="audio/webm,audio/mp4,audio/ogg",
        asr_max_duration_seconds=60,
    )


@pytest.fixture
def make_client(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import services
    from backend.app import main as app_main
    from backend.app.db import get_connection, run_migrations
    from backend.app.models.domain import CONDITIONS, ERROR_TYPE_IDS, SUBCONDITIONS

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        conn.executemany(
            """
            INSERT INTO admin_assignment_units (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                enabled
            ) VALUES ('long', ?, ?, ?, 0)
            ON CONFLICT(participant_type, condition, subcondition, error_type_id)
            DO UPDATE SET enabled = excluded.enabled
            """,
            [
                (condition, subcondition, error_type_id)
                for condition in CONDITIONS
                for subcondition in SUBCONDITIONS
                for error_type_id in ERROR_TYPE_IDS
            ],
        )
    finally:
        conn.close()

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: TEST_DATE,
    )
    monkeypatch.setattr(
        services.questionnaires,
        "_timestamp_now",
        lambda: "2026-07-02T09:30:00+00:00",
    )
    monkeypatch.setattr(
        services.participant_days,
        "current_shanghai_date",
        lambda: TEST_DATE,
    )

    def _build(
        fake_asr_client: FakeAsrClient,
        *,
        use_real_audio_duration: bool = False,
    ) -> TestClient:
        monkeypatch.setattr(
            app_main,
            "get_asr_client",
            lambda _settings=sqlite_settings: fake_asr_client,
            raising=False,
        )
        if not use_real_audio_duration:
            monkeypatch.setattr(
                app_main,
                "read_audio_duration_seconds",
                lambda *_args, **_kwargs: 1.0,
                raising=False,
            )
        return TestClient(app_main.create_app(settings=sqlite_settings))

    return _build


def login_and_prepare_formal_session(client: TestClient) -> str:
    login_response = client.post(
        "/api/auth/login",
        json={
            "name": "Formal ASR Participant",
            "phone": "19900000001",
            "participant_type": "short",
        },
    )
    assert login_response.status_code == 200

    pretest_response = client.post(
        "/api/pretest/final",
        json=build_pretest_payload(),
    )
    assert pretest_response.status_code == 200

    start_response = client.post(
        "/api/sessions/start",
        json={
            "is_test": False,
            "client_info": {
                "device_type": "desktop",
                "viewport_width": 1440,
                "is_secure_context": True,
                "browser_name": "Chrome",
                "browser_version": "126",
                "microphone_available": True,
                "microphone_permission": "granted",
            },
        },
    )
    assert start_response.status_code == 200
    return start_response.json()["session_id"]


def _post_asr(client: TestClient, *, session_id: str, audio_bytes: bytes) -> TestClient:
    return client.post(
        "/api/asr",
        data={"session_id": session_id},
        files={
            "audio": (
                "turn-1.webm",
                audio_bytes,
                "audio/webm",
            )
        },
    )


def _assert_no_upload_staging_files(settings: Settings) -> None:
    staging_dir = settings.data_dir / ".asr-uploads"
    assert not staging_dir.exists() or not any(staging_dir.iterdir())


def _assert_no_asr_artifacts(settings: Settings, *, session_id: str) -> None:
    from backend.app.db import get_connection

    conn = get_connection(settings)
    try:
        asr_attempt_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM asr_attempts a
            JOIN experiment_sessions s ON s.id = a.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
        operation_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM external_operations o
            JOIN experiment_sessions s ON s.id = o.session_id
            WHERE s.session_uuid = ? AND o.kind = 'asr'
            """,
            (session_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert asr_attempt_count == 0
    assert operation_count == 0
    assert not (settings.data_dir / "audio").exists()
    _assert_no_upload_staging_files(settings)


def test_asr_rejects_oversized_upload_without_provider_or_artifacts(
    make_client,
    sqlite_settings: Settings,
):
    fake_asr_client = FakeAsrClient()
    client = make_client(fake_asr_client)

    with client:
        session_id = login_and_prepare_formal_session(client)
        response = client.post(
            "/api/asr",
            data={"session_id": session_id, "operation_id": "asr-too-large-0001"},
            files={"audio": ("turn.webm", b"x" * 2_049, "audio/webm")},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Audio upload exceeds the 2048 byte limit."
    assert fake_asr_client.calls == []
    _assert_no_asr_artifacts(sqlite_settings, session_id=session_id)


def test_asr_rejects_unsupported_media_without_provider_or_artifacts(
    make_client,
    sqlite_settings: Settings,
):
    fake_asr_client = FakeAsrClient()
    client = make_client(fake_asr_client)

    with client:
        session_id = login_and_prepare_formal_session(client)
        response = client.post(
            "/api/asr",
            data={"session_id": session_id, "operation_id": "asr-media-0001"},
            files={"audio": ("turn.wav", b"wave payload", "audio/wav")},
        )

    assert response.status_code == 415
    assert response.json()["detail"] == "Unsupported audio media type: audio/wav."
    assert fake_asr_client.calls == []
    _assert_no_asr_artifacts(sqlite_settings, session_id=session_id)


def test_asr_ignores_reported_duration_for_valid_container(
    make_client,
):
    fake_asr_client = FakeAsrClient(
        SimpleNamespace(
            status="success",
            provider="tencent",
            text="server-derived duration",
            latency_ms=5,
        )
    )
    client = make_client(fake_asr_client, use_real_audio_duration=True)

    with client:
        session_id = login_and_prepare_formal_session(client)
        response = client.post(
            "/api/asr",
            data={
                "session_id": session_id,
                "operation_id": "asr-duration-0001",
                "duration_seconds": "60.1",
            },
            files={"audio": ("turn.webm", build_webm_audio(1.5), "audio/webm")},
        )

    assert response.status_code == 200
    assert len(fake_asr_client.calls) == 1


@pytest.mark.parametrize("media_type", sorted(AUDIO_CONTAINERS))
def test_asr_derives_and_rejects_over_limit_container_duration(
    make_client,
    sqlite_settings: Settings,
    media_type: str,
):
    filename, build_audio = AUDIO_CONTAINERS[media_type]
    fake_asr_client = FakeAsrClient()
    client = make_client(fake_asr_client, use_real_audio_duration=True)

    with client:
        session_id = login_and_prepare_formal_session(client)
        response = client.post(
            "/api/asr",
            data={
                "session_id": session_id,
                "operation_id": f"asr-derived-duration-{filename}",
                "duration_seconds": "1",
            },
            files={"audio": (filename, build_audio(61), media_type)},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Audio duration exceeds the 60 second limit."
    assert fake_asr_client.calls == []
    _assert_no_asr_artifacts(sqlite_settings, session_id=session_id)


def test_asr_rejects_unparseable_accepted_container_without_artifacts(
    make_client,
    sqlite_settings: Settings,
):
    fake_asr_client = FakeAsrClient()
    client = make_client(fake_asr_client, use_real_audio_duration=True)

    with client:
        session_id = login_and_prepare_formal_session(client)
        response = client.post(
            "/api/asr",
            data={"session_id": session_id, "operation_id": "asr-invalid-container"},
            files={"audio": ("turn.webm", b"not a WebM container", "audio/webm")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Audio duration could not be determined."
    assert fake_asr_client.calls == []
    _assert_no_asr_artifacts(sqlite_settings, session_id=session_id)


@pytest.mark.parametrize(
    "audio_bytes",
    [
        build_webm_audio(0),
        build_mp4_audio(1.5),
    ],
    ids=["missing-positive-duration", "declared-container-mismatch"],
)
def test_asr_rejects_invalid_server_duration_evidence(
    make_client,
    sqlite_settings: Settings,
    audio_bytes: bytes,
):
    fake_asr_client = FakeAsrClient()
    client = make_client(fake_asr_client, use_real_audio_duration=True)

    with client:
        session_id = login_and_prepare_formal_session(client)
        response = client.post(
            "/api/asr",
            data={"session_id": session_id, "operation_id": "asr-invalid-evidence"},
            files={"audio": ("turn.webm", audio_bytes, "audio/webm")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Audio duration could not be determined."
    assert fake_asr_client.calls == []
    _assert_no_asr_artifacts(sqlite_settings, session_id=session_id)


def test_runtime_config_exposes_backend_recording_limit(make_client):
    client = make_client(FakeAsrClient())

    with client:
        response = client.get("/api/runtime-config")

    assert response.status_code == 200
    assert response.json() == {"asr_max_duration_seconds": 60}


@pytest.mark.asyncio
async def test_asr_request_cap_stops_chunked_body_before_multipart_endpoint(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main
    from backend.app.db import get_connection, run_migrations

    migration_conn = get_connection(sqlite_settings)
    try:
        run_migrations(migration_conn)
    finally:
        migration_conn.close()

    limited_settings = sqlite_settings.model_copy(
        update={"asr_max_request_bytes": 128},
    )
    stage_calls = 0
    fake_asr_client = FakeAsrClient()
    original_stage = app_main._stage_audio_upload

    def tracking_stage(*args: object, **kwargs: object):
        nonlocal stage_calls
        stage_calls += 1
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(app_main, "_stage_audio_upload", tracking_stage)
    monkeypatch.setattr(
        app_main,
        "get_asr_client",
        lambda _settings=limited_settings: fake_asr_client,
    )
    app = app_main.create_app(settings=limited_settings)
    boundary = "task9-boundary"
    multipart_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="session_id"\r\n\r\n'
        "untrusted-session\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="turn.webm"\r\n'
        "Content-Type: audio/webm\r\n\r\n"
    ).encode() + b"x" * 256 + f"\r\n--{boundary}--\r\n".encode()
    chunks = [multipart_body[:96], multipart_body[96:192], multipart_body[192:]]
    receive_calls = 0
    sent_messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal receive_calls
        receive_calls += 1
        chunk = chunks[receive_calls - 1]
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": receive_calls < len(chunks),
        }

    async def send(message: dict[str, object]) -> None:
        sent_messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/asr",
            "raw_path": b"/api/asr",
            "query_string": b"",
            "headers": [
                (
                    b"content-type",
                    f"multipart/form-data; boundary={boundary}".encode(),
                )
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )

    response_start = next(
        message for message in sent_messages if message["type"] == "http.response.start"
    )
    assert response_start["status"] == 413
    assert receive_calls == 2
    assert stage_calls == 0
    assert fake_asr_client.calls == []
    assert not (sqlite_settings.data_dir / "audio").exists()
    _assert_no_upload_staging_files(sqlite_settings)
    conn = get_connection(sqlite_settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM external_operations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM asr_attempts").fetchone()[0] == 0
    finally:
        conn.close()


def test_asr_saves_audio_even_on_failure(
    make_client,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection

    fake_asr_client = FakeAsrClient(
        SimpleNamespace(
            status="failed",
            provider="tencent",
            text=None,
            latency_ms=87,
        )
    )

    client = make_client(fake_asr_client)
    audio_bytes = b"fake formal audio payload"

    with client:
        session_id = login_and_prepare_formal_session(client)
        response = _post_asr(client, session_id=session_id, audio_bytes=audio_bytes)

    assert response.status_code == 200
    payload = response.json()
    assert payload["asr_status"] == "failed"

    conn = get_connection(sqlite_settings)
    try:
        attempts = conn.execute(
            """
            SELECT turn_index, asr_status, user_audio_path, user_audio_sha256
            FROM asr_attempts a
            JOIN experiment_sessions s ON s.id = a.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchall()
        turn_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    audio_path = sqlite_settings.data_dir / attempts[0]["user_audio_path"]
    assert audio_path.exists()
    assert audio_path.read_bytes() == audio_bytes
    assert [dict(row) for row in attempts] == [
        {
            "turn_index": 1,
            "asr_status": "failed",
            "user_audio_path": str(attempts[0]["user_audio_path"]),
            "user_audio_sha256": sha256(audio_bytes).hexdigest(),
        }
    ]
    assert turn_count == 0
    _assert_no_upload_staging_files(sqlite_settings)


def test_asr_audio_path_uses_participant_day_turn_session_name(
    make_client,
    sqlite_settings: Settings,
):
    fake_asr_client = FakeAsrClient(
        SimpleNamespace(
            status="success",
            provider="tencent",
            text="canonical path transcript",
            latency_ms=87,
        )
    )

    client = make_client(fake_asr_client)

    with client:
        session_id = login_and_prepare_formal_session(client)
        response = _post_asr(
            client,
            session_id=session_id,
            audio_bytes=b"canonical audio payload",
        )

    assert response.status_code == 200
    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        stored_path = conn.execute(
            """
            SELECT a.user_audio_path
            FROM asr_attempts a
            JOIN experiment_sessions s ON s.id = a.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()["user_audio_path"]
    finally:
        conn.close()
    assert stored_path == (
        f"audio/Formal_ASR_Participant_19900000001_short_day_1_turn_1_{session_id}.webm"
    )
    assert (sqlite_settings.data_dir / stored_path).exists()


def test_asr_retry_for_same_turn_uses_collision_safe_canonical_name(
    make_client,
    sqlite_settings: Settings,
):
    fake_asr_client = FakeAsrClient(
        SimpleNamespace(
            status="failed",
            provider="tencent",
            text=None,
            latency_ms=10,
        ),
        SimpleNamespace(
            status="failed",
            provider="tencent",
            text=None,
            latency_ms=11,
        ),
    )

    client = make_client(fake_asr_client)

    with client:
        session_id = login_and_prepare_formal_session(client)
        first_response = _post_asr(
            client,
            session_id=session_id,
            audio_bytes=b"first failed audio",
        )
        second_response = _post_asr(
            client,
            session_id=session_id,
            audio_bytes=b"second failed audio",
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        stored_paths = conn.execute(
            """
            SELECT a.user_audio_path
            FROM asr_attempts a
            JOIN experiment_sessions s ON s.id = a.session_id
            WHERE s.session_uuid = ?
            ORDER BY a.attempt_no
            """,
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    first_path, second_path = [str(row["user_audio_path"]) for row in stored_paths]
    assert first_path == (
        f"audio/Formal_ASR_Participant_19900000001_short_day_1_turn_1_{session_id}.webm"
    )
    assert second_path == (
        f"audio/Formal_ASR_Participant_19900000001_short_day_1_turn_1_{session_id}_retry_2.webm"
    )
    assert (sqlite_settings.data_dir / first_path).read_bytes() == b"first failed audio"
    assert (sqlite_settings.data_dir / second_path).read_bytes() == b"second failed audio"


def test_asr_retry_limit_interrupts_session_without_text_fallback(
    make_client,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection

    fake_asr_client = FakeAsrClient(
        *[
            SimpleNamespace(
                status="failed",
                provider="tencent",
                text=None,
                latency_ms=40 + attempt,
            )
            for attempt in range(sqlite_settings.asr_max_retry_per_turn)
        ]
    )
    client = make_client(fake_asr_client)

    with client:
        session_id = login_and_prepare_formal_session(client)
        for _ in range(sqlite_settings.asr_max_retry_per_turn):
            response = _post_asr(
                client,
                session_id=session_id,
                audio_bytes=b"retry audio payload",
            )
            assert response.status_code == 200

        session_response = client.get(f"/api/sessions/{session_id}")
        text_fallback_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed fallback should stay blocked",
            },
        )

    assert session_response.status_code == 200
    assert session_response.json()["status"] == "interrupted"
    assert text_fallback_response.status_code == 409
    assert text_fallback_response.json() == {
        "detail": "Session is not active: interrupted."
    }

    conn = get_connection(sqlite_settings)
    try:
        repeated_failure_flags = conn.execute(
            """
            SELECT COUNT(*)
            FROM session_risk_flags f
            JOIN experiment_sessions s ON s.id = f.session_id
            WHERE s.session_uuid = ? AND f.flag = 'asr_repeated_failure'
            """,
            (session_id,),
        ).fetchone()[0]
        turn_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert repeated_failure_flags == 1
    assert turn_count == 0


def test_asr_httpx_timeout_maps_to_timeout_status_and_blocks_text_fallback(
    make_client,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection

    fake_asr_client = FakeAsrClient(httpx.TimeoutException("simulated timeout"))
    client = make_client(fake_asr_client)
    audio_bytes = b"timeout audio payload"

    with client:
        session_id = login_and_prepare_formal_session(client)
        asr_response = _post_asr(
            client,
            session_id=session_id,
            audio_bytes=audio_bytes,
        )
        text_fallback_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed fallback should remain blocked",
            },
        )

    assert asr_response.status_code == 200
    asr_payload = asr_response.json()
    assert asr_payload["asr_status"] == "timeout"
    assert asr_payload["asr_text"] is None

    assert text_fallback_response.status_code == 400
    assert text_fallback_response.json() == {
        "detail": "Formal sessions require voice input."
    }

    conn = get_connection(sqlite_settings)
    try:
        attempts = conn.execute(
            """
            SELECT turn_index, asr_status, user_audio_path, user_audio_sha256
            FROM asr_attempts a
            JOIN experiment_sessions s ON s.id = a.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchall()
        turn_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    audio_path = sqlite_settings.data_dir / attempts[0]["user_audio_path"]
    assert audio_path.exists()
    assert audio_path.read_bytes() == audio_bytes
    assert [dict(row) for row in attempts] == [
        {
            "turn_index": 1,
            "asr_status": "timeout",
            "user_audio_path": str(attempts[0]["user_audio_path"]),
            "user_audio_sha256": sha256(audio_bytes).hexdigest(),
        }
    ]
    assert turn_count == 0


def test_formal_turn_requires_voice_mode_after_asr_success(make_client):
    fake_asr_client = FakeAsrClient(
        SimpleNamespace(
            status="success",
            provider="tencent",
            text="formal transcript from ASR",
            latency_ms=123,
        )
    )
    client = make_client(fake_asr_client)

    with client:
        session_id = login_and_prepare_formal_session(client)
        asr_response = _post_asr(
            client,
            session_id=session_id,
            audio_bytes=b"success audio payload",
        )
        assert asr_response.status_code == 200

        turn_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed fallback should be rejected",
            },
        )

    assert turn_response.status_code == 400
    assert turn_response.json() == {
        "detail": "Formal sessions require voice input."
    }


def test_voice_turn_persists_asr_metadata_without_serving_audio_statically(
    make_client,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection
    from backend.app.services.sessions import ProviderRouter

    fake_asr_client = FakeAsrClient(
        SimpleNamespace(
            status="success",
            provider="tencent",
            text="voice transcript approved by ASR",
            latency_ms=222,
        )
    )
    client = make_client(fake_asr_client)

    async def _fake_provider_response(*args: object, **kwargs: object) -> ProviderResponse:
        return ProviderResponse(
            text="assistant reply",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[
                ProviderAttempt(
                    route="chat",
                    provider="yi-zhan",
                    model="gpt-5.1",
                    status="success",
                    latency_ms=9,
                )
            ],
            used_local_fallback=False,
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _fake_provider_response)

    with client:
        session_id = login_and_prepare_formal_session(client)
        asr_response = _post_asr(
            client,
            session_id=session_id,
            audio_bytes=b"voice submission audio",
        )
        assert asr_response.status_code == 200
        asr_payload = asr_response.json()

        turn_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "voice",
                "asr_result_id": asr_payload["asr_result_id"],
            },
        )

    conn = get_connection(sqlite_settings)
    try:
        turn_row = conn.execute(
            """
            SELECT
                t.user_audio_path,
                t.user_audio_sha256,
                t.asr_provider,
                t.asr_status,
                t.asr_text,
                t.asr_latency_ms
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    audio_get_response = client.get(f"/{turn_row['user_audio_path']}")
    assert turn_response.status_code == 200
    assert audio_get_response.status_code == 404
    assert dict(turn_row) == {
        "user_audio_path": str(turn_row["user_audio_path"]),
        "user_audio_sha256": str(turn_row["user_audio_sha256"]),
        "asr_provider": "tencent",
        "asr_status": "success",
        "asr_text": asr_payload["asr_text"],
        "asr_latency_ms": 222,
    }


def test_api_asr_releases_write_lock_during_transcription(
    make_client,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, transaction

    asr_started = Event()
    release_asr = Event()

    class LockProbeAsrClient(FakeAsrClient):
        def transcribe(self, **kwargs: object) -> SimpleNamespace:
            asr_started.set()
            assert release_asr.wait(timeout=5)
            return super().transcribe(**kwargs)

    fake_asr_client = LockProbeAsrClient(
        SimpleNamespace(
            status="success",
            provider="tencent",
            text="unlocked ASR transcript",
            latency_ms=12,
        )
    )
    client = make_client(fake_asr_client)

    with client:
        session_id = login_and_prepare_formal_session(client)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                "/api/asr",
                data={
                    "session_id": session_id,
                    "operation_id": "asr-lock-probe-0001",
                },
                files={"audio": ("turn.webm", b"lock probe audio", "audio/webm")},
            )
            assert asr_started.wait(timeout=5)
            probe_conn = get_connection(sqlite_settings)
            try:
                probe_conn.execute("PRAGMA busy_timeout = 0")
                with transaction(probe_conn):
                    probe_conn.execute(
                        "INSERT OR REPLACE INTO admin_global_controls (key, value) VALUES (?, ?)",
                        ("asr_lock_probe", "committed"),
                    )
            finally:
                probe_conn.close()
                release_asr.set()
            response = future.result(timeout=5)

    assert response.status_code == 200


def test_asr_stale_finalization_removes_unreferenced_audio(
    make_client,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection

    asr_started = Event()
    release_asr = Event()

    class BlockingAsrClient(FakeAsrClient):
        def transcribe(self, **kwargs: object) -> SimpleNamespace:
            asr_started.set()
            assert release_asr.wait(timeout=5)
            return super().transcribe(**kwargs)

    fake_asr_client = BlockingAsrClient(
        SimpleNamespace(
            status="success",
            provider="tencent",
            text="stale transcript",
            latency_ms=12,
        )
    )
    client = make_client(fake_asr_client)

    with client:
        session_id = login_and_prepare_formal_session(client)
        audio_path = sqlite_settings.data_dir / (
            f"audio/Formal_ASR_Participant_19900000001_short_day_1_turn_1_{session_id}.webm"
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                "/api/asr",
                data={"session_id": session_id, "operation_id": "asr-stale-0001"},
                files={"audio": ("turn.webm", b"stale audio", "audio/webm")},
            )
            assert asr_started.wait(timeout=5)
            assert audio_path.exists()
            conn = get_connection(sqlite_settings)
            try:
                conn.execute(
                    "UPDATE experiment_sessions SET status = 'interrupted' WHERE session_uuid = ?",
                    (session_id,),
                )
            finally:
                conn.close()
                release_asr.set()
            response = future.result(timeout=5)

    assert response.status_code == 409
    assert not audio_path.exists()
    conn = get_connection(sqlite_settings)
    try:
        attempt_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM asr_attempts a
            JOIN experiment_sessions s ON s.id = a.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
        operation_status = conn.execute(
            "SELECT status FROM external_operations WHERE operation_id = ?",
            ("asr-stale-0001",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert attempt_count == 0
    assert operation_status == "failed"
    _assert_no_upload_staging_files(sqlite_settings)


def test_duplicate_succeeded_asr_operation_replays_without_transcription(
    make_client,
    sqlite_settings: Settings,
):
    fake_asr_client = FakeAsrClient(
        SimpleNamespace(
            status="success",
            provider="tencent",
            text="idempotent ASR transcript",
            latency_ms=25,
        )
    )
    client = make_client(fake_asr_client)
    form = {
        "operation_id": "asr-idempotency-0001",
    }
    files = {"audio": ("turn.webm", b"same idempotent audio", "audio/webm")}

    with client:
        session_id = login_and_prepare_formal_session(client)
        form["session_id"] = session_id
        first = client.post("/api/asr", data=form, files=files)
        second = client.post("/api/asr", data=form, files=files)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(fake_asr_client.calls) == 1
    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        operation_row = conn.execute(
            """
            SELECT result_entity_id, result_json
            FROM external_operations
            WHERE operation_id = ?
            """,
            ("asr-idempotency-0001",),
        ).fetchone()
        asr_row = conn.execute(
            "SELECT user_audio_path FROM asr_attempts WHERE id = ?",
            (operation_row["result_entity_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert operation_row["result_entity_id"] is not None
    assert "idempotent ASR transcript" not in operation_row["result_json"]
    assert asr_row["user_audio_path"] not in operation_row["result_json"]
    _assert_no_upload_staging_files(sqlite_settings)


def test_asr_operation_id_reuse_with_different_audio_is_rejected(
    make_client,
):
    fake_asr_client = FakeAsrClient(
        SimpleNamespace(
            status="success",
            provider="tencent",
            text="first transcript",
            latency_ms=25,
        )
    )
    client = make_client(fake_asr_client)

    with client:
        session_id = login_and_prepare_formal_session(client)
        first = client.post(
            "/api/asr",
            data={"session_id": session_id, "operation_id": "asr-key-reuse-0001"},
            files={"audio": ("turn.webm", b"first audio", "audio/webm")},
        )
        second = client.post(
            "/api/asr",
            data={"session_id": session_id, "operation_id": "asr-key-reuse-0001"},
            files={"audio": ("turn.webm", b"different audio", "audio/webm")},
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "idempotency_key_reused"
    assert len(fake_asr_client.calls) == 1


def test_duplicate_pending_asr_operation_returns_stable_pending_state(
    make_client,
):
    asr_started = Event()
    release_asr = Event()

    class BlockingAsrClient(FakeAsrClient):
        def transcribe(self, **kwargs: object) -> SimpleNamespace:
            asr_started.set()
            assert release_asr.wait(timeout=5)
            return super().transcribe(**kwargs)

    fake_asr_client = BlockingAsrClient(
        SimpleNamespace(
            status="success",
            provider="tencent",
            text="pending transcript",
            latency_ms=10,
        )
    )
    client = make_client(fake_asr_client)
    with client:
        session_id = login_and_prepare_formal_session(client)
        form = {
            "session_id": session_id,
            "operation_id": "asr-pending-0001",
        }
        files = {"audio": ("turn.webm", b"pending audio", "audio/webm")}
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(client.post, "/api/asr", data=form, files=files)
            assert asr_started.wait(timeout=5)
            duplicate = client.post("/api/asr", data=form, files=files)
            release_asr.set()
            completed = future.result(timeout=5)

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == {
        "code": "external_operation_pending",
        "status": "pending",
        "operation_id": "asr-pending-0001",
        "retryable": True,
        "retry_after_ms": 250,
    }
    assert completed.status_code == 200
    assert len(fake_asr_client.calls) == 1


def test_asr_api_log_records_session_turn_and_formal_scope(
    make_client,
    sqlite_settings: Settings,
):
    fake_asr_client = FakeAsrClient(
        SimpleNamespace(
            status="success",
            provider="tencent",
            text="scoped transcript",
            latency_ms=18,
        )
    )
    client = make_client(fake_asr_client)
    with client:
        session_id = login_and_prepare_formal_session(client)
        response = client.post(
            "/api/asr",
            data={"session_id": session_id, "operation_id": "asr-scope-0001"},
            files={"audio": ("turn.webm", b"scoped audio", "audio/webm")},
        )

    assert response.status_code == 200
    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT l.session_id, l.turn_index, l.is_test, s.session_uuid
            FROM api_call_logs l
            JOIN experiment_sessions s ON s.id = l.session_id
            WHERE l.route = 'asr' AND l.request_id LIKE ?
            """,
            (f"{session_id}-asr-turn-%",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["session_uuid"] == session_id
    assert row["turn_index"] == 1
    assert row["is_test"] == 0
