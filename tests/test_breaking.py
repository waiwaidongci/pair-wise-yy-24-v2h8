import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import breaking
from database import DomainError, RadioDB


class BreakingInsertTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.news = self.db.add_program("整点新闻", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.music = self.db.add_program("午后音乐", "music", 60, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.ad = self.db.add_program("青柠广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        self.db.add_sponsor_policy("青柠", 90)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_unplayed_covered_deferred_and_aired_left_in_day(self):
        aired = self.db.schedule_slot("2026-09-28", "09:15", self.news, "华东")
        deferred = self.db.schedule_slot("2026-09-28", "10:00", self.music, "华东")
        self.db.authorize_region(self.music, "华北")
        outside_region = self.db.schedule_slot("2026-09-28", "10:30", self.music, "华北")
        self.db.record_playout(aired, "09:15", 30, self.news)

        insert = self.db.register_insert("2026-09-28", "09:30", "11:00", "华东", "突发新闻")
        self.assertEqual("active", insert["status"])
        outcomes = {d["slot_id"]: d["outcome"] for d in insert["deferrals"]}
        self.assertEqual({"deferred"}, set(outcomes.values()))
        self.assertIn(deferred, outcomes)
        self.assertNotIn(aired, outcomes)
        slot = self.db.get_slot(deferred)
        self.assertEqual("deferred", slot["status"])
        self.assertEqual("10:00", slot["start_time"])  # 保留原时段
        self.assertEqual("planned", self.db.get_slot(aired)["status"])
        # 存档里记录了原排期状态，便于恢复
        record = next(d for d in insert["deferrals"] if d["slot_id"] == deferred)
        self.assertEqual("planned", record["original_status"])

    def test_withdraw_restores_after_recheck(self):
        first = self.db.schedule_slot("2026-09-28", "10:00", self.news, "华东")
        second = self.db.schedule_slot("2026-09-28", "12:00", self.music, "华东")
        insert_id = self.db.register_insert("2026-09-28", "09:30", "13:00", "华东")["id"]
        self.assertEqual("deferred", self.db.get_slot(first)["status"])

        result = self.db.withdraw_insert(insert_id)
        self.assertEqual("withdrawn", result["status"])
        restored = {d["slot_id"]: d for d in result["deferrals"]}
        self.assertEqual("restored", restored[first]["outcome"])
        self.assertEqual("planned", self.db.get_slot(first)["status"])
        self.assertEqual("planned", self.db.get_slot(second)["status"])

    def test_early_end_restores_only_after_cutoff(self):
        before_cut = self.db.schedule_slot("2026-09-28", "09:45", self.news, "华东")
        after_cut = self.db.schedule_slot("2026-09-28", "10:30", self.music, "华东")
        insert_id = self.db.register_insert("2026-09-28", "09:30", "11:00", "华东")["id"]
        result = self.db.end_insert(insert_id, "10:00")
        outcomes = {d["slot_id"]: d["outcome"] for d in result["deferrals"]}
        self.assertEqual("deferred", outcomes[before_cut])     # 已被占用，留在顺延区
        self.assertEqual("restored", outcomes[after_cut])      # 提前让出，恢复
        self.assertEqual("planned", self.db.get_slot(after_cut)["status"])
        self.assertEqual("deferred", self.db.get_slot(before_cut)["status"])

    def test_full_end_keeps_everything_deferred(self):
        slot = self.db.schedule_slot("2026-09-28", "10:00", self.news, "华东")
        insert_id = self.db.register_insert("2026-09-28", "09:30", "11:00", "华东")["id"]
        result = self.db.end_insert(insert_id, "11:00")
        self.assertEqual("ended", result["status"])
        self.assertEqual("deferred", result["deferrals"][0]["outcome"])
        self.assertEqual("deferred", self.db.get_slot(slot)["status"])

    def test_restore_conflict_sponsor_gap_goes_pending(self):
        # 插播期间临时排了一条同赞助商广告，恢复时原时段间隔不足 90 分钟
        original_ad = self.db.schedule_slot("2026-09-28", "10:00", self.ad, "华东")
        insert_id = self.db.register_insert("2026-09-28", "09:30", "11:30", "华东")["id"]
        self.db.schedule_slot("2026-09-28", "09:35", self.ad, "华东")

        result = self.db.withdraw_insert(insert_id)
        record = next(d for d in result["deferrals"] if d["slot_id"] == original_ad)
        self.assertEqual("pending_reschedule", record["outcome"])
        self.assertIn("青柠", record["reasons"])
        self.assertEqual("pending_reschedule", self.db.get_slot(original_ad)["status"])

    def test_restore_conflict_blocked_window_goes_pending(self):
        slot = self.db.schedule_slot("2026-09-28", "10:00", self.news, "华东")  # 周一
        insert_id = self.db.register_insert("2026-09-28", "09:30", "11:30", "华东")["id"]
        self.db.add_blocked_window("华东", 0, "09:30", "10:30", "插播后新增检修")
        result = self.db.withdraw_insert(insert_id)
        record = result["deferrals"][0]
        self.assertEqual(slot, record["slot_id"])
        self.assertEqual("pending_reschedule", record["outcome"])
        self.assertIn("禁播", record["reasons"])

    def test_restore_conflict_license_revoked_goes_pending(self):
        slot = self.db.schedule_slot("2026-09-28", "10:00", self.news, "华东")
        insert_id = self.db.register_insert("2026-09-28", "09:30", "11:30", "华东")["id"]
        self.db.conn.execute("DELETE FROM program_regions WHERE program_id=? AND region='华东'", (self.news,))
        self.db.conn.commit()
        result = self.db.withdraw_insert(insert_id)
        record = result["deferrals"][0]
        self.assertEqual("pending_reschedule", record["outcome"])
        self.assertIn("未授权", record["reasons"])

    def test_overlapping_active_insert_rejected(self):
        self.db.register_insert("2026-09-28", "09:30", "11:00", "华东")
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.register_insert("2026-09-28", "10:30", "12:00", "华东")

    def test_cannot_withdraw_twice(self):
        insert_id = self.db.register_insert("2026-09-28", "09:30", "11:00", "华东")["id"]
        self.db.withdraw_insert(insert_id)
        with self.assertRaisesRegex(DomainError, "已结束"):
            self.db.withdraw_insert(insert_id)

    def test_deferred_slots_skipped_by_scheduling_and_reconcile(self):
        slot = self.db.schedule_slot("2026-09-28", "10:00", self.news, "华东")
        self.db.register_insert("2026-09-28", "09:30", "11:00", "华东")
        # 顺延期间原时段可以重新排别的节目
        other = self.db.schedule_slot("2026-09-28", "10:00", self.music, "华东")
        self.assertNotEqual(slot, other)
        # 对账不把顺延中的节目算作漏播
        exceptions = self.db.reconcile_date("2026-09-28")
        self.assertFalse(any(e["slot_id"] == slot for e in exceptions))

    def test_replaced_status_restored_to_replaced(self):
        slot = self.db.schedule_slot("2026-09-28", "10:00", self.news, "华东")
        self.db.replace_slot(slot, self.music)
        insert_id = self.db.register_insert("2026-09-28", "09:30", "11:00", "华东")["id"]
        self.assertEqual("deferred", self.db.get_slot(slot)["status"])
        self.db.withdraw_insert(insert_id)
        self.assertEqual("replaced", self.db.get_slot(slot)["status"])
        self.assertEqual(self.music, self.db.get_slot(slot)["program_id"])


    def test_restore_conflict_sponsor_gap_against_aired_program(self):
        # 原排期间隔充足；插播期间又实播了一条同赞助广告，恢复复核应计入已播内容
        early_ad = self.db.schedule_slot("2026-09-28", "08:00", self.ad, "华东")
        pending = self.db.schedule_slot("2026-09-28", "10:00", self.ad, "华东")
        self.db.record_playout(early_ad, "08:00", 5, self.ad)
        insert_id = self.db.register_insert("2026-09-28", "09:30", "11:30", "华东")["id"]
        filler_slot = self.db.schedule_slot("2026-09-28", "09:40", self.music, "华东")
        self.db.replace_slot(filler_slot, self.ad)
        self.db.record_playout(filler_slot, "09:40", 5, self.ad)
        result = self.db.withdraw_insert(insert_id)
        record = next(d for d in result["deferrals"] if d["slot_id"] == pending)
        self.assertEqual("pending_reschedule", record["outcome"])
        self.assertIn("间隔不足", record["reasons"])


class BreakingDecisionTest(unittest.TestCase):
    """判定层只用字典数据，不依赖数据库。"""

    def test_classify_separates_unplayed_and_aired(self):
        slots = [
            {"id": 1, "air_date": "2026-09-28", "region": "华东", "start_time": "09:00",
             "duration_minutes": 45, "status": "planned"},
            {"id": 2, "air_date": "2026-09-28", "region": "华东", "start_time": "10:00",
             "duration_minutes": 30, "status": "planned"},
            {"id": 3, "air_date": "2026-09-28", "region": "华北", "start_time": "10:00",
             "duration_minutes": 30, "status": "planned"},
        ]
        covered, aired = breaking.classify_covered(
            slots, region="华东", air_date="2026-09-28",
            start_time="09:30", end_time="11:00", played_slot_ids={1},
        )
        self.assertEqual([1], [s["id"] for s in aired])
        self.assertEqual([2], [s["id"] for s in covered])

    def test_review_only_checks_license_blocked_sponsor(self):
        slot = {"id": 5, "air_date": "2026-09-28", "region": "华东",
                "start_time": "10:00", "duration_minutes": 5}
        program = {"id": 9, "active": 1, "sponsor": "青柠",
                   "start_date": "2026-01-01", "end_date": "2026-12-31"}
        neighbor = {"id": 8, "start_time": "09:30", "duration_minutes": 5}
        review = breaking.review_restore(
            slot, program=program, authorized=True, windows=[],
            sponsor_gap_minutes=90, sponsored_neighbors=[neighbor],
        )
        self.assertFalse(review.ok)
        self.assertTrue(all("赞助" in r for r in review.reasons))
        review = breaking.review_restore(
            slot, program=program, authorized=True, windows=[],
            sponsor_gap_minutes=90, sponsored_neighbors=[],
        )
        self.assertTrue(review.ok)


if __name__ == "__main__":
    unittest.main()
