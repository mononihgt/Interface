from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from backend.app.services.api_health import ApiHealthService
from backend.app.services.providers import (
    ProviderAdapter,
    ProviderMessage,
    ProviderRoute,
    ProviderRouter,
    normalize_provider_error_code,
)
from backend.app.settings import Settings


@dataclass(frozen=True)
class ProviderTestResult:
    status: str
    provider: str
    model: str
    latency_ms: int | None
    error_code: str | None = None


class ProviderTestService:
    def __init__(
        self,
        *,
        settings: Settings,
        health_service: ApiHealthService,
        adapter_factory: Callable[[ProviderRoute], ProviderAdapter] | None = None,
    ) -> None:
        self._settings = settings
        self._router = ProviderRouter(
            settings=settings,
            health_service=health_service,
            adapter_factory=adapter_factory,
        )

    def test_deepseek(self, request_id: str) -> ProviderTestResult:
        if not (
            self._settings.deepseek_api_key
            and self._settings.deepseek_api_key.strip()
        ):
            return ProviderTestResult(
                status="not_configured",
                provider="deepseek",
                model=self._settings.deepseek_model,
                latency_ms=None,
                error_code="not_configured",
            )

        route = ProviderRoute(
            route="chat",
            provider="deepseek",
            model=self._settings.deepseek_model,
            base_url=self._settings.deepseek_base_url,
            api_key=self._settings.deepseek_api_key,
            timeout_seconds=self._settings.deepseek_timeout_seconds,
            extra_body={"thinking": {"type": "disabled"}},
        )
        _, attempt = asyncio.run(
            self._router.attempt_route(
                request_id=request_id,
                messages=[ProviderMessage(role="user", content="health-check")],
                route=route,
            )
        )
        return ProviderTestResult(
            status=attempt.status,
            provider="deepseek",
            model=self._settings.deepseek_model,
            latency_ms=attempt.latency_ms,
            error_code=normalize_provider_error_code(
                status=attempt.status,
                error_code=attempt.error_code,
            ),
        )
