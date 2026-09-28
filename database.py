from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path

import breaking


class DomainError(ValueError):
    """A business-rule violation that should be shown to the API caller."""


PROGRAM_KINDS = {"music", "ad", "talk", "live"}
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _overlap(a_start: str, a_duration: int, b_start: str, b_duration: int) -> bool:
    start_a, start_b = _minutes(a_start), _minutes(b_start)
    return start_a < start_b + b_duration and start_b < start_a + a_duration


class RadioDB:
    """SQLite-backed radio scheduling service.

    The service keeps planning and actual playout separate. A replacement is
    accepted only when the complete plan remains valid; reconciliation never
    rewrites the plan, it records discrepancies for operators.
    """

    def __init__(self, path: str = "radio.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS programs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              kind TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              start_date TEXT NOT NULL,
              end_date TEXT NOT NULL,
              sponsor TEXT,
              cooldown_minutes INTEGER NOT NULL DEFAULT 0 CHECK(cooldown_minutes >= 0),
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              UNIQUE(title, start_date, end_date)
            );
            CREATE TABLE IF NOT EXISTS program_regions (
              program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
              region TEXT NOT NULL,
              PRIMARY KEY(program_id, region)
            );
            CREATE TABLE IF NOT EXISTS blocked_windows (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              region TEXT NOT NULL,
              weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
              start_time TEXT NOT NULL,
              end_time TEXT NOT NULL,
              reason TEXT NOT NULL,
              CHECK(start_time < end_time)
            );
            CREATE TABLE IF NOT EXISTS sponsor_policies (
              sponsor TEXT PRIMARY KEY,
              min_gap_minutes INTEGER NOT NULL CHECK(min_gap_minutes >= 0)
            );
            CREATE TABLE IF NOT EXISTS slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              program_id INTEGER NOT NULL REFERENCES programs(id),
              region TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'planned'
                CHECK(status IN ('planned','replaced','cancelled','deferred','pending_reschedule')),
              replaced_from INTEGER REFERENCES programs(id),
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_slots_date_region ON slots(air_date, region);
            CREATE TABLE IF NOT EXISTS breaking_inserts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              region TEXT NOT NULL,
              start_time TEXT NOT NULL,
              end_time TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL CHECK(status IN ('active','withdrawn','ended')),
              actual_end_time TEXT,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_insert_date_region ON breaking_inserts(air_date, region);
            CREATE TABLE IF NOT EXISTS slot_deferrals (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              insert_id INTEGER NOT NULL REFERENCES breaking_inserts(id) ON DELETE CASCADE,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              air_date TEXT NOT NULL,
              start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL,
              program_id INTEGER NOT NULL,
              region TEXT NOT NULL,
              original_status TEXT NOT NULL,
              outcome TEXT NOT NULL
                CHECK(outcome IN ('deferred','restored','pending_reschedule')),
              reasons TEXT NOT NULL DEFAULT '',
              reviewed_at TEXT,
              UNIQUE(slot_id, insert_id)
            );
            CREATE TABLE IF NOT EXISTS playout_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              actual_start TEXT NOT NULL,
              actual_duration_minutes INTEGER NOT NULL CHECK(actual_duration_minutes >= 0),
              actual_program_id INTEGER REFERENCES programs(id),
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_exceptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              kind TEXT NOT NULL,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(air_date, slot_id, kind)
            );
            """
        )
        self._migrate_slots_status()
        self.conn.commit()

    def _migrate_slots_status(self) -> None:
        """旧库的 slots CHECK 约束只允许 planned/replaced/cancelled，重建一次放开状态。"""
        constraint = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='slots'"
        ).fetchone()[0]
        if "'deferred'" in constraint:
            return
        self.conn.executescript(
            """
            CREATE TABLE slots_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              program_id INTEGER NOT NULL REFERENCES programs(id),
              region TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'planned'
                CHECK(status IN ('planned','replaced','cancelled','deferred','pending_reschedule')),
              replaced_from INTEGER REFERENCES programs(id),
              created_at TEXT NOT NULL
            );
            INSERT INTO slots_new SELECT id,air_date,start_time,duration_minutes,program_id,region,status,replaced_from,created_at FROM slots;
            DROP TABLE slots;
            ALTER TABLE slots_new RENAME TO slots;
            CREATE INDEX IF NOT EXISTS idx_slots_date_region ON slots(air_date, region);
            """
        )

    def seed_demo(self) -> None:
        existing = self.conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
        if existing:
            return
        music = self.add_program("晨间轻音乐", "music", 30, "2026-01-01", "2026-12-31", "青柠饮品", 45, ["华东"])
        news = self.add_program("城市早报", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        ad = self.add_program("青柠饮品广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠饮品", 60, ["华东"])
        self.add_sponsor_policy("青柠饮品", 90)
        self.add_blocked_window("华东", 0, "08:00", "08:30", "周一设备检修")
        self.schedule_slot("2026-09-28", "09:00", music, "华东")
        self.schedule_slot("2026-09-28", "10:00", news, "华东")
        self.schedule_slot("2026-09-28", "11:00", ad, "华东")

    def add_program(self, title: str, kind: str, duration_minutes: int, start_date: str, end_date: str,
                    sponsor: str | None = None, cooldown_minutes: int = 0,
                    regions: list[str] | None = None) -> int:
        if not title.strip():
            raise DomainError("节目名称不能为空")
        if kind not in PROGRAM_KINDS:
            raise DomainError(f"不支持的节目类型: {kind}")
        if duration_minutes <= 0:
            raise DomainError("节目时长必须大于0")
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        if end < start:
            raise DomainError("授权结束日期不能早于开始日期")
        if cooldown_minutes < 0:
            raise DomainError("冷却时间不能为负数")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO programs(title,kind,duration_minutes,start_date,end_date,sponsor,cooldown_minutes) VALUES(?,?,?,?,?,?,?)",
                (title.strip(), kind, duration_minutes, start_date, end_date, (sponsor or "").strip() or None, cooldown_minutes),
            )
            program_id = int(cur.lastrowid)
            for region in regions or []:
                self.conn.execute("INSERT INTO program_regions(program_id,region) VALUES(?,?)", (program_id, region.strip()))
        return program_id

    def authorize_region(self, program_id: int, region: str) -> None:
        if not region.strip():
            raise DomainError("地区不能为空")
        with self.transaction():
            if not self.conn.execute("SELECT 1 FROM programs WHERE id=?", (program_id,)).fetchone():
                raise DomainError("节目不存在")
            self.conn.execute("INSERT OR IGNORE INTO program_regions(program_id,region) VALUES(?,?)", (program_id, region.strip()))

    def add_sponsor_policy(self, sponsor: str, min_gap_minutes: int) -> None:
        if not sponsor.strip() or min_gap_minutes < 0:
            raise DomainError("赞助商和最小间隔必须有效")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO sponsor_policies(sponsor,min_gap_minutes) VALUES(?,?) "
                "ON CONFLICT(sponsor) DO UPDATE SET min_gap_minutes=excluded.min_gap_minutes",
                (sponsor.strip(), min_gap_minutes),
            )

    def add_blocked_window(self, region: str, weekday: int, start_time: str, end_time: str, reason: str) -> int:
        if weekday not in range(7) or _minutes(start_time) >= _minutes(end_time):
            raise DomainError("禁播时段参数无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES(?,?,?,?,?)",
                (region.strip(), weekday, start_time, end_time, reason.strip() or "禁播"),
            )
        return int(cur.lastrowid)

    def _validate_slot(self, air_date: str, start_time: str, duration: int, program_id: int,
                       region: str, ignore_slot_id: int | None = None) -> None:
        try:
            day = datetime.strptime(air_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("播出日期必须使用 YYYY-MM-DD") from exc
        try:
            _minutes(start_time)
        except ValueError as exc:
            raise DomainError("开始时间必须使用 HH:MM") from exc
        if duration <= 0:
            raise DomainError("排期时长必须大于0")
        program = self.conn.execute("SELECT * FROM programs WHERE id=? AND active=1", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在或未启用")
        if program["duration_minutes"] != duration:
            raise DomainError(f"排期时长必须等于节目时长 {program['duration_minutes']} 分钟")
        if not (program["start_date"] <= air_date <= program["end_date"]):
            raise DomainError("播出日期超出授权窗口")
        if not self.conn.execute(
            "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (program_id, region)
        ).fetchone():
            raise DomainError(f"节目未授权在{region}播出")
        end_minutes = _minutes(start_time) + duration
        blocked = self.conn.execute(
            "SELECT * FROM blocked_windows WHERE region=? AND weekday=?",
            (region, day.weekday()),
        ).fetchall()
        for window in blocked:
            if _minutes(window["start_time"]) < end_minutes and _minutes(start_time) < _minutes(window["end_time"]):
                raise DomainError(f"与禁播时段冲突: {window['reason']}")
        sql = "SELECT * FROM slots WHERE air_date=? AND region=? AND status IN ('planned','replaced')"
        params: list[object] = [air_date, region]
        if ignore_slot_id is not None:
            sql += " AND id!=?"
            params.append(ignore_slot_id)
        for existing in self.conn.execute(sql, params).fetchall():
            if _overlap(start_time, duration, existing["start_time"], existing["duration_minutes"]):
                raise DomainError(f"与排期 #{existing['id']} 时间重叠")
        if program["cooldown_minutes"]:
            previous = self.conn.execute(
                "SELECT * FROM slots WHERE air_date=? AND region=? AND program_id=? AND status IN ('planned','replaced') AND id!=? "
                "AND start_time < ? ORDER BY start_time DESC LIMIT 1",
                (air_date, region, program_id, ignore_slot_id or -1, start_time),
            ).fetchone()
            if previous:
                gap = _minutes(start_time) - (_minutes(previous["start_time"]) + previous["duration_minutes"])
                if gap < program["cooldown_minutes"]:
                    raise DomainError(f"与上一期节目间隔不足冷却时间 {program['cooldown_minutes']} 分钟")
        if program["sponsor"]:
            policy = self.conn.execute("SELECT min_gap_minutes FROM sponsor_policies WHERE sponsor=?", (program["sponsor"],)).fetchone()
            if policy:
                gap = policy["min_gap_minutes"]
                all_sponsored = self.conn.execute(
                    "SELECT s.*, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id "
                    "WHERE s.air_date=? AND s.region=? AND s.status IN ('planned','replaced') AND p.sponsor=? AND s.id!=?",
                    (air_date, region, program["sponsor"], ignore_slot_id or -1),
                ).fetchall()
                for other in all_sponsored:
                    if _overlap(start_time, duration, other["start_time"], other["duration_minutes"]):
                        raise DomainError(f"与赞助商 {program['sponsor']} 的其他节目冲突")
                    distance = abs(_minutes(start_time) - (_minutes(other["start_time"]) + other["duration_minutes"]))
                    if distance < gap:
                        raise DomainError(f"与赞助商 {program['sponsor']} 的节目间隔不足 {gap} 分钟")

    def schedule_slot(self, air_date: str, start_time: str, program_id: int, region: str) -> int:
        program = self.conn.execute("SELECT duration_minutes FROM programs WHERE id=?", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在")
        with self.transaction():
            self._validate_slot(air_date, start_time, int(program["duration_minutes"]), program_id, region)
            cur = self.conn.execute(
                "INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,created_at) VALUES(?,?,?,?,?,?)",
                (air_date, start_time, int(program["duration_minutes"]), program_id, region, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def replace_slot(self, slot_id: int, new_program_id: int) -> dict:
        """Replace a planned item and revalidate the resulting plan atomically."""
        with self.transaction():
            slot = self.conn.execute("SELECT * FROM slots WHERE id=? AND status='planned'", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("只能替换尚未播出且状态为 planned 的排期")
            program = self.conn.execute("SELECT * FROM programs WHERE id=?", (new_program_id,)).fetchone()
            if not program:
                raise DomainError("替换节目不存在")
            self._validate_slot(slot["air_date"], slot["start_time"], int(program["duration_minutes"]), new_program_id, slot["region"], slot_id)
            self.conn.execute(
                "UPDATE slots SET program_id=?, duration_minutes=?, replaced_from=?, status='replaced' WHERE id=?",
                (new_program_id, int(program["duration_minutes"]), slot["program_id"], slot_id),
            )
        return self.get_slot(slot_id)

    def _validate_insert_window(self, air_date: str, start_time: str, end_time: str, region: str) -> None:
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("播出日期必须使用 YYYY-MM-DD") from exc
        if not region.strip():
            raise DomainError("地区不能为空")
        try:
            start, end = _minutes(start_time), _minutes(end_time)
        except ValueError as exc:
            raise DomainError("插播时间必须使用 HH:MM") from exc
        if end <= start:
            raise DomainError("插播结束时刻必须晚于开始时刻")

    def _assert_insert_active(self, insert_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM breaking_inserts WHERE id=?", (insert_id,)).fetchone()
        if not row:
            raise DomainError("插播单不存在")
        if row["status"] != "active":
            raise DomainError("插播单已结束，不能再次操作")
        return dict(row)

    def register_insert(self, air_date: str, start_time: str, end_time: str,
                        region: str, title: str = "") -> dict:
        """登记一张插播占位单：覆盖到的未播节目顺延，已播排期留在当天。"""
        self._validate_insert_window(air_date, start_time, end_time, region)
        region = region.strip()
        now = datetime.now().isoformat()
        with self.transaction():
            overlap = self.conn.execute(
                "SELECT * FROM breaking_inserts WHERE air_date=? AND region=? AND status='active' "
                "AND start_time < ? AND end_time > ?",
                (air_date, region, end_time, start_time),
            ).fetchone()
            if overlap:
                raise DomainError(f"与插播单 #{overlap['id']} 时间重叠，不能重复占位")
            cur = self.conn.execute(
                "INSERT INTO breaking_inserts(air_date,region,start_time,end_time,title,status,created_at) "
                "VALUES(?,?,?,?,?,'active',?)",
                (air_date, region, start_time, end_time, title.strip(), now),
            )
            insert_id = int(cur.lastrowid)
            slots = [dict(r) for r in self.conn.execute(
                "SELECT * FROM slots WHERE air_date=? AND region=?", (air_date, region)
            ).fetchall()]
            played = {r[0] for r in self.conn.execute(
                "SELECT DISTINCT slot_id FROM playout_logs pl JOIN slots s ON s.id=pl.slot_id "
                "WHERE s.air_date=? AND s.region=?",
                (air_date, region),
            ).fetchall()}
            covered, aired = breaking.classify_covered(
                slots, region=region, air_date=air_date,
                start_time=start_time, end_time=end_time, played_slot_ids=played,
            )
            for slot in covered:
                self.conn.execute(
                    "INSERT INTO slot_deferrals(insert_id,slot_id,air_date,start_time,duration_minutes,"
                    "program_id,region,original_status,outcome) VALUES(?,?,?,?,?,?,?,?,'deferred')",
                    (insert_id, slot["id"], slot["air_date"], slot["start_time"], slot["duration_minutes"],
                     slot["program_id"], slot["region"], slot["status"]),
                )
                self.conn.execute("UPDATE slots SET status='deferred' WHERE id=?", (slot["id"],))
        insert = self.get_insert(insert_id)
        insert["aired_untouched"] = aired
        return insert

    def withdraw_insert(self, insert_id: int) -> dict:
        """撤回插播：受影响节目在原时段复核版权/禁播/赞助间隔后恢复。"""
        with self.transaction():
            self._assert_insert_active(insert_id)
            self.conn.execute(
                "UPDATE breaking_inserts SET status='withdrawn', actual_end_time=? WHERE id=?",
                (datetime.now().strftime("%H:%M"), insert_id),
            )
            results = self._restore_deferrals(insert_id)
        return self.get_insert(insert_id)

    def end_insert(self, insert_id: int, actual_end_time: str) -> dict:
        """结束插播：仅在提前结束时复核并恢复受影响节目；正常结束则保留在顺延区。"""
        try:
            actual = _minutes(actual_end_time)
        except ValueError as exc:
            raise DomainError("结束时间必须使用 HH:MM") from exc
        with self.transaction():
            insert = self._assert_insert_active(insert_id)
            if actual > _minutes(insert["end_time"]):
                raise DomainError("结束时间晚于计划结束时刻，请走顺延/重排流程")
            self.conn.execute(
                "UPDATE breaking_inserts SET status='ended', actual_end_time=? WHERE id=?",
                (actual_end_time, insert_id),
            )
            results: list[dict] = []
            if actual < _minutes(insert["end_time"]):
                results = self._restore_deferrals(insert_id, end_time=actual_end_time)
        return self.get_insert(insert_id)

    def _restore_deferrals(self, insert_id: int, end_time: str | None = None) -> list[dict]:
        """把某张插播单的受影响节目按原时段逐个复核，恢复或留在待重排。"""
        sql = ("SELECT d.* FROM slot_deferrals d WHERE d.insert_id=? AND d.outcome='deferred' "
               "ORDER BY d.start_time, d.slot_id")
        deferrals = [dict(r) for r in self.conn.execute(sql, (insert_id,)).fetchall()]
        if end_time is not None:
            deferrals = [d for d in deferrals if _minutes(d["start_time"]) >= _minutes(end_time)]
        air_date = self.conn.execute(
            "SELECT air_date FROM breaking_inserts WHERE id=?", (insert_id,)
        ).fetchone()["air_date"]
        weekday = datetime.strptime(air_date, "%Y-%m-%d").date().weekday()
        results: list[dict] = []
        for deferral in deferrals:
            slot = {k: deferral[k] for k in ("id", "air_date", "start_time", "duration_minutes", "region")}
            slot["id"] = deferral["slot_id"]
            program_row = self.conn.execute(
                "SELECT * FROM programs WHERE id=?", (deferral["program_id"],)
            ).fetchone()
            program = dict(program_row) if program_row else None
            authorized = bool(self.conn.execute(
                "SELECT 1 FROM program_regions WHERE program_id=? AND region=?",
                (deferral["program_id"], deferral["region"]),
            ).fetchone())
            windows = [dict(r) for r in self.conn.execute(
                "SELECT * FROM blocked_windows WHERE region=? AND weekday=?",
                (deferral["region"], weekday),
            ).fetchall()]
            gap_minutes = None
            neighbors: list[dict] = []
            if program and program["sponsor"]:
                policy = self.conn.execute(
                    "SELECT min_gap_minutes FROM sponsor_policies WHERE sponsor=?",
                    (program["sponsor"],),
                ).fetchone()
                if policy:
                    gap_minutes = int(policy["min_gap_minutes"])
                    # 邻居 = 当前节目单上的排期 + 当天已有实播记录的排期（听众确实听到了）
                    rows = self.conn.execute(
                        "SELECT s.id,s.start_time,s.duration_minutes FROM slots s "
                        "JOIN programs p ON p.id=s.program_id "
                        "WHERE s.air_date=? AND s.region=? AND p.sponsor=? AND ("
                        "s.status IN ('planned','replaced') OR EXISTS ("
                        "SELECT 1 FROM playout_logs pl WHERE pl.slot_id=s.id))",
                        (air_date, deferral["region"], program["sponsor"]),
                    ).fetchall()
                    neighbors = [dict(r) for r in rows]
            review = breaking.review_restore(
                slot, program=program, authorized=authorized, windows=windows,
                sponsor_gap_minutes=gap_minutes, sponsored_neighbors=neighbors,
            )
            now = datetime.now().isoformat()
            if review.ok:
                self.conn.execute(
                    "UPDATE slots SET status=? WHERE id=?", (deferral["original_status"], deferral["slot_id"]),
                )
                self.conn.execute(
                    "UPDATE slot_deferrals SET outcome='restored', reasons='', reviewed_at=? WHERE id=?",
                    (now, deferral["id"]),
                )
            else:
                self.conn.execute(
                    "UPDATE slots SET status='pending_reschedule' WHERE id=?", (deferral["slot_id"],)
                )
                self.conn.execute(
                    "UPDATE slot_deferrals SET outcome='pending_reschedule', reasons=?, reviewed_at=? WHERE id=?",
                    ("；".join(review.reasons), now, deferral["id"]),
                )
            results.append({"slot_id": deferral["slot_id"], "ok": review.ok, "reasons": review.reasons})
        return results

    def get_insert(self, insert_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM breaking_inserts WHERE id=?", (insert_id,)).fetchone()
        if not row:
            raise DomainError("插播单不存在")
        insert = dict(row)
        insert["deferrals"] = [dict(r) for r in self.conn.execute(
            "SELECT d.*, p.title, p.kind, p.sponsor FROM slot_deferrals d "
            "JOIN programs p ON p.id=d.program_id WHERE d.insert_id=? ORDER BY d.start_time,d.slot_id",
            (insert_id,),
        ).fetchall()]
        return insert

    def list_inserts(self) -> list[dict]:
        return [self.get_insert(r["id"]) for r in self.conn.execute(
            "SELECT id FROM breaking_inserts ORDER BY id DESC"
        ).fetchall()]

    def get_slot(self, slot_id: int) -> dict:
        row = self.conn.execute(
            "SELECT s.*, p.title, p.kind, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id WHERE s.id=?",
            (slot_id,),
        ).fetchone()
        if not row:
            raise DomainError("排期不存在")
        return dict(row)

    def record_playout(self, slot_id: int, actual_start: str, actual_duration_minutes: int,
                       actual_program_id: int | None = None, note: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM slots WHERE id=?", (slot_id,)).fetchone():
            raise DomainError("排期不存在")
        if actual_duration_minutes < 0:
            raise DomainError("实际时长不能为负数")
        _minutes(actual_start)
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,actual_program_id,note,created_at) VALUES(?,?,?,?,?,?)",
                (slot_id, actual_start, actual_duration_minutes, actual_program_id, note, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def reconcile_date(self, air_date: str) -> list[dict]:
        """Compare the latest playout per slot with the plan and persist exceptions."""
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        with self.transaction():
            self.conn.execute("DELETE FROM reconciliation_exceptions WHERE air_date=?", (air_date,))
            slots = self.conn.execute(
                "SELECT s.*, p.title, p.sponsor, p.kind FROM slots s JOIN programs p ON p.id=s.program_id "
                "WHERE s.air_date=? AND s.status IN ('planned','replaced') ORDER BY s.start_time", (air_date,)
            ).fetchall()
            exceptions: list[tuple[int, str, str]] = []
            for slot in slots:
                log = self.conn.execute(
                    "SELECT * FROM playout_logs WHERE slot_id=? ORDER BY id DESC LIMIT 1", (slot["id"],)
                ).fetchone()
                if not log:
                    exceptions.append((slot["id"], "missed", "没有实播记录"))
                    continue
                actual_program_id = log["actual_program_id"] or slot["program_id"]
                if actual_program_id != slot["program_id"]:
                    exceptions.append((slot["id"], "wrong_program", f"计划节目 #{slot['program_id']}，实播节目 #{actual_program_id}"))
                delta = log["actual_duration_minutes"] - slot["duration_minutes"]
                if abs(delta) > 30:
                    kind = "overrun" if delta > 0 else "underrun"
                    exceptions.append((slot["id"], kind, f"与计划相差 {delta:+d} 分钟"))
                actual = self.conn.execute(
                    "SELECT p.* FROM programs p WHERE p.id=?", (actual_program_id,)
                ).fetchone()
                if actual:
                    region_ok = self.conn.execute(
                        "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (actual_program_id, slot["region"])
                    ).fetchone()
                    if not region_ok or not (actual["start_date"] <= air_date <= actual["end_date"]):
                        exceptions.append((slot["id"], "out_of_license", "实播节目超出地区或日期授权"))
            for slot_id, kind, detail in exceptions:
                self.conn.execute(
                    "INSERT INTO reconciliation_exceptions(air_date,slot_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                    (air_date, slot_id, kind, detail, datetime.now().isoformat()),
                )
        return self.get_exceptions(air_date)

    def get_exceptions(self, air_date: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions WHERE air_date=? ORDER BY slot_id, kind", (air_date,)
        ).fetchall()]

    def snapshot(self) -> dict:
        programs = [dict(row) for row in self.conn.execute("SELECT * FROM programs ORDER BY id").fetchall()]
        slots = [dict(row) for row in self.conn.execute(
            "SELECT s.*, p.title, p.kind FROM slots s JOIN programs p ON p.id=s.program_id ORDER BY s.air_date,s.start_time"
        ).fetchall()]
        return {"programs": programs, "slots": slots, "inserts": self.list_inserts(), "exceptions": [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions ORDER BY id DESC LIMIT 50"
        ).fetchall()]}
