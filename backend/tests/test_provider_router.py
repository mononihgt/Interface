from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from time import perf_counter

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from backend.app.db import get_connection, run_migrations
from backend.app.settings import Settings


class FakeAdapter:
    def __init__(
        self,
        handler: Callable[..., Awaitable[dict[str, object]]],
    ) -> None:
        self._handler = handler

    async def generate(
        self,
        *,
        request_id: str,
        base_url: str,
        api_key: str,
        model: str,
        messages: object,
        route: str,
        extra_body: Mapping[str, object],
    ) -> dict[str, object]:
        return await self._handler(
            request_id=request_id,
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            route=route,
            extra_body=extra_body,
        )


def _user_messages(text: str):
    from backend.app.services.providers import ProviderMessage

    return [ProviderMessage(role="user", content=text)]


class StructuredTestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_text: str
    status: str


class StructuredNumericPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "provider-router.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        yizhan_api_key="TEST_KEY_YIZHAN",
        aabao_api_key="PROVIDER_KEY_SENTINEL",
        packyapi_api_key="PROVIDER_KEY_SENTINEL",
        deepseek_api_key="TEST_KEY_DEEPSEEK",
    )


@pytest.fixture
def conn(sqlite_settings: Settings) -> sqlite3.Connection:
    connection = get_connection(sqlite_settings)
    run_migrations(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_formal_route_order_matches_spec(sqlite_settings: Settings, conn: sqlite3.Connection):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter

    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=ApiHealthService(conn),
        adapter_factory=lambda route: FakeAdapter(_unused_success),
    )

    assert [(route.provider, route.model) for route in router.formal_chat_routes()] == [
        ("yi-zhan", "gpt-5.1"),
        ("yi-zhan", "gpt-5"),
        ("aabao", "gpt-5.1"),
        ("aabao", "gpt-5"),
        ("packyapi", "gpt-5.1"),
        ("packyapi", "gpt-5"),
        ("deepseek", "deepseek-v4-pro"),
    ]


def test_test_channel_uses_deepseek_only(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter

    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=ApiHealthService(conn),
        adapter_factory=lambda route: FakeAdapter(_unused_success),
    )

    assert [(route.provider, route.model) for route in router.test_chat_routes()] == [
        ("deepseek", "deepseek-v4-pro"),
    ]


