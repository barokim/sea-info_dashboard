"""공식 제공자 응답 → 앱 모델. 실패 시 가짜 데이터로 대체하지 않습니다."""
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError, model_validator

from models import DashboardResponse, MarineInfo, MarinePoint, ObservationInfo, Region, TideEvent, TideInfo

KST = timezone(timedelta(hours=9))
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
MARINE_DOCS = "https://open-meteo.com/en/docs/marine-weather-api"
TIDE_URL = "https://apis.data.go.kr/1192136/tideFcstHghLw/GetTideFcstHghLwApiService"
OBS_URL = "https://apis.data.go.kr/1192136/dtRecent/GetDTRecentApiService"
TIDE_DOCS = "https://www.data.go.kr/data/15156018/openapi.do"
OBS_DOCS = "https://www.data.go.kr/data/15155508/openapi.do"
REGIONS = {
    "incheon": Region(id="incheon", name="인천 연안", latitude=37.40, longitude=126.45, station_code="DT_0001", station_name="인천"),
    "busan": Region(id="busan", name="부산 연안", latitude=35.05, longitude=129.10, station_code="DT_0005", station_name="부산"),
    "jeju": Region(id="jeju", name="제주 연안", latitude=33.55, longitude=126.50, station_code="DT_0004", station_name="제주"),
}


def today_kst() -> date:
    return datetime.now(KST).date()


def api_key(service: str) -> str:
    return unquote(os.getenv(f"KHOA_{service}_API_KEY", "") or os.getenv("KHOA_API_KEY", "")).strip()


def parse_time(value) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)


def numeric(value) -> float | None:
    if value is None or str(value).strip().lower() in {"", "-", "null", "none"}:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("유한한 수치가 아닙니다.")
    return None if result in {-99, -999, -9999} else result


def get_json(url: str, params: dict | None = None):
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            return None if response.status_code == 204 else response.json()
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "외부 API 응답 시간이 초과되었습니다.") from exc
    except (httpx.HTTPError, ValueError) as exc:
        # str(exc)는 인증키를 포함한 요청 URL을 노출할 수 있으므로 사용하지 않습니다.
        raise HTTPException(502, "외부 API에서 올바른 데이터를 받지 못했습니다. 연결 상태와 활용신청을 확인하세요.") from exc


def khoa_items(url: str, region: Region, target_date: date, service: str) -> list[dict]:
    key = api_key(service)
    if not key:
        raise HTTPException(503, "국립해양조사원 API 키 설정이 필요합니다. .env의 KHOA_API_KEY를 설정하세요.")
    collected = []
    for page in range(1, 11):
        data = get_json(url, {"serviceKey": key, "obsCode": region.station_code,
                              "reqDate": target_date.strftime("%Y%m%d"), "type": "json",
                              "pageNo": page, "numOfRows": 300})
        items, total = parse_khoa_page(data)
        collected.extend(items)
        if len(collected) >= total:
            return collected
        if not items:
            raise HTTPException(502, "관측 데이터 페이지가 누락되었습니다.")
    raise HTTPException(502, "응답 페이지 수가 예상 범위를 넘었습니다. 일부 자료만 반환하지 않습니다.")


def parse_khoa_page(data) -> tuple[list[dict], int]:
    if not isinstance(data, dict):
        raise HTTPException(502, "국립해양조사원 응답 구조를 확인할 수 없습니다.")
    response = data.get("response", data)
    if not isinstance(response, dict):
        raise HTTPException(502, "국립해양조사원 응답 구조가 다릅니다.")
    header = response.get("header")
    if not isinstance(header, dict):
        raise HTTPException(502, "국립해양조사원 응답 헤더가 올바르지 않습니다.")
    code = str(header.get("resultCode", ""))
    if code in {"3", "03"}:
        return [], 0
    if code not in {"0", "00"}:
        if code in {"20", "30", "31"}:
            raise HTTPException(502, "공공데이터 API 인증 또는 활용승인 상태를 확인하세요.")
        raise HTTPException(502, "국립해양조사원에서 정상 응답을 받지 못했습니다.")
    body = response.get("body")
    if not isinstance(body, dict):
        raise HTTPException(502, "국립해양조사원 응답 본문이 올바르지 않습니다.")
    items = body.get("items") or []
    if isinstance(items, dict):
        items = items.get("item") or []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise HTTPException(502, "관측 데이터 목록 형식이 다릅니다.")
    try:
        total = int(body.get("totalCount", len(items)))
        if total < len(items):
            raise ValueError("총 건수가 항목 수보다 작습니다.")
    except (ValueError, TypeError) as exc:
        raise HTTPException(502, "관측 데이터 총 건수가 올바르지 않습니다.") from exc
    return items, total


def check_station(item: dict, region: Region):
    name = str(item.get("obsvtrNm", "")).strip()
    if not name or name != region.station_name:
        raise ValueError("선택한 관측소와 응답의 관측소가 다릅니다.")


