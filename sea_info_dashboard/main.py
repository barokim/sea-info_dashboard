"""실행: python -m uvicorn main:app --reload --port 8001"""
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

BASE_DIR = Path(__file__).resolve().parent


import database
import image_service
import species_service
from models import (DashboardResponse, HistoryDetail, HistorySummary, RecordCreate, RecordOut,
                    RecordPatch, Region, RegionId, SpeciesDetail, SpeciesSearch, VisionResult)
from services import REGIONS, api_key, get_dashboard, today_kst


@asynccontextmanager
async def lifespan(app):
    database.initialize()
    yield


app = FastAPI(title="Sea-Info Agent · 실제 API 대시보드", version="0.2.0", lifespan=lifespan,
              description="KHOA 물때·실측, 해양 예보, WoRMS 생물 검색, 사진 분석과 SQLite 조회 이력")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class UploadLimitMiddleware:
    """multipart 전체 요청을 11 MiB로 제한합니다. 파일 자체는 다시 10 MiB로 검사합니다."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/api/analyze":
            return await self.app(scope, receive, send)
        maximum = image_service.MAX_IMAGE_BYTES + 1024 * 1024
        length = dict(scope.get("headers", [])).get(b"content-length", b"0")
        try:
            too_large = int(length) > maximum
        except ValueError:
            return await JSONResponse({"detail": "Content-Length가 올바르지 않습니다."}, status_code=400)(scope, receive, send)
        if too_large:
            return await JSONResponse({"detail": "업로드 요청이 너무 큽니다. 사진은 10 MiB 이하로 선택하세요."}, status_code=413)(scope, receive, send)
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > maximum:
                raise HTTPException(413, "업로드 요청이 너무 큽니다.")
            return message

        await self.app(scope, limited_receive, send)


app.add_middleware(UploadLimitMiddleware)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/guide", include_in_schema=False)
def setup_guide():
    return FileResponse(BASE_DIR / "README.md", media_type="text/plain; charset=utf-8")


@app.get("/api/status", tags=["연결 상태"])
def connection_status():
    return {"khoa_tides_configured": bool(api_key("TIDE")),
            "khoa_observations_configured": bool(api_key("OBSERVATION")),
            "image_analysis_configured": bool(os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL")),
            "species_provider": "WoRMS (공개 API, 별도 키 불필요)",
            "storage": "SQLite", "note": "키 설정 여부이며 외부 서비스의 인증 성공을 뜻하지 않습니다."}


@app.get("/api/regions", response_model=list[Region], tags=["조회"])
def list_regions():
    return list(REGIONS.values())


@app.get("/api/dashboard", response_model=DashboardResponse, tags=["조회"])
def dashboard(region: RegionId = "incheon", target_date: date | None = None):
    day = target_date or today_kst()
    result = get_dashboard(region, day)
    status = "ok" if all(s.status == "ok" for s in [result.tides, result.marine, result.observations]) else "partial"
    result.history_id = database.save_history("dashboard", f"{result.region.name} · {day}",
                                            {"region": region, "target_date": day.isoformat()},
                                            result.model_dump(mode="json"), status)
    return result


@app.get("/api/species", response_model=SpeciesSearch, tags=["생물 도감"])
def search_species(q: str = Query(min_length=2, max_length=80)):
    try:
        result = species_service.search_species(q)
    except HTTPException as exc:
        database.save_history("species_search", f"생물 검색: {q}", {"q": q}, {"detail": exc.detail}, "error")
        raise
    result.history_id = database.save_history("species_search", f"생물 검색: {q}", {"q": q}, result.model_dump(mode="json"))
    return result


@app.get("/api/species/{aphia_id}", response_model=SpeciesDetail, tags=["생물 도감"])
def species_detail(aphia_id: int):
    if aphia_id <= 0:
        raise HTTPException(422, "AphiaID는 양수여야 합니다.")
    result = species_service.species_detail(aphia_id)
    result.history_id = database.save_history("species_detail", f"도감: {result.species.scientific_name}",
                                            {"aphia_id": aphia_id}, result.model_dump(mode="json"))
    return result


@app.post("/api/analyze", response_model=VisionResult, tags=["사진 분석"])
async def analyze(file: UploadFile = File(...), consent: bool = Form(...)):
    try:
        if not consent:
            raise HTTPException(400, "사진의 외부 AI 전송 및 SQLite 저장 동의가 필요합니다.")
        raw = await file.read(image_service.MAX_IMAGE_BYTES + 1)
    finally:
        await file.close()
    # 파일 검증은 키가 없어도 먼저 수행합니다. 잘못된 파일을 분석 API로 보내지 않습니다.
    result, jpeg = await run_in_threadpool(image_service.analyze_photo, raw)
    result.history_id = await run_in_threadpool(
        database.save_history, "image_analysis", "해양생물 사진 분석",
        {"image_sha256": result.image_sha256}, result.model_dump(mode="json"), "ok", jpeg)
    return result


@app.get("/api/history", response_model=list[HistorySummary], tags=["SQLite 조회 이력"])
def list_history(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    return database.list_history(limit, offset)


@app.get("/api/history/{history_id}", response_model=HistoryDetail, tags=["SQLite 조회 이력"])
def history_detail(history_id: int):
    return database.get_history(history_id)


@app.get("/api/history/{history_id}/image", tags=["SQLite 조회 이력"])
def history_image(history_id: int):
    return Response(database.get_history_image(history_id), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/api/records", response_model=list[RecordOut], tags=["관찰 기록 CRUD"])
def list_records():
    return database.list_records()


@app.get("/api/records/{record_id}", response_model=RecordOut, tags=["관찰 기록 CRUD"])
def get_record(record_id: int):
    return database.get_record(record_id)


@app.post("/api/records", response_model=RecordOut, status_code=201, tags=["관찰 기록 CRUD"])
def create_record(payload: RecordCreate):
    return database.create_record(payload)


@app.patch("/api/records/{record_id}", response_model=RecordOut, tags=["관찰 기록 CRUD"])
def patch_record(record_id: int, payload: RecordPatch):
    return database.update_record(record_id, payload)


@app.delete("/api/records/{record_id}", status_code=204, tags=["관찰 기록 CRUD"])
def delete_record(record_id: int):
    database.delete_record(record_id)
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    # Render는 외부 요청을 받을 수 있는 주소와 PORT 환경 변수를 요구합니다.
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8001")))

