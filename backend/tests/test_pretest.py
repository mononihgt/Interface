from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.settings import Settings


TEST_DATE = "2026-07-02"


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "pretest.db"
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
        lambda: TEST_DATE,
    )

    try:
        from backend.app.services import participant_days
    except ImportError:
        participant_days = None

    if participant_days is not None:
        monkeypatch.setattr(
            participant_days,
            "current_shanghai_date",
            lambda: TEST_DATE,
        )

    return TestClient(create_app(settings=sqlite_settings))


def login_short_participant(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/auth/login",
        json={
            "name": "Pretest Participant",
            "phone": "19900000004",
            "participant_type": "short",
        },
    )

    assert response.status_code == 200
    return response.json()


def create_incomplete_formal_session(
    sqlite_settings: Settings,
    *,
    participant_id: int,
    attempt_id: int,
    session_uuid: str,
) -> None:
    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        participant_day = conn.execute(
            """
            SELECT id
            FROM participant_days
            WHERE attempt_id = ? AND day_index = 1
            """,
            (attempt_id,),
        ).fetchone()
        assert participant_day is not None
        conn.execute(
            """
            INSERT INTO experiment_sessions (
                participant_id,
                attempt_id,
                participant_day_id,
                session_uuid,
                condition,
                subcondition,
                topic_key,
                scenario_id,
                agent_graph_version,
                error_type_id,
                planned_error_turn,
                status,
                started_at,
                client_info_json,
                is_test
            ) VALUES (?, ?, ?, ?, 'human', 'qa', 'advice', 'scenario-1', 'graph-v1', 'factual_minor', 2, 'started', '2026-07-02T10:00:00+08:00', '{}', 0)
            """,
            (participant_id, attempt_id, int(participant_day["id"]), session_uuid),
        )
        conn.commit()
    finally:
        conn.close()


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


