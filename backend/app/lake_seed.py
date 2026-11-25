"""湖表存储引擎的示例数据：给列画像与压实候选提供可核对的真实样本。

这些数据只写入 lake_store（批次行级数据），与 store 里的业务列表样例互补：
- ods_user_behavior 的 V1 批次带 amount 数字 0、5、空值和一条缺列记录
- ods_user_behavior 的 V2 批次把同名字段写成文本，不应混进 V1 的数值区间
- ods_payment 也有叫 amount 的字段，必须按湖表独立计数
"""
from __future__ import annotations

from app.lake_store import lake_store

SEED_BATCH_IDS = (
    "LKB-USER-V1-001",
    "LKB-USER-V2-001",
    "LKB-PAY-V1-001",
)

USER_V1_ROWS: list[dict[str, object]] = [
    {"event_time": "2026-11-01T08:00:00", "user_id": "u1", "action": "login",
     "amount": 0, "partition_dt": "20261101"},
    {"event_time": "2026-11-01T09:00:00", "user_id": "u2", "action": "purchase",
     "amount": 5, "partition_dt": "20261101"},
    {"event_time": "2026-11-01T10:00:00", "user_id": "u3", "action": "login",
     "amount": None, "partition_dt": "20261101"},
    # 这一行没有 amount 列，用于和空值分开核对
    {"event_time": "2026-11-01T11:00:00", "user_id": "u4", "action": "logout",
     "partition_dt": "20261101"},
]

USER_V2_ROWS: list[dict[str, object]] = [
    {"event_time": "2026-11-02T08:00:00", "user_id": "u1", "action": "login",
     "amount": "120.50", "device_id": "d1", "partition_dt": "20261102"},
    {"event_time": "2026-11-02T09:00:00", "user_id": "u5", "action": "purchase",
     "amount": "free", "device_id": "d2", "partition_dt": "20261102"},
]

PAY_V1_ROWS: list[dict[str, object]] = [
    {"event_time": "2026-11-01T08:00:00", "payment_id": "p1",
     "amount": 700, "partition_dt": "20261101"},
    {"event_time": "2026-11-01T09:00:00", "payment_id": "p2",
     "amount": 900, "partition_dt": "20261101"},
]


def seed_lake_store() -> dict[str, int]:
    """幂等地写入示例批次，重复调用不会追加重复数据。"""
    seeded = 0
    specs = [
        ("LKB-USER-V1-001", "ods_user_behavior", "OLTP_DB", USER_V1_ROWS,
         "2026-11-01T00:00:00", "2026-11-01T23:59:59", "SCH-v1-001"),
        ("LKB-USER-V2-001", "ods_user_behavior", "OLTP_DB", USER_V2_ROWS,
         "2026-11-02T00:00:00", "2026-11-02T23:59:59", "SCH-v2-001"),
        ("LKB-PAY-V1-001", "ods_payment", "OLTP_DB", PAY_V1_ROWS,
         "2026-11-01T00:00:00", "2026-11-01T23:59:59", "SCH-PAY-v1-001"),
    ]
    for batch_id, table, source, rows, start, end, schema_version in specs:
        if lake_store.get_batch(batch_id) is not None:
            continue
        lake_store.store_batch(
            batch_id, table, source, [dict(row) for row in rows], start, end,
            schema_version=schema_version,
        )
        lake_store.update_batch_status(batch_id, "ingested")
        seeded += 1
    return {"seeded_batches": seeded}
