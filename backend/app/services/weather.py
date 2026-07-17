from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
import math
import re
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.settings import Settings


CURRENT_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "wind_speed_10m",
    "weather_code",
)
DAILY_FIELDS = (
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
    "wind_speed_10m_max",
)
WEATHER_LOCATION_REQUIRED_TEXT = "请告诉我具体城市/地区，以便查询天气。"
WEATHER_LOCATION_NOT_FOUND_TEXT = "未能定位该地点，请提供更具体的城市/地区（如城市+省份/国家）。"
WEATHER_UNAVAILABLE_TEXT = "天气服务暂时不可用，请稍后再试。"

_KNOWN_CITIES = (
    "北京",
    "上海",
    "广州",
    "深圳",
    "杭州",
    "成都",
    "西安",
    "武汉",
    "南京",
    "重庆",
    "天津",
    "苏州",
    "郑州",
    "长沙",
    "沈阳",
    "青岛",
    "厦门",
    "宁波",
    "无锡",
    "济南",
    "大连",
    "哈尔滨",
    "福州",
    "昆明",
    "兰州",
    "石家庄",
    "南昌",
    "贵阳",
    "太原",
    "合肥",
    "南宁",
    "乌鲁木齐",
)
_LOCATION_STOP_WORDS = (
    "天气",
    "气温",
    "温度",
    "情况",
    "怎么样",
    "未来",
    "一周",
    "七天",
    "7天",
    "接下来",
    "几天",
    "每日",
    "每天",
    "预报",
    "forecast",
    "weather",
    "today",
    "tomorrow",
    "今天",
    "明天",
    "后天",
    "大后天",
    "昨天",
    "现在",
    "当前",
    "请问",
    "一下",
    "告诉",
    "查询",
    "看看",
    "呢",
    "呀",
    "啊",
    "如何",
    "怎样",
    "怎么",
    "吗",
)
_CHINESE_LOCATION_CUE = re.compile(
    r"^(?P<location>[\u4e00-\u9fff]{2,12}?)(?:的)?"
    r"(?:天气|气温|温度|湿度|风速|风力|体感|气压|降雨量|雨量|"
    r"今天|明天|后天|未来|现在|当前)"
)
_CHINESE_ADMIN_LOCATION = re.compile(
    r"^(?P<location>[\u4e00-\u9fff]{1,12}(?:市|地区|省|自治区|县|州|国))$"
)
_LOCATION_REQUEST_PREFIX = re.compile(
    r"^(?:请问|请查询|查询|查一下|请查|帮我查|帮忙查|看看|请告诉我|告诉我)"
)
_OPEN_METEO_LOCAL_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$"
)
_OPEN_METEO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WMO_DESCRIPTIONS = {
    0: "晴朗",
    1: "大部晴朗",
    2: "局部多云",
    3: "多云/阴",
    45: "雾",
    48: "冻雾",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨/强阵雨",
    85: "小阵雪",
    86: "大阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


class WeatherServiceError(RuntimeError):
    def __init__(
        self,
        code: Literal[
            "location_not_found",
            "timeout",
            "transport_error",
            "http_error",
            "invalid_response",
        ],
    ) -> None:
        super().__init__(code)
        self.code = code


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("finite_number_required")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("finite_number_required")
    return number


def _strict_integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("integer_required")
    return value


class WeatherLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1)
    admin1: str | None = None
    admin2: str | None = None
    country: str | None = None
    country_code: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str = Field(min_length=1)

    _latitude_finite = field_validator("latitude", mode="before")(_finite_number)
    _longitude_finite = field_validator("longitude", mode="before")(_finite_number)


