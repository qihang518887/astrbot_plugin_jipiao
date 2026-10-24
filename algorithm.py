"""Pure price decisions, using the same UTC+8 clock on every host."""

import json
from datetime import datetime, timedelta, timezone

CHINA_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
DAY = 86400


def flight_timestamp(flight_date: str) -> int:
    return int(datetime.strptime(flight_date, "%Y-%m-%d").replace(tzinfo=CHINA_TZ).timestamp())


def normalize_lowest_3(records: list[dict]) -> list[dict]:
    """Return up to three distinct lowest prices with their first recorded times."""
    by_price = {}
    for item in sorted(records, key=lambda item: item["t"]):
        by_price.setdefault(item["p"], item)
    return [by_price[price] for price in sorted(by_price)[:3]]


def decision_threshold(records: list[dict]) -> int | None:
    """Use the second distinct low, or the sole price when only one was observed."""
    lowest = normalize_lowest_3(records)
    return lowest[min(1, len(lowest) - 1)]["p"] if lowest else None


def observation_window(task: dict) -> tuple[int, int]:
    flight_ts = flight_timestamp(task["flight_date"])
    start = max(task["created_at"], flight_ts - 60 * DAY)
    deadline = start + max(int((flight_ts - 7 * DAY - start) * 0.37), 43200)
    return start, deadline


def observation_finished(task: dict, now_ts: int) -> bool:
    """A real observation window ended, including interruption by the seven-day fallback."""
    flight_ts = flight_timestamp(task["flight_date"])
    if task["target_price"] is not None or now_ts >= flight_ts:
        return False
    start, deadline = observation_window(task)
    end_7 = flight_ts - 7 * DAY
    return start < end_7 and (now_ts > deadline or now_ts >= end_7)


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
    _, deadline = observation_window(task)
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
        recorded = json.loads(task["lowest_3_json"])
        lowest = normalize_lowest_3(recorded)
        if phase == "观察期":
            if not any(item["p"] == price for item in lowest):
                lowest = normalize_lowest_3([*lowest, {"p": price, "t": now_ts}])
        if lowest != recorded:
            result["new_lowest_3_json"] = json.dumps(lowest)
        if phase == "观察期":
            return result
        if not lowest:
            return result
        threshold = decision_threshold(lowest)
        if price < threshold:
            label = "第二低价" if len(lowest) >= 2 else "唯一已记录价格"
            reason = f"触发37%观察策略提醒（低于观察期{label}：¥{threshold}）"
            minimum = min(item["p"] for item in lowest)
            if price < minimum:
                reason += "，刷新观察期最低价"
            elif price == minimum:
                reason += "，追平观察期最低价"
    last = task["last_pushed_price"]
    # Decision mode accepts a changed qualifying quote even after a cheaper alert.
    # Target-price and seven-day fallback modes retain further-drop-only alerts.
    fresh_price = price != last if phase == "决策期" else last is None or price < last
    if reason and fresh_price:
        result.update(should_notify=True, notify_reason=reason, new_pushed_price=price)
    return result
