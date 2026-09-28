"""新闻插播的纯判定逻辑（不读写数据库、不碰 HTTP）。

判定层只接收普通字典数据，返回“覆盖了谁”“能否在原时段恢复”的结论；
状态迁移和存档由 database.py 负责，页面只展示结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# 仍在当天节目单上的排期状态；顺延中/待重排/已取消都不占节目单。
ACTIVE_SLOT_STATUSES = ("planned", "replaced")
DEFERRED_STATUS = "deferred"
PENDING_RESCHEDULE_STATUS = "pending_reschedule"


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _overlaps(start_a: int, duration_a: int, start_b: int, duration_b: int) -> bool:
    return start_a < start_b + duration_b and start_b < start_a + duration_a


def classify_covered(slots, *, region, air_date, start_time, end_time, played_slot_ids):
    """按地区和起止时刻把排期分成“顺延覆盖”和“已播不动”两组。

    - 仅处理同日期、同地区、仍在节目单上（planned/replaced）的排期；
    - 时间与插播窗口重叠且没有实播记录的未播节目，转入顺延区；
    - 已有实播记录的排期留在当天，不移动。
    """
    insert_start = _minutes(start_time)
    insert_end = _minutes(end_time)
    covered: list[dict] = []
    aired_untouched: list[dict] = []
    for slot in slots:
        if slot.get("air_date") != air_date or slot.get("region") != region:
            continue
        if slot.get("status") not in ACTIVE_SLOT_STATUSES:
            continue
        slot_start = _minutes(slot["start_time"])
        if not _overlaps(insert_start, insert_end - insert_start,
                         slot_start, int(slot["duration_minutes"])):
            continue
        if slot["id"] in played_slot_ids:
            aired_untouched.append(slot)
        else:
            covered.append(slot)
    covered.sort(key=lambda s: (_minutes(s["start_time"]), s["id"]))
    aired_untouched.sort(key=lambda s: (_minutes(s["start_time"]), s["id"]))
    return covered, aired_untouched


@dataclass
class RestoreReview:
    """受影响节目在原时段的复核结论。"""

    slot_id: int
    ok: bool
    reasons: list[str] = field(default_factory=list)


def review_restore(slot, *, program, authorized, windows,
                   sponsor_gap_minutes, sponsored_neighbors):
    """恢复前只复核三件事：版权、禁播、赞助间隔（在原时段复核）。

    slot 使用顺延存档里的原时段；program/windows/policy 全部取当前最新数据，
    因为插播期间授权、禁播或节目单可能发生变化。
    """
    reasons: list[str] = []
    region = slot["region"]
    air_date = slot["air_date"]
    start = _minutes(slot["start_time"])
    end = start + int(slot["duration_minutes"])

    # 1. 版权：节目有效、日期仍在授权窗口、地区仍授权。
    if program is None:
        reasons.append("节目不存在")
    else:
        if not program.get("active", 1):
            reasons.append("节目已停用")
        if not (program["start_date"] <= air_date <= program["end_date"]):
            reasons.append("播出日期超出授权窗口")
    if not authorized:
        reasons.append(f"节目未授权在{region}播出")

    # 2. 禁播时段：原时段不能落入当前的禁播窗口。
    for window in windows:
        win_start = _minutes(window["start_time"])
        win_end = _minutes(window["end_time"])
        if win_start < end and start < win_end:
            reasons.append(f"与禁播时段冲突: {window['reason']}")

    # 3. 赞助间隔：与当前节目单上同赞助商的其他节目保持政策间隔。
    sponsor = program.get("sponsor") if program else None
    if sponsor and sponsor_gap_minutes is not None:
        for neighbor in sponsored_neighbors:
            if neighbor["id"] == slot["id"]:
                continue
            other_start = _minutes(neighbor["start_time"])
            other_end = other_start + int(neighbor["duration_minutes"])
            if start < other_end and other_start < end:
                reasons.append(f"与赞助商 {sponsor} 的其他节目冲突")
                continue
            distance = max(start - other_end, other_start - end)
            if distance < sponsor_gap_minutes:
                reasons.append(
                    f"与赞助商 {sponsor} 的节目间隔不足 {sponsor_gap_minutes} 分钟"
                )

    return RestoreReview(slot_id=slot["id"], ok=not reasons, reasons=reasons)
