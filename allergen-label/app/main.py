"""FastAPI 入口：过敏原标签核对网页 API。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .service import ServiceError, get_state, upload_declarations, upload_relations
from .storage import Storage

DB_PATH = os.environ.get("ALLERGEN_DB", "/data/allergen.db")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="食品厂过敏原标签核对系统", version="1.0.0")
storage = Storage(DB_PATH)


@app.exception_handler(ServiceError)
async def service_error_handler(_request, exc: ServiceError):
    return JSONResponse(
        status_code=400,
        content={"error": exc.message, "details": exc.details},
    )


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/state")
def api_state():
    """配方、声明、成品标注始终来自同一已提交 state_version。"""
    return get_state(storage)


@app.post("/api/declarations")
async def api_upload_declarations(file: UploadFile = File(...)):
    data = await file.read()
    if not data.strip():
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        return upload_declarations(storage, data)
    except ServiceError as exc:  # 让异常处理器统一格式化
        raise exc


@app.post("/api/relations")
async def api_upload_relations(file: UploadFile = File(...)):
    data = await file.read()
    if not data.strip():
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        return upload_relations(storage, data)
    except ServiceError as exc:
        raise exc


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
