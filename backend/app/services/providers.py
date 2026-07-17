from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from time import perf_counter
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Protocol, TypeVar

import httpx
from pydantic import BaseModel

from backend.app.services.api_health import ApiHealthService, LoggedProviderAttempt
from backend.app.settings import Settings

if TYPE_CHECKING:
    from backend.app.agents.structured import StructuredAgentResult


StructuredT = TypeVar("StructuredT", bound=BaseModel)
MAX_STRUCTURED_PARSE_ATTEMPTS = 2


LOCAL_FALLBACK_TEXT = "抱歉，我遇到了一些技术问题。请稍后再试。"
LOCAL_FALLBACK_PROVIDER = "local-router"
LOCAL_FALLBACK_MODEL = "fixed-text-fallback-v1"
GENERIC_PROVIDER_HTTP_ERROR_CODE = "http_error"
LONG_COOLDOWN_PROVIDER_HTTP_ERROR_CODES = frozenset(
    {"unavailable", "model_not_found"}
)


def normalize_provider_http_error_code(error_code: str | None) -> str:
    normalized = error_code.strip() if isinstance(error_code, str) else None
    if normalized in LONG_COOLDOWN_PROVIDER_HTTP_ERROR_CODES:
        return normalized
    return GENERIC_PROVIDER_HTTP_ERROR_CODE


def normalize_provider_error_code(*, status: object, error_code: object) -> str | None:
    normalized_status = str(status).strip() if status is not None else ""
    normalized_code = str(error_code).strip() if error_code is not None else None
    if normalized_status == "http_error":
        if normalized_code == "transport_error":
            return "transport_error"
        return normalize_provider_http_error_code(normalized_code)
    if normalized_status in {"timeout", "invalid_response", "not_configured"}:
        return normalized_status
    if normalized_status in {"", "success", "local_fallback"}:
        return None
    return "provider_error"


@dataclass(frozen=True)
class ProviderMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ProviderRoute:
    route: str
    provider: str
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float
    extra_body: dict[str, object]


@dataclass(frozen=True)
class ProviderAttempt:
    route: str
    provider: str
    model: str | None
    status: str
    latency_ms: int | None = None
    http_status: int | None = None
    error_code: str | None = None
    error_message_summary: str | None = None
    cooldown_applied: bool = False


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    provider: str | None
    model: str | None
    route: str
    attempts: list[ProviderAttempt]
    used_local_fallback: bool = False


class ProviderRoutesExhausted(RuntimeError):
    def __init__(self, attempts: list[ProviderAttempt]) -> None:
        super().__init__("provider_routes_exhausted")
        self.attempts = list(attempts)


class ProviderAdapter(Protocol):
    async def generate(
        self,
        *,
        request_id: str,
        base_url: str,
        api_key: str,
        model: str,
        messages: Sequence[ProviderMessage],
        route: str,
        extra_body: Mapping[str, object],
    ) -> dict[str, object]:
        ...


class ProviderHTTPError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_code = error_code


class ProviderTimeoutError(RuntimeError):
    pass


class ProviderTransportError(RuntimeError):
    pass


class InvalidProviderResponseError(RuntimeError):
    pass


