from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.models.api import (
    normalize_enrollment_name,
    normalize_enrollment_phone,
)
from backend.app.security import read_signed_session, sign_session_payload
from backend.app.settings import Settings


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "login.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="test-secret-key",
    )


@pytest.fixture
def client(sqlite_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from backend.app import services
    from backend.app.main import create_app

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-02",
    )

    return TestClient(create_app(settings=sqlite_settings))


def force_next_assignment_long(sqlite_settings: Settings) -> None:
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
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(participant_type, condition, subcondition, error_type_id)
            DO UPDATE SET enabled = excluded.enabled
            """,
            [
                ("short", condition, subcondition, error_type_id, 0)
                for condition in CONDITIONS
                for subcondition in SUBCONDITIONS
                for error_type_id in ERROR_TYPE_IDS
            ],
        )
    finally:
        conn.close()


def read_client_session_payload(
    client: TestClient,
    sqlite_settings: Settings,
) -> dict[str, object]:
    session_cookie = client.cookies.get(sqlite_settings.session_cookie_name)
    assert session_cookie is not None
    session_payload = read_signed_session(
        session_cookie.strip('"'),
        sqlite_settings.app_secret_key,
    )
    assert session_payload is not None
    return session_payload


def test_login_response_masks_phone(client: TestClient):
    with client:
        response = client.post(
            "/api/auth/login",
            json={
                "name": "Example Short",
                "phone": "13800000012",
            },
        )

        me_response = client.get("/api/me")

    assert response.status_code == 200
    payload = response.json()
    assert "phone" not in payload
    assert payload["masked_phone"] == "138****0012"
    assert "phone_hash" not in payload
    assert payload["participant_type"] in {"short", "long"}
    assert payload["target_days"] == (
        1 if payload["participant_type"] == "short" else 3
    )
    assert payload["current_day"]["day_index"] == 1
    assert payload["current_day"]["calendar_date"] == "2026-07-02"
    assert payload["current_day"]["status"] == "not_started"
    hidden_fields = {
        "condition",
        "subcondition",
        "topic_key",
        "error_type_id",
    }
    assert hidden_fields.isdisjoint(payload)
    assert me_response.status_code == 200
    assert me_response.json() == payload


def test_login_identity_uses_name_and_phone(client: TestClient):
    with client:
        first_response = client.post(
            "/api/auth/login",
            json={
                "name": "Same Name",
                "phone": "13800001001",
            },
        )
        second_response = client.post(
            "/api/auth/login",
            json={
                "name": "Same Name",
                "phone": "13800001002",
            },
        )
        first_again_response = client.post(
            "/api/auth/login",
            json={
                "name": "Same Name",
                "phone": "13800001001",
            },
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_again_response.status_code == 200
    assert first_response.json()["participant_id"] != second_response.json()["participant_id"]
    assert first_response.json()["participant_id"] == first_again_response.json()["participant_id"]


def test_login_session_cookie_includes_attempt_identity(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        response = client.post(
            "/api/auth/login",
            json={
                "name": "Cookie Attempt",
                "phone": "13800000011",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    session_payload = read_client_session_payload(client, sqlite_settings)
    assert session_payload["participant_id"] == payload["participant_id"]
    assert session_payload["attempt_id"] == payload["attempt_id"]
    assert "phone_hash" in session_payload


def test_relogin_restores_recoverable_attempt_and_cookie_identity(
    client: TestClient,
    sqlite_settings: Settings,
):
    credentials = {
        "name": "Recoverable Login",
        "phone": "13800000013",
    }

    with client:
        first_response = client.post("/api/auth/login", json=credentials)
        first_cookie = read_client_session_payload(client, sqlite_settings)
        second_response = client.post("/api/auth/login", json=credentials)
        second_cookie = read_client_session_payload(client, sqlite_settings)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["attempt_id"] == first_response.json()["attempt_id"]
    assert second_cookie["attempt_id"] == first_cookie["attempt_id"]

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        attempt_rows = conn.execute(
            """
            SELECT id, status, valid_for_export, blocked_reason
            FROM participant_attempts
            WHERE participant_id = ?
            ORDER BY attempt_no
            """,
            (first_response.json()["participant_id"],),
        ).fetchall()
    finally:
        conn.close()

    assert len(attempt_rows) == 1
    assert attempt_rows[0]["status"] == "active"
    assert attempt_rows[0]["valid_for_export"] == 1
    assert attempt_rows[0]["blocked_reason"] is None


def test_login_ignores_assignment_override_fields(client: TestClient):
    with client:
        response = client.post(
            "/api/auth/login",
            json={
                "name": "Override Attempt",
                "phone": "13800000014",
                "condition": "tool",
                "subcondition": "execution",
                "topic_key": "fake-topic",
                "error_type_id": "system_failure",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert "phone" not in payload
    assert "phone_hash" not in payload
    assert "topic_key" not in payload
    assert "error_type_id" not in payload
    assert "condition" not in payload
    assert "subcondition" not in payload


def test_me_requires_login(client: TestClient):
    with client:
        response = client.get("/api/me")

    assert response.status_code == 401


def test_login_requires_non_empty_app_secret_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from backend.app import services
    from backend.app.main import create_app

    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'missing-secret.db'}",
        app_secret_key="",
    )
    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-02",
    )

    client = TestClient(create_app(settings=settings))

    with client:
        response = client.post(
            "/api/auth/login",
            json={
                "name": "Missing Secret",
                "phone": "13800000015",
            },
        )

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Server configuration error: participant sessions require app_secret_key."
    }


def test_me_rejects_malformed_session_cookie(client: TestClient):
    client.cookies.set("aitrust_v2_sid", "not-a-valid-session-token")

    with client:
        response = client.get("/api/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid session."}


def test_login_request_does_not_require_participant_type(client: TestClient):
    with client:
        response = client.post(
            "/api/auth/login",
            json={"name": "李四", "phone": "13800000001"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["participant_type"] in {"short", "long"}


@pytest.mark.parametrize(
    ("request_payload", "invalid_field"),
    [
        ({"phone": "13800138000"}, "name"),
        ({"name": "张三"}, "phone"),
        ({"name": "", "phone": "13800138000"}, "name"),
        ({"name": "张\x00三", "phone": "13800138000"}, "name"),
        ({"name": "张" * 65, "phone": "13800138000"}, "name"),
        ({"name": "张三<script>", "phone": "13800138000"}, "name"),
        ({"name": "张三", "phone": ""}, "phone"),
        ({"name": "张三", "phone": "1380013800"}, "phone"),
        ({"name": "张三", "phone": "12800138000"}, "phone"),
        ({"name": "张三", "phone": "1380013800x"}, "phone"),
        ({"name": "张三", "phone": "1380013800１"}, "phone"),
        ({"name": "张三", "phone": "1380013800١"}, "phone"),
    ],
)
def test_login_rejects_invalid_enrollment_fields_before_assignment(
    client: TestClient,
    sqlite_settings: Settings,
    request_payload: dict[str, str],
    invalid_field: str,
) -> None:
    from backend.app.db import get_connection

    with client:
        response = client.post("/api/auth/login", json=request_payload)

    assert response.status_code == 422
    assert any(
        detail["loc"][-1] == invalid_field
        for detail in response.json()["detail"]
    )

    conn = get_connection(sqlite_settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM participant_attempts").fetchone()[0] == 0
    finally:
        conn.close()


def test_login_invalid_phone_returns_chinese_participant_message(client: TestClient):
    response = client.post(
        "/api/auth/login",
        json={"name": "测试用户", "phone": "12345"},
    )

    assert response.status_code == 422
    payload = response.json()
    messages = [error["msg"] for error in payload["detail"]]
    assert "Value error, 请输入有效的中国大陆手机号码。" in messages
    assert all("Phone must" not in message for message in messages)


@pytest.mark.parametrize(
    ("normalizer", "value", "expected_message"),
    [
        (normalize_enrollment_name, "A\x00B", "姓名不能包含控制字符。"),
        (normalize_enrollment_name, "A", "姓名长度必须为 2 到 64 个字符。"),
        (normalize_enrollment_name, "123", "姓名必须至少包含一个文字字符。"),
        (normalize_enrollment_name, "测试🙂", "姓名包含不支持的字符。"),
        (normalize_enrollment_phone, "１２3", "手机号只能使用半角数字。"),
        (normalize_enrollment_phone, "13800000000\x00", "手机号不能包含控制字符。"),
        (normalize_enrollment_phone, "12345", "请输入有效的中国大陆手机号码。"),
    ],
)
def test_enrollment_normalizers_return_chinese_errors(
    normalizer,
    value,
    expected_message,
):
    with pytest.raises(ValueError) as exc_info:
        normalizer(value)
    assert str(exc_info.value) == expected_message


def test_enrollment_normalizes_chinese_name_and_canonical_phone_identity(
    client: TestClient,
    sqlite_settings: Settings,
) -> None:
    from backend.app.db import get_connection

    with client:
        formatted_response = client.post(
            "/api/auth/login",
            json={
                "name": "\u3000欧阳  娜娜·阿明\u3000",
                "phone": "+86 138-0013-8000",
            },
        )
        canonical_response = client.post(
            "/api/auth/login",
            json={"name": "欧阳 娜娜·阿明", "phone": "13800138000"},
        )

    assert formatted_response.status_code == 200
    assert canonical_response.status_code == 200
    assert formatted_response.json()["name"] == "欧阳 娜娜·阿明"
    assert formatted_response.json()["participant_id"] == canonical_response.json()[
        "participant_id"
    ]

    conn = get_connection(sqlite_settings)
    try:
        rows = conn.execute("SELECT name, phone FROM participants").fetchall()
    finally:
        conn.close()

    assert [dict(row) for row in rows] == [
        {"name": "欧阳 娜娜·阿明", "phone": "13800138000"}
    ]


def test_login_ignores_frontend_participant_type(client: TestClient):
    with client:
        response = client.post(
            "/api/auth/login",
            json={"name": "王五", "phone": "13800000002", "participant_type": "long"},
        )

    assert response.status_code == 200
    assert response.json()["participant_type"] in {"short", "long"}


def test_login_creates_current_attempt_record(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection
    from backend.app.repositories.participants import (
        LEGACY_COMPAT_CONDITION,
        LEGACY_COMPAT_ERROR_TYPE_ID,
        LEGACY_COMPAT_PARTICIPANT_TYPE,
        LEGACY_COMPAT_SUBCONDITION,
        LEGACY_COMPAT_TARGET_DAYS,
        LEGACY_COMPAT_TOPIC_KEY,
    )

    force_next_assignment_long(sqlite_settings)

    with client:
        response = client.post(
            "/api/auth/login",
            json={"name": "Attempt Login", "phone": "13800000003"},
        )

    assert response.status_code == 200
    payload = response.json()

    conn = get_connection(sqlite_settings)
    try:
        participant_row = conn.execute(
            """
            SELECT id, participant_type, condition, subcondition, topic_key,
                   error_type_id, target_days, current_attempt_id
            FROM participants
            WHERE id = ?
            """,
            (payload["participant_id"],),
        ).fetchone()
        attempt_row = conn.execute(
            """
            SELECT *
            FROM participant_attempts
            WHERE id = ?
            """,
            (participant_row["current_attempt_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert participant_row["current_attempt_id"] is not None
    assert attempt_row is not None
    assert payload["attempt_id"] == attempt_row["id"]
    assert payload["attempt_no"] == 1
    assert participant_row["participant_type"] == LEGACY_COMPAT_PARTICIPANT_TYPE
    assert participant_row["condition"] == LEGACY_COMPAT_CONDITION
    assert participant_row["subcondition"] == LEGACY_COMPAT_SUBCONDITION
    assert participant_row["topic_key"] == LEGACY_COMPAT_TOPIC_KEY
    assert participant_row["error_type_id"] == LEGACY_COMPAT_ERROR_TYPE_ID
    assert participant_row["target_days"] == LEGACY_COMPAT_TARGET_DAYS
    assert payload["participant_type"] == attempt_row["participant_type"]
    assert payload["target_days"] == attempt_row["target_days"]
    assert attempt_row["status"] == "active"


def test_existing_participant_without_current_attempt_logs_in_with_new_attempt(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import insert_participant_identity
    from backend.app.security import hash_phone, normalize_phone

    normalized_phone = normalize_phone("13800000005")
    phone_hash = hash_phone(normalized_phone)

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Existing Identity",
                phone=normalized_phone,
                phone_hash=phone_hash,
            )
    finally:
        conn.close()

    with client:
        response = client.post(
            "/api/auth/login",
            json={"name": "Existing Identity", "phone": "13800000005"},
        )

    assert response.status_code == 200
    payload = response.json()

    conn = get_connection(sqlite_settings)
    try:
        participant_row = conn.execute(
            """
            SELECT current_attempt_id
            FROM participants
            WHERE id = ?
            """,
            (participant_id,),
        ).fetchone()
        attempt_row = conn.execute(
            """
            SELECT *
            FROM participant_attempts
            WHERE id = ?
            """,
            (participant_row["current_attempt_id"],),
        ).fetchone()
        participant_days = conn.execute(
            """
            SELECT *
            FROM participant_days
            WHERE participant_id = ?
            ORDER BY day_index
            """,
            (participant_id,),
        ).fetchall()
    finally:
        conn.close()

    assert participant_row["current_attempt_id"] is not None
    assert attempt_row is not None
    assert payload["participant_id"] == participant_id
    assert payload["attempt_id"] == attempt_row["id"]
    assert payload["attempt_no"] == 1
    assert len(participant_days) == int(attempt_row["target_days"])
    assert {int(row["attempt_id"]) for row in participant_days} == {int(attempt_row["id"])}


def test_existing_identity_with_old_attempt_days_gets_new_attempt_scoped_days(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.models.domain import CONDITIONS, ERROR_TYPE_IDS, SUBCONDITIONS
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import insert_participant_identity
    from backend.app.security import hash_phone, normalize_phone

    normalized_phone = normalize_phone("13800000008")
    phone_hash = hash_phone(normalized_phone)

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
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Legacy Attempt Days",
                phone=normalized_phone,
                phone_hash=phone_hash,
            )
            old_attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="short",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=1,
                status="completed",
            )
            set_current_attempt(
                conn,
                participant_id=participant_id,
                attempt_id=old_attempt_id,
            )
            conn.execute(
                """
                INSERT INTO participant_days (
                    participant_id,
                    day_index,
                    calendar_date,
                    status,
                    completed_at,
                    attempt_id
                ) VALUES (?, 1, '2026-06-30', 'completed', '2026-06-30T09:00:00+08:00', ?)
                """,
                (participant_id, old_attempt_id),
            )
            conn.execute(
                """
                UPDATE participants
                SET current_attempt_id = NULL
                WHERE id = ?
                """,
                (participant_id,),
            )
    finally:
        conn.close()

    with client:
        response = client.post(
            "/api/auth/login",
            json={"name": "Legacy Attempt Days", "phone": "13800000008"},
        )

    assert response.status_code == 200
    payload = response.json()

    conn = get_connection(sqlite_settings)
    try:
        participant_row = conn.execute(
            """
            SELECT current_attempt_id
            FROM participants
            WHERE id = ?
            """,
            (participant_id,),
        ).fetchone()
        attempts = conn.execute(
            """
            SELECT id, attempt_no, status
            FROM participant_attempts
            WHERE participant_id = ?
            ORDER BY attempt_no
            """,
            (participant_id,),
        ).fetchall()
        participant_days = conn.execute(
            """
            SELECT attempt_id, day_index, calendar_date, status, completed_at
            FROM participant_days
            WHERE participant_id = ?
            ORDER BY attempt_id, day_index, id
            """,
            (participant_id,),
        ).fetchall()
    finally:
        conn.close()

    assert participant_row["current_attempt_id"] == payload["attempt_id"]
    assert len(attempts) == 2
    assert [int(row["attempt_no"]) for row in attempts] == [1, 2]
    assert payload["attempt_id"] != old_attempt_id
    assert payload["attempt_no"] == 2
    assert [dict(row) for row in participant_days] == [
        {
            "attempt_id": old_attempt_id,
            "day_index": 1,
            "calendar_date": "2026-06-30",
            "status": "completed",
            "completed_at": "2026-06-30T09:00:00+08:00",
        },
        {
            "attempt_id": payload["attempt_id"],
            "day_index": 1,
            "calendar_date": "2026-07-02",
            "status": "not_started",
            "completed_at": None,
        },
    ]


def test_participant_view_uses_current_attempt_status_and_requires_current_attempt(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import services
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import (
        create_participant_days,
        get_participant_by_id,
        insert_participant,
        set_attempt_id_for_participant_days,
    )

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-02",
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant(
                conn,
                name="Status Source",
                phone="13800000004",
                phone_hash="hash-status-source",
                participant_type="short",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=1,
            )
            create_participant_days(
                conn,
                participant_id=participant_id,
                target_days=1,
                start_date=date.fromisoformat("2026-07-02"),
            )
            attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="short",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=1,
                status="blocked",
                blocked_reason="operator_note: phone=13800000000; secret=do-not-expose",
            )
            set_current_attempt(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )
            set_attempt_id_for_participant_days(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )
            conn.execute(
                """
                UPDATE participants
                SET current_status = 'active'
                WHERE id = ?
                """,
                (participant_id,),
            )

        participant_row = get_participant_by_id(conn, participant_id=participant_id)
        assert participant_row is not None

        participant_view = services.participants.build_participant_view(
            conn,
            participant_row=participant_row,
        )

        conn.execute(
            """
            UPDATE participants
            SET current_attempt_id = NULL
            WHERE id = ?
            """,
            (participant_id,),
        )
        participant_row_without_attempt = get_participant_by_id(
            conn,
            participant_id=participant_id,
        )
        assert participant_row_without_attempt is not None

        with pytest.raises(LookupError, match="Participant has no current attempt."):
            services.participants.build_participant_view(
                conn,
                participant_row=participant_row_without_attempt,
            )
    finally:
        conn.close()

    assert participant_view.current_status == "blocked"
    assert participant_view.participation_state == "blocked"
    assert participant_view.participation_message == "您当前无法继续实验，请联系研究人员。"
    assert "operator_note" not in participant_view.participation_message
    assert "13800000000" not in participant_view.participation_message
    assert "do-not-expose" not in participant_view.participation_message


def test_participant_view_treats_converted_short_as_completed(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import services
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import (
        create_participant_days,
        get_participant_by_id,
        insert_participant,
        set_attempt_id_for_participant_days,
    )

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-02",
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant(
                conn,
                name="Converted Short",
                phone="13800000014",
                phone_hash="hash-converted-short",
                participant_type="short",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=1,
            )
            create_participant_days(
                conn,
                participant_id=participant_id,
                target_days=1,
                start_date=date.fromisoformat("2026-07-02"),
            )
            attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="short",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=1,
                status="converted_to_short",
            )
            set_current_attempt(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )
            set_attempt_id_for_participant_days(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )
            conn.execute(
                """
                UPDATE participants
                SET current_status = 'active'
                WHERE id = ?
                """,
                (participant_id,),
            )

        participant_row = get_participant_by_id(conn, participant_id=participant_id)
        assert participant_row is not None

        participant_view = services.participants.build_participant_view(
            conn,
            participant_row=participant_row,
        )
    finally:
        conn.close()

    assert participant_view.current_status == "converted_to_short"
    assert participant_view.participation_state == "completed"


def test_participant_view_ignores_legacy_assignment_columns(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection
    from backend.app.services.participants import get_participant_view_by_id

    with client:
        response = client.post(
            "/api/auth/login",
            json={"name": "Legacy Drift", "phone": "13800000007"},
        )

    assert response.status_code == 200
    payload = response.json()

    conn = get_connection(sqlite_settings)
    try:
        attempt_row = conn.execute(
            """
            SELECT participant_type, condition, subcondition, topic_key, error_type_id, target_days
            FROM participant_attempts
            WHERE id = ?
            """,
            (payload["attempt_id"],),
        ).fetchone()
        assert attempt_row is not None

        mutated_participant_type = (
            "long" if attempt_row["participant_type"] == "short" else "short"
        )
        mutated_condition = "tool" if attempt_row["condition"] == "human" else "human"
        mutated_subcondition = (
            "execution" if attempt_row["subcondition"] != "execution" else "qa"
        )
        mutated_target_days = 3 if mutated_participant_type == "long" else 1

        conn.execute(
            """
            UPDATE participants
            SET
                participant_type = ?,
                condition = ?,
                subcondition = ?,
                topic_key = ?,
                error_type_id = ?,
                target_days = ?
            WHERE id = ?
            """,
            (
                mutated_participant_type,
                mutated_condition,
                mutated_subcondition,
                "legacy-override-topic",
                "logic_major",
                mutated_target_days,
                payload["participant_id"],
            ),
        )

        participant_view = get_participant_view_by_id(
            conn,
            participant_id=payload["participant_id"],
        )
    finally:
        conn.close()

    assert participant_view is not None
    assert participant_view.attempt_id == payload["attempt_id"]
    assert participant_view.participant_type == attempt_row["participant_type"]
    assert participant_view.condition == attempt_row["condition"]
    assert participant_view.subcondition == attempt_row["subcondition"]
    assert participant_view.topic_key == attempt_row["topic_key"]
    assert participant_view.error_type_id == attempt_row["error_type_id"]
    assert participant_view.target_days == attempt_row["target_days"]


def test_participant_view_scopes_day_and_pretest_to_current_attempt(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import services
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import (
        get_participant_by_id,
        insert_participant_identity,
    )
    from backend.app.repositories.pretests import upsert_pretest_response

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-02",
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Scoped Attempt",
                phone="13800000006",
                phone_hash="hash-scoped-attempt",
            )
            old_attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="long",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=3,
            )
            current_attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="short",
                condition="tool",
                subcondition="planning",
                topic_key="career",
                error_type_id="logic_minor",
                target_days=1,
            )
            set_current_attempt(
                conn,
                participant_id=participant_id,
                attempt_id=current_attempt_id,
            )
            conn.execute(
                """
                INSERT INTO participant_days (
                    participant_id,
                    day_index,
                    calendar_date,
                    status,
                    attempt_id
                ) VALUES (?, 2, '2026-07-02', 'completed', ?)
                """,
                (participant_id, old_attempt_id),
            )
            conn.execute(
                """
                INSERT INTO participant_days (
                    participant_id,
                    day_index,
                    calendar_date,
                    status,
                    attempt_id
                ) VALUES (?, 1, '2026-07-03', 'not_started', ?)
                """,
                (participant_id, current_attempt_id),
            )
            upsert_pretest_response(
                conn,
                participant_id=participant_id,
                day_index=2,
                status="final",
                payload_json='{"from":"old-attempt"}',
                autosave_count=0,
                last_saved_at="2026-07-02T10:00:00+00:00",
                submitted_at="2026-07-02T10:00:00+00:00",
            )
            conn.execute(
                """
                UPDATE pretest_responses
                SET attempt_id = ?
                WHERE participant_id = ?
                """,
                (old_attempt_id, participant_id),
            )

        participant_row = get_participant_by_id(conn, participant_id=participant_id)
        assert participant_row is not None

        participant_view = services.participants.build_participant_view(
            conn,
            participant_row=participant_row,
        )
    finally:
        conn.close()

    assert participant_view.attempt_id == current_attempt_id
    assert participant_view.current_day.day_index == 1
    assert participant_view.current_day.calendar_date == "2026-07-03"
    assert participant_view.current_day.status == "not_started"
    assert participant_view.current_day.can_start_experiment is False
    assert participant_view.pretest_status.status == "not_started"
    assert participant_view.participation_state == "not_scheduled_today"


def test_short_unfinished_day_one_can_continue_after_scheduled_date(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import services
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import (
        create_participant_days,
        get_participant_by_id,
        insert_participant_identity,
        set_attempt_id_for_participant_days,
    )
    from backend.app.repositories.pretests import upsert_pretest_response

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-07",
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Late Short",
                phone="13800002001",
                phone_hash="hash-late-short",
            )
            create_participant_days(
                conn,
                participant_id=participant_id,
                target_days=1,
                start_date=date.fromisoformat("2026-07-06"),
            )
            attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="short",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=1,
            )
            set_current_attempt(conn, participant_id=participant_id, attempt_id=attempt_id)
            set_attempt_id_for_participant_days(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )
            upsert_pretest_response(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=1,
                status="final",
                payload_json="{}",
                autosave_count=0,
                last_saved_at="2026-07-06T09:00:00+08:00",
                submitted_at="2026-07-06T09:00:00+08:00",
            )

        participant_row = get_participant_by_id(conn, participant_id=participant_id)
        assert participant_row is not None
        participant_view = services.participants.build_participant_view(
            conn,
            participant_row=participant_row,
        )
    finally:
        conn.close()

    assert participant_view.current_day.day_index == 1
    assert participant_view.current_day.calendar_date == "2026-07-06"
    assert participant_view.current_day.can_start_experiment is True
    assert participant_view.participation_state == "ready_for_experiment"
    assert participant_view.participation_message is None


def test_short_unfinished_day_one_can_start_session_after_scheduled_date(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import services
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.models.api import ClientInfo, SessionStartRequest
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import (
        create_participant_days,
        insert_participant_identity,
        set_attempt_id_for_participant_days,
    )
    from backend.app.repositories.pretests import upsert_pretest_response
    from backend.app.services.sessions import start_session

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-07",
    )
    monkeypatch.setattr(
        services.sessions,
        "current_shanghai_date",
        lambda: "2026-07-07",
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Late Short Start",
                phone="13800002004",
                phone_hash="hash-late-short-start",
            )
            create_participant_days(
                conn,
                participant_id=participant_id,
                target_days=1,
                start_date=date.fromisoformat("2026-07-06"),
            )
            attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="short",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=1,
            )
            set_current_attempt(conn, participant_id=participant_id, attempt_id=attempt_id)
            set_attempt_id_for_participant_days(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )
            upsert_pretest_response(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=1,
                status="final",
                payload_json="{}",
                autosave_count=0,
                last_saved_at="2026-07-06T09:00:00+08:00",
                submitted_at="2026-07-06T09:00:00+08:00",
            )
            session = start_session(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                request=SessionStartRequest(
                    is_test=False,
                    client_info=ClientInfo(
                        device_type="desktop",
                        viewport_width=1280,
                        is_secure_context=True,
                        browser_name="chrome",
                        microphone_available=True,
                        microphone_permission="granted",
                    ),
                ),
                settings=sqlite_settings,
            )
    finally:
        conn.close()

    assert session.day_index == 1
    assert session.status == "started"


def test_long_unfinished_day_one_can_continue_after_scheduled_date(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import services
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import (
        create_participant_days,
        get_participant_by_id,
        insert_participant_identity,
        set_attempt_id_for_participant_days,
    )

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-08",
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Late Long Day One",
                phone="13800002002",
                phone_hash="hash-late-long-day-one",
            )
            create_participant_days(
                conn,
                participant_id=participant_id,
                target_days=3,
                start_date=date.fromisoformat("2026-07-06"),
            )
            attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="long",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=3,
            )
            set_current_attempt(conn, participant_id=participant_id, attempt_id=attempt_id)
            set_attempt_id_for_participant_days(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )

        participant_row = get_participant_by_id(conn, participant_id=participant_id)
        assert participant_row is not None
        participant_view = services.participants.build_participant_view(
            conn,
            participant_row=participant_row,
        )
    finally:
        conn.close()

    assert participant_view.current_day.day_index == 1
    assert participant_view.current_day.calendar_date == "2026-07-06"
    assert participant_view.current_day.can_start_experiment is False
    assert participant_view.participation_state == "needs_pretest"
    assert participant_view.participation_message is None


def test_long_unfinished_day_one_can_save_pretest_after_scheduled_date(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import services
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.models.api import PretestSubmissionRequest
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import (
        create_participant_days,
        insert_participant_identity,
        set_attempt_id_for_participant_days,
    )
    from backend.app.services.questionnaires import save_pretest_draft

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-08",
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Late Long Pretest",
                phone="13800002005",
                phone_hash="hash-late-long-pretest",
            )
            create_participant_days(
                conn,
                participant_id=participant_id,
                target_days=3,
                start_date=date.fromisoformat("2026-07-06"),
            )
            attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="long",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=3,
            )
            set_current_attempt(conn, participant_id=participant_id, attempt_id=attempt_id)
            set_attempt_id_for_participant_days(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )
            response = save_pretest_draft(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                request=PretestSubmissionRequest(
                    demographics={},
                    scales={},
                    slider_touch_state={},
                    page_progress={},
                    client_timestamp="2026-07-08T09:00:00+08:00",
                ),
            )
    finally:
        conn.close()

    assert response.day_index == 1
    assert response.status == "draft"


def test_long_day_two_state_is_actionable_then_blocked_after_missed_day(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import services
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import (
        create_participant_days,
        get_participant_by_id,
        insert_participant_identity,
        set_attempt_id_for_participant_days,
    )

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-07",
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Missed Long Day Two",
                phone="13800002003",
                phone_hash="hash-missed-long-day-two",
            )
            create_participant_days(
                conn,
                participant_id=participant_id,
                target_days=3,
                start_date=date.fromisoformat("2026-07-06"),
            )
            attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="long",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=3,
            )
            set_current_attempt(conn, participant_id=participant_id, attempt_id=attempt_id)
            set_attempt_id_for_participant_days(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )
            conn.execute(
                """
                UPDATE participant_days
                SET status = 'completed', completed_at = '2026-07-06T20:00:00+08:00'
                WHERE attempt_id = ? AND day_index = 1
                """,
                (attempt_id,),
            )

        participant_row = get_participant_by_id(conn, participant_id=participant_id)
        assert participant_row is not None
        day_two_view = services.participants.build_participant_view(
            conn,
            participant_row=participant_row,
        )
        monkeypatch.setattr(
            services.participants,
            "current_shanghai_date",
            lambda: "2026-07-08",
        )
        missed_view = services.participants.build_participant_view(
            conn,
            participant_row=participant_row,
        )
    finally:
        conn.close()

    assert day_two_view.current_day.day_index == 2
    assert day_two_view.current_day.calendar_date == "2026-07-07"
    assert day_two_view.current_day.can_start_experiment is True
    assert day_two_view.pretest_status.status == "not_started"
    assert day_two_view.pretest_status.has_final is False
    assert day_two_view.participation_state == "ready_for_experiment"

    assert missed_view.current_day.day_index == 2
    assert missed_view.current_day.calendar_date == "2026-07-07"
    assert missed_view.current_day.can_start_experiment is False
    assert missed_view.pretest_status.status == "not_started"
    assert missed_view.pretest_status.has_final is False
    assert missed_view.participation_state == "blocked"
    assert missed_view.participation_message == "您未按要求连续三天完成实验，已无法参与实验。"


def test_me_rejects_bad_signature_session_cookie(client: TestClient):
    bad_cookie = sign_session_payload(
        {
            "participant_id": 999,
            "phone_hash": "bad-hash",
        },
        "different-secret",
    )
    client.cookies.set("aitrust_v2_sid", bad_cookie)

    with client:
        response = client.get("/api/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid session."}


def test_me_rejects_semantically_invalid_signed_session_cookie(
    client: TestClient, sqlite_settings: Settings
):
    bad_cookie = sign_session_payload(
        {
            "participant_id": "not-an-int",
            "phone_hash": "bad-hash",
        },
        sqlite_settings.app_secret_key,
    )
    client.cookies.set(sqlite_settings.session_cookie_name, bad_cookie)

    with client:
        response = client.get("/api/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid session."}


def test_require_authenticated_session_rejects_missing_attempt_id(
    sqlite_settings: Settings,
):
    from fastapi import HTTPException

    from backend.app.main import require_authenticated_session

    cookie = sign_session_payload(
        {
            "participant_id": 1,
            "phone_hash": "hash-without-attempt",
        },
        sqlite_settings.app_secret_key,
    )

    with pytest.raises(HTTPException) as exc_info:
        require_authenticated_session(
            session_token=cookie,
            settings=sqlite_settings,
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid session."


def test_require_authenticated_session_rejects_non_int_attempt_id(
    sqlite_settings: Settings,
):
    from fastapi import HTTPException

    from backend.app.main import require_authenticated_session

    cookie = sign_session_payload(
        {
            "participant_id": 1,
            "attempt_id": "not-an-int",
            "phone_hash": "hash-with-bad-attempt",
        },
        sqlite_settings.app_secret_key,
    )

    with pytest.raises(HTTPException) as exc_info:
        require_authenticated_session(
            session_token=cookie,
            settings=sqlite_settings,
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid session."


def test_me_rejects_session_cookie_for_different_attempt(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        login_response = client.post(
            "/api/auth/login",
            json={
                "name": "Mismatched Attempt",
                "phone": "13800000016",
            },
        )
        assert login_response.status_code == 200
        login_payload = login_response.json()
        session_payload = read_client_session_payload(client, sqlite_settings)
        mismatched_cookie = sign_session_payload(
            {
                "participant_id": login_payload["participant_id"],
                "attempt_id": login_payload["attempt_id"] + 999,
                "phone_hash": session_payload["phone_hash"],
            },
            sqlite_settings.app_secret_key,
        )
        client.cookies.set(
            sqlite_settings.session_cookie_name,
            mismatched_cookie,
            domain="testserver.local",
            path="/",
        )

        response = client.get("/api/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid session."}
