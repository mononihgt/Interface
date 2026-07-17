from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import math
import os
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from backend.app.settings import Settings


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: object | None = None,
        json_error: ValueError | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def _truncate_daily_forecast(payload: dict[str, object]) -> None:
    daily = payload["daily"]
    assert isinstance(daily, dict)
    for values in daily.values():
        assert isinstance(values, list)
        values.pop()


class FakeAsyncClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, object]] = []

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, FakeResponse)
        return response


def _geocoding_payload() -> dict[str, object]:
    return {
        "results": [
            {
                "name": "杭州",
                "latitude": 30.29365,
                "longitude": 120.16142,
                "timezone": "Asia/Shanghai",
                "country": "中国",
                "country_code": "CN",
                "admin1": "浙江",
                "admin2": "杭州市",
                "population": 9_236_032,
            },
            {
                "name": "杭州",
                "latitude": 22.3,
                "longitude": 114.1,
                "timezone": "Asia/Hong_Kong",
                "country": "中国",
                "country_code": "CN",
                "admin1": "香港",
                "admin2": None,
                "population": 20_000,
            },
        ]
    }


def _forecast_payload() -> dict[str, object]:
    return {
        "latitude": 30.263618,
        "longitude": 120.14051,
        "timezone": "Asia/Shanghai",
        "current": {
            "time": "2026-07-12T19:00",
            "temperature_2m": 28.2,
            "relative_humidity_2m": 84,
            "apparent_temperature": 32.2,
            "wind_speed_10m": 5.35,
            "weather_code": 3,
        },
        "daily": {
            "time": [
                "2026-07-12",
                "2026-07-13",
                "2026-07-14",
                "2026-07-15",
                "2026-07-16",
                "2026-07-17",
                "2026-07-18",
            ],
            "weather_code": [81, 80, 3, 2, 1, 61, 63],
            "temperature_2m_max": [30.3, 31.0, 32.0, 33.0, 34.0, 31.0, 29.0],
            "temperature_2m_min": [25.4, 25.0, 25.5, 26.0, 26.5, 24.0, 23.0],
            "precipitation_probability_max": [100, 70, 20, 10, 10, 80, 90],
            "wind_speed_10m_max": [9.84, 8.0, 6.0, 5.0, 4.0, 7.0, 8.5],
        },
    }


def _service_with_responses(
    responses: list[object],
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
):
    from backend.app.services.weather import WeatherService

    client = FakeAsyncClient(responses)
    observed_timeout: list[float] = []

    def client_factory(timeout_seconds: float) -> FakeAsyncClient:
        observed_timeout.append(timeout_seconds)
        return client

    service = WeatherService(
        settings=settings or Settings(app_env="test"),
        client_factory=client_factory,
        now_fn=lambda: now or datetime(2026, 7, 12, 11, 2, tzinfo=timezone.utc),
    )
    return service, client, observed_timeout


@pytest.mark.parametrize(
    "user_text",
    ["明天呢？", "明天会下雨吗？", "后天适合出门吗？", "要带伞吗？", "湿度怎么样？"],
)
def test_extract_weather_location_rejects_locationless_followups(user_text: str):
    from backend.app.services.weather import extract_weather_location

    assert extract_weather_location(user_text) is None


@pytest.mark.parametrize(
    ("user_text", "expected_location"),
    [
        ("杭州明天会下雨吗？", "杭州"),
        ("巴黎湿度怎么样？", "巴黎"),
        ("东京风速如何？", "东京"),
        ("请查询阿坝地区天气", "阿坝地区"),
        ("London weather tomorrow", "London"),
    ],
)
def test_extract_weather_location_accepts_explicit_locations(
    user_text: str,
    expected_location: str,
):
    from backend.app.services.weather import extract_weather_location

    assert extract_weather_location(user_text) == expected_location


def test_select_open_meteo_place_uses_name_admin_country_and_population():
    from backend.app.services.weather import select_open_meteo_place

    results = _geocoding_payload()["results"]
    assert isinstance(results, list)
    selected = select_open_meteo_place("杭州", results, country_code="")
    assert selected["admin1"] == "浙江"

    foreign_population_winner = [
        {**results[0], "country_code": "CN", "population": 10},
        {**results[1], "country_code": "GB", "population": 4_000_000},
    ]
    selected_country = select_open_meteo_place(
        "杭州",
        foreign_population_winner,
        country_code="CN",
    )
    assert selected_country["country_code"] == "CN"


def test_select_open_meteo_place_ignores_invalid_candidates():
    from backend.app.services.weather import select_open_meteo_place

    results = _geocoding_payload()["results"]
    assert isinstance(results, list)
    invalid_population_winner = {
        **results[0],
        "latitude": 999,
        "population": 99_999_999,
    }

    selected = select_open_meteo_place(
        "杭州",
        [invalid_population_winner, results[1]],
        country_code="",
    )

    assert selected["latitude"] == 22.3