class WeatherCurrent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    time: datetime
    temperature_c: float
    relative_humidity_percent: float = Field(ge=0, le=100)
    apparent_temperature_c: float
    wind_speed_mps: float = Field(ge=0)
    weather_code: int = Field(ge=0, le=99)

    _finite_values = field_validator(
        "temperature_c",
        "relative_humidity_percent",
        "apparent_temperature_c",
        "wind_speed_mps",
        mode="before",
    )(_finite_number)
    _weather_code_integer = field_validator("weather_code", mode="before")(
        _strict_integer
    )

    @field_validator("time", mode="before")
    @classmethod
    def validate_time_input(cls, value: object) -> object:
        if (
            not isinstance(value, str)
            or _OPEN_METEO_LOCAL_DATETIME.fullmatch(value) is None
        ):
            raise ValueError("open_meteo_local_datetime_required")
        return value


class WeatherDaily(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    date: date
    weather_code: int = Field(ge=0, le=99)
    temperature_max_c: float
    temperature_min_c: float
    precipitation_probability_percent: float = Field(ge=0, le=100)
    wind_speed_max_mps: float = Field(ge=0)

    _finite_values = field_validator(
        "temperature_max_c",
        "temperature_min_c",
        "precipitation_probability_percent",
        "wind_speed_max_mps",
        mode="before",
    )(_finite_number)
    _weather_code_integer = field_validator("weather_code", mode="before")(
        _strict_integer
    )

    @field_validator("date", mode="before")
    @classmethod
    def validate_date_input(cls, value: object) -> object:
        if not isinstance(value, str) or _OPEN_METEO_DATE.fullmatch(value) is None:
            raise ValueError("open_meteo_date_required")
        return value

    @model_validator(mode="after")
    def validate_temperature_range(self) -> "WeatherDaily":
        if self.temperature_max_c < self.temperature_min_c:
            raise ValueError("daily_temperature_maximum_below_minimum")
        return self


class WeatherSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["openmeteo"] = "openmeteo"
    query: str = Field(min_length=1)
    fetched_at: datetime
    location: WeatherLocation
    current: WeatherCurrent
    daily: list[WeatherDaily] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def validate_daily_dates(self) -> "WeatherSnapshot":
        first_date = self.daily[0].date
        if first_date != self.current.time.date():
            raise ValueError("daily_must_start_on_current_date")
        expected = [
            first_date + timedelta(days=offset) for offset in range(len(self.daily))
        ]
        if [item.date for item in self.daily] != expected:
            raise ValueError("daily_dates_must_be_contiguous")
        return self


class ParticipantWeatherLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1)
    admin1: str | None = None
    country: str | None = None
    timezone: str = Field(min_length=1)


class ParticipantWeatherCurrent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    time: str = Field(min_length=1)
    temperature_c: float
    relative_humidity_percent: float = Field(ge=0, le=100)
    apparent_temperature_c: float
    wind_speed_mps: float = Field(ge=0)
    weather_code: int = Field(ge=0, le=99)
    description: str = Field(min_length=1)