def login_long_participant(
    client: TestClient,
    sqlite_settings: Settings,
) -> dict[str, object]:
    force_next_assignment_long(sqlite_settings)
    response = client.post(
        "/api/auth/login",
        json={
            "name": "Long Schedule Participant",
            "phone": "19900000005",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["participant_type"] == "long"
    return payload


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


@pytest.fixture
def logged_in_participant(client: TestClient) -> dict[str, object]:
    with client:
        return login_short_participant(client)


@pytest.fixture
def complete_pretest_payload() -> dict[str, object]:
    return build_pretest_payload()


def assert_pretest_field_error(
    response,
    *,
    field: str,
    message: str,
) -> None:
    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "pretest_validation_error",
        "message": "前测问卷包含需要修正的内容。",
        "field_errors": {field: message},
    }


def test_pretest_current_requires_participant_authentication(client: TestClient):
    with client:
        response = client.get("/api/pretest/current")

    assert response.status_code == 401
    assert response.json() == {"detail": "Login required."}


def test_pretest_current_returns_none_before_first_acknowledged_save(
    client: TestClient,
):
    with client:
        login_short_participant(client)
        response = client.get("/api/pretest/current")

    assert response.status_code == 200
    assert response.json() is None


def test_pretest_current_restores_last_acknowledged_partial_draft(
    client: TestClient,
):
    draft = {
        "demographics": {"gender": "男"},
        "page_progress": {
            "section": "demographics",
            "current_step": "demographics",
            "completed_steps": ["intro"],
        },
        "client_timestamp": "2026-07-02T09:30:00+08:00",
    }

    with client:
        login_short_participant(client)
        save_response = client.post("/api/pretest/draft", json=draft)
        current_response = client.get("/api/pretest/current")

    assert save_response.status_code == 200
    assert current_response.status_code == 200
    assert current_response.json() == save_response.json()
    assert current_response.json()["payload"]["demographics"] == {"gender": "男"}


@pytest.mark.parametrize(
    ("payload", "field", "message"),
    [
        (
            {"demographics": {"unknown": "value"}},
            "demographics.unknown",
            "包含未知字段。",
        ),
        (
            {"demographics": {"gender": "unknown"}},
            "demographics.gender",
            "格式或选项无效。",
        ),
        (
            {"demographics": {"gender": ""}},
            "demographics.gender",
            "格式或选项无效。",
        ),
        (
            {"demographics": {"gender": []}},
            "demographics.gender",
            "格式或选项无效。",
        ),
        (
            {"demographics": {"idNumber": "short"}},
            "demographics.idNumber",
            "格式或选项无效。",
        ),
        (
            {"scales": {"q1": 6}},
            "scales.q1",
            "数值超出允许范围。",
        ),
        (
            {"scales": {"q1": ""}},
            "scales.q1",
            "格式或选项无效。",
        ),
        (
            {"scales": {"q48": {"option": "A"}}},
            "scales.q48",
            "格式或选项无效。",
        ),
        (
            {"scales": {"q27": 50}, "slider_touch_state": {"q27": False}},
            "slider_touch_state.q27",
            "答案与滑块确认状态不一致。",
        ),
        (
            {"scales": {"unknown": 3}},
            "scales.unknown",
            "包含未知字段。",
        ),
        (
            {"unexpected": True},
            "unexpected",
            "包含未知字段。",
        ),
        (
            {"client_timestamp": ""},
            "client_timestamp",
            "格式或选项无效。",
        ),
    ],
)
def test_pretest_draft_rejects_supplied_invalid_values(
    client: TestClient,
    payload: dict[str, object],
    field: str,
    message: str,
):
    with client:
        login_short_participant(client)
        response = client.post("/api/pretest/draft", json=payload)

    assert_pretest_field_error(response, field=field, message=message)


@pytest.mark.parametrize(
    ("mutate", "field", "message"),
    [
        (
            lambda payload: payload["demographics"].pop("birthDate"),
            "demographics.birthDate",
            "此项为必填项。",
        ),
        (
            lambda payload: payload["scales"].__setitem__("unknown", 3),
            "scales.unknown",
            "包含未知字段。",
        ),
        (
            lambda payload: payload["demographics"].__setitem__("idNumber", "short"),
            "demographics.idNumber",
            "格式或选项无效。",
        ),
        (
            lambda payload: payload["demographics"].__setitem__("gender", {}),
            "demographics.gender",
            "格式或选项无效。",
        ),
        (
            lambda payload: payload["scales"].__setitem__("q1", "3"),
            "scales.q1",
            "格式或选项无效。",
        ),
        (
            lambda payload: payload["scales"].__setitem__("q27", 101),
            "scales.q27",
            "数值超出允许范围。",
        ),
        (
            lambda payload: payload["scales"].__setitem__("q49", []),
            "scales.q49",
            "格式或选项无效。",
        ),
        (
            lambda payload: payload["slider_touch_state"].__setitem__("q27", False),
            "slider_touch_state.q27",
            "答案与滑块确认状态不一致。",
        ),
    ],
)
def test_pretest_final_strictly_rejects_invalid_questionnaires(
    client: TestClient,
    mutate,
    field: str,
    message: str,
):
    payload = build_pretest_payload()
    mutate(payload)

    with client:
        login_short_participant(client)
        response = client.post("/api/pretest/final", json=payload)

    assert_pretest_field_error(response, field=field, message=message)


def test_pretest_draft_can_be_overwritten(
    client: TestClient, sqlite_settings: Settings
):
    with client:
        participant = login_short_participant(client)

        first_response = client.post(
            "/api/pretest/draft",
            json=build_pretest_payload(trust_score=2),
        )
        second_response = client.post(
            "/api/pretest/draft",
            json=build_pretest_payload(trust_score=5),
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    second_payload = second_response.json()
    assert second_payload["status"] == "draft"
    assert second_payload["autosave_count"] == 2
    assert second_payload["payload"]["scales"]["q21"] == 5
    assert second_payload["submitted_at"] is None

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        draft_rows = conn.execute(
            """
            SELECT id, autosave_count, payload_json
            FROM pretest_responses
            WHERE participant_id = ? AND day_index = 1 AND status = 'draft'
            """,
            (participant["participant_id"],),
        ).fetchall()
    finally:
        conn.close()

    assert len(draft_rows) == 1
    assert draft_rows[0]["autosave_count"] == 2
    assert '"q21": 5' in draft_rows[0]["payload_json"]


def test_pretest_final_locks_day_one_gate(
    client: TestClient, sqlite_settings: Settings
):
    with client:
        participant = login_short_participant(client)

        me_before = client.get("/api/me")

        final_response = client.post(
            "/api/pretest/final",
            json=build_pretest_payload(),
        )

        me_after = client.get("/api/me")

    assert me_before.status_code == 200
    assert me_before.json()["current_day"]["can_start_experiment"] is False
    assert me_before.json()["pretest_status"]["status"] == "not_started"

    assert final_response.status_code == 200
    final_payload = final_response.json()
    assert final_payload["status"] == "final"
    assert final_payload["autosave_count"] == 0
    assert final_payload["submitted_at"] is not None
    assert final_payload["can_start_experiment"] is True

    assert me_after.status_code == 200
    me_after_payload = me_after.json()
    assert me_after_payload["current_day"]["status"] == "pretest"
    assert me_after_payload["current_day"]["can_start_experiment"] is True
    assert me_after_payload["pretest_status"]["status"] == "final"
    assert me_after_payload["pretest_status"]["has_final"] is True

    from backend.app.db import get_connection
    from backend.app.services.questionnaires import can_start_formal_session

    conn = get_connection(sqlite_settings)
    try:
        assert (
            can_start_formal_session(
                conn,
                participant_id=participant["participant_id"],
                day_index=1,
                attempt_id=participant["attempt_id"],
            )
            is True
        )
        final_rows = conn.execute(
            """
            SELECT attempt_id, submitted_at, payload_json
            FROM pretest_responses
            WHERE participant_id = ? AND day_index = 1 AND status = 'final'
            """,
            (participant["participant_id"],),
        ).fetchall()
    finally:
        conn.close()

    assert len(final_rows) == 1
    assert final_rows[0]["attempt_id"] == participant["attempt_id"]
    assert final_rows[0]["submitted_at"] is not None
    assert '"q21": 4' in final_rows[0]["payload_json"]


def test_pretest_final_cannot_be_overwritten_for_current_attempt(
    client: TestClient, sqlite_settings: Settings
):
    with client:
        participant = login_short_participant(client)
        first_response = client.post(
            "/api/pretest/final",
            json=build_pretest_payload(trust_score=2),
        )
        second_response = client.post(
            "/api/pretest/final",
            json=build_pretest_payload(trust_score=5),
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 409

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        final_rows = conn.execute(
            """
            SELECT payload_json
            FROM pretest_responses
            WHERE participant_id = ? AND attempt_id = ? AND day_index = 1 AND status = 'final'
            ORDER BY id
            """,
            (participant["participant_id"], participant["attempt_id"]),
        ).fetchall()
    finally:
        conn.close()

    assert len(final_rows) == 1
    assert '"q21": 2' in final_rows[0]["payload_json"]
    assert '"q21": 5' not in final_rows[0]["payload_json"]


def test_identical_repeated_pretest_final_returns_persisted_response(
    client: TestClient,
):
    payload = build_pretest_payload()

    with client:
        login_short_participant(client)
        first_response = client.post("/api/pretest/final", json=payload)
        second_response = client.post("/api/pretest/final", json=payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()


def test_pretest_final_transitions_the_existing_draft_record(
    client: TestClient,
    sqlite_settings: Settings,
):
    payload = build_pretest_payload()

    with client:
        participant = login_short_participant(client)
        draft_response = client.post("/api/pretest/draft", json=payload)
        final_response = client.post("/api/pretest/final", json=payload)

    assert draft_response.status_code == 200
    assert final_response.status_code == 200
    assert final_response.json()["autosave_count"] == 1

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        rows = conn.execute(
            """
            SELECT status, autosave_count, payload_json
            FROM pretest_responses
            WHERE participant_id = ? AND attempt_id = ? AND day_index = 1
            """,
            (participant["participant_id"], participant["attempt_id"]),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["status"] == "final"
    assert rows[0]["autosave_count"] == 1


def test_pretest_draft_is_rejected_after_final_for_current_attempt(
    client: TestClient, sqlite_settings: Settings
):
    with client:
        participant = login_short_participant(client)
        final_response = client.post(
            "/api/pretest/final",
            json=build_pretest_payload(trust_score=2),
        )
        draft_response = client.post(
            "/api/pretest/draft",
            json=build_pretest_payload(trust_score=5),
        )

    assert final_response.status_code == 200
    assert draft_response.status_code == 409

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        final_rows = conn.execute(
            """
            SELECT payload_json
            FROM pretest_responses
            WHERE participant_id = ? AND attempt_id = ? AND day_index = 1 AND status = 'final'
            ORDER BY id
            """,
            (participant["participant_id"], participant["attempt_id"]),
        ).fetchall()
        draft_rows = conn.execute(
            """
            SELECT payload_json
            FROM pretest_responses
            WHERE participant_id = ? AND attempt_id = ? AND day_index = 1 AND status = 'draft'
            ORDER BY id
            """,
            (participant["participant_id"], participant["attempt_id"]),
        ).fetchall()
    finally:
        conn.close()

    assert len(final_rows) == 1
    assert '"q21": 2' in final_rows[0]["payload_json"]
    assert draft_rows == []


def test_relogin_restores_day_one_final_pretest_on_current_attempt(
    client: TestClient, sqlite_settings: Settings
):
    with client:
        first = login_short_participant(client)
        final_response = client.post(
            "/api/pretest/final",
            json=build_pretest_payload(),
        )
    create_incomplete_formal_session(
        sqlite_settings,
        participant_id=int(first["participant_id"]),
        attempt_id=int(first["attempt_id"]),
        session_uuid="pretest-copy-session",
    )

    with client:
        relogin_response = client.post(
            "/api/auth/login",
            json={"name": "Pretest Participant", "phone": "19900000004"},
        )

    assert final_response.status_code == 200
    assert relogin_response.status_code == 200
    second = relogin_response.json()
    assert second["attempt_id"] == first["attempt_id"]

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        old_final = conn.execute(
            """
            SELECT id, payload_json
            FROM pretest_responses
            WHERE participant_id = ? AND attempt_id = ? AND day_index = 1 AND status = 'final'
            """,
            (first["participant_id"], first["attempt_id"]),
        ).fetchone()
        restored_final = conn.execute(
            """
            SELECT attempt_id, payload_json, source_pretest_response_id
            FROM pretest_responses
            WHERE participant_id = ? AND attempt_id = ? AND day_index = 1 AND status = 'final'
            """,
            (first["participant_id"], second["attempt_id"]),
        ).fetchone()
    finally:
        conn.close()

    assert old_final is not None
    assert restored_final is not None
    assert restored_final["attempt_id"] == second["attempt_id"]
    assert restored_final["payload_json"] == old_final["payload_json"]
    assert restored_final["source_pretest_response_id"] is None


def test_relogin_restores_draft_pretest_on_current_attempt(
    client: TestClient, sqlite_settings: Settings
):
    with client:
        first = login_short_participant(client)
        draft_response = client.post(
            "/api/pretest/draft",
            json=build_pretest_payload(),
        )
    create_incomplete_formal_session(
        sqlite_settings,
        participant_id=int(first["participant_id"]),
        attempt_id=int(first["attempt_id"]),
        session_uuid="pretest-draft-only-session",
    )

    with client:
        relogin_response = client.post(
            "/api/auth/login",
            json={"name": "Pretest Participant", "phone": "19900000004"},
        )

    assert draft_response.status_code == 200
    assert relogin_response.status_code == 200
    second = relogin_response.json()
    assert second["attempt_id"] == first["attempt_id"]

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        old_draft = conn.execute(
            """
            SELECT id, payload_json
            FROM pretest_responses
            WHERE participant_id = ? AND attempt_id = ? AND day_index = 1 AND status = 'draft'
            """,
            (first["participant_id"], first["attempt_id"]),
        ).fetchone()
        restored_attempt_rows = conn.execute(
            """
            SELECT status, source_pretest_response_id, payload_json
            FROM pretest_responses
            WHERE participant_id = ? AND attempt_id = ? AND day_index = 1
            ORDER BY id
            """,
            (first["participant_id"], second["attempt_id"]),
        ).fetchall()
    finally:
        conn.close()

    assert old_draft is not None
    assert old_draft["payload_json"] is not None
    assert len(restored_attempt_rows) == 1
    assert restored_attempt_rows[0]["status"] == "draft"
    assert restored_attempt_rows[0]["source_pretest_response_id"] is None
    assert restored_attempt_rows[0]["payload_json"] == old_draft["payload_json"]


def test_final_pretest_rejects_empty_payload(client: TestClient):
    with client:
        login_short_participant(client)

        response = client.post(
            "/api/pretest/final",
            json={
                "demographics": {},
                "scales": {},
                "slider_touch_state": {},
                "page_progress": {"section": "intro"},
                "client_timestamp": "2026-07-02T09:45:00+08:00",
            },
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "pretest_validation_error"
    assert detail["field_errors"]["demographics.birthDate"] == "此项为必填项。"
    assert detail["field_errors"]["scales.q49"] == "此项为必填项。"
    assert (
        detail["field_errors"]["slider_touch_state.confidence_q46"]
        == "此项为必填项。"
    )


def test_pretest_rejects_untouched_slider(
    client: TestClient,
    logged_in_participant: dict[str, object],
):
    payload = {
        "demographics": {
            "birthDate": "2000-01-01",
            "gender": "男",
            "idNumber": "ID1234567",
        },
        "scales": {"q1": 3, "q2": 4, "q27": 50},
        "slider_touch_state": {"q27": False},
        "page_progress": {"section": "scales"},
        "client_timestamp": "2026-07-02T10:00:00+08:00",
    }

    with client:
        response = client.post("/api/pretest/final", json=payload)

    assert response.status_code == 400
    assert response.json()["detail"]["field_errors"]["slider_touch_state.q27"] == (
        "答案与滑块确认状态不一致。"
    )


def test_pretest_accepts_touched_slider(
    client: TestClient,
    logged_in_participant: dict[str, object],
    complete_pretest_payload: dict[str, object],
):
    complete_pretest_payload["slider_touch_state"]["q27"] = True

    with client:
        response = client.post("/api/pretest/final", json=complete_pretest_payload)

    assert response.status_code == 200
    assert response.json()["status"] == "final"


def test_long_participant_keeps_unfinished_day_one_on_day_two(
    client: TestClient, sqlite_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    with client:
        participant = login_long_participant(client, sqlite_settings)

        from backend.app import services
        from backend.app.services import participant_days

        monkeypatch.setattr(
            services.participants,
            "current_shanghai_date",
            lambda: "2026-07-03",
        )
        monkeypatch.setattr(
            participant_days,
            "current_shanghai_date",
            lambda: "2026-07-03",
        )

        draft_response = client.post(
            "/api/pretest/draft",
            json=build_pretest_payload(),
        )
        me_response = client.get("/api/me")

    assert participant["current_day"]["day_index"] == 1
    assert draft_response.status_code == 200
    assert draft_response.json()["day_index"] == 1
    assert me_response.status_code == 200
    me_payload = me_response.json()
    assert me_payload["current_day"]["day_index"] == 1
    assert me_payload["current_day"]["calendar_date"] == "2026-07-02"
    assert me_payload["pretest_status"]["status"] == "draft"


def test_long_participant_can_submit_late_day_one_final_pretest(
    client: TestClient, sqlite_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    with client:
        participant = login_long_participant(client, sqlite_settings)

        from backend.app import services
        from backend.app.services import participant_days

        monkeypatch.setattr(
            services.participants,
            "current_shanghai_date",
            lambda: "2026-07-03",
        )
        monkeypatch.setattr(
            participant_days,
            "current_shanghai_date",
            lambda: "2026-07-03",
        )

        final_response = client.post(
            "/api/pretest/final",
            json=build_pretest_payload(),
        )
        me_response = client.get("/api/me")

    assert participant["current_day"]["day_index"] == 1
    assert final_response.status_code == 200
    final_payload = final_response.json()
    assert final_payload["day_index"] == 1
    assert final_payload["status"] == "final"

    assert me_response.status_code == 200
    me_payload = me_response.json()
    assert me_payload["current_day"]["day_index"] == 1
    assert me_payload["pretest_status"]["status"] == "final"


def test_unfinished_day_one_after_scheduled_range_accepts_pretest_writes(
    client: TestClient, sqlite_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    with client:
        participant = login_long_participant(client, sqlite_settings)

        from backend.app import services
        from backend.app.services import participant_days

        monkeypatch.setattr(
            services.participants,
            "current_shanghai_date",
            lambda: "2026-07-07",
        )
        monkeypatch.setattr(
            participant_days,
            "current_shanghai_date",
            lambda: "2026-07-07",
        )

        me_response = client.get("/api/me")
        draft_response = client.post(
            "/api/pretest/draft",
            json=build_pretest_payload(),
        )
        final_response = client.post(
            "/api/pretest/final",
            json=build_pretest_payload(),
        )

    assert participant["current_day"]["day_index"] == 1
    assert me_response.status_code == 200
    me_payload = me_response.json()
    assert me_payload["current_day"]["day_index"] == 1
    assert me_payload["current_day"]["calendar_date"] == "2026-07-02"
    assert me_payload["current_day"]["status"] == "not_started"
    assert me_payload["current_day"]["can_start_experiment"] is False
    assert draft_response.status_code == 200
    assert draft_response.json()["day_index"] == 1
    assert final_response.status_code == 200
    assert final_response.json()["day_index"] == 1


def test_missing_scheduled_day_pretest_write_is_controlled_error(
    client: TestClient, sqlite_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    with client:
        participant = login_long_participant(client, sqlite_settings)

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            """
            UPDATE participant_days
            SET status = 'completed', completed_at = '2026-07-02T20:00:00+08:00'
            WHERE participant_id = ? AND day_index = 1
            """,
            (participant["participant_id"],),
        )
        conn.execute(
            """
            DELETE FROM participant_days
            WHERE participant_id = ? AND day_index = 2
            """,
            (participant["participant_id"],),
        )
    finally:
        conn.close()

    with client:
        from backend.app import services
        from backend.app.services import participant_days

        monkeypatch.setattr(
            services.participants,
            "current_shanghai_date",
            lambda: "2026-07-03",
        )
        monkeypatch.setattr(
            participant_days,
            "current_shanghai_date",
            lambda: "2026-07-03",
        )

        me_response = client.get("/api/me")
        response = client.post(
            "/api/pretest/draft",
            json=build_pretest_payload(),
        )
        final_response = client.post(
            "/api/pretest/final",
            json=build_pretest_payload(),
        )

    assert me_response.status_code == 200
    me_payload = me_response.json()
    assert me_payload["current_day"]["day_index"] == 3
    assert me_payload["current_day"]["calendar_date"] == "2026-07-04"
    assert me_payload["current_day"]["can_start_experiment"] is False
    assert me_payload["pretest_status"]["status"] == "not_started"
    assert response.status_code == 409
    assert "scheduled" in response.json()["detail"].lower()
    assert final_response.status_code == 409
    assert "scheduled" in final_response.json()["detail"].lower()