@pytest.mark.asyncio
async def test_deepseek_request_disables_thinking_on_wire(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderMessage, ProviderRouter

    observed: list[dict[str, object]] = []

    class CapturingAdapter:
        async def generate(
            self,
            *,
            model: str,
            messages: object,
            extra_body: Mapping[str, object] | None = None,
            **_: object,
        ) -> dict[str, object]:
            request_options = dict(extra_body or {})
            wire_payload = {
                "model": model,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
                **request_options,
            }
            observed.append(
                {"extra_body": request_options, "wire_payload": wire_payload}
            )
            return {"text": "DeepSeek response"}

    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=ApiHealthService(conn),
        adapter_factory=lambda route: CapturingAdapter(),
    )

    response = await router.generate_chat(
        request_id="req-deepseek-thinking-disabled",
        messages=[ProviderMessage(role="user", content="hello")],
        is_test=True,
    )

    assert response.provider == "deepseek"
    assert observed[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "extra_body" not in observed[0]["wire_payload"]
    assert observed[0]["wire_payload"]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_formal_chat_reaches_deepseek_after_all_gpt_routes_fail(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter, ProviderTimeoutError

    def adapter_for(route):
        async def _generate(**_: object) -> dict[str, object]:
            if route.provider != "deepseek":
                raise ProviderTimeoutError("provider timeout")
            return {"text": "DeepSeek recovered the formal turn"}

        return FakeAdapter(_generate)

    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=ApiHealthService(conn),
        adapter_factory=adapter_for,
    )

    response = await router.generate_chat(
        request_id="req-formal-deepseek-last",
        messages=_user_messages("recover this turn"),
        is_test=False,
    )

    assert response.text == "DeepSeek recovered the formal turn"
    assert response.provider == "deepseek"
    assert response.model == "deepseek-v4-pro"
    assert [(attempt.provider, attempt.model, attempt.status) for attempt in response.attempts] == [
        ("yi-zhan", "gpt-5.1", "timeout"),
        ("yi-zhan", "gpt-5", "timeout"),
        ("aabao", "gpt-5.1", "timeout"),
        ("aabao", "gpt-5", "timeout"),
        ("packyapi", "gpt-5.1", "timeout"),
        ("packyapi", "gpt-5", "timeout"),
        ("deepseek", "deepseek-v4-pro", "success"),
    ]


@pytest.mark.asyncio
async def test_deepseek_hard_deadline_reaches_stable_local_fallback(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter

    timeout_settings = sqlite_settings.model_copy(
        update={"deepseek_timeout_seconds": 0.01}
    )

    async def _slow_response(**_: object) -> dict[str, object]:
        await asyncio.sleep(0.2)
        return {"text": "too late"}

    router = ProviderRouter(
        settings=timeout_settings,
        health_service=ApiHealthService(conn),
        adapter_factory=lambda route: FakeAdapter(_slow_response),
    )

    started_at = perf_counter()
    response = await router.generate_chat(
        request_id="req-deepseek-hard-timeout",
        messages=_user_messages("deadline"),
        is_test=True,
    )
    elapsed = perf_counter() - started_at

    assert elapsed < 0.15
    assert response.used_local_fallback is True
    assert response.provider == "local-router"
    assert response.model == "fixed-text-fallback-v1"
    assert [
        (attempt.provider, attempt.model, attempt.status, attempt.error_code)
        for attempt in response.attempts
    ] == [
        ("deepseek", "deepseek-v4-pro", "timeout", "timeout"),
        ("local-router", "fixed-text-fallback-v1", "local_fallback", None),
    ]


@pytest.mark.asyncio
async def test_transport_error_reaches_next_formal_route(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter, ProviderTransportError

    def adapter_for(route):
        async def _generate(**_: object) -> dict[str, object]:
            if route.provider == "yi-zhan" and route.model == "gpt-5.1":
                raise ProviderTransportError("transport_error")
            return {"text": "next route response"}

        return FakeAdapter(_generate)

    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=ApiHealthService(conn),
        adapter_factory=adapter_for,
    )

    response = await router.generate_chat(
        request_id="req-transport-next-route",
        messages=_user_messages("continue after transport failure"),
        is_test=False,
    )

    assert response.provider == "yi-zhan"
    assert response.model == "gpt-5"
    assert [
        (attempt.status, attempt.error_code, attempt.cooldown_applied)
        for attempt in response.attempts
    ] == [
        ("http_error", "transport_error", True),
        ("success", None, False),
    ]


@pytest.mark.asyncio
async def test_deepseek_transport_error_reaches_local_fallback_with_safe_log_status(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter, ProviderTransportError

    async def _transport_error(**_: object) -> dict[str, object]:
        raise ProviderTransportError("unsafe upstream connection details")

    health_service = ApiHealthService(conn)
    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=health_service,
        adapter_factory=lambda route: FakeAdapter(_transport_error),
    )

    response = await router.generate_chat(
        request_id="req-deepseek-transport-fallback",
        messages=_user_messages("transport fallback"),
        is_test=True,
    )
    health_service.flush()

    assert response.provider == "local-router"
    assert response.model == "fixed-text-fallback-v1"
    rows = conn.execute(
        """
        SELECT provider, model, status, error_code, error_message_summary
        FROM api_call_logs
        WHERE request_id = ?
        ORDER BY id
        """,
        ("req-deepseek-transport-fallback",),
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "status": "http_error",
            "error_code": "transport_error",
            "error_message_summary": "http_error:transport_error",
        },
        {
            "provider": "local-router",
            "model": "fixed-text-fallback-v1",
            "status": "local_fallback",
            "error_code": None,
            "error_message_summary": "local_fallback",
        },
    ]


@pytest.mark.asyncio
async def test_attempt_route_executes_one_external_route_without_local_fallback(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter

    health_service = ApiHealthService(conn)
    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=health_service,
        adapter_factory=lambda route: FakeAdapter(_unused_success),
    )

    text, attempt = await router.attempt_route(
        request_id="req-one-route",
        messages=_user_messages("one route only"),
        route=router.test_chat_routes()[0],
    )
    health_service.flush()

    assert text == "unused"
    assert attempt.provider == "deepseek"
    assert attempt.status == "success"
    rows = conn.execute(
        "SELECT provider, status FROM api_call_logs WHERE request_id = ? ORDER BY id",
        ("req-one-route",),
    ).fetchall()
    assert [tuple(row) for row in rows] == [("deepseek", "success")]


def test_provider_test_service_uses_single_deepseek_route(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.provider_testing import (
        ProviderTestResult,
        ProviderTestService,
    )
    from backend.app.services.providers import ProviderMessage

    observed: dict[str, object] = {}

    async def _capture_request(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {"text": "private response"}

    health_service = ApiHealthService(conn, is_test=True)
    service = ProviderTestService(
        settings=sqlite_settings,
        health_service=health_service,
        adapter_factory=lambda route: FakeAdapter(_capture_request),
    )

    result = service.test_deepseek(request_id="admin-deepseek-service-test")
    health_service.flush()

    assert result == ProviderTestResult(
        status="success",
        provider="deepseek",
        model="deepseek-v4-pro",
        latency_ms=result.latency_ms,
        error_code=None,
    )
    assert observed["messages"] == [
        ProviderMessage(role="user", content="health-check")
    ]
    assert observed["extra_body"] == {"thinking": {"type": "disabled"}}
    rows = conn.execute(
        """
        SELECT provider, model, status
        FROM api_call_logs
        WHERE request_id = ?
        ORDER BY id
        """,
        ("admin-deepseek-service-test",),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("deepseek", "deepseek-v4-pro", "success")
    ]


@pytest.mark.asyncio
async def test_complete_ordered_history_reaches_deepseek(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderMessage, ProviderRouter

    messages = [
        ProviderMessage(role="system", content="scenario prompt"),
        ProviderMessage(role="user", content="turn one"),
        ProviderMessage(role="assistant", content="reply one"),
        ProviderMessage(role="user", content="turn two"),
    ]
    observed_messages: list[ProviderMessage] = []

    async def _capture_messages(**kwargs: object) -> dict[str, object]:
        observed_messages.extend(kwargs["messages"])
        return {"text": "history received"}

    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=ApiHealthService(conn),
        adapter_factory=lambda route: FakeAdapter(_capture_messages),
    )

    response = await router.generate_chat(
        request_id="req-deepseek-history",
        messages=messages,
        is_test=True,
    )

    assert response.provider == "deepseek"
    assert observed_messages == messages


def test_evaluator_route_uses_configured_deepseek(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter

    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=ApiHealthService(conn),
        adapter_factory=lambda route: FakeAdapter(_unused_success),
    )

    routes = router.evaluator_routes()

    assert len(routes) == 1
    route = routes[0]
    assert route.route == "evaluator"
    assert route.provider == "deepseek"
    assert route.model == sqlite_settings.deepseek_model
    assert route.base_url == sqlite_settings.deepseek_base_url
    assert route.api_key == (sqlite_settings.deepseek_api_key or "")
    assert route.timeout_seconds == sqlite_settings.deepseek_timeout_seconds
    assert route.extra_body == {"thinking": {"type": "disabled"}}


def test_parse_structured_output_accepts_one_valid_json_object():
    from backend.app.agents.structured import parse_structured_output

    result = parse_structured_output(
        '{"assistant_text":"已整理。","status":"completed"}',
        StructuredTestPayload,
    )

    assert result.validation_error is None
    assert result.value == StructuredTestPayload(
        assistant_text="已整理。",
        status="completed",
    )


@pytest.mark.parametrize(
    "raw_output",
    [
        '```json\n{"assistant_text":"已整理。","status":"completed"}\n```',
        '{"assistant_text":"已整理。","status":"completed"} trailing prose',
        '结构化结果如下：\n{"assistant_text":"已整理。","status":"completed"}',
    ],
)
def test_parse_structured_output_accepts_one_bounded_wrapped_object(
    raw_output: str,
):
    from backend.app.agents.structured import parse_structured_output

    result = parse_structured_output(raw_output, StructuredTestPayload)

    assert result.validation_error is None
    assert result.value == StructuredTestPayload(
        assistant_text="已整理。",
        status="completed",
    )


@pytest.mark.parametrize(
    "raw_output",
    [
        '[{"assistant_text":"已整理。","status":"completed"}]',
        '{"assistant_text":"已整理。","status":"completed"} {"extra":true}',
    ],
)
def test_parse_structured_output_rejects_non_objects_and_multiple_values(
    raw_output: str,
):
    from backend.app.agents.structured import parse_structured_output

    result = parse_structured_output(raw_output, StructuredTestPayload)

    assert result.value is None
    assert result.validation_error == "invalid_json_object"


def test_parse_structured_output_rejects_schema_mismatch():
    from backend.app.agents.structured import parse_structured_output

    result = parse_structured_output(
        '{"assistant_text":"已整理。","status":"completed","invented":true}',
        StructuredTestPayload,
    )

    assert result.value is None
    assert result.validation_error == "schema_validation_error"


@pytest.mark.parametrize(
    "raw_output",
    [
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
    ],
)
def test_parse_structured_output_rejects_non_standard_json_constants(raw_output: str):
    from backend.app.agents.structured import parse_structured_output

    result = parse_structured_output(raw_output, StructuredNumericPayload)

    assert result.value is None
    assert result.validation_error == "invalid_json_object"


@pytest.mark.parametrize(
    "raw_output",
    [
        '{"value":1e999}',
        '{"value":"Infinity"}',
    ],
)
def test_parse_structured_output_rejects_non_finite_numeric_values(raw_output: str):
    from backend.app.agents.structured import parse_structured_output

    result = parse_structured_output(raw_output, StructuredNumericPayload)

    assert result.value is None
    assert result.validation_error == "non_finite_number"


@pytest.mark.asyncio
async def test_structured_generation_preserves_provider_fallback_without_value(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import InvalidProviderResponseError, ProviderRouter

    async def _always_invalid(**_: object) -> dict[str, object]:
        raise InvalidProviderResponseError("missing text field")

    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=ApiHealthService(conn),
        adapter_factory=lambda route: FakeAdapter(_always_invalid),
    )

    result = await router.generate_structured_agent(
        request_id="req-structured-fallback",
        messages=_user_messages("make a structured result"),
        is_test=True,
        schema=StructuredTestPayload,
    )

    assert result.value is None
    assert result.validation_error == "provider_local_fallback"
    assert result.response.used_local_fallback is True
    assert result.response.provider == "local-router"


@pytest.mark.asyncio
async def test_strict_chat_generation_raises_without_local_fallback(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import (
        ProviderRoutesExhausted,
        ProviderRouter,
        ProviderTimeoutError,
    )

    async def _always_timeout(**_: object) -> dict[str, object]:
        raise ProviderTimeoutError("provider timeout")

    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=ApiHealthService(conn),
        adapter_factory=lambda route: FakeAdapter(_always_timeout),
    )

    with pytest.raises(ProviderRoutesExhausted) as exc_info:
        await router.generate_chat(
            request_id="req-strict-error-generation",
            messages=_user_messages("generate the experimental response"),
            is_test=False,
            allow_local_fallback=False,
        )

    assert len(exc_info.value.attempts) == 7
    assert all(
        attempt.status != "local_fallback"
        for attempt in exc_info.value.attempts
    )


@pytest.mark.asyncio
async def test_structured_generation_retries_once_without_replaying_invalid_output(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderMessage, ProviderRouter

    invalid_output = "INVALID_OUTPUT_MUST_NOT_BE_REPLAYED"
    observed_messages: list[list[ProviderMessage]] = []

    async def _invalid_then_valid(**kwargs: object) -> dict[str, object]:
        observed_messages.append(list(kwargs["messages"]))
        if len(observed_messages) == 1:
            return {"text": invalid_output}
        return {
            "text": '{"assistant_text":"已整理。","status":"completed"}'
        }

    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=ApiHealthService(conn),
        adapter_factory=lambda route: FakeAdapter(_invalid_then_valid),
    )

    result = await router.generate_structured_agent(
        request_id="req-structured-retry",
        messages=[
            ProviderMessage(role="system", content="scenario prompt"),
            ProviderMessage(role="user", content="participant request"),
        ],
        is_test=True,
        schema=StructuredTestPayload,
    )

    assert result.validation_error is None
    assert result.value == StructuredTestPayload(
        assistant_text="已整理。",
        status="completed",
    )
    assert len(observed_messages) == 2
    assert observed_messages[0][0] == ProviderMessage(
        role="system",
        content="scenario prompt",
    )
    assert "JSON Schema" in observed_messages[0][1].content
    assert "invalid_json_object" in observed_messages[1][1].content
    assert invalid_output not in " ".join(
        message.content for message in observed_messages[1]
    )
    assert len(result.response.attempts) == 2


@pytest.mark.asyncio
async def test_unauthorized_applies_long_cooldown(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderHTTPError, ProviderRouter

    now = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    health_service = ApiHealthService(conn, now_fn=lambda: now)
    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=health_service,
        adapter_factory=lambda route: FakeAdapter(
            _unauthorized_then_success(route.provider, route.model)
        ),
    )

    response = await router.generate_chat(
        request_id="req-401",
        messages=_user_messages("Please summarize this"),
        is_test=False,
    )
    health_service.flush()

    assert response.provider == "yi-zhan"
    assert response.model == "gpt-5"
    assert health_service.is_on_cooldown(
        route="chat",
        provider="yi-zhan",
        model="gpt-5.1",
        at=now + timedelta(minutes=29, seconds=59),
    )
    assert not health_service.is_on_cooldown(
        route="chat",
        provider="yi-zhan",
        model="gpt-5.1",
        at=now + timedelta(minutes=30),
    )

    row = conn.execute(
        """
        SELECT provider, model, status, http_status, cooldown_applied
        FROM api_call_logs
        WHERE request_id = ?
        ORDER BY id
        """,
        ("req-401",),
    ).fetchone()

    assert dict(row) == {
        "provider": "yi-zhan",
        "model": "gpt-5.1",
        "status": "http_error",
        "http_status": 401,
        "cooldown_applied": 1,
    }


@pytest.mark.asyncio
async def test_cooldown_persists_across_router_instances(sqlite_settings: Settings):
    from backend.app.db import get_connection
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter

    now = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    first_conn = get_connection(sqlite_settings)
    run_migrations(first_conn)
    try:
        first_health_service = ApiHealthService(first_conn, now_fn=lambda: now)
        first_router = ProviderRouter(
            settings=sqlite_settings,
            health_service=first_health_service,
            adapter_factory=lambda route: FakeAdapter(
                _503_route_failure_then_success(
                    route.provider,
                    route.model,
                    status_code=503,
                    error_code="unavailable",
                )
            ),
        )
        first_response = await first_router.generate_chat(
            request_id="req-shared-cooldown-1",
            messages=_user_messages("first request"),
            is_test=False,
        )
        first_health_service.flush()
    finally:
        first_conn.close()

    assert first_response.provider == "yi-zhan"
    assert first_response.model == "gpt-5"

    second_conn = get_connection(sqlite_settings)
    second_conn.row_factory = sqlite3.Row
    try:
        called_routes: list[tuple[str, str]] = []

        async def _record_success(**kwargs: object) -> dict[str, object]:
            called_routes.append((str(kwargs["base_url"]), str(kwargs["model"])))
            return {"text": "shared cooldown respected"}

        second_router = ProviderRouter(
            settings=sqlite_settings,
            health_service=ApiHealthService(second_conn, now_fn=lambda: now),
            adapter_factory=lambda route: FakeAdapter(_record_success),
        )

        response = await second_router.generate_chat(
            request_id="req-shared-cooldown-2",
            messages=_user_messages("second request"),
            is_test=False,
        )
    finally:
        second_conn.close()

    assert response.text == "shared cooldown respected"
    assert response.provider == "yi-zhan"
    assert response.model == "gpt-5"
    assert called_routes[0] == (sqlite_settings.yizhan_base_url, "gpt-5")


@pytest.mark.parametrize("error_code", ["unavailable", "model_not_found"])
@pytest.mark.asyncio
async def test_503_unavailable_and_model_not_found_apply_long_cooldown(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    error_code: str,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter

    now = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    health_service = ApiHealthService(conn, now_fn=lambda: now)
    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=health_service,
        adapter_factory=lambda route: FakeAdapter(
            _503_route_failure_then_success(
                route.provider,
                route.model,
                status_code=503,
                error_code=error_code,
            )
        ),
    )

    response = await router.generate_chat(
        request_id=f"req-503-{error_code}",
        messages=_user_messages("Please summarize this"),
        is_test=False,
    )
    health_service.flush()

    assert response.provider == "yi-zhan"
    assert response.model == "gpt-5"
    assert health_service.is_on_cooldown(
        route="chat",
        provider="yi-zhan",
        model="gpt-5.1",
        at=now + timedelta(minutes=29, seconds=59),
    )
    assert not health_service.is_on_cooldown(
        route="chat",
        provider="yi-zhan",
        model="gpt-5.1",
        at=now + timedelta(minutes=30),
    )

    row = conn.execute(
        """
        SELECT provider, model, status, http_status, error_code, cooldown_applied
        FROM api_call_logs
        WHERE request_id = ?
        ORDER BY id
        """,
        (f"req-503-{error_code}",),
    ).fetchone()

    assert dict(row) == {
        "provider": "yi-zhan",
        "model": "gpt-5.1",
        "status": "http_error",
        "http_status": 503,
        "error_code": error_code,
        "cooldown_applied": 1,
    }


@pytest.mark.asyncio
async def test_provider_logs_do_not_include_api_key_or_prompt(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderHTTPError, ProviderRouter

    prompt = "participant prompt should never be stored verbatim"
    health_service = ApiHealthService(conn)
    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=health_service,
        adapter_factory=lambda route: FakeAdapter(
            _error_then_success(
                route.provider,
                route.model,
                prompt=prompt,
                api_key=sqlite_settings.yizhan_api_key or "",
            )
        ),
    )

    response = await router.generate_chat(
        request_id="req-sanitize",
        messages=_user_messages(prompt),
        is_test=False,
    )
    health_service.flush()

    assert response.text == "Recovered response"

    rows = conn.execute(
        """
        SELECT provider, model, status, http_status, error_message_summary
        FROM api_call_logs
        WHERE request_id = ?
        ORDER BY id
        """,
        ("req-sanitize",),
    ).fetchall()
    serialized = " | ".join(
        f"{row['provider']} {row['model']} {row['status']} {row['http_status']} {row['error_message_summary'] or ''}"
        for row in rows
    )

    assert "TEST_KEY_YIZHAN" not in serialized
    assert prompt not in serialized
    assert "Recovered response" not in serialized
    assert "http_error:500" in serialized.lower()


def test_summarize_error_does_not_store_arbitrary_provider_message(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService, LoggedProviderAttempt

    health_service = ApiHealthService(conn)
    health_service.log_attempt(
        request_id="req-structured-summary",
        attempt=LoggedProviderAttempt(
            route="chat",
            provider="yi-zhan",
            model="gpt-5.1",
            status="http_error",
            http_status=503,
            error_code="model_not_found",
            error_message_summary=(
                'Provider rejected input: "participant secret prompt" '
                "bearer token=abc123"
            ),
            cooldown_applied=True,
        ),
    )
    health_service.flush()

    row = conn.execute(
        """
        SELECT status, http_status, error_code, error_message_summary
        FROM api_call_logs
        WHERE request_id = ?
        """,
        ("req-structured-summary",),
    ).fetchone()

    assert dict(row) == {
        "status": "http_error",
        "http_status": 503,
        "error_code": "model_not_found",
        "error_message_summary": "http_error:503:model_not_found",
    }


@pytest.mark.asyncio
async def test_httpx_adapter_preserves_error_code_for_503_classification(
    monkeypatch: pytest.MonkeyPatch,
):
    import httpx

    from backend.app.services.providers import HttpxProviderAdapter, ProviderHTTPError

    class FakeResponse:
        status_code = 503

        def json(self) -> dict[str, object]:
            return {
                "error": {
                    "code": "model_not_found",
                    "message": "requested model is temporarily unavailable",
                }
            }

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    adapter = HttpxProviderAdapter(timeout_seconds=1.0)
    with pytest.raises(ProviderHTTPError) as exc_info:
        await adapter.generate(
            request_id="req-httpx-503",
            base_url="https://example.com/v1",
            api_key="PROVIDER_KEY_SENTINEL",
            model="gpt-5.1",
            messages=_user_messages("hello"),
            route="chat",
            extra_body={},
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.error_code == "model_not_found"
    assert exc_info.value.message == "requested model is temporarily unavailable"


@pytest.mark.asyncio
async def test_httpx_adapter_sends_exact_role_ordered_messages(
    monkeypatch: pytest.MonkeyPatch,
):
    import httpx

    from backend.app.services.providers import HttpxProviderAdapter, ProviderMessage

    captured_request: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "reply two"}}]}

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            captured_request.update({"url": url, **kwargs})
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    messages = [
        ProviderMessage(role="system", content="scenario prompt"),
        ProviderMessage(role="user", content="turn one"),
        ProviderMessage(role="assistant", content="reply one"),
        ProviderMessage(role="user", content="turn two"),
    ]
    adapter = HttpxProviderAdapter(timeout_seconds=1.0)

    result = await adapter.generate(
        request_id="req-message-order",
        base_url="https://example.com/v1/",
        api_key="PROVIDER_KEY_SENTINEL",
        model="gpt-5.1",
        messages=messages,
        route="chat",
        extra_body={"thinking": {"type": "disabled"}},
    )

    assert result == {"text": "reply two"}
    assert captured_request["url"] == "https://example.com/v1/chat/completions"
    assert captured_request["json"] == {
        "model": "gpt-5.1",
        "messages": [
            {"role": "system", "content": "scenario prompt"},
            {"role": "user", "content": "turn one"},
            {"role": "assistant", "content": "reply one"},
            {"role": "user", "content": "turn two"},
        ],
        "thinking": {"type": "disabled"},
    }


@pytest.mark.asyncio
async def test_httpx_adapter_classifies_request_error_as_safe_transport_error(
    monkeypatch: pytest.MonkeyPatch,
):
    import httpx

    from backend.app.services.providers import HttpxProviderAdapter, ProviderTransportError

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> object:
            request = httpx.Request("POST", "https://private-upstream.example/v1")
            raise httpx.ConnectError("private DNS details", request=request)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    adapter = HttpxProviderAdapter(timeout_seconds=1.0)
    with pytest.raises(ProviderTransportError, match="^transport_error$"):
        await adapter.generate(
            request_id="req-httpx-transport",
            base_url="https://example.com/v1",
            api_key="PROVIDER_KEY_SENTINEL",
            model="deepseek-v4-pro",
            messages=_user_messages("hello"),
            route="chat",
            extra_body={"thinking": {"type": "disabled"}},
        )


@pytest.mark.parametrize(
    "timeout_type",
    [
        pytest.param(httpx.ConnectTimeout, id="connect-timeout"),
        pytest.param(httpx.ReadTimeout, id="read-timeout"),
        pytest.param(httpx.WriteTimeout, id="write-timeout"),
        pytest.param(httpx.PoolTimeout, id="pool-timeout"),
    ],
)
@pytest.mark.asyncio
async def test_httpx_adapter_classifies_timeout_subclasses_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
    timeout_type: type[httpx.TimeoutException],
):
    from backend.app.services.providers import HttpxProviderAdapter, ProviderTimeoutError

    raw_failure = (
        "PRIVATE_TIMEOUT_HOST prompt=PRIVATE_PROMPT "
        "Authorization=Bearer PRIVATE_TOKEN key=PRIVATE_KEY"
    )

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> object:
            request = httpx.Request("POST", "https://private-timeout.example/v1")
            raise timeout_type(raw_failure, request=request)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    adapter = HttpxProviderAdapter(timeout_seconds=1.0)
    with pytest.raises(ProviderTimeoutError, match="^provider timeout$") as exc_info:
        await adapter.generate(
            request_id="req-httpx-timeout-classification",
            base_url="https://example.com/v1",
            api_key="PRIVATE_KEY",
            model="gpt-5.1",
            messages=_user_messages("PRIVATE_PROMPT"),
            route="chat",
            extra_body={},
        )

    public_error = str(exc_info.value)
    for sentinel in (
        "PRIVATE_TIMEOUT_HOST",
        "PRIVATE_PROMPT",
        "PRIVATE_TOKEN",
        "PRIVATE_KEY",
        "private-timeout.example",
    ):
        assert sentinel not in public_error