def test_select_open_meteo_place_reports_no_legal_candidate():
    from backend.app.services.weather import WeatherServiceError, select_open_meteo_place

    with pytest.raises(WeatherServiceError) as exc_info:
        select_open_meteo_place(
            "杭州",
            [
                {
                    "name": "杭州",
                    "latitude": "30.2",
                    "longitude": 120.1,
                    "timezone": "Asia/Shanghai",
                }
            ],
            country_code="",
        )

    assert exc_info.value.code == "location_not_found"


@pytest.mark.parametrize(
    "time_value",
    [
        pytest.param("2026-07-12", id="date-only"),
        pytest.param("2026-07-12T19", id="missing-minutes"),
        pytest.param("2026-07-12T19:00Z", id="utc-suffix"),
        pytest.param("2026-07-12T19:00+08:00", id="offset-suffix"),
        pytest.param(datetime(2026, 7, 12, 19, 0), id="datetime-object"),
        pytest.param(1_752_321_600, id="integer-coercion"),
    ],
)
def test_weather_current_rejects_non_open_meteo_local_datetime(time_value: object):
    from pydantic import ValidationError

    from backend.app.services.weather import WeatherCurrent

    with pytest.raises(ValidationError):
        WeatherCurrent(
            time=time_value,
            temperature_c=28.2,
            relative_humidity_percent=84,
            apparent_temperature_c=32.2,
            wind_speed_mps=5.35,
            weather_code=3,
        )


@pytest.mark.parametrize("time_value", ["2026-07-12T19:00", "2026-07-12T19:00:30"])
def test_weather_current_accepts_open_meteo_local_datetime(time_value: str):
    from backend.app.services.weather import WeatherCurrent

    current = WeatherCurrent(
        time=time_value,
        temperature_c=28.2,
        relative_humidity_percent=84,
        apparent_temperature_c=32.2,
        wind_speed_mps=5.35,
        weather_code=3,
    )

    assert current.time == datetime.fromisoformat(time_value)


@pytest.mark.parametrize(
    "date_value",
    [
        pytest.param("2026-07-12T00:00", id="datetime-minutes"),
        pytest.param("2026-07-12T00:00:00", id="datetime-seconds"),
        pytest.param(date(2026, 7, 12), id="date-object"),
        pytest.param(datetime(2026, 7, 12, 0, 0), id="datetime-object"),
        pytest.param(1_752_278_400, id="integer-coercion"),
    ],
)
def test_weather_daily_rejects_non_open_meteo_date(date_value: object):
    from pydantic import ValidationError

    from backend.app.services.weather import WeatherDaily

    with pytest.raises(ValidationError):
        WeatherDaily(
            date=date_value,
            weather_code=80,
            temperature_max_c=31,
            temperature_min_c=25,
            precipitation_probability_percent=70,
            wind_speed_max_mps=8,
        )


def test_weather_daily_accepts_open_meteo_date():
    from backend.app.services.weather import WeatherDaily

    daily = WeatherDaily(
        date="2026-07-12",
        weather_code=80,
        temperature_max_c=31,
        temperature_min_c=25,
        precipitation_probability_percent=70,
        wind_speed_max_mps=8,
    )

    assert daily.date.isoformat() == "2026-07-12"


@pytest.mark.asyncio
async def test_lookup_uses_exact_open_meteo_params_without_identity_or_auth():
    from backend.app.services.weather import WeatherService

    service, client, observed_timeout = _service_with_responses(
        [
            FakeResponse(payload=_geocoding_payload()),
            FakeResponse(payload={"results": []}),
            FakeResponse(payload=_forecast_payload()),
        ]
    )

    snapshot = await service.lookup("杭州")

    assert snapshot.provider == "openmeteo"
    assert snapshot.query == "杭州"
    assert snapshot.location.name == "杭州"
    assert len(snapshot.daily) == 7
    assert observed_timeout == [20.0]
    assert client.requests == [
        {
            "url": GEOCODING_URL,
            "params": {
                "name": "杭州",
                "count": 10,
                "language": "zh",
                "format": "json",
            },
        },
        {
            "url": GEOCODING_URL,
            "params": {
                "name": "杭州市",
                "count": 10,
                "language": "zh",
                "format": "json",
            },
        },
        {
            "url": FORECAST_URL,
            "params": WeatherService.forecast_params(
                latitude=30.29365,
                longitude=120.16142,
            ),
        },
    ]
    serialized_requests = repr(client.requests).lower()
    for forbidden in (
        "authorization",
        "api_key",
        "participant",
        "session",
        "phone",
        "name-of-person",
    ):
        assert forbidden not in serialized_requests


