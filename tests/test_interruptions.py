import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import DomainError, RadioDB
from interruptions import DEFERRED_STATUS, PENDING_STATUS, covered_slots, overlaps, review_slot


class InterruptionFlowTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.news = self.db.add_program("整点新闻", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.ad = self.db.add_program("青柠广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        self.db.add_sponsor_policy("青柠", 90)
        # 2026-09-28 是周一
        self.s9 = self.db.schedule_slot("2026-09-28", "09:00", self.news, "华东")
        self.s10 = self.db.schedule_slot("2026-09-28", "10:00", self.news, "华东")
        self.s11 = self.db.schedule_slot("2026-09-28", "11:00", self.ad, "华东")

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_register_defers_unplayed_and_keeps_played(self):
        self.db.record_playout(self.s9, "09:00", 30, self.news)
        result = self.db.register_interruption("华东", "2026-09-28", "09:30", "10:30", "突发新闻")
        self.assertEqual("active", result["status"])
        ids = {s["id"] for s in result["slots"]}
        self.assertEqual({self.s10}, ids)  # 09:00 已播不动，11:00 不重叠
        self.assertEqual("planned", self.db.get_slot(self.s9)["status"])
        slot = self.db.get_slot(self.s10)
        self.assertEqual(DEFERRED_STATUS, slot["status"])
        self.assertEqual("09:30", self.db.get_interruption(result["id"])["start_time"])
        # 占位期间，新排期不能落进插播窗口，顺延节目也不占排期位置
        with self.assertRaisesRegex(DomainError, "插播"):
            self.db.schedule_slot("2026-09-28", "10:15", self.news, "华东")
        # 第二张插播单不能与进行中的重叠
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.register_interruption("华东", "2026-09-28", "10:00", "11:00")

    def test_release_restores_after_recheck(self):
        bid = self.db.register_interruption("华东", "2026-09-28", "09:30", "10:30")["id"]
        self.assertEqual(DEFERRED_STATUS, self.db.get_slot(self.s10)["status"])
        result = self.db.release_interruption(bid)
        self.assertEqual("released", result["status"])
        self.assertEqual([self.s10], [r["slot_id"] for r in result["restored"]])
        self.assertEqual([], result["pending"])
        self.assertEqual("planned", self.db.get_slot(self.s10)["status"])
        with self.assertRaisesRegex(DomainError, "已经结束"):
            self.db.release_interruption(bid)

    def test_early_end_keeps_actual_end_time(self):
        bid = self.db.register_interruption("华东", "2026-09-28", "09:30", "10:30")["id"]
        result = self.db.release_interruption(bid, "09:45")
        self.assertEqual("09:45", result["actual_end_time"])
        with self.assertRaisesRegex(DomainError, "实际结束时刻"):
            other = self.db.register_interruption("华东", "2026-09-28", "12:30", "13:00")["id"]
            self.db.release_interruption(other, "09:00")

    def test_recheck_blocks_on_blocked_window(self):
        # 周一 08:00-08:30 禁播：把节目顺延到恢复时撞上禁播窗口
        early = self.db.schedule_slot("2026-09-28", "08:30", self.news, "华东")
        self.db.add_blocked_window("华东", 0, "08:00", "09:00", "临时管制")
        bid = self.db.register_interruption("华东", "2026-09-28", "08:30", "08:45")["id"]
        self.assertEqual(DEFERRED_STATUS, self.db.get_slot(early)["status"])
        result = self.db.release_interruption(bid)
        self.assertEqual([], result["restored"])
        pending = result["pending"][0]
        self.assertEqual(early, pending["slot_id"])
        self.assertTrue(any("禁播" in r for r in pending["reasons"]))
        slot = self.db.get_slot(early)
        self.assertEqual(PENDING_STATUS, slot["status"])
        self.assertIn("禁播", slot["pending_reason"])

    def test_recheck_blocks_on_sponsor_gap(self):
        # 11:00 的广告被顺延；插播窗口结束后、原广告前面补排一个同赞助商节目，
        # 恢复复核必须按最小间隔把原广告留在待重排。
        bid = self.db.register_interruption("华东", "2026-09-28", "10:45", "11:15")["id"]
        near = self.db.add_program("青柠短版", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        self.db.schedule_slot("2026-09-28", "11:15", near, "华东")
        self.assertEqual(DEFERRED_STATUS, self.db.get_slot(self.s11)["status"])
        result = self.db.release_interruption(bid)
        pending = {p["slot_id"]: p for p in result["pending"]}
        self.assertIn(self.s11, pending)
        self.assertTrue(any("赞助商" in r for r in pending[self.s11]["reasons"]))
        self.assertEqual(PENDING_STATUS, self.db.get_slot(self.s11)["status"])

    def test_recheck_finds_takeover_conflict(self):
        # 占位保护下正常排期无法落进窗口；若数据层面原时段已被别的正式排期占用
        # （如人工直接补录），恢复复核必须发现冲突而不是制造重叠。
        bid = self.db.register_interruption("华东", "2026-09-28", "09:30", "10:30")["id"]
        filler = self.db.add_program("垫乐", "music", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.db.conn.execute(
            "INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,status,created_at) "
            "VALUES('2026-09-28','10:00',30,?,'华东','planned',?)",
            (filler, "2026-09-28T10:00:00"),
        )
        self.db.conn.commit()
        result = self.db.release_interruption(bid)
        self.assertEqual([], result["restored"])
        self.assertIn("占用", result["pending"][0]["reasons"][0])

    def test_license_window_rechecked_before_restore(self):
        # 顺延后授权窗口被收窄，恢复复核必须能发现版权越界
        short = self.db.add_program("短期节目", "talk", 30, "2026-09-01", "2026-09-30", None, 0, ["华东"])
        slot = self.db.schedule_slot("2026-09-28", "12:00", short, "华东")
        bid = self.db.register_interruption("华东", "2026-09-28", "12:00", "12:30")["id"]
        self.db.conn.execute("UPDATE programs SET end_date='2026-09-27' WHERE id=?", (short,))
        self.db.conn.commit()
        result = self.db.release_interruption(bid)
        self.assertEqual([], result["restored"])
        self.assertEqual(slot, result["pending"][0]["slot_id"])
        self.assertTrue(any("授权窗口" in r for r in result["pending"][0]["reasons"]))


class PureRulesTest(unittest.TestCase):
    def test_overlaps_is_half_open(self):
        self.assertTrue(overlaps(0, 10, 5, 15))
        self.assertFalse(overlaps(0, 10, 10, 20))

    def test_covered_slots_skips_played_and_non_live(self):
        slots = [
            {"id": 1, "status": "planned", "has_played": True, "start_minutes": 0, "duration_minutes": 30},
            {"id": 2, "status": "replaced", "has_played": False, "start_minutes": 30, "duration_minutes": 30},
            {"id": 3, "status": "deferred", "has_played": False, "start_minutes": 35, "duration_minutes": 10},
        ]
        self.assertEqual([2], [s["id"] for s in covered_slots(slots, 10, 40)])

    def test_review_slot_collects_reasons(self):
        slot = {"id": 1, "air_date": "2026-09-28", "region": "华东", "start_minutes": 0,
                "duration_minutes": 30, "weekday": 0}
        program = {"active": 1, "start_date": "2026-01-01", "end_date": "2026-12-31", "sponsor": None}
        reasons = review_slot(slot, program, ["华北"], [], [], [], None)
        self.assertEqual(["节目未授权在华东播出"], reasons)
        reasons = review_slot(slot, None, [], [], [], [], None)
        self.assertEqual(["节目不存在或已停用"], reasons)


class LegacyMigrationTest(unittest.TestCase):
    def test_old_schema_is_migrated(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE slots (id INTEGER PRIMARY KEY, air_date TEXT, start_time TEXT, "
                "duration_minutes INTEGER, program_id INTEGER, region TEXT, status TEXT DEFAULT 'planned' "
                "CHECK(status IN ('planned','replaced','cancelled')), replaced_from INTEGER, created_at TEXT)"
            )
            conn.execute("INSERT INTO slots VALUES(1,'2026-09-28','09:00',30,1,'华东','planned',NULL,'t')")
            conn.commit()
            conn.close()
            db = RadioDB(path)
            cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(slots)").fetchall()}
            self.assertIn("interruption_id", cols)
            row = db.conn.execute("SELECT status FROM slots WHERE id=1").fetchone()
            self.assertEqual("planned", row["status"])
            db.close()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
