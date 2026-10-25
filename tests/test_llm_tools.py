from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from astrbot.api.star import StarTools
from astrbot.core.star.register.star_handler import llm_tools
from astrbot_plugin_jipiao.algorithm import CHINA_TZ
from astrbot_plugin_jipiao.main import JipiaoPlugin

FUTURE = (datetime.now(CHINA_TZ) + timedelta(days=30)).strftime("%Y-%m-%d")


class DummyEvent:
    unified_msg_origin = "qq:FriendMessage:user_test"

    def get_platform_id(self):
        return "qq"

    def get_sender_id(self):
        return "user_test"


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(StarTools, "get_data_dir", lambda *args: tmp_path)
    instance = JipiaoPlugin(
        SimpleNamespace(
            send_message=AsyncMock(return_value=True),
            platform_manager=SimpleNamespace(get_insts=lambda: []),
        ),
        {},
    )
    yield instance
    instance.db.close()


def test_llm_tools_registered():
    """Verify that AstrBot registered all 4 tools in the function tools manager."""
    tools = {f.name: f for f in llm_tools.func_list}
    assert "search_flights" in tools
    assert "set_flight_monitor" in tools
    assert "get_flight_monitor" in tools
    assert "cancel_flight_monitor" in tools

    search_params = tools["search_flights"].parameters["properties"]
    assert "dep_city" in search_params
    assert "arr_city" in search_params
    assert "flight_date" in search_params
    assert search_params["flight_date"]["type"] == "string"
    assert search_params["allow_stopovers"]["type"] == "boolean"
    assert search_params["allow_connections"]["type"] == "boolean"
    for name in ["search_flights", "set_flight_monitor"]:
        description = tools[name].parameters["properties"]["airlines"]["description"]
        assert "!9C,!KN" in description and "排除" in description
    assert "allow_stopovers" not in tools["search_flights"].parameters.get("required", [])
    assert "allow_connections" not in tools["search_flights"].parameters.get("required", [])
    assert (
        tools["set_flight_monitor"].parameters["properties"]["allow_stopovers"]["type"] == "boolean"
    )
    assert (
        tools["set_flight_monitor"].parameters["properties"]["allow_connections"]["type"]
        == "boolean"
    )


async def test_search_flights_tool(plugin, monkeypatch):
    event = DummyEvent()

    async def fake_fetch(dep, arr, date, allow_stopovers=False, allow_connections=False):
        return [
            {
                "itinerary_type": "nonstop",
                "flight_no": "CA1234",
                "airline": "国航",
                "dep_time": "08:00",
                "arr_time": "10:15",
                "price": 500,
                "currency": "CNY",
                "dep_airport": "PEK",
                "arr_airport": "SHA",
                "arr_date": FUTURE,
                "duration_minutes": 135,
                "stops": 0,
                "layovers": [],
                "self_transfer": False,
            },
            {
                "itinerary_type": "nonstop",
                "flight_no": "MU5678",
                "airline": "东航",
                "dep_time": "14:00",
                "arr_time": "16:20",
                "price": 420,
                "currency": "CNY",
                "dep_airport": "PKX",
                "arr_airport": "PVG",
                "arr_date": FUTURE,
                "duration_minutes": 140,
                "stops": 0,
                "layovers": [],
                "self_transfer": False,
            },
        ]

    plugin.scheduler.fetcher = fake_fetch

    # 1. Normal query
    res = await plugin.search_flights_tool(event, "北京", "上海", FUTURE)
    assert "【机票实时查询结果】" in res
    assert "MU5678" in res
    assert "¥420" in res

    # 2. Time window filter
    res_filtered = await plugin.search_flights_tool(
        event, "北京", "上海", FUTURE, time_window="07:00-10:00"
    )
    assert "CA1234" in res_filtered
    assert "¥500" in res_filtered
    assert "MU5678" not in res_filtered

    # 3. Bad route
    res_bad = await plugin.search_flights_tool(event, "北京", "PEK", FUTURE)
    assert "不能包含相同机场" in res_bad


