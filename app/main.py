"""FastAPI 应用入口与路由。

端点：
- GET  /healthz                       健康检查
- GET  /docs                          Swagger UI（入口）
- PUT  /api/maps/{version}            按版上传/整版更正乡镇 GeoJSON
- GET  /api/maps                      已受理乡镇版本列表（含修订号）
- GET  /api/maps/{version}            乡镇版本元数据
- PUT  /api/county-boundaries/{version}  按版上传/整版更正单个县界
- GET  /api/county-boundaries            已受理县界版本列表
- GET  /api/county-boundaries/{version}  县界版本元数据
- POST /api/weights/jobs              提交旧版→新版权重计算任务
- GET  /api/weights/jobs              任务列表
- GET  /api/weights/jobs/{job_id}     查询并交付结果（含失效检测）
- POST /api/verifications/jobs        提交同版县界覆盖核验任务
- GET  /api/verifications/jobs        核验记录列表（可按版过滤）
- GET  /api/verifications/jobs/{id}   查询核验结论（含失效检测）
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from shapely.geometry import mapping as _shapely_mapping

from . import geo
from .db import Database
from .geo import GeoValidationError
from .jobs import COMPLETED, FAILED, PENDING, RUNNING, STALE, JobError, JobManager
from .verifications import (
    COMPLETED as V_COMPLETED,
    FAILED as V_FAILED,
    PENDING as V_PENDING,
    RUNNING as V_RUNNING,
    STALE as V_STALE,
    VerificationError,
    VerificationManager,
)


# ----------------------------------------------------------------------
# 响应模型
# ----------------------------------------------------------------------
class MapUploadResponse(BaseModel):
    version: str
    revision: int = Field(..., description="该版当前修订号，重传一次加 1")
    feature_count: int
    replaced_previous: bool
    message: str


class MapVersionMeta(BaseModel):
    version: str
    revision: int
    feature_count: int
    created_at: str
    updated_at: str


class CountyUploadResponse(BaseModel):
    version: str
    revision: int = Field(..., description="该县界当前修订号，重传一次加 1")
    geometry_type: str
    replaced_previous: bool
    invalidated_verifications: int = Field(
        ..., description="本次更正立即失效的同版核验记录数"
    )
    message: str


class CountyVersionMeta(BaseModel):
    version: str
    revision: int
    geometry_type: str
    created_at: str
    updated_at: str


class JobSubmitRequest(BaseModel):
    old_version: str = Field(..., description="旧版（历史指标所在边界）版号")
    new_version: str = Field(..., description="新版边界版号")


class VerificationSubmitRequest(BaseModel):
    version: str = Field(..., description="受理县界与乡镇面的同一版号")


# ----------------------------------------------------------------------
# 生命周期
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database()
    app.state.jobs = JobManager(app.state.db)
    app.state.verifications = VerificationManager(app.state.db)
    # 重启恢复：中断的核验落 failed；过期结论（stale）原样保留、绝不复活
    app.state.verifications.recover_after_restart()
    yield
    app.state.verifications.shutdown()
    app.state.jobs.shutdown()
    app.state.db.close()


app = FastAPI(
    title="乡镇边界面积权重 API",
    description=(
        "按版受理 WGS84 乡镇面 GeoJSON，计算旧版乡镇到新版乡镇的"
        "等面积交叠权重。同版重传为整版更正；源版在计算期间变化时，"
        "旧权重拒绝交付并提示重试。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def get_verifications(request: Request) -> VerificationManager:
    return request.app.state.verifications


def _invalidate_version_verifications(
    db: Database, version: str, changed_source: str
) -> int:
    """同版县界/乡镇图更正后，立即把该版核验结论置 stale（旧图斑不交付）。"""
    return db.mark_version_verifications_stale(
        version=version,
        stale_at=datetime.now(timezone.utc).isoformat(),
        changed_source=changed_source,
        current_township_revision=db.get_revision(version),
        current_county_revision=db.get_county_revision(version),
    )


async def _read_geojson_body(request: Request) -> bytes:
    """同时支持 application/json 原始体与 multipart/form-data 文件上传。"""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(
                status_code=400,
                detail="multipart 上传需提供名为 file 的 GeoJSON 文件字段",
            )
        return await upload.read()
    return await request.body()


# ----------------------------------------------------------------------
# 基础端点
# ----------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/healthz", tags=["system"])
def healthz(request: Request):
    return {"status": "ok", "versions": len(get_db(request).list_versions())}


# ----------------------------------------------------------------------
# 地图上传 / 查询
# ----------------------------------------------------------------------
@app.put(
    "/api/maps/{version}",
    response_model=MapUploadResponse,
    tags=["maps"],
    status_code=status.HTTP_200_OK,
    summary="按版上传 GeoJSON（同版重传 = 整版更正）",
)
async def upload_map(version: str, request: Request) -> MapUploadResponse:
    if not version or len(version) > 128:
        raise HTTPException(status_code=400, detail="版号不能为空且长度不超过 128")

    raw = await _read_geojson_body(request)
    if not raw:
        raise HTTPException(status_code=400, detail="请求体为空，需要 GeoJSON FeatureCollection")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"GeoJSON 解析失败：{exc}") from exc

    # 整版校验：任何要素无效/乡镇号重复都会在这里抛错，旧版保持不动
    try:
        parsed = geo.parse_feature_collection(payload)
    except GeoValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db = get_db(request)
    replaced = db.get_revision(version) is not None
    stored = geo.to_storage_geometries(parsed)
    # 单事务替换，失败回滚，旧版完整保留
    revision = db.replace_map(version, stored)
    # 乡镇图更正后，同版既有核验结论立即失效（旧图斑不得交付）
    _invalidate_version_verifications(db, version, changed_source="township_map")

    return MapUploadResponse(
        version=version,
        revision=revision,
        feature_count=len(stored),
        replaced_previous=replaced,
        message=(
            f"版号 {version} 已整版更正（第 {revision} 次受理）"
            if replaced
            else f"版号 {version} 首次受理成功"
        ),
    )


@app.get("/api/maps", response_model=list[MapVersionMeta], tags=["maps"])
def list_maps(request: Request) -> list[MapVersionMeta]:
    rows = get_db(request).list_versions()
    return [
        MapVersionMeta(
            version=r["version"],
            revision=r["revision"],
            feature_count=r["feature_cnt"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


@app.get("/api/maps/{version}", response_model=MapVersionMeta, tags=["maps"])
def get_map(version: str, request: Request) -> MapVersionMeta:
    for r in get_db(request).list_versions():
        if r["version"] == version:
            return MapVersionMeta(
                version=r["version"],
                revision=r["revision"],
                feature_count=r["feature_cnt"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
    raise HTTPException(status_code=404, detail=f"版号不存在：{version}")


# ----------------------------------------------------------------------
# 县界上传 / 查询
# ----------------------------------------------------------------------
@app.put(
    "/api/county-boundaries/{version}",
    response_model=CountyUploadResponse,
    tags=["county-boundaries"],
    status_code=status.HTTP_200_OK,
    summary="按版上传单个县界（Polygon/MultiPolygon；同版重传 = 整版更正）",
)
async def upload_county_boundary(
    version: str, request: Request
) -> CountyUploadResponse:
    if not version or len(version) > 128:
        raise HTTPException(status_code=400, detail="版号不能为空且长度不超过 128")

    raw = await _read_geojson_body(request)
    if not raw:
        raise HTTPException(status_code=400, detail="请求体为空，需要县界 GeoJSON")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"GeoJSON 解析失败：{exc}") from exc

    try:
        county_geom = geo.parse_county_boundary(payload)
    except GeoValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db = get_db(request)
    replaced = db.get_county_revision(version) is not None
    geom_dict = _shapely_mapping(county_geom)
    # 单事务替换，失败回滚，旧县界完整保留
    revision = db.replace_county_boundary(version, geom_dict)
    # 县界更正后，同版既有核验结论立即失效（旧图斑不得交付）
    invalidated = _invalidate_version_verifications(
        db, version, changed_source="county_boundary"
    )

    return CountyUploadResponse(
        version=version,
        revision=revision,
        geometry_type=county_geom.geom_type,
        replaced_previous=replaced,
        invalidated_verifications=invalidated,
        message=(
            f"版号 {version} 县界已整版更正（第 {revision} 次受理）"
            if replaced
            else f"版号 {version} 县界首次受理成功"
        ),
    )


@app.get(
    "/api/county-boundaries",
    response_model=list[CountyVersionMeta],
    tags=["county-boundaries"],
    summary="已受理县界版本列表",
)
def list_county_boundaries(request: Request) -> list[CountyVersionMeta]:
    db = get_db(request)
    metas: list[CountyVersionMeta] = []
    for r in db.list_county_versions():
        county = db.get_county_boundary(r["version"])
        geom_type = county["geom"]["type"] if county else None
        metas.append(
            CountyVersionMeta(
                version=r["version"],
                revision=r["revision"],
                geometry_type=geom_type,
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
        )
    return metas


@app.get(
    "/api/county-boundaries/{version}",
    response_model=CountyVersionMeta,
    tags=["county-boundaries"],
    summary="县界版本元数据",
)
def get_county_boundary_meta(version: str, request: Request) -> CountyVersionMeta:
    db = get_db(request)
    for r in db.list_county_versions():
        if r["version"] == version:
            county = db.get_county_boundary(version)
            return CountyVersionMeta(
                version=r["version"],
                revision=r["revision"],
                geometry_type=county["geom"]["type"] if county else None,
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
    raise HTTPException(status_code=404, detail=f"县界版号不存在：{version}")


# ----------------------------------------------------------------------
# 权重计算任务
# ----------------------------------------------------------------------
@app.post(
    "/api/weights/jobs",
    tags=["weights"],
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交旧版→新版交叠权重计算任务",
)
def submit_job(body: JobSubmitRequest, request: Request) -> dict[str, Any]:
    db = get_db(request)
    jobs = get_jobs(request)

    if db.get_revision(body.old_version) is None:
        raise HTTPException(
            status_code=404, detail=f"旧版地图不存在：{body.old_version}"
        )
    if db.get_revision(body.new_version) is None:
        raise HTTPException(
            status_code=404, detail=f"新版地图不存在：{body.new_version}"
        )

    # 同步几何预检：新版乡镇存在正面积交叠候选 -> 立即拒绝，不建任务。
    # 定量平方米面积在工作线程的等面积投影阶段复核并报告。
    new_stored = db.get_features(body.new_version)
    from shapely.geometry import shape

    new_geoms = [(tid, shape(g)) for tid, g in (new_stored or [])]
    overlap_pairs = geo.find_overlapping_pairs(new_geoms)
    if overlap_pairs:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"新版 {body.new_version} 内乡镇之间存在 "
                    f"{len(overlap_pairs)} 处正面积交叠，拒绝计算（不能重复计权），"
                    "请更正新版后重试"
                ),
                "overlapping_pairs": [
                    {"township_a": a, "township_b": b} for a, b in overlap_pairs
                ],
            },
        )

    try:
        job = jobs.submit(body.old_version, body.new_version)
    except JobError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return job.public()


@app.get(
    "/api/weights/jobs",
    tags=["weights"],
    summary="列出全部计算任务",
)
def list_jobs(request: Request) -> dict[str, Any]:
    return {"jobs": get_jobs(request).list_jobs()}


@app.get(
    "/api/weights/jobs/{job_id}",
    tags=["weights"],
    summary="查询任务并交付结果（源版变化则拒绝交付）",
)
def get_job(job_id: str, request: Request):
    jobs = get_jobs(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}")

    # 交付前失效检测
    job = jobs.deliver(job_id)
    payload = job.public()

    if job.state in (PENDING, RUNNING):
        return payload  # 200 + state=pending/running，客户端轮询即可
    if job.state == COMPLETED:
        return payload
    if job.state == STALE:
        raise HTTPException(status_code=409, detail=payload)
    if job.state == FAILED:
        raise HTTPException(status_code=422, detail=payload)
    return payload


# ----------------------------------------------------------------------
# 县界覆盖核验
# ----------------------------------------------------------------------
@app.post(
    "/api/verifications/jobs",
    tags=["verifications"],
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交同版县界覆盖核验（漏划/越界图斑 + 乡镇互叠）",
)
def submit_verification(
    body: VerificationSubmitRequest, request: Request
) -> dict[str, Any]:
    manager = get_verifications(request)
    try:
        record = manager.submit(body.version)
    except VerificationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _verification_public(record)


@app.get(
    "/api/verifications/jobs",
    tags=["verifications"],
    summary="核验记录列表（可按 ?version= 过滤）",
)
def list_verifications(request: Request, version: Optional[str] = None) -> dict[str, Any]:
    if version is not None and len(version) > 128:
        raise HTTPException(status_code=400, detail="版号长度不超过 128")
    records = get_verifications(request).list(version)
    return {"jobs": [_verification_public(r) for r in records]}


@app.get(
    "/api/verifications/jobs/{verification_id}",
    tags=["verifications"],
    summary="查询核验结论（来源更正则拒绝交付旧图斑）",
)
def get_verification(verification_id: str, request: Request):
    manager = get_verifications(request)
    if manager.get(verification_id) is None:
        raise HTTPException(
            status_code=404, detail=f"核验记录不存在：{verification_id}"
        )

    # 交付前失效检测：同版县界/乡镇图 revision 变化则置 stale 并清空旧图斑
    record = manager.deliver(verification_id)
    payload = _verification_public(record)

    if record["state"] in (V_PENDING, V_RUNNING):
        return payload  # 200 + state=pending/running，客户端轮询即可
    if record["state"] == V_COMPLETED:
        return payload
    if record["state"] == V_STALE:
        raise HTTPException(status_code=409, detail=payload)
    if record["state"] == V_FAILED:
        raise HTTPException(status_code=422, detail=payload)
    return payload


def _verification_public(record: dict) -> dict[str, Any]:
    """核验记录对外序列化。"""
    data = {
        "verification_id": record["verification_id"],
        "state": record["state"],
        "version": record["version"],
        "pinned_township_revision": record["pinned_township_revision"],
        "pinned_county_revision": record["pinned_county_revision"],
        "submitted_at": record["submitted_at"],
        "started_at": record["started_at"],
        "finished_at": record["finished_at"],
    }
    if record["error"] is not None:
        data["error"] = record["error"]
    if record["error_detail"] is not None:
        data["error_detail"] = record["error_detail"]
    if record["result"] is not None:
        data["result"] = record["result"]
    return data