@pytest.mark.parametrize(
    ("request_error_type", "failure_label"),
    [
        pytest.param(httpx.ConnectError, "connect", id="connect-error"),
        pytest.param(
            httpx.RemoteProtocolError,
            "remote-protocol",
            id="remote-protocol-error",
        ),
        pytest.param(httpx.ProxyError, "proxy", id="proxy-error"),
        pytest.param(httpx.ConnectError, "tls-handshake", id="tls-request-error"),
    ],
)
@pytest.mark.asyncio
async def test_httpx_adapter_classifies_request_error_subclasses_safely(
    monkeypatch: pytest.MonkeyPatch,
    request_error_type: type[httpx.RequestError],
    failure_label: str,
):
    from backend.app.services.providers import HttpxProviderAdapter, ProviderTransportError

    raw_failure = (
        f"PRIVATE_{failure_label.upper()}_HOST prompt=PRIVATE_PROMPT "
        "Authorization=Bearer PRIVATE_TOKEN key=PRIVATE_KEY"
    )

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> object:
            request = httpx.Request("POST", "https://private-transport.example/v1")
            raise request_error_type(raw_failure, request=request)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    adapter = HttpxProviderAdapter(timeout_seconds=1.0)
    with pytest.raises(ProviderTransportError, match="^transport_error$") as exc_info:
        await adapter.generate(
            request_id="req-httpx-request-error-classification",
            base_url="https://example.com/v1",
            api_key="PRIVATE_KEY",
            model="gpt-5.1",
            messages=_user_messages("PRIVATE_PROMPT"),
            route="chat",
            extra_body={},
        )

    public_error = str(exc_info.value)
    for sentinel in (
        "PRIVATE_PROMPT",
        "PRIVATE_TOKEN",
        "PRIVATE_KEY",
        "private-transport.example",
    ):
        assert sentinel not in public_error


