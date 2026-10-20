from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


async def test_search_flights_tool(plugin, monkeypatch):
    event = DummyEvent()

    async def fake_fetch(dep, arr, date):
        return [
            {
                "flight_no": "CA1234",
                "airline": "国航",
                "dep_time": "08:00",
                "arr_time": "10:15",
                "price": 500,
                "currency": "CNY",
                "dep_airport": "PEK",
                "arr_airport": "SHA",
            },
            {
                "flight_no": "MU5678",
                "airline": "东航",
                "dep_time": "14:00",
                "arr_time": "16:20",
                "price": 420,
                "currency": "CNY",
                "dep_airport": "PKX",
                "arr_airport": "PVG",
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

    # 4. Cancel monitor via tool
    cancel_res = await plugin.cancel_flight_monitor_tool(event)
    assert "已成功取消并停止" in cancel_res

    # 5. Confirm cancellation
    status_final = await plugin.get_flight_monitor_tool(event)
    assert "没有任何" in status_final
