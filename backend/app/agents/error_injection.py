from __future__ import annotations

from backend.app.services.providers import ProviderResponse


def generation_fallback_prevents_error_presentation(
    *,
    provider_response: ProviderResponse | None = None,
    provider_status: str | None = None,
    provider_name: str | None = None,
    provider_route: str | None = None,
) -> bool:
    if provider_response is not None:
        if provider_response.used_local_fallback:
            return True
        if provider_response.provider == "local-router":
            return True
        if (
            provider_response.attempts
            and provider_response.attempts[-1].status == "local_fallback"
        ):
            return True
    return (
        provider_status == "local_fallback"
        or provider_name == "local-router"
        or provider_route == "local_fallback"
    )