async def test_monitor_lifecycle_tools(plugin):
    event = DummyEvent()

    # 1. Initially no monitor
    status = await plugin.get_flight_monitor_tool(event)
    assert "没有任何" in status

    # 2. Set monitor via tool
    set_res = await plugin.set_flight_monitor_tool(
        event, "北京", "上海", FUTURE, target_price="600"
    )
    assert "已成功开启机票低价监控" in set_res
    assert "目标价模式(≤ ¥600)" in set_res

    # 3. Get monitor status
    status_after = await plugin.get_flight_monitor_tool(event)
    assert "北京 → 上海" in status_after
    assert "¥600" in status_after
    assert "仅直飞" in status_after
    assert plugin.db.get_all_tasks()[0]["allow_stopovers"] == 0
    assert plugin.db.get_all_tasks()[0]["allow_connections"] == 0

    # 4. Cancel monitor via tool
    cancel_res = await plugin.cancel_flight_monitor_tool(event)
    assert "已成功取消并停止" in cancel_res

    # 5. Confirm cancellation
    status_final = await plugin.get_flight_monitor_tool(event)
    assert "没有任何" in status_final


async def test_search_tool_discloses_real_connection_and_next_day_arrival(plugin, connecting_quote):
    plugin.scheduler.fetcher = AsyncMock(return_value=[connecting_quote])
    result = await plugin.search_flights_tool(
        DummyEvent(), "北京", "上海", "2026-12-01", allow_connections=True
    )
    assert "KE2224 / KE2071" in result
    assert "2026-12-02 09:40" in result
    assert "PUS 中转，停留 1110 分钟，过夜" in result
    assert "全程 1395 分钟" in result
    assert "¥1718" in result
    assert "直飞（接口单航段）" not in result
    plugin.scheduler.fetcher.assert_awaited_once_with(
        "北京", "上海", "2026-12-01", allow_stopovers=False, allow_connections=True
    )


async def test_empty_direct_query_shows_stop_alternatives_without_creating_task(
    plugin, connecting_quote
):
    stopover_quote = dict(
        connecting_quote,
        flight_no="CA1234",
        airline="国航",
        airline_code="CA",
        itinerary_type="stopover",
        layovers=[dict(connecting_quote["layovers"][0], kind="same_flight_number")],
    )

    # 1. Fallback stops at stopovers if available
    plugin.scheduler.fetcher = AsyncMock(side_effect=[[], [stopover_quote]])
    result = await plugin.search_flights_tool(DummyEvent(), "北京", "上海", "2026-12-01")
    assert "未查到符合条件的直飞报价" in result
    assert "以下为同航班号经停备选" in result
    assert "明确说接受经停才会纳入" in result
    assert "CA1234" in result
    assert plugin.scheduler.fetcher.await_args_list == [
        call("北京", "上海", "2026-12-01", allow_stopovers=False, allow_connections=False),
        call("北京", "上海", "2026-12-01", allow_stopovers=True, allow_connections=False),
    ]
    assert plugin.db.get_all_tasks() == []

    # 2. Fallback continues to connections if no stopovers either
    plugin.scheduler.fetcher = AsyncMock(side_effect=[[], [], [connecting_quote]])
    result_conn = await plugin.search_flights_tool(DummyEvent(), "北京", "上海", "2026-12-01")
    assert "未查到符合条件的直飞或经停报价" in result_conn
    assert "以下为换乘中转备选" in result_conn
    assert "明确说接受中转才会纳入" in result_conn
    assert "KE2224 / KE2071" in result_conn
    assert plugin.scheduler.fetcher.await_args_list == [
        call("北京", "上海", "2026-12-01", allow_stopovers=False, allow_connections=False),
        call("北京", "上海", "2026-12-01", allow_stopovers=True, allow_connections=False),
        call("北京", "上海", "2026-12-01", allow_stopovers=False, allow_connections=True),
    ]


async def test_direct_quote_stops_query_before_fallback(plugin, connecting_quote):
    direct = dict(
        connecting_quote,
        flight_no="CA123",
        stops=0,
        layovers=[],
        price=2000,
        itinerary_type="nonstop",
    )
    plugin.scheduler.fetcher = AsyncMock(return_value=[direct])
    result = await plugin.search_flights_tool(DummyEvent(), "北京", "上海", FUTURE)
    assert "查询范围：仅直飞" in result
    assert "¥2000" in result
    plugin.scheduler.fetcher.assert_awaited_once_with(
        "北京", "上海", FUTURE, allow_stopovers=False, allow_connections=False
    )


async def test_query_failure_does_not_switch_stop_policy(plugin):
    plugin.scheduler.fetcher = AsyncMock(side_effect=TimeoutError("offline"))
    result = await plugin.search_flights_tool(DummyEvent(), "北京", "上海", FUTURE)
    assert "机票查询失败" in result
    assert plugin.scheduler.fetcher.await_count == 1


