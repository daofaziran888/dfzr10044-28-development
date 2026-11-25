"""列画像与压实候选接口：按结构版本、来源批次筛定样本后计算列画像。

路径都落在具体模块前缀下（如 /api/column_stat/profiles），因此必须注册在
通用资源路由 /api/{module}/{entry_id} 之前，否则会被动态路径抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from app.lake_store import lake_store
from app.services import service_for
from app.services.compact_job import CompactJobService
from app.services.column_stat import ColumnStatService
from app.services.rebuild_backfill import RebuildBackfillService

router = APIRouter(prefix="/api", tags=["列画像与压实候选"])


def column_stat_service() -> ColumnStatService:
    service = service_for("column_stat")
    assert isinstance(service, ColumnStatService)
    return service


def compact_job_service() -> CompactJobService:
    service = service_for("compact_job")
    assert isinstance(service, CompactJobService)
    return service


class ProfileScopePayload(BaseModel):
    lake_table: str
    column: str
    schema_version: str | None = None
    batch_ids: list[str] = Field(default_factory=list)
    source_system: str | None = None
    partition_column: str = "partition_dt"


class CandidateBuildPayload(ProfileScopePayload):
    partition_key: str | None = None
    row_threshold: int = 1000
    created_by: str = "compact_job"


class ClaimPayload(BaseModel):
    claimed_by: str = "compact_worker"


class RevalidatePayload(BaseModel):
    lake_table: str
    changed_batch_ids: list[str] = Field(default_factory=list)


class BatchCorrectionPayload(BaseModel):
    rows: list[dict[str, Any]]
    schema_version: str | None = None
    source_system: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    revalidate: bool = True


@router.get("/column_stat/scope", response_model=dict)
def sample_scope(
    lake_table: str = Query(...),
    column: str = Query(...),
    schema_version: str | None = Query(default=None),
    batch_ids: str | None = Query(default=None, description="逗号分隔的批次号"),
    source_system: str | None = Query(default=None),
    partition_column: str = Query(default="partition_dt"),
) -> dict[str, Any]:
    """列出该列可用的样本批次、结构版本与来源系统，便于前端筛定范围。"""
    return column_stat_service().describe_sample_scope(
        lake_table, column,
        schema_version=schema_version,
        batch_ids=[b for b in (batch_ids or "").split(",") if b.strip()],
        source_system=source_system,
        partition_column=partition_column,
    )


@router.post("/column_stat/profiles/compute", response_model=dict)
def compute_profile(payload: ProfileScopePayload) -> dict[str, Any]:
    """按筛定的批次计算列画像并落成不可变的画像原件。"""
    return column_stat_service().compute_column_profile(
        payload.lake_table, payload.column,
        schema_version=payload.schema_version,
        batch_ids=payload.batch_ids or None,
        source_system=payload.source_system,
        partition_column=payload.partition_column,
    )


@router.get("/column_stat/profiles", response_model=dict)
def list_profiles(
    lake_table: str | None = Query(default=None),
    column: str | None = Query(default=None),
) -> dict[str, Any]:
    profiles = column_stat_service().list_profiles(lake_table, column)
    return {"items": profiles, "total": len(profiles)}


@router.get("/column_stat/profiles/{profile_id}", response_model=dict)
def get_profile(profile_id: str) -> dict[str, Any]:
    profile = column_stat_service().get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"列画像 {profile_id} 不存在")
    candidates = [
        candidate
        for candidate in compact_job_service().list_candidates(profile["lake_table"])
        if candidate["profile_id"] == profile_id
        or candidate.get("revalidated_profile_id") == profile_id
    ]
    profile["candidates"] = candidates
    return profile


@router.post("/column_stat/profiles/{profile_id}/recompute", response_model=dict)
def recompute_profile(profile_id: str) -> dict[str, Any]:
    result = column_stat_service().recompute_profile(profile_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "复算失败"))
    return result


@router.post("/compact_job/candidates/build", response_model=dict)
def build_candidates(payload: CandidateBuildPayload) -> dict[str, Any]:
    """按筛定批次计算列画像，生成带画像版本与来源批次的压实候选。"""
    return compact_job_service().build_candidates_from_profile(
        payload.lake_table, payload.column,
        schema_version=payload.schema_version,
        batch_ids=payload.batch_ids or None,
        source_system=payload.source_system,
        partition_column=payload.partition_column,
        partition_key=payload.partition_key,
        row_threshold=payload.row_threshold,
        created_by=payload.created_by,
    )


@router.get("/compact_job/candidates", response_model=dict)
def list_candidates(
    lake_table: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    items = compact_job_service().list_candidates(lake_table, status)
    return {"items": items, "total": len(items)}


@router.get("/compact_job/candidates/{candidate_id}", response_model=dict)
def get_candidate(candidate_id: str) -> dict[str, Any]:
    candidate = compact_job_service().get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"压实候选 {candidate_id} 不存在")
    return candidate


@router.post("/compact_job/candidates/{candidate_id}/claim", response_model=dict)
def claim_candidate(candidate_id: str, payload: ClaimPayload = Body(default=ClaimPayload())) -> dict[str, Any]:
    result = compact_job_service().claim_candidate(candidate_id, claimed_by=payload.claimed_by)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "领取失败"))
    return result


@router.post("/compact_job/candidates/{candidate_id}/verify", response_model=dict)
def verify_candidate(candidate_id: str) -> dict[str, Any]:
    result = compact_job_service().verify_claimed_candidate(candidate_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "复核失败"))
    return result


@router.post("/compact_job/candidates/revalidate", response_model=dict)
def revalidate_candidates(payload: RevalidatePayload) -> dict[str, Any]:
    items = compact_job_service().revalidate_pending_candidates(
        payload.lake_table, changed_batch_ids=payload.changed_batch_ids)
    return {"items": items, "total": len(items)}


@router.post("/rebuild_backfill/batches/{batch_id}/correct", response_model=dict)
def correct_batch(batch_id: str, payload: BatchCorrectionPayload) -> dict[str, Any]:
    """旧版补数更正：替换批次行并推进修订号，随后让待领压实计划重新比对门槛。"""
    existing = lake_store.get_batch(batch_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
    batch = RebuildBackfillService().correct_batch_rows(
        batch_id,
        payload.rows,
        schema_version=payload.schema_version,
        source_system=payload.source_system,
        window_start=payload.window_start,
        window_end=payload.window_end,
    )
    if batch is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
    revalidated: list[dict[str, Any]] = []
    if payload.revalidate:
        revalidated = compact_job_service().revalidate_pending_candidates(
            batch.lake_table, changed_batch_ids=[batch_id])
    return {
        "batch_id": batch.batch_id,
        "lake_table": batch.lake_table,
        "row_count": batch.row_count,
        "revision": batch.revision,
        "checksum": batch.checksum,
        "revalidated_candidates": revalidated,
    }
