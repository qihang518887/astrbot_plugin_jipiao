"""Pure price decisions, using the same UTC+8 clock on every host."""

import json
from datetime import datetime, timedelta, timezone

CHINA_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
DAY = 86400


def flight_timestamp(flight_date: str) -> int:
    return int(datetime.strptime(flight_date, "%Y-%m-%d").replace(tzinfo=CHINA_TZ).timestamp())


def task_phase(task: dict, now_ts: int) -> tuple[str, int | None]:
    """Return the phase and observation deadline shared by commands and decisions."""
    flight_ts = flight_timestamp(task["flight_date"])
    if now_ts >= flight_ts:
        return "已到期", None
    if task["target_price"] is not None:
        return "目标价监控", None
    start_60, end_7 = flight_ts - 60 * DAY, flight_ts - 7 * DAY
    if now_ts < start_60:
        return "等待期（起飞前60天开始观察）", None
    if now_ts >= end_7:
        return "起飞前7天兜底期", None
    start = max(task["created_at"], start_60)
    deadline = start + max(int((end_7 - start) * 0.37), 43200)
    return ("观察期" if now_ts <= deadline else "决策期"), deadline


def evaluate_flight(task: dict, best_flight: dict | None, now_ts: int) -> dict:
    """Evaluate the supplied quote without I/O or changes to the task.

    Args:
        task: Persisted monitoring settings and observation state.
        best_flight: Cheapest matching flight, or None when no quote is available.
        now_ts: Current Unix timestamp in seconds.

    Returns:
        Notification decision, proposed state updates, and expiry flag.
    """
    result = {
        "should_notify": False,
        "notify_reason": "",
        "new_lowest_3_json": None,
        "new_pushed_price": None,
        "is_expired": False,
    }
    phase, deadline = task_phase(task, now_ts)
    if phase == "已到期":
        result["is_expired"] = True
        return result
    if best_flight is None:
        return result
    price = best_flight["price"]
    reason = ""
    if task["target_price"] is not None:
        if price <= task["target_price"]:
            reason = f"已达到或低于您的目标价 ¥{task['target_price']}"
    elif phase.startswith("等待期"):
        return result
    elif phase == "起飞前7天兜底期":
        reason = "已进入起飞前7天临期阶段，当前符合条件的最低价"
    else:
        lowest = json.loads(task["lowest_3_json"])
        if phase == "观察期":
            if not any(item["p"] == price and now_ts - item["t"] < 21600 for item in lowest):
                lowest.append({"p": price, "t": now_ts})
                lowest.sort(key=lambda item: item["p"])
                result["new_lowest_3_json"] = json.dumps(lowest[:3])
            return result
        if not lowest:
            return result
        threshold = max(item["p"] for item in lowest)
        if price <= threshold:
            reason = f"触发37%观察策略提醒（观察期低谷基准价：¥{threshold}）"
            minimum = min(item["p"] for item in lowest)
            if price < minimum:
                reason += "，刷新观察期最低价"
            elif price == minimum:
                reason += "，追平观察期最低价"
    last = task["last_pushed_price"]
    if reason and (last is None or price < last):
        result.update(should_notify=True, notify_reason=reason, new_pushed_price=price)
    return result