async def test_monitor_stop_permission_is_explicit_and_persisted(plugin):
    # 1. Stopover only
    response = await plugin.set_flight_monitor_tool(
        DummyEvent(), "北京", "上海", FUTURE, allow_stopovers=True
    )
    assert "直飞、经停" in response
    task = plugin.db.get_all_tasks()[0]
    assert task["allow_stopovers"] == 1
    assert task["allow_connections"] == 0
    assert "直飞、经停" in await plugin.get_flight_monitor_tool(DummyEvent())

    # 2. Connection only
    response = await plugin.set_flight_monitor_tool(
        DummyEvent(), "北京", "上海", FUTURE, allow_connections=True
    )
    assert "直飞、中转" in response
    task = plugin.db.get_all_tasks()[0]
    assert task["allow_stopovers"] == 0
    assert task["allow_connections"] == 1
    assert "直飞、中转" in await plugin.get_flight_monitor_tool(DummyEvent())

    # 3. Both stopover and connection
    response = await plugin.set_flight_monitor_tool(
        DummyEvent(), "北京", "上海", FUTURE, allow_stopovers=True, allow_connections=True
    )
    assert "直飞、经停、中转" in response
    task = plugin.db.get_all_tasks()[0]
    assert task["allow_stopovers"] == 1
    assert task["allow_connections"] == 1
    assert "直飞、经停、中转" in await plugin.get_flight_monitor_tool(DummyEvent())

    # 4. Default back to nonstop
    await plugin.set_flight_monitor_tool(DummyEvent(), "北京", "上海", FUTURE)
    task = plugin.db.get_all_tasks()[0]
    assert task["allow_stopovers"] == 0
    assert task["allow_connections"] == 0
    assert "仅直飞" in await plugin.get_flight_monitor_tool(DummyEvent())


async def test_llm_monitor_accepts_blacklist_only_and_keeps_it_on_restart(plugin):
    response = await plugin.set_flight_monitor_tool(
        DummyEvent(), "北京", "上海", FUTURE, airlines="!春秋,!联航"
    )
    assert "排除 9C、KN" in response and "其余航司不限" in response
    before = plugin.db.get_all_tasks()[0]
    assert before["airlines"] == "!9C,!KN"
    from astrbot_plugin_jipiao.database import FlightDB

    path = plugin.db.conn.execute("PRAGMA database_list").fetchone()[2]
    reopened = FlightDB(path)
    try:
        assert reopened.get_all_tasks()[0] == before
    finally:
        reopened.close()
    assert "排除 9C、KN" in await plugin.get_flight_monitor_tool(DummyEvent())


async def test_fallback_never_reintroduces_a_blacklisted_airline(plugin, connecting_quote):
    direct = dict(
        connecting_quote,
        flight_no="9C123",
        airline_code="9C",
        price=99,
        stops=0,
        itinerary_type="nonstop",
    )
    stopover = dict(direct, flight_no="9C456", itinerary_type="stopover", stops=1)
    plugin.scheduler.fetcher = AsyncMock(side_effect=[[direct], [stopover], [connecting_quote]])
    result = await plugin.search_flights_tool(DummyEvent(), "北京", "上海", FUTURE, airlines="!9C")
    assert plugin.scheduler.fetcher.await_count == 3
    assert "KE2224 / KE2071" in result
    assert "9C123" not in result and "9C456" not in result and "¥99" not in result
    assert "排除 9C" in result


async def test_fallback_applies_filters_and_keeps_existing_monitor_unchanged(
    plugin, connecting_quote
):
    await plugin.set_flight_monitor_tool(DummyEvent(), "北京", "上海", FUTURE)
    before = plugin.db.get_all_tasks()[0]
    direct = dict(
        connecting_quote, stops=0, dep_time="18:00", layovers=[], itinerary_type="nonstop"
    )
    plugin.scheduler.fetcher = AsyncMock(side_effect=[[direct], [], [connecting_quote]])
    result = await plugin.search_flights_tool(
        DummyEvent(), "北京", "上海", FUTURE, time_window="08:00-11:00"
    )
    assert "以下为换乘中转备选" in result
    assert plugin.scheduler.fetcher.await_count == 3
    assert plugin.db.get_all_tasks()[0] == before


async def test_no_direct_or_connecting_quotes_returns_empty_status(plugin):
    plugin.scheduler.fetcher = AsyncMock(return_value=[])
    result = await plugin.search_flights_tool(DummyEvent(), "北京", "上海", FUTURE)
    assert "未查到" in result and "已查询直飞、经停及中转" in result
    assert plugin.scheduler.fetcher.await_count == 3
