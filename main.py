"""AstrBot commands and lifecycle for the flight monitoring plugin."""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .algorithm import CHINA_TZ, task_phase
from .database import FlightDB
from .fetcher import (
    describe_itinerary,
    describe_scope,
    fetch_mock_flights,
    fetch_raw_flights,
    filter_and_get_lowest,
    normalize_airlines,
    resolve_airports,
    validate_time_window,
)
from .scheduler import MonitorScheduler, NotificationNotReady

USAGE = (
    "用法：/机票监控 出发地 到达地 YYYY-MM-DD [时间段] [航司] [目标价] [直飞|接受经停|接受中转|接受经停与中转]\n"
    "不限制的可选项用 - 占位；日期必须晚于北京时间今天。\n"
    "示例：/机票监控 北京 上海 2026-12-01 08:00-18:00 国航,东航 500\n"
    "只设目标价：/机票监控 PEK SHA 2026-12-01 - - 500\n"
    "不设目标价将使用37%观察策略。默认只监控直飞；末尾填写 接受经停、接受中转 或 接受经停与中转 才纳入非直飞行程。"
)


def validate_settings(
    dep_city,
    arr_city,
    flight_date,
    time_window="-",
    airlines="-",
    target_price="-",
    stop_policy="直飞",
):
    """Validate command input before replacing any existing task.

    Args:
        dep_city: Departure city or airport code.
        arr_city: Arrival city or airport code.
        flight_date: Departure date strictly formatted as YYYY-MM-DD.
        time_window: Inclusive local departure interval, or '-'.
        airlines: Comma-separated airline aliases or codes, or '-'.
        target_price: Positive whole-yuan threshold, or '-'.
        stop_policy: '直飞' by default, or '接受经停', '接受中转', '接受经停与中转'.

    Returns:
        Normalized database keyword arguments.

    Raises:
        ValueError: Input cannot be mapped to a supported search.
    """
    if not dep_city or not arr_city or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", flight_date):
        raise ValueError("请填写出发地、到达地和 YYYY-MM-DD 格式的日期。")
    try:
        departure = datetime.strptime(flight_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("日期不存在，请检查年月日。") from exc
    if departure <= datetime.now(CHINA_TZ).date():
        raise ValueError("出发日期必须晚于北京时间今天；出发日零点停止监控。")
    dep_codes = resolve_airports(dep_city)
    arr_codes = resolve_airports(arr_city)
    if set(dep_codes) & set(arr_codes):
        raise ValueError("出发地和到达地不能包含相同机场。")
    policy = (stop_policy or "直飞").strip()
    if policy in {"直飞"}:
        allow_stopovers = False
        allow_connections = False
    elif policy in {"经停", "接受经停"}:
        allow_stopovers = True
        allow_connections = False
    elif policy in {"中转", "接受中转"}:
        allow_stopovers = False
        allow_connections = True
    elif policy in {"经停与中转", "接受经停与中转", "经停中转", "不限"}:
        allow_stopovers = True
        allow_connections = True
    else:
        raise ValueError("航程类型请填写 直飞、接受经停、接受中转 或 接受经停与中转。")
    target = None
    if target_price != "-":
        if not re.fullmatch(r"[0-9]{1,7}", target_price) or int(target_price) <= 0:
            raise ValueError("目标价必须是正整数人民币金额，或用 - 启用观察策略。")
        target = int(target_price)
    return {
        "dep_city": dep_city.strip().upper(),
        "arr_city": arr_city.strip().upper(),
        "flight_date": flight_date,
        "time_window": validate_time_window(time_window),
        "airlines": normalize_airlines(airlines),
        "target_price": target,
        "allow_stopovers": allow_stopovers,
        "allow_connections": allow_connections,
    }


@register("astrbot_plugin_jipiao", "Local", "机票低价监控与 QQ 主动提醒", "0.3.3")
class JipiaoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        mode = config.get("data_source", "fli")
        if mode not in {"fli", "mock"}:
            raise ValueError("data_source must be fli or mock")
        self.mock = mode == "mock"
        interval = max(60, int(config.get("round_interval_seconds", 1800)))
        delay = max(1, int(config.get("task_delay_seconds", 30)))
        data_dir = StarTools.get_data_dir("astrbot_plugin_jipiao")
        self.db = FlightDB(str(data_dir / ("mock.sqlite3" if self.mock else "flights.sqlite3")))
        self.scheduler = MonitorScheduler(
            self.db,
            self._send_message,
            fetcher=fetch_mock_flights if self.mock else fetch_raw_flights,
            round_interval=interval,
            task_delay=delay,
            mock=self.mock,
        )

    async def initialize(self):
        self.scheduler.start()

    async def terminate(self):
        await self.scheduler.stop()
        self.db.close()

    async def _send_message(self, session_id: str, text: str):
        session = MessageSession.from_str(session_id)
        # The inspected QQ adapter silently returns if group scene state was
        # lost on restart. Do not consume a low-price notification in that case.
        for platform in self.context.platform_manager.get_insts():
            if platform.meta().id != session.platform_id:
                continue
            if (
                platform.meta().name == "qq_official"
                and session.message_type == MessageType.GROUP_MESSAGE
            ):
                group_id = session.session_id.rsplit("_", 1)[-1]
                if getattr(platform, "_session_scene", {}).get(group_id) != "group" or not getattr(
                    platform, "_allow_group_proactive_send", False
                ):
                    raise NotificationNotReady(
                        "QQ群会话未就绪，请在设置监控的原群发送 /我的监控，并确认已开启群主动发言"
                    )
            break
        chain = MessageChain().message(text)
        chain.use_markdown_ = False
        chain.use_t2i_ = False
        matched = await self.context.send_message(session_id, chain)
        if not matched:
            raise RuntimeError("No matching AstrBot platform for stored session")
        return True

    def _user_key(self, event: AstrMessageEvent) -> str:
        platform_id = event.get_platform_id() if event else "unknown"
        sender_id = event.get_sender_id() if event else "unknown"
        return json.dumps([platform_id, sender_id], ensure_ascii=False)

    @filter.command("机票监控")
    async def set_monitor(
        self,
        event: AstrMessageEvent,
        dep_city: str = "",
        arr_city: str = "",
        flight_date: str = "",
        time_window: str = "-",
        airlines: str = "-",
        target_price: str = "-",
        stop_policy: str = "直飞",
    ):
        """设置单程机票监控；可选参数用 - 占位。"""
        try:
            settings = validate_settings(
                dep_city, arr_city, flight_date, time_window, airlines, target_price, stop_policy
            )
        except (ValueError, ImportError) as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "缺少 fli 依赖，请管理员按 README 安装本地 fli。"
            )
            yield event.plain_result(f"{message}\n\n{USAGE}")
            return
        user_id = self._user_key(event)
        async with self.scheduler.user_lock(user_id):
            existed = self.db.get_task(user_id) is not None
            self.db.upsert_task(user_id=user_id, session_id=event.unified_msg_origin, **settings)
        self.scheduler.wake()
        target = settings["target_price"]
        mode = (
            f"目标价模式：≤ ¥{target}"
            if target is not None
            else "37%观察策略模式（经验规则，不保证最低买入价）"
        )
        prefix = "【模拟模式，不是真实票价】\n" if self.mock else ""
        scope_text = describe_scope(settings["allow_stopovers"], settings["allow_connections"])
        scope_detail = (
            f"航程类型：{scope_text}（含直飞），可能跨境或过夜；无结果时不自动放宽范围。\n"
            if (settings["allow_stopovers"] or settings["allow_connections"])
            else "航程类型：仅直飞；无直飞报价时继续等待，不自动监控经停或中转。\n"
        )
        yield event.plain_result(
            f"{prefix}已开启监控"
            + ("，已覆盖您原有的监控任务。" if existed else "。")
            + f"\n{settings['dep_city']} → {settings['arr_city']}｜{flight_date}\n"
            + f"出发机场：{','.join(resolve_airports(dep_city))}；到达机场：{','.join(resolve_airports(arr_city))}\n"
            + f"起飞时段：{settings['time_window'] or '不限'}；航司：{settings['airlines'] or '不限'}\n"
            + f"{mode}\n单程、1成人、经济舱、人民币。\n"
            + scope_detail
            + "提醒发送到本次设置所在的会话；同一平台账号每人一个任务。\n"
            + "后台依次检查，出发日北京时间零点结束。/我的监控 可查进度。"
        )

    @filter.command("我的监控")
    async def my_monitor(self, event: AstrMessageEvent):
        """查看条件、阶段、观察价格和最近查询状态。"""
        task = self.db.get_task(self._user_key(event))
        if task is None:
            yield event.plain_result("您当前没有监控任务。\n" + USAGE)
            return
        phase, deadline = task_phase(task, int(time.time()))
        prices = json.loads(task["lowest_3_json"])
        recorded = "、".join(f"¥{item['p']}" for item in prices) or "暂无"
        checked = (
            datetime.fromtimestamp(task["last_checked_at"], CHINA_TZ).strftime("%m-%d %H:%M:%S")
            if task["last_checked_at"]
            else "尚未查询"
        )
        best = json.loads(task["last_best_json"]) if task["last_best_json"] else None
        scope_text = describe_scope(bool(task["allow_stopovers"]), bool(task["allow_connections"]))
        text = (
            ("【模拟模式，不是真实票价】\n" if self.mock else "")
            + f"{task['dep_city']} → {task['arr_city']}｜{task['flight_date']}\n"
            + f"时间段：{task['time_window'] or '不限'}；航司：{task['airlines'] or '不限'}\n"
            + f"航程类型：{scope_text}\n"
            + f"目标价：{'¥' + str(task['target_price']) if task['target_price'] is not None else '未设置'}\n"
            + f"当前阶段：{phase}\n观察期最低3次：{recorded}\n"
            + f"上次提交推送价格：{'¥' + str(task['last_pushed_price']) if task['last_pushed_price'] is not None else '暂无'}\n"
            + f"最近检查（北京时间）：{checked}\n状态：{task['last_status'] or '等待后台检查'}"
        )
        if deadline:
            text += (
                "\n观察截止："
                + datetime.fromtimestamp(deadline, CHINA_TZ).strftime("%Y-%m-%d %H:%M")
                + "（进入前7天时优先兜底）"
            )
        if best:
            text += (
                f"\n最近匹配最低价：¥{best['price']}，{best['flight_no']}，{best['dep_time']} 起飞"
            )
        text += "\n提醒投递到最后一次设置任务的会话。"
        yield event.plain_result(text)

    @filter.command("取消监控")
    async def cancel_monitor(self, event: AstrMessageEvent):
        """取消当前平台账号下的监控任务。"""
        user_id = self._user_key(event)
        async with self.scheduler.user_lock(user_id):
            deleted = self.db.delete_task(user_id)
        yield event.plain_result("已停止机票监控。" if deleted else "您当前没有监控任务。")

    # =========================================================================
    # LLM Function Calling Tools (大语言模型工具)
    # =========================================================================

    @filter.llm_tool(name="search_flights")
    async def search_flights_tool(
        self,
        event: AstrMessageEvent,
        dep_city: str,
        arr_city: str,
        flight_date: str,
        time_window: str = "-",
        airlines: str = "-",
        allow_stopovers: bool = False,
        allow_connections: bool = False,
    ) -> str:
        """查询机票，默认先查直飞；无直飞报价时依次查询经停备选、中转备选。备选查询不代表用户同意监控经停或中转。须告知停靠机场和跨日到达，不得保证同航班号不换机。

        Args:
            dep_city(string): 出发城市名称或机场三字码，例如北京、上海、PEK
            arr_city(string): 到达城市名称或机场三字码，例如广州、深圳、SHA
            flight_date(string): 出发日期，格式必须为 YYYY-MM-DD，例如 2026-11-25
            time_window(string): 可选出发时段，格式为 HH:MM-HH:MM，不限填 '-'
            airlines(string): 可选航司白名单，逗号分隔，如国航,东航或CA,MU，不限填 '-'
            allow_stopovers(boolean): 默认 false；仅用户明确接受经停时传 true，不可根据直飞无结果自行设 true
            allow_connections(boolean): 默认 false；仅用户明确接受换乘中转时传 true，不可根据直飞无结果自行设 true
        """
        try:
            dep_codes = resolve_airports(dep_city)
            arr_codes = resolve_airports(arr_city)
            if set(dep_codes) & set(arr_codes):
                return "出发地和到达地不能包含相同机场。"
            window = validate_time_window(time_window)
            norm_airlines = normalize_airlines(airlines)

            fallback_kind = None
            if not allow_stopovers and not allow_connections:
                # Step 1: Query direct only
                flights = await self.scheduler.fetcher(
                    dep_city, arr_city, flight_date, allow_stopovers=False, allow_connections=False
                )
                best = filter_and_get_lowest(flights, window, norm_airlines)

                # Step 2: Fallback to stopovers
                if not best:
                    stopover_flights = await self.scheduler.fetcher(
                        dep_city,
                        arr_city,
                        flight_date,
                        allow_stopovers=True,
                        allow_connections=False,
                    )
                    stopover_candidates = [
                        f
                        for f in stopover_flights
                        if f.get("itinerary_type") == "stopover"
                        or (
                            f.get("itinerary_type") is None
                            and f.get("stops", 0) > 0
                            and all(
                                layover.get("kind") == "same_flight_number"
                                for layover in f.get("layovers", [])
                            )
                        )
                    ]
                    best = filter_and_get_lowest(stopover_candidates, window, norm_airlines)
                    if best:
                        fallback_kind = "stopover"
                        flights = stopover_candidates

                # Step 3: Fallback to connections
                if not best:
                    conn_flights = await self.scheduler.fetcher(
                        dep_city,
                        arr_city,
                        flight_date,
                        allow_stopovers=False,
                        allow_connections=True,
                    )
                    conn_candidates = [
                        f
                        for f in conn_flights
                        if f.get("itinerary_type") == "connection"
                        or (
                            f.get("itinerary_type") is None
                            and f.get("stops", 0) > 0
                            and any(
                                layover.get("kind") != "same_flight_number"
                                for layover in f.get("layovers", [])
                            )
                        )
                    ]
                    best = filter_and_get_lowest(conn_candidates, window, norm_airlines)
                    if best:
                        fallback_kind = "connection"
                        flights = conn_candidates

                if not best:
                    return (
                        f"未查到 {flight_date} 从 {dep_city} 到 {arr_city} 符合时段({time_window})/航司({airlines})的有效报价，"
                        "已查询直飞、经停及中转；不会因此自动开启经停或中转监控。"
                    )
            else:
                # Explicit stop policy from user
                flights = await self.scheduler.fetcher(
                    dep_city,
                    arr_city,
                    flight_date,
                    allow_stopovers=allow_stopovers,
                    allow_connections=allow_connections,
                )
                best = filter_and_get_lowest(flights, window, norm_airlines)
                if not best:
                    scope_label = describe_scope(allow_stopovers, allow_connections)
                    return (
                        f"未查到 {flight_date} 从 {dep_city} 到 {arr_city} 符合时段({time_window})/航司({airlines})的有效报价，"
                        f"已查询{scope_label}；不会因此自动开启其他范围监控。"
                    )

            matched = [f for f in flights if filter_and_get_lowest([f], window, norm_airlines)]
            matched.sort(key=lambda f: f["price"])

            if fallback_kind == "stopover":
                scope_note = "未查到符合条件的直飞报价，以下为同航班号经停备选；监控仍默认仅直飞，明确说接受经停才会纳入。"
            elif fallback_kind == "connection":
                scope_note = "未查到符合条件的直飞或经停报价，以下为换乘中转备选；监控仍默认仅直飞，明确说接受中转才会纳入。"
            elif allow_stopovers or allow_connections:
                scope_note = f"查询范围：{describe_scope(allow_stopovers, allow_connections)}（用户已接受）。"
            else:
                scope_note = "查询范围：仅直飞。"

            lines = [
                f"【机票实时查询结果】{dep_city} -> {arr_city} | 日期: {flight_date}",
                scope_note,
                f"符合条件最低价: ¥{best['price']} ({best['flight_no']} {best['airline']}, 起飞 {best['dep_time']}，到达 {best['arr_date']} {best['arr_time']})",
                describe_itinerary(best),
                f"共查询到 {len(flights)} 条行程报价，符合筛选 {len(matched)} 条，以下为最便宜的前 5 条：",
            ]
            for i, f in enumerate(matched[:5], 1):
                lines.append(
                    f"{i}. {f['flight_no']} ({f['airline']}) 起飞 {f['dep_time']}，到达 {f['arr_date']} {f['arr_time']} 整程票价: ¥{f['price']}\n"
                    + describe_itinerary(f)
                )
            lines.append(
                "注：以上为单程成人经济舱人民币报价；经停/中转行程可能跨境或过夜。"
                "航司白名单适用于全部航段的营销航司；实际票价、税费及出入境要求以订票渠道为准。"
            )
            return "\n".join(lines)
        except Exception as exc:
            return f"机票查询失败: {exc}"

    @filter.llm_tool(name="set_flight_monitor")
    async def set_flight_monitor_tool(
        self,
        event: AstrMessageEvent,
        dep_city: str,
        arr_city: str,
        flight_date: str,
        time_window: str = "-",
        airlines: str = "-",
        target_price: int | None = None,
        allow_stopovers: bool = False,
        allow_connections: bool = False,
    ) -> str:
        """开启或修改当前用户的机票低价监控任务。出发日北京时间零点自动结束，降价主动推送提醒。

        Args:
            dep_city(string): 出发城市名称或机场三字码，例如北京、上海、PEK
            arr_city(string): 到达城市名称或机场三字码，例如广州、深圳、SHA
            flight_date(string): 出发日期，格式必须为 YYYY-MM-DD，必须晚于今天，例如 2026-11-25
            time_window(string): 可选出发时段，格式为 HH:MM-HH:MM，不限填 '-'
            airlines(string): 可选航司白名单，逗号分隔，如国航,东航或CA,MU，不限填 '-'
            target_price(number): 可选目标价格（人民币正整数）；不设置时传 null，自动启用 37% 观察策略
            allow_stopovers(boolean): 默认 false；只有用户明确接受同航班号经停时才传 true
            allow_connections(boolean): 默认 false；只有用户明确接受换乘中转时才传 true
        """
        if allow_stopovers and allow_connections:
            stop_policy = "接受经停与中转"
        elif allow_stopovers:
            stop_policy = "接受经停"
        elif allow_connections:
            stop_policy = "接受中转"
        else:
            stop_policy = "直飞"

        target_str = str(target_price).strip() if target_price is not None else "-"
        if target_str in {"", "0", "None", "null"}:
            target_str = "-"
        try:
            settings = validate_settings(
                dep_city,
                arr_city,
                flight_date,
                time_window,
                airlines,
                target_str,
                stop_policy,
            )
        except Exception as exc:
            return f"设置监控任务失败: {exc}"

        user_id = self._user_key(event)
        session_id = (
            getattr(event, "unified_msg_origin", None)
            or f"{event.get_platform_id()}:FriendMessage:{event.get_sender_id()}"
        )
        async with self.scheduler.user_lock(user_id):
            existed = self.db.get_task(user_id) is not None
            self.db.upsert_task(user_id=user_id, session_id=session_id, **settings)
        self.scheduler.wake()
        target = settings["target_price"]
        mode_desc = f"目标价模式(≤ ¥{target})" if target is not None else "37%观察策略模式"
        scope_text = describe_scope(settings["allow_stopovers"], settings["allow_connections"])
        scope_detail = (
            f"{scope_text}（无结果不自动放宽范围）"
            if (settings["allow_stopovers"] or settings["allow_connections"])
            else "仅直飞，无结果不自动改为经停或中转"
        )
        return (
            f"已成功开启机票低价监控{'（已覆盖原有监控任务）' if existed else ''}：\n"
            f"航线：{settings['dep_city']} → {settings['arr_city']}，出发日期：{flight_date}。\n"
            f"筛选条件：起飞时段 {settings['time_window'] or '不限'}，航司 {settings['airlines'] or '不限'}。\n"
            f"监控模式：{mode_desc}。\n"
            f"航程类型：{scope_detail}。\n"
            "单程成人经济舱；航司白名单适用于全部航段。\n"
            "系统将在后台自动轮询，并在满足降价条件时主动推送消息提醒。出发日北京时间零点自动结束。"
        )

    @filter.llm_tool(name="get_flight_monitor")
    async def get_flight_monitor_tool(self, event: AstrMessageEvent) -> str:
        """查看当前用户已开启的机票监控任务及最新状态。当用户询问自己的监控任务、进度或价格时调用。"""
        user_id = self._user_key(event)
        task = self.db.get_task(user_id)
        if task is None:
            return "您当前没有任何正在运行的机票监控任务。"
        phase, deadline = task_phase(task, int(time.time()))
        prices = json.loads(task["lowest_3_json"])
        recorded = "、".join(f"¥{item['p']}" for item in prices) or "暂无"
        checked = (
            datetime.fromtimestamp(task["last_checked_at"], CHINA_TZ).strftime("%m-%d %H:%M:%S")
            if task["last_checked_at"]
            else "尚未查询"
        )
        best = json.loads(task["last_best_json"]) if task["last_best_json"] else None
        scope_text = describe_scope(bool(task["allow_stopovers"]), bool(task["allow_connections"]))
        lines = [
            "【您的机票监控任务详情】",
            f"航线: {task['dep_city']} → {task['arr_city']} | 出发日期: {task['flight_date']}",
            f"筛选条件: 时段 {task['time_window'] or '不限'} | 航司 {task['airlines'] or '不限'}",
            f"航程类型: {scope_text}",
            f"目标价: {'¥' + str(task['target_price']) if task['target_price'] is not None else '未设置(启用37%观察策略)'}",
            f"当前阶段: {phase}",
            f"观察期已记录低价: {recorded}",
            f"上次推送价格: {'¥' + str(task['last_pushed_price']) if task['last_pushed_price'] is not None else '暂无'}",
            f"最近检查状态: {task['last_status'] or '等待后台检查'} (检查时间: {checked})",
        ]
        if deadline:
            lines.append(
                "观察截止: "
                + datetime.fromtimestamp(deadline, CHINA_TZ).strftime("%Y-%m-%d %H:%M")
                + " (进入起飞前7天时优先兜底)"
            )
        if best:
            lines.append(
                f"最近匹配航班: {best['flight_no']} ({best['airline']})，起飞 {best['dep_time']}，最低价 ¥{best['price']}"
            )
        return "\n".join(lines)

    @filter.llm_tool(name="cancel_flight_monitor")
    async def cancel_flight_monitor_tool(self, event: AstrMessageEvent) -> str:
        """取消当前用户的机票低价监控任务。当用户要求停止或取消监控时调用。"""
        user_id = self._user_key(event)
        async with self.scheduler.user_lock(user_id):
            deleted = self.db.delete_task(user_id)
        return (
            "已成功取消并停止您的机票低价监控任务。"
            if deleted
            else "您当前并没有正在运行的机票监控任务。"
        )
