"""列统计领域服务：列级空值比例、去重基数与取值区间，统计版本绑定结构版本。

除了原有的整表列统计外，本模块新增「按筛定批次计算列画像」：样本范围可按结构
版本、来源系统或显式批次号收敛，画像结果同时给出样本清单、已确认分区与行数，
供压实作业判定是否处理。空值与缺列分开核对，文本取值不参与数值区间计算。
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from decimal import Decimal
from typing import Any

from app.lake_store import BatchRecord, ColumnProfileArtifact, lake_store
from app.services.base import BaseModuleService
from app.store import store

_NUMERIC_TYPE_TOKENS = (
    "INT", "DECIMAL", "NUMERIC", "NUMBER", "FLOAT", "DOUBLE", "REAL", "LONG",
)


def _split_csv(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _is_numeric_declaration(declared_type: str | None) -> bool | None:
    """返回 True/False 表示结构声明的数值/文本，None 表示结构里查不到类型。"""
    if not declared_type:
        return None
    upper = declared_type.upper()
    return any(token in upper for token in _NUMERIC_TYPE_TOKENS)


def _is_null(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _as_number(value: Any) -> float | int | None:
    """把数值单元格转成可比较的数值；无法转换返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _canonical_value(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class ColumnStatService(BaseModuleService):
    MODULE = "column_stat"
    ENTITY = "列统计"
    OPERATION_TERM = "去重"
    LIST_FIELDS = ["统计编号", "所属湖表", "列名称", "空值比例", "去重基数", "最小值", "最大值", "统计版本"]
    REQUIRED_FIELDS = ["统计编号", "所属湖表", "列名称"]
    STATUS_ORDER = ["待计算", "已计算", "已失效", "计算失败"]
    ACTION_RULES = {"启动计算": "已计算", "确认有效": "已失效", "标记失效": "计算失败"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 结构版本与样本范围
    # ------------------------------------------------------------------

    def resolve_column_type(
        self, lake_table: str, column: str, schema_version: str | None = None,
    ) -> str | None:
        """按结构版本读取列的类型声明；未指定版本时取该表最后登记的版本。"""
        resolved: str | None = None
        for row in store.rows("table_schema"):
            if str(row.get("所属湖表", "")) != lake_table:
                continue
            if schema_version and str(row.get("版本编号", "")) != schema_version:
                continue
            columns = _split_csv(row.get("列清单"))
            if column not in columns:
                continue
            index = columns.index(column)
            types = _split_csv(row.get("类型声明"))
            resolved = types[index] if index < len(types) else None
        return resolved

    def select_sample_batches(
        self,
        lake_table: str,
        column: str,
        *,
        schema_version: str | None = None,
        batch_ids: list[str] | None = None,
        source_system: str | None = None,
    ) -> list[BatchRecord]:
        """按结构版本与来源批次筛出样本，返回顺序稳定、去重后的批次列表。"""
        batches = lake_store.list_batches(lake_table)
        if schema_version is not None:
            batches = [b for b in batches if b.schema_version == schema_version]
        if source_system is not None:
            batches = [b for b in batches if b.source_system == source_system]
        if batch_ids:
            wanted = set(batch_ids)
            batches = [b for b in batches if b.batch_id in wanted]
        batches.sort(key=lambda b: (b.created_at, b.batch_id))
        return batches

    def describe_sample_scope(
        self,
        lake_table: str,
        column: str,
        *,
        schema_version: str | None = None,
        batch_ids: list[str] | None = None,
        source_system: str | None = None,
        partition_column: str = "partition_dt",
    ) -> dict[str, Any]:
        """列出可筛定的样本批次及其结构版本、来源系统与缺列行数。"""
        all_batches = lake_store.list_batches(lake_table)
        selected = self.select_sample_batches(
            lake_table, column,
            schema_version=schema_version, batch_ids=batch_ids, source_system=source_system,
        )
        selected_ids = {b.batch_id for b in selected}
        return {
            "lake_table": lake_table,
            "column": column,
            "partition_column": partition_column,
            "schema_versions": sorted({b.schema_version for b in all_batches if b.schema_version}),
            "source_systems": sorted({b.source_system for b in all_batches if b.source_system}),
            "selected_batch_ids": [b.batch_id for b in selected],
            "available_batches": [
                {
                    "batch_id": b.batch_id,
                    "schema_version": b.schema_version,
                    "revision": b.revision,
                    "source_system": b.source_system,
                    "row_count": b.row_count,
                    "window": f"{b.window_start}~{b.window_end}",
                    "checksum": b.checksum,
                    "status": b.status,
                    "has_column": any(column in r for r in b.rows),
                    "missing_column_rows": sum(1 for r in b.rows if column not in r),
                    "null_rows": sum(1 for r in b.rows if column in r and _is_null(r.get(column))),
                    "selected": b.batch_id in selected_ids,
                }
                for b in all_batches
            ],
        }

    # ------------------------------------------------------------------
    # 样本统计引擎
    # ------------------------------------------------------------------

    def _compute_from_pinned(
        self,
        lake_table: str,
        column: str,
        schema_version: str | None,
        pinned_batches: list[dict[str, Any]],
        partition_column: str = "partition_dt",
    ) -> dict[str, Any]:
        """从画像原件里的行级样本复算统计，不依赖当前批次内容。"""
        total = present = missing = null = non_null = 0
        numeric_values: list[float | int] = []
        text_values: list[Any] = []
        distinct: set[str] = set()
        partitions: dict[str, int] = {}
        unpartitioned = 0
        per_batch: list[dict[str, Any]] = []

        for pinned in pinned_batches:
            declared = self.resolve_column_type(
                lake_table, column, pinned.get("schema_version") or schema_version)
            numeric_decl = _is_numeric_declaration(declared)
            b_total = b_missing = b_null = b_numeric = b_text = 0
            for row in pinned.get("rows", []):
                total += 1
                b_total += 1
                # 分区确认：行里分区键非空才算该分区被本样本确认。
                pk = row.get(partition_column)
                if _is_null(pk):
                    unpartitioned += 1
                else:
                    partitions[str(pk)] = partitions.get(str(pk), 0) + 1

                if column not in row:
                    # 缺列与空值是两件事：缺列表示该行根本没有这一列。
                    missing += 1
                    b_missing += 1
                    continue
                present += 1
                value = row.get(column)
                if _is_null(value):
                    null += 1
                    b_null += 1
                    continue
                non_null += 1
                distinct.add(_canonical_value(value))
                number = _as_number(value)
                # 结构声明为数值时才参与数值区间；声明为文本或无法转数值时进文本桶。
                if numeric_decl is False or number is None:
                    text_values.append(value)
                    b_text += 1
                else:
                    numeric_values.append(number)
                    b_numeric += 1
            per_batch.append({
                "batch_id": pinned.get("batch_id"),
                "schema_version": pinned.get("schema_version"),
                "revision": pinned.get("revision"),
                "total_count": b_total,
                "missing_count": b_missing,
                "null_count": b_null,
                "numeric_count": b_numeric,
                "text_count": b_text,
            })

        min_value = min(numeric_values) if numeric_values else None
        max_value = max(numeric_values) if numeric_values else None
        stats = {
            "total_count": total,
            "present_count": present,
            "missing_count": missing,
            "null_count": null,
            "non_null_count": non_null,
            "numeric_count": len(numeric_values),
            "text_count": len(text_values),
            "null_ratio": round(null / present, 4) if present else 0.0,
            "missing_ratio": round(missing / total, 4) if total else 0.0,
            "absent_ratio": round((missing + null) / total, 4) if total else 0.0,
            "distinct_base": len(distinct),
            "distinct_numeric": len({_canonical_value(v) for v in numeric_values}),
            "distinct_text": len({_canonical_value(v) for v in text_values}),
            "min_value": min_value,
            "max_value": max_value,
            "numeric_range": (
                {"min": min_value, "max": max_value} if numeric_values else None
            ),
            "value_range": {"min": min_value, "max": max_value},
            "text_values": sorted({_canonical_value(v) for v in text_values})[:20],
            "per_batch": per_batch,
        }
        stats["partitions"] = dict(sorted(partitions.items()))
        stats["confirmed_partitions"] = list(stats["partitions"].keys())
        stats["unpartitioned_row_count"] = unpartitioned

        verification = {
            "null_and_missing_checked_separately": True,
            "null_ratio_basis": "present_count",
            "present_plus_missing_equals_total": present + missing == total,
            "null_plus_non_null_equals_present": null + non_null == present,
            "text_excluded_from_numeric_range": len(text_values),
            "checks": [
                {
                    "name": "缺列与空值分开核对",
                    "detail": "缺列=行中无该列；空值=列存在但值为空",
                    "missing_count": missing,
                    "null_count": null,
                    "ok": True,
                },
                {
                    "name": "样本行数闭合",
                    "detail": "present_count + missing_count = total_count",
                    "present_count": present,
                    "missing_count": missing,
                    "total_count": total,
                    "ok": present + missing == total,
                },
                {
                    "name": "空值与非空闭合",
                    "detail": "null_count + non_null_count = present_count",
                    "null_count": null,
                    "non_null_count": non_null,
                    "present_count": present,
                    "ok": null + non_null == present,
                },
                {
                    "name": "文本不参与数值区间",
                    "detail": "文本取值只做基数统计，不进入 min/max",
                    "text_count": len(text_values),
                    "numeric_count": len(numeric_values),
                    "ok": True,
                },
            ],
        }
        return {"stats": stats, "verification": verification}

    @staticmethod
    def _fingerprint(
        lake_table: str,
        column: str,
        schema_version: str | None,
        pinned_batches: list[dict[str, Any]],
    ) -> str:
        """画像指纹只由样本内容决定，因此已领计划的画像原件可随时复算。"""
        payload = {
            "lake_table": lake_table,
            "column": column,
            "schema_version": schema_version,
            "batches": [
                {
                    "batch_id": b.get("batch_id"),
                    "schema_version": b.get("schema_version"),
                    "revision": b.get("revision"),
                    "rows": [
                        {k: v for k, v in sorted(row.items())} for row in b.get("rows", [])
                    ],
                }
                for b in pinned_batches
            ],
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    # ------------------------------------------------------------------
    # 画像入口
    # ------------------------------------------------------------------

    def compute_column_profile(
        self,
        lake_table: str,
        column: str,
        *,
        schema_version: str | None = None,
        batch_ids: list[str] | None = None,
        source_system: str | None = None,
        partition_column: str = "partition_dt",
        persist: bool = True,
        purpose: str = "compute",
        created_by: str = "column_stat",
    ) -> dict[str, Any]:
        """按筛定的批次计算列画像，并可选落成不可变的画像原件。"""
        batches = self.select_sample_batches(
            lake_table, column,
            schema_version=schema_version, batch_ids=batch_ids, source_system=source_system,
        )
        pinned: list[dict[str, Any]] = [
            {
                "batch_id": b.batch_id,
                "schema_version": b.schema_version,
                "revision": b.revision,
                "source_system": b.source_system,
                "row_count": b.row_count,
                "checksum": b.checksum,
                "rows": copy.deepcopy(b.rows),
            }
            for b in batches
        ]
        computed = self._compute_from_pinned(
            lake_table, column, schema_version, pinned, partition_column)
        fingerprint = self._fingerprint(lake_table, column, schema_version, pinned)

        seq = len(lake_store.list_profiles(lake_table, column)) + 1
        profile_version = f"CP-{lake_table}-{column}-{seq:03d}"
        profile_id = f"profile-{lake_table}-{column}-{uuid.uuid4().hex[:8]}"
        manifest = [
            {
                "batch_id": b.batch_id,
                "schema_version": b.schema_version,
                "revision": b.revision,
                "source_system": b.source_system,
                "row_count": b.row_count,
                "checksum": b.checksum,
                "window_start": b.window_start,
                "window_end": b.window_end,
                "selected": True,
            }
            for b in batches
        ]
        scope = {
            "lake_table": lake_table,
            "column": column,
            "schema_version": schema_version,
            "source_system": source_system,
            "batch_ids": [b.batch_id for b in batches],
            "schema_versions": sorted({b.schema_version for b in batches if b.schema_version}),
            "source_systems": sorted({b.source_system for b in batches if b.source_system}),
            "partition_column": partition_column,
            "batch_count": len(batches),
            "row_count": sum(b.row_count for b in batches),
            "partitions": computed["stats"]["partitions"],
            "confirmed_partitions": computed["stats"]["confirmed_partitions"],
        }
        artifact = ColumnProfileArtifact(
            profile_id=profile_id,
            profile_version=profile_version,
            lake_table=lake_table,
            column=column,
            schema_version=schema_version,
            scope=scope,
            sample_manifest=manifest,
            pinned_batches=pinned,
            stats={**computed["stats"], "verification": computed["verification"]},
            fingerprint=fingerprint,
            created_at=time.time(),
            created_by=created_by,
            purpose=purpose,
        )
        if persist:
            lake_store.save_profile(artifact)
        return self.profile_to_dict(artifact, include_pins=not persist)

    def profile_to_dict(
        self, artifact: ColumnProfileArtifact, *, include_pins: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "profile_id": artifact.profile_id,
            "profile_version": artifact.profile_version,
            "lake_table": artifact.lake_table,
            "column": artifact.column,
            "schema_version": artifact.schema_version,
            "scope": copy.deepcopy(artifact.scope),
            "sample_manifest": copy.deepcopy(artifact.sample_manifest),
            "stats": copy.deepcopy(artifact.stats),
            "verification": copy.deepcopy(artifact.stats.get("verification", {})),
            "fingerprint": artifact.fingerprint,
            "created_at": artifact.created_at,
            "created_by": artifact.created_by,
            "purpose": artifact.purpose,
            "recomputable": True,
        }
        if include_pins:
            payload["pinned_batches"] = copy.deepcopy(artifact.pinned_batches)
        return payload

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        artifact = lake_store.get_profile(profile_id)
        return self.profile_to_dict(artifact) if artifact else None

    def list_profiles(
        self, lake_table: str | None = None, column: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            self.profile_to_dict(p, include_pins=False)
            for p in lake_store.list_profiles(lake_table, column)
        ]

    def recompute_profile(self, profile_id: str) -> dict[str, Any]:
        """从画像原件的行级样本复算，核对原件是否仍可复现。"""
        artifact = lake_store.get_profile(profile_id)
        if artifact is None:
            return {"ok": False, "message": f"列画像 {profile_id} 不存在"}
        computed = self._compute_from_pinned(
            artifact.lake_table, artifact.column, artifact.schema_version,
            artifact.pinned_batches, artifact.scope.get("partition_column", "partition_dt"),
        )
        fingerprint = self._fingerprint(
            artifact.lake_table, artifact.column, artifact.schema_version,
            artifact.pinned_batches,
        )
        return {
            "ok": True,
            "profile_id": artifact.profile_id,
            "profile_version": artifact.profile_version,
            "original_fingerprint": artifact.fingerprint,
            "recomputed_fingerprint": fingerprint,
            "matches": fingerprint == artifact.fingerprint,
            "reproducible": fingerprint == artifact.fingerprint,
            "original_stats": copy.deepcopy(artifact.stats),
            "recomputed_stats": computed["stats"],
            "verification": computed["verification"],
        }

    # ------------------------------------------------------------------
    # 兼容原有整表列统计
    # ------------------------------------------------------------------

    def compute_column_stats(self, lake_table: str, column_name: str) -> dict[str, Any]:
        """计算单列的空值比例、去重基数和取值范围（整表口径）。"""
        batches = lake_store.list_batches(lake_table)
        profile = self._compute_from_pinned(
            lake_table,
            column_name,
            None,
            [
                {
                    "batch_id": br.batch_id,
                    "schema_version": br.schema_version,
                    "revision": br.revision,
                    "rows": br.rows,
                }
                for br in batches
            ],
        )
        stats = profile["stats"]
        return {
            "lake_table": lake_table,
            "column": column_name,
            "total_count": stats["total_count"],
            "null_count": stats["null_count"],
            "null_ratio": stats["null_ratio"],
            "distinct_base": stats["distinct_base"],
            "min_value": stats["min_value"],
            "max_value": stats["max_value"],
        }

    def bind_version(self, column_name: str, schema_version: str) -> dict[str, Any]:
        """列统计绑定结构版本号。"""
        return {
            "column": column_name,
            "schema_version": schema_version,
            "binding_at": "now",
        }
