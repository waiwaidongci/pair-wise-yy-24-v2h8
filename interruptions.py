"""新闻插播的纯判定逻辑。

这一层只做判断，不连接数据库、不读写任何状态：哪些排期被插播覆盖、恢复时
原时段上的节目是否仍然合法，全部以纯函数形式给出，便于单独测试。存档由
RadioDB 负责，接口与页面只负责传入数据和展示结论。
"""

from __future__ import annotations

# 排期当前处于当天的正式排播单，插播不应移动它们
LIVE_STATUSES = ("planned", "replaced")
# 排期被某次插播挪走，原时段保留在记录里等待恢复
DEFERRED_STATUS = "deferred"
# 恢复复核未通过，需要人工重新安排
PENDING_STATUS = "pending_reschedule"


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """两个半开区间是否重叠（端点相接不算冲突）。"""
    return a_start < b_end and b_start < a_end


def covered_slots(slots: list[dict], bulletin_start: int, bulletin_end: int) -> list[dict]:
    """挑出被插播覆盖的未播节目：状态在排播单上且没有任何实播记录。

    已经播过（有 playout_logs）的排期留在当天，不进入顺延区。
    """
    selected = []
    for slot in slots:
        if slot["status"] not in LIVE_STATUSES or slot.get("has_played"):
            continue
        start = slot["start_minutes"]
        if overlaps(bulletin_start, bulletin_end, start, start + slot["duration_minutes"]):
            selected.append(slot)
    return selected


def review_slot(slot: dict, program, regions: list[str], blocked_windows: list[dict],
                live_slots: list[dict], sponsored_slots: list[dict],
                sponsor_gap: int | None) -> list[str]:
    """恢复前在原时段复核一条受影响节目，返回冲突原因列表（空列表表示可恢复）。

    只复核版权（日期窗口 + 地区授权）、禁播时段和赞助商间隔；另外检查原时段
    是否已经被别的正式排期占用。冷却时间是对同一节目的滚动间隔约束，不属于
    本次恢复复核的项目。
    """
    reasons: list[str] = []
    start = slot["start_minutes"]
    end = start + slot["duration_minutes"]
    air_date = slot["air_date"]

    if program is None or not program["active"]:
        return ["节目不存在或已停用"]
    if not (program["start_date"] <= air_date <= program["end_date"]):
        reasons.append("播出日期超出授权窗口")
    if slot["region"] not in regions:
        reasons.append(f"节目未授权在{slot['region']}播出")

    for window in blocked_windows:
        if window["weekday"] != slot["weekday"]:
            continue
        if overlaps(start, end, window["start_minutes"], window["end_minutes"]):
            reasons.append(f"与禁播时段冲突: {window['reason']}")

    sponsor = program["sponsor"]
    for other in live_slots:
        if other["id"] == slot["id"]:
            continue
        other_start = other["start_minutes"]
        other_end = other_start + other["duration_minutes"]
        if not overlaps(start, end, other_start, other_end):
            continue
        same_sponsor = sponsor and other["sponsor"] == sponsor
        label = f"赞助商 {sponsor} 的" if same_sponsor else ""
        reasons.append(f"原时段已被{label}排期 #{other['id']} 占用")

    if sponsor and sponsor_gap is not None:
        for other in sponsored_slots:
            if other["id"] == slot["id"] or other["sponsor"] != sponsor:
                continue
            other_start = other["start_minutes"]
            other_end = other_start + other["duration_minutes"]
            if overlaps(start, end, other_start, other_end):
                continue  # 占用冲突上面已经报过
            if other_end <= start:
                distance = start - other_end
            elif end <= other_start:
                distance = other_start - end
            else:
                continue
            if distance < sponsor_gap:
                reasons.append(f"与赞助商 {sponsor} 的节目间隔不足 {sponsor_gap} 分钟")
    return reasons