@pytest.mark.asyncio
async def test_lookup_honors_optional_country_code():
    settings = Settings(app_env="test", open_meteo_country_code="CN")
    service, client, _ = _service_with_responses(
        [
            FakeResponse(payload=_geocoding_payload()),
            FakeResponse(payload={"results": []}),
            FakeResponse(payload=_forecast_payload()),
        ],
        settings=settings,
    )

    await service.lookup("杭州")

    assert client.requests[0]["params"]["country_code"] == "CN"
    assert client.requests[1]["params"]["country_code"] == "CN"


@pytest.mark.asyncio
async def test_lookup_reports_location_not_found_without_forecast_call():
    from backend.app.services.weather import WeatherServiceError

    service, client, _ = _service_with_responses(
        [
            FakeResponse(payload={"results": []}),
            FakeResponse(payload={"results": []}),
        ]
    )

    with pytest.raises(WeatherServiceError) as exc_info:
        await service.lookup("火星城")

    assert exc_info.value.code == "location_not_found"
    assert [request["url"] for request in client.requests] == [
        GEOCODING_URL,
        GEOCODING_URL,
    ]


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        pytest.param(httpx.ConnectTimeout("slow"), "timeout", id="timeout"),
        pytest.param(httpx.ConnectError("dns"), "transport_error", id="transport"),
    ],
)
@pytest.mark.asyncio
async def test_lookup_classifies_timeout_before_request_error(
    failure: httpx.RequestError,
    expected_code: str,
):
    from backend.app.services.weather import WeatherServiceError

    request = httpx.Request("GET", GEOCODING_URL)
    failure.request = request
    service, _, _ = _service_with_responses([failure])

    with pytest.raises(WeatherServiceError) as exc_info:
        await service.lookup("杭州")

    assert exc_info.value.code == expected_code
    assert str(exc_info.value) == expected_code


@pytest.mark.parametrize(
    ("responses", "expected_code"),
    [
        pytest.param(
            [
                FakeResponse(status_code=302, payload=_geocoding_payload()),
                FakeResponse(payload={"results": []}),
                FakeResponse(payload=_forecast_payload()),
            ],
            "http_error",
            id="redirect-302",
        ),
        pytest.param(
            [
                FakeResponse(status_code=307, payload=_geocoding_payload()),
                FakeResponse(payload={"results": []}),
                FakeResponse(payload=_forecast_payload()),
            ],
            "http_error",
            id="redirect-307",
        ),
        pytest.param(
            [FakeResponse(status_code=503, payload={})],
            "http_error",
            id="http-error",
        ),
        pytest.param(
            [FakeResponse(json_error=ValueError("html"))],
            "invalid_response",
            id="invalid-json",
        ),
    ],
)
@pytest.mark.asyncio
async def test_lookup_classifies_http_and_json_failures(
    responses: list[object],
    expected_code: str,
):
    from backend.app.services.weather import WeatherServiceError

    service, _, _ = _service_with_responses(responses)

    with pytest.raises(WeatherServiceError) as exc_info:
        await service.lookup("杭州")

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(("current", "temperature_2m"), math.inf, id="infinite-temperature"),
        pytest.param(("current", "relative_humidity_2m"), 101, id="humidity-range"),
        pytest.param(("current", "wind_speed_10m"), -1, id="negative-wind"),
        pytest.param(
            ("current", "temperature_2m"),
            "28.2",
            id="numeric-string",
        ),
        pytest.param(
            ("current", "weather_code"),
            "3",
            id="weather-code-string",
        ),
        pytest.param(
            ("daily", "precipitation_probability_max", 0),
            101,
            id="precipitation-range",
        ),
        pytest.param(
            ("daily", "temperature_2m_max", 0),
            20.0,
            id="daily-maximum-below-minimum",
        ),
    ],
)
@pytest.mark.asyncio
async def test_lookup_rejects_non_finite_and_out_of_range_weather_values(
    path: tuple[str | int, ...],
    value: object,
):
    from backend.app.services.weather import WeatherServiceError

    payload = deepcopy(_forecast_payload())
    target: Any = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    service, _, _ = _service_with_responses(
        [
            FakeResponse(payload=_geocoding_payload()),
            FakeResponse(payload={"results": []}),
            FakeResponse(payload=payload),
        ]
    )

    with pytest.raises(WeatherServiceError) as exc_info:
        await service.lookup("杭州")

    assert exc_info.value.code == "invalid_response"