class ParticipantWeatherDaily(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    date: str = Field(min_length=1)
    temperature_max_c: float
    temperature_min_c: float
    precipitation_probability_percent: float = Field(ge=0, le=100)
    wind_speed_max_mps: float = Field(ge=0)
    weather_code: int = Field(ge=0, le=99)
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_temperature_range(self) -> "ParticipantWeatherDaily":
        if self.temperature_max_c < self.temperature_min_c:
            raise ValueError("daily_temperature_maximum_below_minimum")
        return self


class ParticipantWeatherCard(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: str = Field(min_length=1)
    location: ParticipantWeatherLocation
    current: ParticipantWeatherCurrent
    daily: list[ParticipantWeatherDaily] = Field(min_length=7, max_length=7)


class _WeatherHttpClient(Protocol):
    async def __aenter__(self) -> "_WeatherHttpClient": ...

    async def __aexit__(self, exc_type, exc, tb) -> None: ...

    async def get(self, url: str, **kwargs: object) -> Any: ...


WeatherClientFactory = Callable[[float], _WeatherHttpClient]


def extract_weather_location(raw_text: str) -> str | None:
    text = str(raw_text or "").strip()
    if not text:
        return None
    for city in _KNOWN_CITIES:
        if city in text:
            return city

    query_text = _LOCATION_REQUEST_PREFIX.sub("", text).strip()
    chinese_match = _CHINESE_LOCATION_CUE.match(query_text)
    if chinese_match is not None:
        return chinese_match.group("location")

    cleaned = text
    for stop_word in _LOCATION_STOP_WORDS:
        cleaned = re.sub(re.escape(stop_word), "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[？?。！!,.，\s]+", " ", cleaned).strip()
    cleaned = re.sub(r"的+$", "", cleaned).strip()
    if not cleaned:
        return None
    admin_match = _CHINESE_ADMIN_LOCATION.fullmatch(
        _LOCATION_REQUEST_PREFIX.sub("", cleaned).strip()
    )
    if admin_match is not None:
        return admin_match.group("location")
    if re.fullmatch(r"[A-Za-z .-]{2,64}", cleaned):
        return re.sub(r"\s+", " ", cleaned).strip()
    return None


def select_open_meteo_place(
    query: str,
    results: Sequence[dict[str, object]],
    *,
    country_code: str,
) -> dict[str, object]:
    legal_results = [item for item in results if _is_legal_open_meteo_place(item)]
    if not legal_results:
        raise WeatherServiceError("location_not_found")
    normalized_country = country_code.strip().upper()

    def score(item: dict[str, object]) -> int:
        population = item.get("population")
        population_score = (
            int(population)
            if isinstance(population, (int, float))
            and not isinstance(population, bool)
            and math.isfinite(population)
            and population > 0
            else 0
        )
        country_bonus = (
            5_000_000
            if normalized_country
            and str(item.get("country_code") or "").upper() == normalized_country
            else 0
        )
        admin_bonus = 2_000_000 if query in str(item.get("admin1") or "") else 0
        name_bonus = (
            3_000_000 if str(item.get("name") or "").startswith(query) else 0
        )
        return population_score + country_bonus + admin_bonus + name_bonus

    return max(legal_results, key=score)


def _is_legal_open_meteo_place(item: dict[str, object]) -> bool:
    name = item.get("name")
    timezone_name = item.get("timezone")
    if not isinstance(name, str) or not name.strip():
        return False
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        return False

    latitude = item.get("latitude")
    longitude = item.get("longitude")
    return (
        isinstance(latitude, (int, float))
        and not isinstance(latitude, bool)
        and math.isfinite(latitude)
        and -90 <= latitude <= 90
        and isinstance(longitude, (int, float))
        and not isinstance(longitude, bool)
        and math.isfinite(longitude)
        and -180 <= longitude <= 180
    )


class WeatherService:
    def __init__(
        self,
        *,
        settings: Settings,
        client_factory: WeatherClientFactory | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or (
            lambda timeout_seconds: httpx.AsyncClient(timeout=timeout_seconds)
        )
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def forecast_params(*, latitude: float, longitude: float) -> dict[str, object]:
        return {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": "auto",
            "forecast_days": 7,
            "temperature_unit": "celsius",
            "wind_speed_unit": "ms",
            "current": ",".join(CURRENT_FIELDS),
            "daily": ",".join(DAILY_FIELDS),
        }

    async def lookup(self, city_query: str) -> WeatherSnapshot:
        query = str(city_query or "").strip()
        if not query:
            raise WeatherServiceError("location_not_found")
        try:
            async with self._client_factory(
                self._settings.open_meteo_timeout_seconds
            ) as client:
                results = await self._geocode(client, query)
                place = select_open_meteo_place(
                    query,
                    results,
                    country_code=self._settings.open_meteo_country_code,
                )
                latitude = self._required_number(place, "latitude")
                longitude = self._required_number(place, "longitude")
                forecast_payload = await self._request_json(
                    client,
                    self._settings.open_meteo_forecast_url,
                    params=self.forecast_params(
                        latitude=latitude,
                        longitude=longitude,
                    ),
                )
        except httpx.TimeoutException as exc:
            raise WeatherServiceError("timeout") from exc
        except httpx.RequestError as exc:
            raise WeatherServiceError("transport_error") from exc
        except WeatherServiceError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherServiceError("invalid_response") from exc

        try:
            return self._build_snapshot(
                query=query,
                place=place,
                forecast_payload=forecast_payload,
            )
        except WeatherServiceError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherServiceError("invalid_response") from exc

    async def _geocode(
        self,
        client: _WeatherHttpClient,
        query: str,
    ) -> list[dict[str, object]]:
        queries = [query]
        if re.fullmatch(r"[\u4e00-\u9fff]{2,6}", query) and not query.endswith("市"):
            queries.append(f"{query}市")
        results: list[dict[str, object]] = []
        for geo_query in queries:
            params: dict[str, object] = {
                "name": geo_query,
                "count": 10,
                "language": self._settings.open_meteo_language,
                "format": "json",
            }
            if self._settings.open_meteo_country_code:
                params["country_code"] = self._settings.open_meteo_country_code
            payload = await self._request_json(
                client,
                self._settings.open_meteo_geocoding_url,
                params=params,
            )
            raw_results = payload.get("results", [])
            if not isinstance(raw_results, list):
                raise WeatherServiceError("invalid_response")
            if not all(isinstance(item, dict) for item in raw_results):
                raise WeatherServiceError("invalid_response")
            results.extend(raw_results)
        if not results:
            raise WeatherServiceError("location_not_found")
        return results

    async def _request_json(
        self,
        client: _WeatherHttpClient,
        url: str,
        *,
        params: dict[str, object],
    ) -> dict[str, object]:
        response = await client.get(url, params=params)
        if not 200 <= response.status_code < 300:
            raise WeatherServiceError("http_error")
        try:
            payload = response.json()
        except ValueError as exc:
            raise WeatherServiceError("invalid_response") from exc
        if not isinstance(payload, dict):
            raise WeatherServiceError("invalid_response")
        return payload

    def _build_snapshot(
        self,
        *,
        query: str,
        place: dict[str, object],
        forecast_payload: dict[str, object],
    ) -> WeatherSnapshot:
        current = forecast_payload["current"]
        daily = forecast_payload["daily"]
        if not isinstance(current, dict) or not isinstance(daily, dict):
            raise WeatherServiceError("invalid_response")
        daily_arrays = {
            field: daily[field]
            for field in ("time", *DAILY_FIELDS)
        }
        if not all(isinstance(values, list) for values in daily_arrays.values()):
            raise WeatherServiceError("invalid_response")
        lengths = {len(values) for values in daily_arrays.values()}
        if lengths != {7}:
            raise WeatherServiceError("invalid_response")

        daily_items = [
            WeatherDaily(
                date=daily_arrays["time"][index],
                weather_code=daily_arrays["weather_code"][index],
                temperature_max_c=daily_arrays["temperature_2m_max"][index],
                temperature_min_c=daily_arrays["temperature_2m_min"][index],
                precipitation_probability_percent=daily_arrays[
                    "precipitation_probability_max"
                ][index],
                wind_speed_max_mps=daily_arrays["wind_speed_10m_max"][index],
            )
            for index in range(next(iter(lengths)))
        ]
        return WeatherSnapshot(
            query=query,
            fetched_at=self._now_fn(),
            location=WeatherLocation(
                name=self._required_string(place, "name"),
                admin1=self._optional_string(place, "admin1"),
                admin2=self._optional_string(place, "admin2"),
                country=self._optional_string(place, "country"),
                country_code=self._optional_string(place, "country_code"),
                latitude=self._required_number(place, "latitude"),
                longitude=self._required_number(place, "longitude"),
                timezone=self._required_string(place, "timezone"),
            ),
            current=WeatherCurrent(
                time=current["time"],
                temperature_c=current["temperature_2m"],
                relative_humidity_percent=current["relative_humidity_2m"],
                apparent_temperature_c=current["apparent_temperature"],
                wind_speed_mps=current["wind_speed_10m"],
                weather_code=current["weather_code"],
            ),
            daily=daily_items,
        )

    @staticmethod
    def _required_number(payload: dict[str, object], key: str) -> float:
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WeatherServiceError("invalid_response")
        number = float(value)
        if not math.isfinite(number):
            raise WeatherServiceError("invalid_response")
        return number

    @staticmethod
    def _required_string(payload: dict[str, object], key: str) -> str:
        value = payload[key]
        if not isinstance(value, str) or not value.strip():
            raise WeatherServiceError("invalid_response")
        return value.strip()

    @staticmethod
    def _optional_string(payload: dict[str, object], key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise WeatherServiceError("invalid_response")
        return value.strip() or None


def render_weather_text(snapshot: WeatherSnapshot, user_text: str) -> str:
    location = snapshot.location.name
    if snapshot.location.admin1:
        location = f"{location}·{snapshot.location.admin1}"
    day_index = _requested_day_index(user_text)
    if day_index is not None and day_index < len(snapshot.daily):
        day = snapshot.daily[day_index]
        day_label = {0: "今天", 1: "明天", 2: "后天"}.get(
            day_index,
            day.date.isoformat(),
        )
        return (
            f"{location}{day_label}：{weather_code_to_zh(day.weather_code)}，"
            f"{_format_number(day.temperature_min_c)}~"
            f"{_format_number(day.temperature_max_c)}°C，"
            f"降水概率{_format_number(day.precipitation_probability_percent)}%，"
            f"最大风速{_format_number(day.wind_speed_max_mps)}m/s。"
        )
    current = snapshot.current
    return (
        f"{location}当前：{weather_code_to_zh(current.weather_code)}，"
        f"{_format_number(current.temperature_c)}°C，"
        f"体感{_format_number(current.apparent_temperature_c)}°C，"
        f"湿度{_format_number(current.relative_humidity_percent)}%，"
        f"风速{_format_number(current.wind_speed_mps)}m/s。"
    )


def render_weather_card(
    snapshot: WeatherSnapshot,
    user_text: str,
) -> dict[str, object]:
    card = {
        "summary": render_weather_text(snapshot, user_text),
        "location": {
            "name": snapshot.location.name,
            "admin1": snapshot.location.admin1,
            "country": snapshot.location.country,
            "timezone": snapshot.location.timezone,
        },
        "current": {
            "time": snapshot.current.time.isoformat(),
            "temperature_c": snapshot.current.temperature_c,
            "relative_humidity_percent": snapshot.current.relative_humidity_percent,
            "apparent_temperature_c": snapshot.current.apparent_temperature_c,
            "wind_speed_mps": snapshot.current.wind_speed_mps,
            "weather_code": snapshot.current.weather_code,
            "description": weather_code_to_zh(snapshot.current.weather_code),
        },
        "daily": [
            {
                "date": item.date.isoformat(),
                "temperature_max_c": item.temperature_max_c,
                "temperature_min_c": item.temperature_min_c,
                "precipitation_probability_percent": (
                    item.precipitation_probability_percent
                ),
                "wind_speed_max_mps": item.wind_speed_max_mps,
                "weather_code": item.weather_code,
                "description": weather_code_to_zh(item.weather_code),
            }
            for item in snapshot.daily
        ],
    }
    return ParticipantWeatherCard.model_validate(card).model_dump(mode="json")


def weather_code_to_zh(code: int) -> str:
    return _WMO_DESCRIPTIONS.get(code, f"天气代码{code}")


def _requested_day_index(user_text: str) -> int | None:
    text = str(user_text or "")
    if "大后天" in text:
        return 3
    if "后天" in text:
        return 2
    if "明天" in text or "tomorrow" in text.lower():
        return 1
    if "今天" in text:
        return 0
    return None


def _format_number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")
