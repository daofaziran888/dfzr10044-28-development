"""测试按样本范围计算列画像、压实候选领取与修订后重新比对门槛。"""
from __future__ import annotations

import pytest

from app.lake_seed import seed_lake_store
from app.lake_store import lake_store
from app.pipeline.chain_core import ChainContext
from app.pipeline.chain_runner import _step_column_stat_compute
from app.services.column_stat import ColumnStatService
from app.services.compact_job import CompactJobService
from app.services.rebuild_backfill import RebuildBackfillService


@pytest.fixture(autouse=True)
def _reset_profiles_and_candidates():
    lake_store._profiles.clear()
    lake_store._compact_candidates.clear()
    yield
    lake_store._profiles.clear()
    lake_store._compact_candidates.clear()


@pytest.fixture
def seeded():
    seed_lake_store()
    return "ods_user_behavior"


def _correct_user_v1(rows: list[dict]) -> None:
    RebuildBackfillService().correct_batch_rows(
        "LKB-USER-V1-001", rows, schema_version="SCH-v1-001")


def _restore_user_v1() -> None:
    from app.lake_seed import USER_V1_ROWS
    RebuildBackfillService().correct_batch_rows(
        "LKB-USER-V1-001", [dict(row) for row in USER_V1_ROWS], schema_version="SCH-v1-001")


class TestSampleScope:
    def test_scope_lists_versions_and_batches(self, seeded):
        scope = ColumnStatService().describe_sample_scope(seeded, "amount")
        versions = set(scope["schema_versions"])
        assert {"SCH-v1-001", "SCH-v2-001"}.issubset(versions)
        selected = set(scope["selected_batch_ids"])
        assert "LKB-USER-V1-001" in selected
        assert "LKB-USER-V2-001" in selected

    def test_scope_filters_by_schema_version(self, seeded):
        scope = ColumnStatService().describe_sample_scope(
            seeded, "amount", schema_version="SCH-v1-001")
        assert scope["selected_batch_ids"] == ["LKB-USER-V1-001"]

    def test_scope_reports_missing_column_rows(self, seeded):
        scope = ColumnStatService().describe_sample_scope(
            seeded, "amount", schema_version="SCH-v1-001")
        batch = scope["available_batches"][0]
        assert batch["missing_column_rows"] == 1
        assert batch["null_rows"] == 1


class TestProfileSemantics:
    def test_v1_amount_range_sees_zero_and_five(self, seeded):
        profile = ColumnStatService().compute_column_profile(
            seeded, "amount", schema_version="SCH-v1-001")
        assert profile["stats"]["numeric_range"] == {"min": 0, "max": 5}
        assert profile["stats"]["numeric_count"] == 2

    def test_null_and_missing_checked_separately(self, seeded):
        profile = ColumnStatService().compute_column_profile(
            seeded, "amount", schema_version="SCH-v1-001")
        stats = profile["stats"]
        assert stats["total_count"] == 4
        assert stats["present_count"] == 3
        assert stats["missing_count"] == 1
        assert stats["null_count"] == 1
        checks = {c["name"]: c for c in profile["verification"]["checks"]}
        assert checks["缺列与空值分开核对"]["ok"]
        assert checks["样本行数闭合"]["ok"]
        assert checks["空值与非空闭合"]["ok"]
        assert profile["verification"]["present_plus_missing_equals_total"]

    def test_v2_text_excluded_from_numeric_range(self, seeded):
        profile = ColumnStatService().compute_column_profile(
            seeded, "amount", schema_version="SCH-v2-001")
        stats = profile["stats"]
        assert stats["numeric_range"] is None
        assert stats["numeric_count"] == 0
        assert stats["text_count"] == 2
        assert "free" in stats["text_values"]

    def test_querying_v1_does_not_pull_v2_text(self, seeded):
        v1 = ColumnStatService().compute_column_profile(
            seeded, "amount", schema_version="SCH-v1-001")
        assert v1["stats"]["numeric_range"] == {"min": 0, "max": 5}
        assert v1["stats"]["text_count"] == 0
        assert v1["scope"]["batch_ids"] == ["LKB-USER-V1-001"]

    def test_other_lake_table_amount_counted_independently(self, seeded):
        service = ColumnStatService()
        user = service.compute_column_profile(seeded, "amount", schema_version="SCH-v1-001")
        payment = service.compute_column_profile("ods_payment", "amount")
        assert user["stats"]["numeric_range"] == {"min": 0, "max": 5}
        assert payment["stats"]["numeric_range"] == {"min": 700, "max": 900}
        assert user["fingerprint"] != payment["fingerprint"]
        # 两张表的画像互不串扰
        assert set(user["scope"]["batch_ids"]) == {"LKB-USER-V1-001"}
        assert set(payment["scope"]["batch_ids"]) == {"LKB-PAY-V1-001"}

    def test_profile_records_manifest_and_confirmed_partitions(self, seeded):
        profile = ColumnStatService().compute_column_profile(
            seeded, "amount", schema_version="SCH-v1-001")
        assert profile["scope"]["confirmed_partitions"] == ["20261101"]
        manifest = profile["sample_manifest"]
        assert [m["batch_id"] for m in manifest] == ["LKB-USER-V1-001"]
        assert manifest[0]["schema_version"] == "SCH-v1-001"
        assert manifest[0]["row_count"] == 4

    def test_empty_scope_profile_still_verifiable(self):
        profile = ColumnStatService().compute_column_profile(
            "no_such_table", "amount")
        assert profile["stats"]["total_count"] == 0
        assert profile["stats"]["confirmed_partitions"] == []
        assert profile["verification"]["present_plus_missing_equals_total"]


