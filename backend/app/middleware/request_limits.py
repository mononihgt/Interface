from __future__ import annotations

from typing import Any, Awaitable, Callable

from starlette.responses import JSONResponse


AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]
AsgiApp = Callable[[dict[str, Any], AsgiReceive, AsgiSend], Awaitable[None]]


class RequestBodyTooLarge(BaseException):
    pass


class AsrRequestBodyLimitMiddleware:
    def __init__(self, app: AsgiApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/api/asr"
        ):
            await self._app(scope, receive, send)
            return

        content_length = self._content_length(scope)
        if content_length is not None and content_length > self._max_bytes:
            await self._reject(scope, receive, send)
            return

        consumed_bytes = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal consumed_bytes
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                consumed_bytes += len(body) if isinstance(body, bytes) else 0
                if consumed_bytes > self._max_bytes:
                    raise RequestBodyTooLarge
            return message

        try:
            await self._app(scope, limited_receive, send)
        except RequestBodyTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    def _content_length(scope: dict[str, Any]) -> int | None:
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        return None

    async def _reject(
        self,
        scope: dict[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "detail": (
                    "ASR request body exceeds the "
                    f"{self._max_bytes} byte limit."
                )
            },
        )
        await response(scope, receive, send)