class HttpxProviderAdapter:
    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    async def generate(
        self,
        *,
        request_id: str,
        base_url: str,
        api_key: str,
        model: str,
        messages: Sequence[ProviderMessage],
        route: str,
        extra_body: Mapping[str, object],
    ) -> dict[str, object]:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                }
                for message in messages
            ],
            **dict(extra_body),
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("provider timeout") from exc
        except httpx.RequestError as exc:
            raise ProviderTransportError("transport_error") from exc

        if response.status_code >= 400:
            error_code: str | None = None
            error_message = f"HTTP {response.status_code} from provider"
            try:
                data = response.json()
            except ValueError:
                data = None

            if isinstance(data, dict):
                error_payload = data.get("error")
                if isinstance(error_payload, dict):
                    raw_code = error_payload.get("code")
                    raw_message = error_payload.get("message")
                    if isinstance(raw_code, str) and raw_code.strip():
                        error_code = raw_code.strip()
                    if isinstance(raw_message, str) and raw_message.strip():
                        error_message = raw_message.strip()

            raise ProviderHTTPError(
                status_code=response.status_code,
                message=error_message,
                error_code=error_code,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise InvalidProviderResponseError("malformed json response") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidProviderResponseError("missing chat completion content") from exc

        if not isinstance(content, str) or not content.strip():
            raise InvalidProviderResponseError("empty chat completion content")

        return {"text": content}


class ProviderRouter:
    def __init__(
        self,
        *,
        settings: Settings,
        health_service: ApiHealthService,
        adapter_factory: Callable[[ProviderRoute], ProviderAdapter] | None = None,
    ) -> None:
        self._settings = settings
        self._health_service = health_service
        self._adapter_factory = adapter_factory or (
            lambda route: HttpxProviderAdapter(
                timeout_seconds=route.timeout_seconds
            )
        )

    def formal_chat_routes(self) -> list[ProviderRoute]:
        return self._chat_routes(test_only=False)

    def test_chat_routes(self) -> list[ProviderRoute]:
        return self._chat_routes(test_only=True)

    def evaluator_routes(self) -> list[ProviderRoute]:
        return [
            ProviderRoute(
                route="evaluator",
                provider="deepseek",
                model=self._settings.deepseek_model,
                base_url=self._settings.deepseek_base_url,
                api_key=self._settings.deepseek_api_key or "",
                timeout_seconds=self._settings.deepseek_timeout_seconds,
                extra_body={"thinking": {"type": "disabled"}},
            )
        ]

    async def generate_chat(
        self,
        *,
        request_id: str,
        messages: Sequence[ProviderMessage],
        is_test: bool,
        allow_local_fallback: bool = True,
    ) -> ProviderResponse:
        return await self._generate_with_routes(
            request_id=request_id,
            messages=messages,
            routes=self.test_chat_routes() if is_test else self.formal_chat_routes(),
            fallback_route="chat",
            fallback_text=LOCAL_FALLBACK_TEXT,
            allow_local_fallback=allow_local_fallback,
        )

    async def generate_structured_agent(
        self,
        *,
        request_id: str,
        messages: Sequence[ProviderMessage],
        is_test: bool,
        schema: type[StructuredT],
        max_parse_attempts: int = MAX_STRUCTURED_PARSE_ATTEMPTS,
        allow_local_fallback: bool = True,
        payload_normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> "StructuredAgentResult[StructuredT]":
        from backend.app.agents.structured import (
            StructuredAgentResult,
            parse_structured_output,
        )

        if not 1 <= max_parse_attempts <= MAX_STRUCTURED_PARSE_ATTEMPTS:
            raise ValueError(
                f"max_parse_attempts must be between 1 and {MAX_STRUCTURED_PARSE_ATTEMPTS}."
            )

        structured_messages = self._structured_messages(messages=messages, schema=schema)
        accumulated_attempts: list[ProviderAttempt] = []
        last_response: ProviderResponse | None = None
        last_validation_error = "invalid_json_object"

        for parse_attempt in range(max_parse_attempts):
            response = await self.generate_chat(
                request_id=request_id,
                messages=structured_messages,
                is_test=is_test,
                allow_local_fallback=allow_local_fallback,
            )
            accumulated_attempts.extend(response.attempts)
            response = ProviderResponse(
                text=response.text,
                provider=response.provider,
                model=response.model,
                route=response.route,
                attempts=list(accumulated_attempts),
                used_local_fallback=response.used_local_fallback,
            )
            last_response = response

            if response.used_local_fallback:
                return StructuredAgentResult(
                    value=None,
                    response=response,
                    validation_error="provider_local_fallback",
                    parse_attempts=parse_attempt + 1,
                )

            parsed = parse_structured_output(
                response.text,
                schema,
                payload_normalizer=payload_normalizer,
            )
            if parsed.value is not None:
                return StructuredAgentResult(
                    value=parsed.value,
                    response=response,
                    validation_error=None,
                    parse_attempts=parse_attempt + 1,
                )
            last_validation_error = parsed.validation_error or "schema_validation_error"
            if parse_attempt + 1 < max_parse_attempts:
                structured_messages = self._structured_messages(
                    messages=messages,
                    schema=schema,
                    correction_code=last_validation_error,
                )

        if last_response is None:
            raise RuntimeError("Structured generation completed without a provider response.")
        return StructuredAgentResult(
            value=None,
            response=last_response,
            validation_error=last_validation_error,
            parse_attempts=max_parse_attempts,
        )

    async def generate_evaluator(
        self,
        *,
        request_id: str,
        messages: Sequence[ProviderMessage],
    ) -> ProviderResponse:
        return await self._generate_with_routes(
            request_id=request_id,
            messages=messages,
            routes=self.evaluator_routes(),
            fallback_route="evaluator",
            fallback_text='{"pass": false, "reason": "evaluator_unavailable"}',
            allow_local_fallback=True,
        )

    async def _generate_with_routes(
        self,
        *,
        request_id: str,
        messages: Sequence[ProviderMessage],
        routes: list[ProviderRoute],
        fallback_route: str,
        fallback_text: str,
        allow_local_fallback: bool,
    ) -> ProviderResponse:
        attempts: list[ProviderAttempt] = []
        for route in routes:
            if self._health_service.is_on_cooldown(
                route=route.route,
                provider=route.provider,
                model=route.model,
            ):
                continue

            text, attempt = await self.attempt_route(
                request_id=request_id,
                messages=messages,
                route=route,
            )
            attempts.append(attempt)
            if text is None:
                continue
            return ProviderResponse(
                text=text,
                provider=route.provider,
                model=route.model,
                route=route.route,
                attempts=attempts,
            )

        if not allow_local_fallback:
            raise ProviderRoutesExhausted(attempts)

        fallback_attempt = ProviderAttempt(
            route=fallback_route,
            provider=LOCAL_FALLBACK_PROVIDER,
            model=LOCAL_FALLBACK_MODEL,
            status="local_fallback",
            error_message_summary="provider route exhausted",
        )
        self._record_attempt(request_id=request_id, attempt=fallback_attempt)
        attempts.append(fallback_attempt)
        return ProviderResponse(
            text=fallback_text,
            provider=LOCAL_FALLBACK_PROVIDER,
            model=LOCAL_FALLBACK_MODEL,
            route=fallback_route,
            attempts=attempts,
            used_local_fallback=True,
        )

    async def attempt_route(
        self,
        *,
        request_id: str,
        messages: Sequence[ProviderMessage],
        route: ProviderRoute,
    ) -> tuple[str | None, ProviderAttempt]:
        """Execute and record exactly one external route without local fallback."""
        adapter = self._adapter_factory(route)
        started_at = perf_counter()
        try:
            async with asyncio.timeout(route.timeout_seconds):
                payload = await adapter.generate(
                    request_id=request_id,
                    base_url=route.base_url,
                    api_key=route.api_key,
                    model=route.model,
                    messages=messages,
                    route=route.route,
                    extra_body=route.extra_body,
                )
            text = self._extract_text(payload)
        except ProviderHTTPError as exc:
            error_code = normalize_provider_http_error_code(exc.error_code)
            cooldown_seconds = self._cooldown_seconds_for_http_error(
                status_code=exc.status_code,
                error_code=error_code,
            )
            attempt = ProviderAttempt(
                route=route.route,
                provider=route.provider,
                model=route.model,
                status="http_error",
                latency_ms=self._elapsed_ms(started_at),
                http_status=exc.status_code,
                error_code=error_code,
                error_message_summary=(
                    f"http_error:{exc.status_code}:{error_code}"
                ),
                cooldown_applied=cooldown_seconds > 0,
            )
            self._record_failure(
                request_id=request_id,
                route=route,
                attempt=attempt,
                cooldown_seconds=cooldown_seconds,
            )
            return None, attempt
        except ProviderTransportError:
            attempt = ProviderAttempt(
                route=route.route,
                provider=route.provider,
                model=route.model,
                status="http_error",
                latency_ms=self._elapsed_ms(started_at),
                error_code="transport_error",
                error_message_summary="transport_error",
                cooldown_applied=True,
            )
            self._record_failure(
                request_id=request_id,
                route=route,
                attempt=attempt,
                cooldown_seconds=self._settings.provider_cooldown_seconds,
            )
            return None, attempt
        except (TimeoutError, ProviderTimeoutError):
            attempt = ProviderAttempt(
                route=route.route,
                provider=route.provider,
                model=route.model,
                status="timeout",
                latency_ms=self._elapsed_ms(started_at),
                error_code="timeout",
                error_message_summary="provider timeout",
                cooldown_applied=True,
            )
            self._record_failure(
                request_id=request_id,
                route=route,
                attempt=attempt,
                cooldown_seconds=self._settings.provider_cooldown_seconds,
            )
            return None, attempt
        except InvalidProviderResponseError as exc:
            attempt = ProviderAttempt(
                route=route.route,
                provider=route.provider,
                model=route.model,
                status="invalid_response",
                latency_ms=self._elapsed_ms(started_at),
                error_message_summary=str(exc),
                cooldown_applied=True,
            )
            self._record_failure(
                request_id=request_id,
                route=route,
                attempt=attempt,
                cooldown_seconds=self._settings.provider_cooldown_seconds,
            )
            return None, attempt

        attempt = ProviderAttempt(
            route=route.route,
            provider=route.provider,
            model=route.model,
            status="success",
            latency_ms=self._elapsed_ms(started_at),
        )
        self._record_attempt(request_id=request_id, attempt=attempt)
        return text, attempt

    def _record_failure(
        self,
        *,
        request_id: str,
        route: ProviderRoute,
        attempt: ProviderAttempt,
        cooldown_seconds: int,
    ) -> None:
        self._record_attempt(request_id=request_id, attempt=attempt)
        if cooldown_seconds > 0:
            self._health_service.apply_cooldown(
                route=route.route,
                provider=route.provider,
                model=route.model,
                seconds=cooldown_seconds,
            )

    def _chat_routes(self, *, test_only: bool) -> list[ProviderRoute]:
        if test_only:
            if not self._settings.deepseek_api_key:
                return []
            return [
                ProviderRoute(
                    route="chat",
                    provider="deepseek",
                    model=self._settings.deepseek_model,
                    base_url=self._settings.deepseek_base_url,
                    api_key=self._settings.deepseek_api_key,
                    timeout_seconds=self._settings.deepseek_timeout_seconds,
                    extra_body={"thinking": {"type": "disabled"}},
                )
            ]

        providers = [
                ("yi-zhan", self._settings.yizhan_base_url, self._settings.yizhan_api_key),
                ("aabao", self._settings.aabao_base_url, self._settings.aabao_api_key),
                (
                    "packyapi",
                    self._settings.packyapi_base_url,
                    self._settings.packyapi_api_key,
                ),
            ]

        routes: list[ProviderRoute] = []
        for provider, base_url, api_key in providers:
            if not api_key:
                continue
            routes.append(
                ProviderRoute(
                    route="chat",
                    provider=provider,
                    model=self._settings.main_model_primary,
                    base_url=base_url,
                    api_key=api_key,
                    timeout_seconds=self._settings.provider_timeout_seconds,
                    extra_body={},
                )
            )
            routes.append(
                ProviderRoute(
                    route="chat",
                    provider=provider,
                    model=self._settings.main_model_fallback,
                    base_url=base_url,
                    api_key=api_key,
                    timeout_seconds=self._settings.provider_timeout_seconds,
                    extra_body={},
                )
            )
        if self._settings.deepseek_api_key:
            routes.append(
                ProviderRoute(
                    route="chat",
                    provider="deepseek",
                    model=self._settings.deepseek_model,
                    base_url=self._settings.deepseek_base_url,
                    api_key=self._settings.deepseek_api_key,
                    timeout_seconds=self._settings.deepseek_timeout_seconds,
                    extra_body={"thinking": {"type": "disabled"}},
                )
            )
        return routes

    def _record_attempt(self, *, request_id: str, attempt: ProviderAttempt) -> None:
        self._health_service.log_attempt(
            request_id=request_id,
            attempt=LoggedProviderAttempt(
                route=attempt.route,
                provider=attempt.provider,
                model=attempt.model,
                status=attempt.status,
                http_status=attempt.http_status,
                error_code=attempt.error_code,
                error_message_summary=attempt.error_message_summary,
                latency_ms=attempt.latency_ms,
                cooldown_applied=attempt.cooldown_applied,
            ),
        )

    def _structured_messages(
        self,
        *,
        messages: Sequence[ProviderMessage],
        schema: type[BaseModel],
        correction_code: str | None = None,
    ) -> list[ProviderMessage]:
        schema_json = json.dumps(
            schema.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        instruction = (
            "Return exactly one valid JSON object matching this JSON Schema. "
            "Do not use Markdown fences and do not include prose outside JSON. "
            f"Schema: {schema_json}"
        )
        if correction_code is not None:
            instruction += (
                " The previous response failed validation with safe code "
                f"{correction_code}; correct the shape without quoting the previous response."
            )
        structured_instruction = ProviderMessage(role="system", content=instruction)
        if messages and messages[0].role == "system":
            return [messages[0], structured_instruction, *messages[1:]]
        return [structured_instruction, *messages]

    def _extract_text(self, payload: dict[str, object]) -> str:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidProviderResponseError("missing text field")
        return text

    def _cooldown_seconds_for_http_error(
        self,
        *,
        status_code: int,
        error_code: str,
    ) -> int:
        if status_code in (401, 403):
            return self._settings.provider_unauthorized_cooldown_seconds
        if (
            status_code == 503
            and error_code in LONG_COOLDOWN_PROVIDER_HTTP_ERROR_CODES
        ):
            return self._settings.provider_unauthorized_cooldown_seconds
        return self._settings.provider_cooldown_seconds

    def _elapsed_ms(self, started_at: float) -> int:
        return int((perf_counter() - started_at) * 1000)