class TestCompactCandidates:
    def test_candidate_binds_profile_version_and_batches(self, seeded):
        result = CompactJobService().build_candidates_from_profile(
            seeded, "amount", schema_version="SCH-v1-001", row_threshold=3)
        candidate = result["candidates"][0]
        assert candidate["profile_version"].startswith("CP-")
        assert candidate["sample_batch_ids"] == ["LKB-USER-V1-001"]
        assert candidate["decision"] is True
        assert candidate["profile_id"]
        assert candidate["profile_fingerprint"]
        assert candidate["judgement"]["checks_ok"] is True

    def test_candidate_below_threshold_not_processed(self, seeded):
        result = CompactJobService().build_candidates_from_profile(
            seeded, "amount", schema_version="SCH-v1-001", row_threshold=10)
        candidate = result["candidates"][0]
        assert candidate["decision"] is False
        assert "未达门槛" in candidate["reason"]

    def test_claim_records_consumed_sample_and_judgement(self, seeded):
        service = CompactJobService()
        built = service.build_candidates_from_profile(
            seeded, "amount", schema_version="SCH-v1-001", row_threshold=3)
        candidate_id = built["candidates"][0]["candidate_id"]
        claimed = service.claim_candidate(candidate_id)
        assert claimed["ok"]
        consumed = claimed["consumed_sample"]
        assert consumed["batch_ids"] == ["LKB-USER-V1-001"]
        assert consumed["profile_version"] == built["candidates"][0]["profile_version"]
        assert consumed["batch_revisions"] == {"LKB-USER-V1-001": 0}
        verification = claimed["claim_verification"]
        assert verification["reproducible"] is True
        assert all(c["ok"] for c in verification["verification"]["checks"])

    def test_claimed_candidate_survives_batch_correction(self, seeded):
        service = CompactJobService()
        built = service.build_candidates_from_profile(
            seeded, "amount", schema_version="SCH-v1-001", row_threshold=3)
        candidate_id = built["candidates"][0]["candidate_id"]
        service.claim_candidate(candidate_id)

        # 旧版补数更正把批次改小，已领计划不应因此丢失画像原件。
        _correct_user_v1([{"amount": 1, "partition_dt": "20261101"}])
        verify = service.verify_claimed_candidate(candidate_id)
        assert verify["reproducible"] is True
        assert verify["matches_original"] is True
        assert verify["sample_changed_after_claim"] is True
        assert verify["batch_revision_drift"]["LKB-USER-V1-001"] == {
            "claimed_revision": 0, "current_revision": 1}
        assert verify["judgement"]["partition_rows"] == 4
        _restore_user_v1()

    def test_pending_candidate_recompares_threshold_after_revision(self, seeded):
        service = CompactJobService()
        built = service.build_candidates_from_profile(
            seeded, "amount", schema_version="SCH-v1-001", row_threshold=3)
        candidate_id = built["candidates"][0]["candidate_id"]

        _correct_user_v1([{"amount": 1, "partition_dt": "20261101"}])
        revalidated = service.revalidate_pending_candidates(
            seeded, changed_batch_ids=["LKB-USER-V1-001"])
        assert len(revalidated) == 1
        candidate = revalidated[0]
        assert candidate["candidate_id"] == candidate_id
        assert candidate["decision"] is False
        assert candidate["status"] == "已失效"
        assert candidate["partition_rows"] == 1
        assert candidate["recompare_history"][0]["previous"]["decision"] is True
        assert candidate["recompare_history"][0]["current"]["decision"] is False
        _restore_user_v1()

    def test_claimed_candidate_not_revalidated(self, seeded):
        service = CompactJobService()
        built = service.build_candidates_from_profile(
            seeded, "amount", schema_version="SCH-v1-001", row_threshold=3)
        candidate_id = built["candidates"][0]["candidate_id"]
        service.claim_candidate(candidate_id)
        _correct_user_v1([{"amount": 1, "partition_dt": "20261101"}])
        assert service.revalidate_pending_candidates(
            seeded, changed_batch_ids=["LKB-USER-V1-001"]) == []
        candidate = service.get_candidate(candidate_id)
        assert candidate["status"] == "已领取"
        assert candidate["recompare_history"] == []
        _restore_user_v1()


    def test_claimed_candidate_cannot_be_claimed_twice(self, seeded):
        service = CompactJobService()
        built = service.build_candidates_from_profile(
            seeded, "amount", schema_version="SCH-v1-001", row_threshold=3)
        candidate_id = built["candidates"][0]["candidate_id"]
        assert service.claim_candidate(candidate_id)["ok"]
        second = service.claim_candidate(candidate_id)
        assert second["ok"] is False
        assert "已被领取" in second["message"]

    def test_revalidation_skips_unaffected_candidates(self, seeded):
        service = CompactJobService()
        built = service.build_candidates_from_profile(
            seeded, "amount", schema_version="SCH-v1-001", row_threshold=3)
        _correct_user_v1([{"amount": 1, "partition_dt": "20261101"}])
        revalidated = service.revalidate_pending_candidates(
            seeded, changed_batch_ids=["LKB-PAY-V1-001"])
        assert revalidated == []
        candidate = service.get_candidate(built["candidates"][0]["candidate_id"])
        assert candidate["status"] == "待领取"
        _restore_user_v1()


