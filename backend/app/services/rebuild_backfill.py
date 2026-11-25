"""重建补数领域服务：按窗口重算分区，覆盖策略与补数水位。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.lake_store import lake_store


class RebuildBackfillService(BaseModuleService):
    MODULE = "rebuild_backfill"
    ENTITY = "补数任务"
    OPERATION_TERM = "水位"
    LIST_FIELDS = ["补数编号", "所属湖表", "补数窗口", "依赖上游表", "重算分区数", "覆盖策略", "补数水位", "补数耗时"]
    REQUIRED_FIELDS = ["补数编号", "所属湖表", "补数窗口"]
    STATUS_ORDER = ["待排期", "补数中", "已补齐", "补数失败"]
    ACTION_RULES = {"排期补数": "补数中", "确认补齐": "已补齐", "标记失败": "补数失败"}
    NEGATIVE_ACTIONS = []

    def execute_backfill(self, lake_table: str, window_start: str, window_end: str,
                         strategy: str = "replace") -> dict[str, Any]:
        """按窗口重算分区：根据覆盖策略决定是替换旧分区还是并存新分区。"""
        start_time = time.time()
        files = lake_store.list_files(lake_table)
        # 选择窗口内的分区
        affected_files = [
            f for f in files
            if f.partition_key and window_start <= f.partition_key <= window_end
        ]
        affected_ids = [f.file_id for f in affected_files]

        # 覆盖策略：replace=替换旧分区 replicate=新分区并存
        if strategy == "replace":
            pf = lake_store.register_file(lake_table, window_start, rows=100, bytes_=25600)
            lake_store.replace_file_list(lake_table, affected_ids, [pf])
        elif strategy == "replicate":
            for f in affected_files:
                lake_store.register_file(lake_table, f.partition_key + "_v2",
                                         rows=f.file_rows, bytes_=f.file_bytes)

        elapsed = time.time() - start_time
        wm = lake_store.advance_watermark(lake_table, window_end)
        return {
            "lake_table": lake_table,
            "window": f"{window_start}~{window_end}",
            "affected_partitions": len(affected_files),
            "strategy": strategy,
            "elapsed": elapsed,
            "watermark": wm.pending_value if wm else None,
        }

    def has_upstream_gap(self, upstream_tables: list[str]) -> dict[str, Any]:
        """检测上游表数据是否补齐。"""
        gaps = {}
        for t in upstream_tables:
            wm = lake_store.get_watermark(t)
            gaps[t] = wm is None or wm.committed_value == ""
        return {"upstream_tables": upstream_tables, "gaps": gaps}

    def correct_batch_rows(
        self,
        batch_id: str,
        rows: list[dict[str, Any]],
        *,
        schema_version: str | None = None,
        source_system: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
    ):
        """旧版补数更正：用更正后的行替换批次内容并推进修订号。

        这里只改批次数据，不直接改动待领压实计划；计划门槛的重比在路由层
        编排（先更正、再让待领候选按当前样本重新计算）。
        """
        return lake_store.replace_batch_rows(
            batch_id, rows,
            schema_version=schema_version,
            source_system=source_system,
            window_start=window_start,
            window_end=window_end,
        )