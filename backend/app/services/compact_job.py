"""压实作业领域服务：基于触发条件的湖表分区压实，保留快照可读性。

在文件级触发之外，压实计划的另一条入口是「列画像候选」：先用筛定批次的列画像
确认分区与行数，再据此决定是否处理。候选记录绑定采用的画像版本和来源批次；
批次被旧版补数更正后，待领候选重新比对门槛，已领候选仍保留画像原件可复算。
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from typing import Any

from app.lake_store import CompactCandidate, lake_store
from app.services.base import BaseModuleService
from app.services.column_stat import ColumnStatService

# 候选状态迁移锁：压实领取与旧版补数更正后的门槛重比可能同时发生，
# 两者都必须先拿到这把锁再改候选，避免重比把刚领取的计划改回待领。
_CANDIDATE_LOCK = threading.Lock()


class CompactJobService(BaseModuleService):
    MODULE = "compact_job"
    ENTITY = "压实作业"
    OPERATION_TERM = "读取"
    LIST_FIELDS = ["压实编号", "所属湖表", "触发条件", "目标分区", "读取文件数", "输出文件数", "占用资源额度", "压实耗时"]
    REQUIRED_FIELDS = ["压实编号", "所属湖表", "触发条件"]
    STATUS_ORDER = ["待触发", "压实中", "已完成", "压实中断"]
    ACTION_RULES = {"触发压实": "压实中", "确认完成": "已完成", "标记中断": "压实中断"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self.column_stat = ColumnStatService()

    def evaluate_compact_trigger(self, lake_table: str) -> dict[str, Any]:
        """评估压实触发条件：按文件数量与总字节数判定。"""
        files = lake_store.list_files(lake_table)
        total_files = len(files)
        total_bytes = sum(f.file_bytes for f in files)
        small_files = [f for f in files if f.file_bytes < 3000]
        triggers = {
            "文件过多": total_files > 20,
            "碎片文件超过阈值": len(small_files) > 5,
            "单分区文件密度高": any(
                sum(1 for ff in files if ff.partition_key == f.partition_key) > 8
                for f in files
            ),
        }
        return {
            "total_files": total_files,
            "total_bytes": total_bytes,
            "small_file_count": len(small_files),
            "trigger_conditions": triggers,
            "should_trigger": any(triggers.values()),
        }

    def execute_compact(self, lake_table: str, partition_key: str) -> dict[str, Any]:
        """执行压实：读取指定分区的全部文件，合并为一个输出文件。"""
        start_time = time.time()
        files = lake_store.list_files(lake_table, partition_key)
        read_count = len(files)
        total_rows = sum(f.file_rows for f in files)
        # 压缩后体积降低
        compact_pf = lake_store.register_file(
            lake_table, partition_key,
            rows=total_rows, bytes_=max(1, sum(f.file_bytes for f in files) // 2),
        )
        # 原子替换
        old_ids = [f.file_id for f in files]
        lake_store.replace_file_list(lake_table, old_ids, [compact_pf])
        elapsed = time.time() - start_time
        return {
            "lake_table": lake_table,
            "partition": partition_key,
            "read_files": read_count,
            "output_files": 1,
            "total_rows": total_rows,
            "elapsed": elapsed,
            "compact_success": True,
        }

    # ------------------------------------------------------------------
    # 列画像 → 压实候选
    # ------------------------------------------------------------------

    def build_candidates_from_profile(
        self,
        lake_table: str,
        column: str,
        *,
        schema_version: str | None = None,
        batch_ids: list[str] | None = None,
        source_system: str | None = None,
        partition_column: str = "partition_dt",
        partition_key: str | None = None,
        row_threshold: int = 1000,
        created_by: str = "compact_job",
    ) -> dict[str, Any]:
        """按筛定批次计算列画像，并据此生成待领取的压实候选。"""
        profile = self.column_stat.compute_column_profile(
            lake_table, column,
            schema_version=schema_version, batch_ids=batch_ids,
            source_system=source_system, partition_column=partition_column,
            purpose="compact_candidate", created_by=created_by,
        )
        partitions: dict[str, int] = profile["scope"].get("partitions") or {}
        if partition_key is not None:
            partitions = (
                {partition_key: partitions[partition_key]}
                if partition_key in partitions else {}
            )

        if not partitions:
            # 没有已确认分区时仍生成一条候选，decision=False，便于核对样本为空。
            partitions = {partition_key or partition_column: 0}

        created: list[dict[str, Any]] = []
        for pk, rows in sorted(partitions.items()):
            decision, reason = self._decide(rows, row_threshold)
            candidate = CompactCandidate(
                candidate_id=f"cand-{lake_table}-{uuid.uuid4().hex[:8]}",
                lake_table=lake_table,
                column=column,
                partition_key=pk,
                profile_id=profile["profile_id"],
                profile_version=profile["profile_version"],
                profile_fingerprint=profile["fingerprint"],
                sample_batch_ids=profile["scope"].get("batch_ids", []),
                sample_manifest=copy.deepcopy(profile["sample_manifest"]),
                sample_scope=copy.deepcopy(profile["scope"]),
                total_rows=profile["scope"].get("row_count", 0),
                partition_rows=rows,
                row_threshold=row_threshold,
                decision=decision,
                reason=reason,
                judgement=self._build_judgement(profile, pk, rows, row_threshold),
                status="待领取",
                created_at=time.time(),
            )
            lake_store.save_compact_candidate(candidate)
            created.append(self.candidate_to_dict(candidate))
        return {
            "ok": True,
            "lake_table": lake_table,
            "column": column,
            "profile": profile,
            "candidates": created,
            "candidate_count": len(created),
        }

    @staticmethod
    def _decide(partition_rows: int, row_threshold: int) -> tuple[bool, str]:
        if partition_rows <= 0:
            return False, "样本未确认任何分区行，不处理"
        if partition_rows < row_threshold:
            return False, f"分区行数 {partition_rows} 未达门槛 {row_threshold}，不处理"
        return True, f"分区行数 {partition_rows} 达到门槛 {row_threshold}，可处理"

    def _build_judgement(
        self, profile: dict[str, Any], partition_key: str,
        partition_rows: int, row_threshold: int,
    ) -> dict[str, Any]:
        stats = profile["stats"]
        verification = profile["verification"]
        return {
            "profile_id": profile["profile_id"],
            "profile_version": profile["profile_version"],
            "profile_fingerprint": profile["fingerprint"],
            "sample_batch_ids": profile["scope"].get("batch_ids", []),
            "sample_manifest": copy.deepcopy(profile["sample_manifest"]),
            "sample_scope": copy.deepcopy(profile["scope"]),
            "confirmed_partitions": stats.get("confirmed_partitions", []),
            "partition": partition_key,
            "partition_rows": partition_rows,
            "row_threshold": row_threshold,
            "decision": self._decide(partition_rows, row_threshold)[0],
            "reason": self._decide(partition_rows, row_threshold)[1],
            "null_count": stats.get("null_count"),
            "missing_count": stats.get("missing_count"),
            "numeric_range": stats.get("numeric_range"),
            "text_count": stats.get("text_count"),
            "checks": copy.deepcopy(verification.get("checks", [])),
            "checks_ok": all(c.get("ok") for c in verification.get("checks", [])),
            "judged_at": time.time(),
        }

    def candidate_to_dict(self, candidate: CompactCandidate) -> dict[str, Any]:
        return {
            "candidate_id": candidate.candidate_id,
            "lake_table": candidate.lake_table,
            "column": candidate.column,
            "partition_key": candidate.partition_key,
            "profile_id": candidate.profile_id,
            "profile_version": candidate.profile_version,
            "profile_fingerprint": candidate.profile_fingerprint,
            "sample_batch_ids": list(candidate.sample_batch_ids),
            "sample_manifest": copy.deepcopy(candidate.sample_manifest),
            "sample_scope": copy.deepcopy(candidate.sample_scope),
            "total_rows": candidate.total_rows,
            "partition_rows": candidate.partition_rows,
            "row_threshold": candidate.row_threshold,
            "decision": candidate.decision,
            "reason": candidate.reason,
            "judgement": copy.deepcopy(candidate.judgement),
            "status": candidate.status,
            "created_at": candidate.created_at,
            "claimed_at": candidate.claimed_at,
            "claimed_by": candidate.claimed_by,
            "claimed_profile_id": candidate.claimed_profile_id,
            "claimed_profile_version": candidate.claimed_profile_version,
            "claimed_fingerprint": candidate.claimed_fingerprint,
            "claimed_batch_ids": list(candidate.claimed_batch_ids),
            "claimed_batch_revisions": dict(candidate.claimed_batch_revisions),
            "claimed_sample_manifest": copy.deepcopy(candidate.claimed_sample_manifest),
            "claim_verification": copy.deepcopy(candidate.claim_verification),
            "revalidated_at": candidate.revalidated_at,
            "revalidated_profile_id": candidate.revalidated_profile_id,
            "revalidated_fingerprint": candidate.revalidated_fingerprint,
            "recompare_history": copy.deepcopy(candidate.recompare_history),
        }

    def list_candidates(
        self, lake_table: str | None = None, status: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            self.candidate_to_dict(c)
            for c in lake_store.list_compact_candidates(lake_table, status)
        ]

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        candidate = lake_store.get_compact_candidate(candidate_id)
        if candidate is None:
            return None
        payload = self.candidate_to_dict(candidate)
        profile = lake_store.get_profile(candidate.profile_id)
        if profile is not None:
            payload["profile"] = self.column_stat.profile_to_dict(profile)
        return payload

    def claim_candidate(
        self, candidate_id: str, *, claimed_by: str = "compact_worker",
    ) -> dict[str, Any]:
        """领取候选：固定本次消费的画像版本、样本批次与判断结果。"""
        with _CANDIDATE_LOCK:
            return self._claim_candidate_locked(candidate_id, claimed_by)

    def _claim_candidate_locked(
        self, candidate_id: str, claimed_by: str,
    ) -> dict[str, Any]:
        candidate = lake_store.get_compact_candidate(candidate_id)
        if candidate is None:
            return {"ok": False, "message": f"压实候选 {candidate_id} 不存在"}
        if candidate.status == "已领取":
            return {"ok": False, "message": f"压实候选 {candidate_id} 已被领取"}
        if candidate.status == "已失效":
            return {"ok": False, "message": f"压实候选 {candidate_id} 已失效，需重新比对门槛"}

        profile = lake_store.get_profile(candidate.profile_id)
        if profile is None:
            return {"ok": False, "message": f"候选引用的画像 {candidate.profile_id} 不存在"}

        # 领取与统计同时发生时，以画像原件里的样本为准，记录领取瞬间的批次修订号。
        consumed_revisions = lake_store.batch_revisions(candidate.sample_batch_ids)
        candidate.status = "已领取"
        candidate.claimed_at = time.time()
        candidate.claimed_by = claimed_by
        candidate.claimed_profile_id = candidate.profile_id
        candidate.claimed_profile_version = candidate.profile_version
        candidate.claimed_fingerprint = candidate.profile_fingerprint
        candidate.claimed_batch_revisions = consumed_revisions
        candidate.claimed_batch_ids = list(candidate.sample_batch_ids)
        candidate.claimed_sample_manifest = copy.deepcopy(candidate.sample_manifest)

        reproducibility = self.column_stat.recompute_profile(profile.profile_id)
        drift = {
            batch_id: {
                "claimed_revision": consumed_revisions.get(batch_id),
                "current_revision": lake_store.batch_revisions([batch_id]).get(batch_id),
            }
            for batch_id in candidate.sample_batch_ids
        }
        candidate.claim_verification = {
            "consumed_profile_id": profile.profile_id,
            "consumed_profile_version": profile.profile_version,
            "consumed_fingerprint": profile.fingerprint,
            "consumed_batch_ids": list(candidate.sample_batch_ids),
            "consumed_batch_revisions": consumed_revisions,
            "reproducible": reproducibility.get("reproducible", False),
            "recomputed_fingerprint": reproducibility.get("recomputed_fingerprint"),
            "verification": reproducibility.get("verification"),
            "batch_revision_drift": drift,
            "decision": candidate.decision,
            "reason": candidate.reason,
            "checked_at": time.time(),
        }
        return {
            "ok": True,
            "message": f"压实候选 {candidate_id} 已领取",
            "candidate": self.candidate_to_dict(candidate),
            "consumed_sample": {
                "profile_id": profile.profile_id,
                "profile_version": profile.profile_version,
                "fingerprint": profile.fingerprint,
                "batch_ids": list(candidate.sample_batch_ids),
                "batch_revisions": consumed_revisions,
                "sample_manifest": copy.deepcopy(candidate.sample_manifest),
            },
            "claim_verification": candidate.claim_verification,
        }

    def verify_claimed_candidate(self, candidate_id: str) -> dict[str, Any]:
        """复核已领计划：用画像原件复算，并对照当前批次修订。"""
        candidate = lake_store.get_compact_candidate(candidate_id)
        if candidate is None:
            return {"ok": False, "message": f"压实候选 {candidate_id} 不存在"}
        if candidate.status != "已领取":
            return {"ok": False, "message": f"压实候选 {candidate_id} 尚未领取"}
        profile = lake_store.get_profile(candidate.profile_id)
        if profile is None:
            return {"ok": False, "message": f"候选引用的画像 {candidate.profile_id} 不存在"}
        reproducibility = self.column_stat.recompute_profile(profile.profile_id)
        current_revisions = lake_store.batch_revisions(candidate.sample_batch_ids)
        drift = {
            batch_id: {
                "claimed_revision": candidate.claimed_batch_revisions.get(batch_id),
                "current_revision": current_revisions.get(batch_id),
            }
            for batch_id in candidate.sample_batch_ids
        }
        return {
            "ok": True,
            "candidate_id": candidate.candidate_id,
            "consumed_profile_id": candidate.claimed_profile_id,
            "consumed_profile_version": candidate.claimed_profile_version,
            "consumed_fingerprint": candidate.claimed_fingerprint,
            "recomputed_fingerprint": reproducibility.get("recomputed_fingerprint"),
            "reproducible": reproducibility.get("reproducible", False),
            "matches_original": reproducibility.get("matches", False),
            "batch_revision_drift": drift,
            "sample_changed_after_claim": any(
                item["claimed_revision"] != item["current_revision"]
                for item in drift.values()
            ),
            "judgement": copy.deepcopy(candidate.judgement),
            "verification": reproducibility.get("verification"),
        }

    def revalidate_pending_candidates(
        self,
        lake_table: str,
        *,
        changed_batch_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """批次修订后，待领候选按当前样本重新计算并比对门槛。

        已领候选不受影响：它们绑定的是画像原件里的行级样本。
        """
        with _CANDIDATE_LOCK:
            return self._revalidate_pending_locked(lake_table, changed_batch_ids)

    def _revalidate_pending_locked(
        self, lake_table: str, changed_batch_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        changed = set(changed_batch_ids or [])
        revalidated: list[dict[str, Any]] = []
        for candidate in lake_store.list_compact_candidates(lake_table, status="待领取"):
            if changed and not (changed & set(candidate.sample_batch_ids)):
                continue
            scope = candidate.sample_scope
            profile = self.column_stat.compute_column_profile(
                candidate.lake_table,
                candidate.column,
                schema_version=scope.get("schema_version"),
                batch_ids=scope.get("batch_ids") or None,
                source_system=scope.get("source_system"),
                partition_column=scope.get("partition_column", "partition_dt"),
                purpose="revalidate",
                created_by="compact_job",
            )
            partitions = profile["scope"].get("partitions") or {}
            rows = partitions.get(candidate.partition_key, 0)
            decision, reason = self._decide(rows, candidate.row_threshold)
            previous = {
                "profile_id": candidate.profile_id,
                "profile_version": candidate.profile_version,
                "fingerprint": candidate.profile_fingerprint,
                "partition_rows": candidate.partition_rows,
                "decision": candidate.decision,
                "reason": candidate.reason,
            }
            candidate.recompare_history.append({
                "at": time.time(),
                "changed_batch_ids": sorted(changed) or list(candidate.sample_batch_ids),
                "previous": previous,
                "current": {
                    "profile_id": profile["profile_id"],
                    "profile_version": profile["profile_version"],
                    "fingerprint": profile["fingerprint"],
                    "partition_rows": rows,
                    "decision": decision,
                    "reason": reason,
                },
            })
            candidate.profile_id = profile["profile_id"]
            candidate.profile_version = profile["profile_version"]
            candidate.profile_fingerprint = profile["fingerprint"]
            candidate.sample_batch_ids = profile["scope"].get("batch_ids", [])
            candidate.sample_manifest = copy.deepcopy(profile["sample_manifest"])
            candidate.sample_scope = copy.deepcopy(profile["scope"])
            candidate.total_rows = profile["scope"].get("row_count", 0)
            candidate.partition_rows = rows
            candidate.decision = decision
            candidate.reason = reason
            candidate.judgement = self._build_judgement(
                profile, candidate.partition_key, rows, candidate.row_threshold)
            candidate.revalidated_at = time.time()
            candidate.revalidated_profile_id = profile["profile_id"]
            candidate.revalidated_fingerprint = profile["fingerprint"]
            if not decision:
                candidate.status = "已失效"
            revalidated.append(self.candidate_to_dict(candidate))
        return revalidated
