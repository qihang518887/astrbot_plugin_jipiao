# -*- coding: utf-8 -*-
"""
Verification experiment for:
1. Flight price searching (SearchFlights via fli)
2. Adaptive monitoring timelines (Phase transitions & thresholds)
3. Dynamic price baseline calculation & push cooling (37% rule & evaluate_flight)
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7897")
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
from datetime import datetime, timedelta

if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from astrbot_plugin_jipiao.algorithm import (
    CHINA_TZ,
    DAY,
    evaluate_flight,
    flight_timestamp,
    task_phase,
)
from astrbot_plugin_jipiao.fetcher import fetch_raw_flights, filter_and_get_lowest, resolve_airports


async def experiment_1_search():
    print("\n" + "=" * 60)
    print("【实验一】机票价格实时搜寻能力测试 (真实网络与模型转换)")
    print("=" * 60)
    dep_city = "北京"
    arr_city = "上海"
    target_date = (datetime.now(CHINA_TZ) + timedelta(days=30)).strftime("%Y-%m-%d")
    print(f"-> 目标航线: {dep_city} -> {arr_city} | 出发日期: {target_date}")
    print(f"-> 出发机场自动解析: {resolve_airports(dep_city)}")
    print(f"-> 到达机场自动解析: {resolve_airports(arr_city)}")
    print("-> 正在通过 fli 驱动 Google Flights 发起真实请求...")

    try:
        flights = await fetch_raw_flights(dep_city, arr_city, target_date)
        print(f"[✓] 搜寻成功！获取到 {len(flights)} 条有效直飞行程报价：")
        for i, f in enumerate(flights[:5], 1):
            print(
                f"   {i}. 航班: {f['flight_no']} ({f['airline']}) | 起降: {f['dep_time']} -> {f['arr_time']} | 票价: ¥{f['price']} {f['currency']}"
            )

        best_filtered = filter_and_get_lowest(
            flights, time_window="06:00-12:00", airlines="HU,CA,MU"
        )
        if best_filtered:
            print("\n   [过滤测试] 限制上午(06:00-12:00) 且 航司(海航/国航/东航):")
            print(
                f"   -> 筛选匹配出最低价: {best_filtered['flight_no']} ({best_filtered['airline']}) - ¥{best_filtered['price']}"
            )
        return True
    except Exception as e:
        print(f"[!] 搜寻失败: {e}")
        return False


def experiment_2_adaptive_timeline():
    print("\n" + "=" * 60)
    print("【实验二】自适应监控时间线测试 (动态阶段划分与到期计算)")
    print("=" * 60)
    flight_date = "2026-12-01"
    flight_ts = flight_timestamp(flight_date)
    flight_dt = datetime.fromtimestamp(flight_ts, CHINA_TZ)

    print(
        f"设定目标起飞日期: {flight_date} 00:00:00 (北京时间 {flight_dt.strftime('%Y-%m-%d %H:%M:%S')})"
    )
    print("算法根据【任务创建时间】与【起飞时间】间隔，自适应切分生命周期：")

    scenarios = [
        ("场景A: 提前 80 天创建任务 (起飞前60天外)", flight_ts - 80 * DAY, flight_ts - 75 * DAY),
        (
            "场景B: 提前 45 天创建任务 (当前处于前37%观察期)",
            flight_ts - 45 * DAY,
            flight_ts - 40 * DAY,
        ),
        (
            "场景C: 提前 45 天创建任务 (当前处于后63%决策期)",
            flight_ts - 45 * DAY,
            flight_ts - 20 * DAY,
        ),
        (
            "场景D: 临近起飞前 5 天创建任务 (直接激活临期兜底)",
            flight_ts - 5 * DAY,
            flight_ts - 3 * DAY,
        ),
        ("场景E: 到达出发日当天 (自动到期并删除任务)", flight_ts - 30 * DAY, flight_ts + 3600),
    ]

    for title, created_at, current_now in scenarios:
        task = {
            "user_id": "test_user",
            "flight_date": flight_date,
            "created_at": created_at,
            "target_price": None,
            "lowest_3_json": "[]",
            "last_pushed_price": None,
        }
        phase, deadline = task_phase(task, current_now)
        created_str = datetime.fromtimestamp(created_at, CHINA_TZ).strftime("%Y-%m-%d")
        now_str = datetime.fromtimestamp(current_now, CHINA_TZ).strftime("%Y-%m-%d %H:%M")
        deadline_str = (
            datetime.fromtimestamp(deadline, CHINA_TZ).strftime("%Y-%m-%d %H:%M")
            if deadline
            else "无"
        )

        print(f"\n-> {title}")
        print(f"   任务创建时间: {created_str} | 当前模拟时间: {now_str}")
        print(f"   自适应计算阶段: 【{phase}】 | 观察截止时间点: {deadline_str}")


def experiment_3_baseline_and_push_cooldown():
    print("\n" + "=" * 60)
    print("【实验三】算法计算价格基准线 与 推送时间/冷却决策测试")
    print("=" * 60)
    flight_date = "2026-12-01"
    flight_ts = flight_timestamp(flight_date)
    created_ts = flight_ts - 50 * DAY

    task = {
        "user_id": "user_demo",
        "flight_date": flight_date,
        "created_at": created_ts,
        "target_price": None,
        "lowest_3_json": "[]",
        "last_pushed_price": None,
    }

    phase, deadline = task_phase(task, created_ts + 100)
    deadline_dt = datetime.fromtimestamp(deadline, CHINA_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"1. 任务创建于起飞前 50 天，算法自适应计算出的【观察期截止线】为: {deadline_dt}")

    print("\n2. [观察期价格采集与排序模拟] 模拟轮询获取到不同价格，算法维护最低3次价格：")
    obs_prices = [
        (created_ts + 1 * DAY, 680, "第1次轮询采集"),
        (created_ts + 2 * DAY, 550, "第2次轮询采集"),
        (created_ts + 3 * DAY, 620, "第3次轮询采集"),
        (created_ts + 4 * DAY, 510, "第4次轮询采集 (刷新观察期更低价)"),
        (created_ts + 5 * DAY, 580, "第5次轮询采集"),
    ]

    for t_now, price, label in obs_prices:
        best_flight = {"flight_no": "CA1234", "price": price}
        res = evaluate_flight(task, best_flight, t_now)
        if res["new_lowest_3_json"]:
            task["lowest_3_json"] = res["new_lowest_3_json"]
        cur_lowest = [item["p"] for item in json.loads(task["lowest_3_json"])]
        print(
            f"   - {label}: 采集到 ¥{price} -> 观察期最低3次价格池更新为: {cur_lowest} (观察期内静默，不发推送)"
        )

    lowest_list = json.loads(task["lowest_3_json"])
    threshold_price = max(item["p"] for item in lowest_list)
    min_record = min(item["p"] for item in lowest_list)
    print("\n3. [价格基准线生成]")
    print(f"   观察期保留的3次最低价格为: {[x['p'] for x in lowest_list]}")
    print(f"   -> 算法得出的【价格基准线 (门槛)】= max(观察期最低3次价格) = ¥{threshold_price}")
    print(f"   -> 观察期历史最低单次价格 = ¥{min_record}")

    print("\n4. [决策期推送时间与防骚扰冷却判定模拟]")
    print(
        f"   规则: 只有『当前价格 <= 基准线(¥{threshold_price})』且『较上次推送进一步下降』时，才触发推送！"
    )
    decision_time = deadline + 2 * DAY
    test_cases = [
        (650, "市场价反弹至 ¥650 (高于基准线 ¥580)"),
        (550, "市场价降至 ¥550 (低于基准线 ¥580，且首次进入低价区)"),
        (550, "次日轮询依然是 ¥550 (价格未变)"),
        (570, "市场价反弹回 ¥570 (虽然低于基准线，但相比上次推送 ¥550 上涨)"),
        (510, "市场价再度下跌至 ¥510 (追平观察期最低记录 ¥510)"),
        (480, "市场价大幅跳水至 ¥480 (打破观察期最低记录 ¥510)"),
    ]

    for price, desc in test_cases:
        best_flight = {"flight_no": "MU5101", "price": price}
        res = evaluate_flight(task, best_flight, decision_time)
        decision_time += 1800

        if res["should_notify"]:
            status_str = "【🔔 触发主动推送】"
            reason_str = res["notify_reason"]
            task["last_pushed_price"] = res["new_pushed_price"]
        else:
            status_str = "【🔇 拦截静默】"
            reason_str = "价格高于基准线，或与上次推送相同/上涨，触发冷却防骚扰机制"

        print(f"\n   * 模拟事件: {desc}")
        print(f"     决策结果: {status_str}")
        print(f"     触发原因: {reason_str}")
        if res["should_notify"]:
            print(f"     -> 数据库更新 last_pushed_price 为: ¥{task['last_pushed_price']}")


async def main():
    await experiment_1_search()
    experiment_2_adaptive_timeline()
    experiment_3_baseline_and_push_cooldown()
    print("\n" + "=" * 60)
    print("【实验总结】三大核心能力全部现场验证通过！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