class TestProfileDetailAndRecompute:
    def test_profile_detail_carries_candidates(self, seeded):
        service = CompactJobService()
        built = service.build_candidates_from_profile(
            seeded, "amount", schema_version="SCH-v1-001", row_threshold=3)
        profile_id = built["profile"]["profile_id"]
        detail = ColumnStatService().get_profile(profile_id)
        assert detail["profile_version"] == built["profile"]["profile_version"]
        candidates = service.list_candidates(seeded)
        linked = [c for c in candidates if c["profile_id"] == profile_id]
        assert linked and linked[0]["sample_batch_ids"] == ["LKB-USER-V1-001"]

    def test_recompute_matches_original_fingerprint(self, seeded):
        profile = ColumnStatService().compute_column_profile(
            seeded, "amount", schema_version="SCH-v1-001")
        result = ColumnStatService().recompute_profile(profile["profile_id"])
        assert result["matches"] is True
        assert result["original_fingerprint"] == result["recomputed_fingerprint"]

    def test_corrected_batch_changes_new_profile_fingerprint(self, seeded):
        service = ColumnStatService()
        before = service.compute_column_profile(seeded, "amount", schema_version="SCH-v1-001")
        _correct_user_v1([{"amount": 0, "partition_dt": "20261101"},
                          {"amount": 5, "partition_dt": "20261101"}])
        after = service.compute_column_profile(seeded, "amount", schema_version="SCH-v1-001")
        assert before["fingerprint"] != after["fingerprint"]
        assert after["stats"]["missing_count"] == 0
        assert service.recompute_profile(before["profile_id"])["matches"] is True
        _restore_user_v1()


class TestChainStillComputes:
    def test_chain_column_stat_step_unchanged(self, seeded):
        ctx = ChainContext(lake_table=seeded)
        ctx = _step_column_stat_compute(ctx)
        assert ctx.output["column_stat"]["column"] == "amount"