@pytest.mark.asyncio
async def test_all_formal_transport_failures_reach_local_fallback_only_after_every_route(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter, ProviderTransportError

    called_routes: list[tuple[str, str]] = []

    def adapter_for(route):
        async def _transport_failure(**_: object) -> dict[str, object]:
            called_routes.append((route.provider, route.model))
            raise ProviderTransportError("PRIVATE_UPSTREAM_DETAILS")

        return FakeAdapter(_transport_failure)

    health_service = ApiHealthService(conn)
    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=health_service,
        adapter_factory=adapter_for,
    )
    expected_routes = [
        (route.provider, route.model) for route in router.formal_chat_routes()
    ]

    response = await router.generate_chat(
        request_id="req-all-formal-transport-failures",
        messages=_user_messages("continue through every route"),
        is_test=False,
    )
    health_service.flush()

    assert called_routes == expected_routes
    assert response.used_local_fallback is True
    assert response.provider == "local-router"
    assert response.model == "fixed-text-fallback-v1"
    assert [
        (attempt.provider, attempt.model, attempt.status, attempt.error_code)
        for attempt in response.attempts
    ] == [
        (provider, model, "http_error", "transport_error")
        for provider, model in expected_routes
    ] + [
        ("local-router", "fixed-text-fallback-v1", "local_fallback", None)
    ]
    rows = conn.execute(
        """
        SELECT status, error_code, error_message_summary
        FROM api_call_logs
        WHERE request_id = ?
        ORDER BY id
        """,
        ("req-all-formal-transport-failures",),
    ).fetchall()
    serialized = " | ".join(str(dict(row)) for row in rows)
    assert len(rows) == len(expected_routes) + 1
    assert "PRIVATE_UPSTREAM_DETAILS" not in serialized


