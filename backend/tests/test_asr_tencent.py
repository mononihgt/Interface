from __future__ import annotations

from datetime import datetime, timezone
import json

import httpx
import pytest

from backend.app.services.asr_tencent import TencentAsrClient
from backend.app.settings import Settings


@pytest.mark.parametrize(
    ("raw_text", "expected_text"),
    [
        ("[0:0.020,0:1.980]  你好呀。", "你好呀。"),
        (
            "[0:0.020,0:1.980]  你好呀。\n[0:2.000,0:4.500] 今天天气怎么样？",
            "你好呀。\n今天天气怎么样？",
        ),
        ("你好呀。", "你好呀。"),
        ("[提醒] 你好呀。", "[提醒] 你好呀。"),
        (
            "请保留 [0:0.020,0:1.980] 这一段。",
            "请保留 [0:0.020,0:1.980] 这一段。",
        ),
    ],
)
def test_tencent_asr_success_removes_only_line_leading_timestamps(
    raw_text: str,
    expected_text: str,
) -> None:
    settings = Settings(
        tencent_secret_id="test-id",
        tencent_secret_key="test-key",
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as http_client:
        client = TencentAsrClient(
            settings,
            http_client=http_client,
            monotonic_fn=lambda: 1.5,
        )
        result = client._parse_status_response(
            {
                "Response": {
                    "Data": {
                        "StatusStr": "success",
                        "Result": raw_text,
                    }
                }
            },
            started_at=1.0,
        )

    assert result is not None
    assert result.status == "success"
    assert result.text == expected_text


def test_tencent_asr_create_task_uses_supported_parameters() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        action = request.headers["X-TC-Action"]
        payload = json.loads(request.content)
        requests.append((action, payload))
        if action == "CreateRecTask":
            assert "UsrAudioKey" not in payload
            return httpx.Response(
                200,
                json={
                    "Response": {
                        "Data": {"TaskId": 42},
                        "RequestId": "create",
                    }
                },
            )
        assert action == "DescribeTaskStatus"
        assert payload == {"TaskId": 42}
        return httpx.Response(
            200,
            json={
                "Response": {
                    "Data": {
                        "StatusStr": "success",
                        "Result": "识别成功",
                    },
                    "RequestId": "status",
                }
            },
        )

    settings = Settings(
        tencent_secret_id="test-id",
        tencent_secret_key="test-key",
        tencent_asr_endpoint="asr.ap-hongkong.tencentcloudapi.com",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TencentAsrClient(
            settings,
            http_client=http_client,
            now_fn=lambda: datetime(2026, 7, 13, tzinfo=timezone.utc),
            monotonic_fn=lambda: 1.0,
        )
        result = client.transcribe(
            audio_bytes=b"browser webm audio",
            filename="turn.webm",
            content_type="audio/webm",
            request_id="internal-operation-id",
        )

    assert result.status == "success"
    assert result.text == "识别成功"
    assert [action for action, _ in requests] == [
        "CreateRecTask",
        "DescribeTaskStatus",
    ]
    create_payload = requests[0][1]
    assert create_payload["EngineModelType"] == "16k_zh"
    assert create_payload["SourceType"] == 1
    assert create_payload["DataLen"] == len(b"browser webm audio")