@pytest.mark.parametrize(
    "mutate_forecast",
    [
        pytest.param(
            lambda payload: payload["current"].pop("temperature_2m"),
            id="missing-current-field",
        ),
        pytest.param(
            lambda payload: payload["daily"]["wind_speed_10m_max"].pop(),
            id="unequal-daily-arrays",
        ),
        pytest.param(
            _truncate_daily_forecast,
            id="six-day-forecast",
        ),
        pytest.param(
            lambda payload: payload["daily"]["time"].__setitem__(2, "2026-07-16"),
            id="non-contiguous-dates",
        ),
        pytest.param(
            lambda payload: payload["current"].__setitem__(
                "time",
                datetime(2026, 7, 12, 19, 0),
            ),
            id="current-time-not-string",
        ),
        pytest.param(
            lambda payload: payload["daily"].__setitem__(
                "time",
                [
                    (date(2026, 7, 13) + timedelta(days=offset)).isoformat()
                    for offset in range(7)
                ],
            ),
            id="daily-starts-after-current-date",
        ),
    ],
)
@pytest.mark.asyncio
async def test_lookup_rejects_incomplete_or_inconsistent_forecast_schema(
    mutate_forecast,
):
    from backend.app.services.weather import WeatherServiceError

    payload = deepcopy(_forecast_payload())
    mutate_forecast(payload)
    service, _, _ = _service_with_responses(
        [
            FakeResponse(payload=_geocoding_payload()),
            FakeResponse(payload={"results": []}),
            FakeResponse(payload=payload),
        ]
    )

    with pytest.raises(WeatherServiceError) as exc_info:
        await service.lookup("杭州")

    assert exc_info.value.code == "invalid_response"


@pytest.mark.asyncio
async def test_server_owned_weather_projection_excludes_source_provenance():
    from backend.app.services.weather import render_weather_card, render_weather_text

    service, _, _ = _service_with_responses(
        [
            FakeResponse(payload=_geocoding_payload()),
            FakeResponse(payload={"results": []}),
            FakeResponse(payload=_forecast_payload()),
        ]
    )
    snapshot = await service.lookup("杭州")

    assistant_text = render_weather_text(snapshot, "明天呢？")
    card = render_weather_card(snapshot, "明天呢？")

    assert assistant_text == "杭州·浙江明天：阵雨，25~31°C，降水概率70%，最大风速8m/s。"
    assert card["summary"] == assistant_text
    assert card["location"] == {
        "name": "杭州",
        "admin1": "浙江",
        "country": "中国",
        "timezone": "Asia/Shanghai",
    }
    assert len(card["daily"]) == 7
    serialized_card = repr(card)
    for hidden in ("openmeteo", "30.29365", "120.16142", "fetched_at", "query"):
        assert hidden not in serialized_card


@pytest.mark.live_weather
@pytest.mark.asyncio
async def test_live_open_meteo_weather_lookup(monkeypatch: pytest.MonkeyPatch):
    if os.getenv("RUN_LIVE_OPEN_METEO") != "1":
        pytest.skip("set RUN_LIVE_OPEN_METEO=1 for explicit network acceptance")

    from backend.app.services.weather import WeatherService

    monkeypatch.setenv(
        "OPEN_METEO_GEOCODING_URL",
        "https://untrusted.example.test/geocoding",
    )
    monkeypatch.setenv(
        "OPEN_METEO_FORECAST_URL",
        "https://untrusted.example.test/forecast",
    )
    settings = Settings(
        app_env="test",
        open_meteo_geocoding_url=GEOCODING_URL,
        open_meteo_forecast_url=FORECAST_URL,
    )
    assert settings.open_meteo_geocoding_url == GEOCODING_URL
    assert settings.open_meteo_forecast_url == FORECAST_URL
    assert {
        (urlsplit(url).scheme, urlsplit(url).hostname)
        for url in (
            settings.open_meteo_geocoding_url,
            settings.open_meteo_forecast_url,
        )
    } == {
        ("https", "geocoding-api.open-meteo.com"),
        ("https", "api.open-meteo.com"),
    }

    started_at = datetime.now(timezone.utc)
    snapshot = await WeatherService(settings=settings).lookup("杭州")
    finished_at = datetime.now(timezone.utc)

    assert snapshot.provider == "openmeteo"
    assert snapshot.location.name.startswith("杭州")
    assert -90 <= snapshot.location.latitude <= 90
    assert -180 <= snapshot.location.longitude <= 180
    assert started_at <= snapshot.fetched_at <= finished_at
    assert snapshot.current.time
    assert len(snapshot.daily) == 7
    assert [day.date for day in snapshot.daily] == [
        snapshot.daily[0].date + timedelta(days=offset)
        for offset in range(len(snapshot.daily))
    ]
    assert all(math.isfinite(day.temperature_max_c) for day in snapshot.daily)
    assert isinstance(snapshot.daily[0].date, date)