@pytest.mark.asyncio
async def test_cancelled_error_stops_routing_without_cooldown_or_fallback(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter

    called_routes: list[tuple[str, str]] = []

    def adapter_for(route):
        async def _cancel(**_: object) -> dict[str, object]:
            called_routes.append((route.provider, route.model))
            raise asyncio.CancelledError

        return FakeAdapter(_cancel)

    health_service = ApiHealthService(conn)
    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=health_service,
        adapter_factory=adapter_for,
    )
    first_route = router.formal_chat_routes()[0]

    with pytest.raises(asyncio.CancelledError):
        await router.generate_chat(
            request_id="req-cancel-routing",
            messages=_user_messages("cancel now"),
            is_test=False,
        )
    health_service.flush()

    assert called_routes == [(first_route.provider, first_route.model)]
    assert conn.execute(
        "SELECT COUNT(*) FROM api_call_logs WHERE request_id = ?",
        ("req-cancel-routing",),
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM provider_cooldowns").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_non_network_programming_error_crosses_real_adapter_and_router_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
):
    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter

    program_error = RuntimeError("programming defect sentinel")
    called_urls: list[str] = []

    class FailingAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FailingAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> object:
            called_urls.append(url)
            raise program_error

    monkeypatch.setattr(httpx, "AsyncClient", FailingAsyncClient)

    health_service = ApiHealthService(conn)
    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=health_service,
    )
    first_route = router.formal_chat_routes()[0]

    with pytest.raises(RuntimeError, match="^programming defect sentinel$") as exc_info:
        await router.generate_chat(
            request_id="req-programming-error",
            messages=_user_messages("do not recover from a programming defect"),
            is_test=False,
        )
    health_service.flush()

    assert exc_info.value is program_error
    assert called_urls == [f"{first_route.base_url}/chat/completions"]
    assert conn.execute(
        "SELECT COUNT(*) FROM api_call_logs WHERE request_id = ?",
        ("req-programming-error",),
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM provider_cooldowns").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_malformed_http_200_response_becomes_invalid_response_and_local_fallback(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    import httpx

    from backend.app.services.api_health import ApiHealthService
    from backend.app.services.providers import ProviderRouter

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            raise ValueError("malformed json response")

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    health_service = ApiHealthService(conn)
    router = ProviderRouter(
        settings=sqlite_settings,
        health_service=health_service,
    )

    response = await router.generate_chat(
        request_id="req-malformed-200",
        messages=_user_messages("hello"),
        is_test=True,
    )
    health_service.flush()

    assert response.used_local_fallback is True
    assert response.provider == "local-router"
    assert [attempt.status for attempt in response.attempts] == [
        "invalid_response",
        "local_fallback",
    ]
    assert response.model == "fixed-text-fallback-v1"

    rows = conn.execute(
        """
        SELECT provider, model, status, cooldown_applied, error_message_summary
        FROM api_call_logs
        WHERE request_id = ?
        ORDER BY id
        """,
        ("req-malformed-200",),
    ).fetchall()

    assert [dict(row) for row in rows] == [
        {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "status": "invalid_response",
            "cooldown_applied": 1,
            "error_message_summary": "invalid_response",
        },
        {
            "provider": "local-router",
            "model": "fixed-text-fallback-v1",
            "status": "local_fallback",
            "cooldown_applied": 0,
            "error_message_summary": "local_fallback",
        },
    ]


async def _unused_success(**_: object) -> dict[str, object]:
    return {"text": "unused"}


def _unauthorized_then_success(
    provider: str,
    model: str,
) -> Callable[..., Awaitable[dict[str, object]]]:
    async def _handler(**_: object) -> dict[str, object]:
        if provider == "yi-zhan" and model == "gpt-5.1":
            from backend.app.services.providers import ProviderHTTPError

            raise ProviderHTTPError(status_code=401, message="unauthorized")
        return {"text": f"{provider}/{model} ok"}

    return _handler


def _503_route_failure_then_success(
    provider: str,
    model: str,
    *,
    status_code: int,
    error_code: str,
) -> Callable[..., Awaitable[dict[str, object]]]:
    async def _handler(**_: object) -> dict[str, object]:
        if provider == "yi-zhan" and model == "gpt-5.1":
            from backend.app.services.providers import ProviderHTTPError

            raise ProviderHTTPError(
                status_code=status_code,
                message=f"{error_code} prompt=should-not-persist PROVIDER_KEY_SENTINEL",
                error_code=error_code,
            )
        return {"text": f"{provider}/{model} ok"}

    return _handler


def _error_then_success(
    provider: str,
    model: str,
    *,
    prompt: str,
    api_key: str,
) -> Callable[..., Awaitable[dict[str, object]]]:
    async def _handler(**_: object) -> dict[str, object]:
        if provider == "yi-zhan" and model == "gpt-5.1":
            from backend.app.services.providers import ProviderHTTPError

            raise ProviderHTTPError(
                status_code=500,
                message=f"http 500 prompt={prompt} api_key={api_key}",
            )
        return {"text": "Recovered response"}

    return _handler