def fetch_tides(region: Region, target_date: date) -> TideInfo:
    base = dict(source_name="국립해양조사원 조석예보(고, 저조)", source_url=TIDE_DOCS,
                station_code=region.station_code, station_name=region.station_name)
    try:
        items = khoa_items(TIDE_URL, region, target_date, "TIDE")
        events = []
        for item in items:
            check_station(item, region)
            moment = parse_time(item["predcDt"])
            kind = int(item["extrSe"])
            height = numeric(item.get("predcTdlvVl"))
            if moment.date() != target_date or kind not in {1, 2, 3, 4} or height is None:
                raise ValueError("조석 예보의 날짜·극치 구분·조위를 확인할 수 없습니다.")
            events.append(TideEvent(time=moment, kind="high" if kind in {1, 3} else "low", height_cm=height))
        return TideInfo(**base, status="ok" if events else "no_data",
                        note="공식 고·저조 예보 · 현장 출입 가능 시간을 뜻하지 않습니다." if events else "선택일의 조석 예보가 없습니다.",
                        events=sorted(events, key=lambda e: e.time))
    except HTTPException as exc:
        return TideInfo(**base, status="not_configured" if exc.status_code == 503 else "error", note=str(exc.detail))
    except (ValueError, KeyError, TypeError):
        return TideInfo(**base, status="error", note="조석 예보의 관측소·날짜·응답 필드 검증에 실패했습니다.")


def fetch_observations(region: Region, target_date: date) -> ObservationInfo:
    base = dict(station_code=region.station_code, station_name=region.station_name, fetched_at=datetime.now(KST))
    if target_date > today_kst():
        return ObservationInfo(**base, status="no_data", note="미래 날짜의 실측 관측값은 없습니다.")
    try:
        items = khoa_items(OBS_URL, region, target_date, "OBSERVATION")
        if not items:
            return ObservationInfo(**base, status="no_data", note="선택일에 수신된 관측값이 없습니다.")
        for item in items:
            check_station(item, region)
            if parse_time(item["obsrvnDt"]).date() != target_date:
                raise ValueError("관측일이 다릅니다.")
        latest = max(items, key=lambda i: parse_time(i["obsrvnDt"]))
        moment = parse_time(latest["obsrvnDt"])
        stale = target_date == today_kst() and datetime.now(KST) - moment > timedelta(hours=2)
        return ObservationInfo(**base, status="ok", observed_at=moment,
                               water_temperature_c=numeric(latest.get("wtem")),
                               tide_height_cm=numeric(latest.get("bscTdlvHgt")),
                               wind_speed_ms=numeric(latest.get("wspd")),
                               air_temperature_c=numeric(latest.get("artmp")), stale=stale,
                               note="선택일의 마지막 수신 관측값입니다." + (" 관측 시각이 2시간 이상 지났습니다." if stale else ""))
    except HTTPException as exc:
        return ObservationInfo(**base, status="not_configured" if exc.status_code == 503 else "error", note=str(exc.detail))
    except (ValueError, KeyError, TypeError):
        return ObservationInfo(**base, status="error", note="실측 데이터의 관측소·날짜·응답 필드 검증에 실패했습니다.")


class ExternalHourly(BaseModel):
    time: list[datetime]
    wave_height: list[float | None]
    sea_surface_temperature: list[float | None]

    @model_validator(mode="after")
    def check_lengths(self):
        if not self.time or not (len(self.time) == len(self.wave_height) == len(self.sea_surface_temperature)):
            raise ValueError("시간과 값 개수가 다릅니다.")
        return self


class ExternalMarine(BaseModel):
    utc_offset_seconds: int
    hourly_units: dict[str, str]
    hourly: ExternalHourly

    @model_validator(mode="after")
    def check_units(self):
        if self.utc_offset_seconds != 32400 or self.hourly_units.get("wave_height") != "m" or self.hourly_units.get("sea_surface_temperature") != "°C":
            raise ValueError("시간 기준 또는 단위가 다릅니다.")
        return self


def fetch_live_marine(region: Region, target_date: date) -> MarineInfo:
    base = dict(source_name="Open-Meteo Marine Weather API", source_url=MARINE_DOCS, fetched_at=datetime.now(KST))
    if not today_kst() <= target_date <= today_kst() + timedelta(days=3):
        return MarineInfo(**base, status="no_data", note="이 화면의 모델 예보는 오늘~3일 뒤까지 지원합니다.")
    try:
        external = ExternalMarine.model_validate(get_json(MARINE_URL, {
            "latitude": region.latitude, "longitude": region.longitude,
            "hourly": "wave_height,sea_surface_temperature", "timezone": "Asia/Seoul",
            "start_date": target_date.isoformat(), "end_date": target_date.isoformat(), "cell_selection": "sea"}))
        points = []
        for index, moment in enumerate(external.hourly.time):
            moment = parse_time(moment)
            if moment.date() != target_date:
                raise ValueError("조회일과 다릅니다.")
            points.append(MarinePoint(time=moment, wave_height_m=external.hourly.wave_height[index],
                                     sea_surface_temperature_c=external.hourly.sea_surface_temperature[index]))
        if not any(p.wave_height_m is not None or p.sea_surface_temperature_c is not None for p in points):
            return MarineInfo(**base, status="no_data", note="사용 가능한 예보값이 없습니다.")
        return MarineInfo(**base, status="ok", points=points,
                          note="대표 해역의 모델 예보이며 국립해양조사원 실측값과 다릅니다.")
    except HTTPException as exc:
        return MarineInfo(**base, status="error", note=str(exc.detail))
    except (ValidationError, ValueError, TypeError):
        return MarineInfo(**base, status="error", note="해양 예보의 형식·시간·단위 검증에 실패했습니다.")


def get_dashboard(region_id: str, target_date: date) -> DashboardResponse:
    region = REGIONS[region_id]
    with ThreadPoolExecutor(max_workers=3) as executor:
        tides = executor.submit(fetch_tides, region, target_date)
        marine = executor.submit(fetch_live_marine, region, target_date)
        observations = executor.submit(fetch_observations, region, target_date)
        return DashboardResponse(region=region, target_date=target_date, timezone="Asia/Seoul",
                                 tides=tides.result(), marine=marine.result(), observations=observations.result())
