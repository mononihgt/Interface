from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import time
from typing import Callable, Protocol

import httpx

from backend.app.settings import Settings


_TENCENT_SEGMENT_TIMESTAMP_PATTERN = re.compile(
    r"^\[\d+:\d+(?:\.\d+)?,\d+:\d+(?:\.\d+)?\]\s*",
    re.MULTILINE,
)


def _normalize_transcript(text: str) -> str:
    return _TENCENT_SEGMENT_TIMESTAMP_PATTERN.sub("", text).strip()


@dataclass(frozen=True)
class AsrResult:
    status: str
    provider: str
    text: str | None
    latency_ms: int | None


class AsrClient(Protocol):
    def transcribe(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str | None,
        request_id: str,
    ) -> AsrResult:
        ...


def read_bounded_audio_file(audio_path: Path, *, max_bytes: int) -> bytes:
    with audio_path.open("rb") as audio_file:
        audio_bytes = audio_file.read(max_bytes + 1)
    if not audio_bytes:
        raise ValueError("Audio upload must not be empty.")
    if len(audio_bytes) > max_bytes:
        raise ValueError(f"Audio upload exceeds the {max_bytes} byte limit.")
    return audio_bytes


class TencentAsrClient:
    _service = "asr"
    _version = "2019-06-14"
    _region = "ap-hongkong"
    _provider = "tencent"

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.Client | None = None,
        now_fn: Callable[[], datetime] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._http_client = http_client or httpx.Client(timeout=settings.asr_timeout_seconds)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._monotonic_fn = monotonic_fn or time.monotonic

    def transcribe(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str | None,
        request_id: str,
    ) -> AsrResult:
        if not self._settings.tencent_secret_id or not self._settings.tencent_secret_key:
            raise RuntimeError("Tencent ASR credentials are not configured.")
        if not audio_bytes:
            raise ValueError("Audio upload must not be empty.")
        if len(audio_bytes) > self._settings.asr_max_upload_bytes:
            raise ValueError(
                f"Audio upload exceeds the {self._settings.asr_max_upload_bytes} byte limit."
            )

        started = self._monotonic_fn()
        create_payload = {
            "EngineModelType": "16k_zh",
            "ChannelNum": 1,
            "ResTextFormat": 0,
            "SourceType": 1,
            "Data": base64.b64encode(audio_bytes).decode("ascii"),
            "DataLen": len(audio_bytes),
        }
        create_response = self._post_action(
            action="CreateRecTask",
            payload=create_payload,
        )
        task_id = self._extract_task_id(create_response)
        deadline = started + self._settings.asr_poll_timeout_seconds

        while self._monotonic_fn() <= deadline:
            status_response = self._post_action(
                action="DescribeTaskStatus",
                payload={"TaskId": task_id},
            )
            result = self._parse_status_response(status_response, started_at=started)
            if result is not None:
                return result
            time.sleep(0.5)

        return AsrResult(
            status="timeout",
            provider=self._provider,
            text=None,
            latency_ms=int((self._monotonic_fn() - started) * 1000),
        )

    def _post_action(self, *, action: str, payload: dict[str, object]) -> dict[str, object]:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        timestamp = int(self._now_fn().timestamp())
        headers = self._build_headers(
            action=action,
            body=body,
            timestamp=timestamp,
        )
        response = self._http_client.post(
            f"https://{self._settings.tencent_asr_endpoint}",
            content=body.encode("utf-8"),
            headers=headers,
        )
        response.raise_for_status()
        return response.json()

    def _build_headers(self, *, action: str, body: str, timestamp: int) -> dict[str, str]:
        canonical_uri = "/"
        canonical_query = ""
        canonical_headers = (
            f"content-type:application/json; charset=utf-8\nhost:{self._settings.tencent_asr_endpoint}\n"
        )
        signed_headers = "content-type;host"
        hashed_payload = hashlib.sha256(body.encode("utf-8")).hexdigest()
        canonical_request = "\n".join(
            [
                "POST",
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                hashed_payload,
            ]
        )

        date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
        credential_scope = f"{date}/{self._service}/tc3_request"
        string_to_sign = "\n".join(
            [
                "TC3-HMAC-SHA256",
                str(timestamp),
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = self._sign(
            secret_key=self._settings.tencent_secret_key or "",
            date=date,
            string_to_sign=string_to_sign,
        )
        authorization = (
            "TC3-HMAC-SHA256 "
            f"Credential={self._settings.tencent_secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": self._settings.tencent_asr_endpoint,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": self._version,
            "X-TC-Region": self._region,
        }

    def _sign(self, *, secret_key: str, date: str, string_to_sign: str) -> str:
        secret_date = hmac.new(
            f"TC3{secret_key}".encode("utf-8"),
            date.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        secret_service = hmac.new(
            secret_date,
            self._service.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        secret_signing = hmac.new(
            secret_service,
            b"tc3_request",
            hashlib.sha256,
        ).digest()
        return hmac.new(
            secret_signing,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _extract_task_id(self, response: dict[str, object]) -> int:
        response_body = response.get("Response", response)
        if not isinstance(response_body, dict):
            raise RuntimeError("Tencent ASR returned an invalid create response.")
        task_id = response_body.get("Data", {}).get("TaskId")
        if task_id is None:
            task_id = response_body.get("TaskId")
        if task_id is None:
            raise RuntimeError("Tencent ASR create response did not include TaskId.")
        return int(task_id)

    def _parse_status_response(
        self,
        response: dict[str, object],
        *,
        started_at: float,
    ) -> AsrResult | None:
        response_body = response.get("Response", response)
        if not isinstance(response_body, dict):
            raise RuntimeError("Tencent ASR returned an invalid status response.")

        data = response_body.get("Data", response_body)
        if not isinstance(data, dict):
            raise RuntimeError("Tencent ASR returned malformed task data.")

        status = str(data.get("StatusStr") or data.get("Status") or "").lower()
        if status in {"waiting", "doing", "running", "0", "1"}:
            return None

        latency_ms = int((self._monotonic_fn() - started_at) * 1000)
        if status in {"success", "succeeded", "2"}:
            text = data.get("Result") or data.get("ResultText")
            return AsrResult(
                status="success",
                provider=self._provider,
                text=_normalize_transcript(str(text)) if text is not None else "",
                latency_ms=latency_ms,
            )

        return AsrResult(
            status="failed",
            provider=self._provider,
            text=None,
            latency_ms=latency_ms,
        )


def get_asr_client(settings: Settings) -> AsrClient:
    return TencentAsrClient(settings)
