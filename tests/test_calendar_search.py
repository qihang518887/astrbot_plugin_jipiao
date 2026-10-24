import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from astrbot.api.star import StarTools
from astrbot_plugin_jipiao import calendar_search
from astrbot_plugin_jipiao.algorithm import CHINA_TZ
from astrbot_plugin_jipiao.calendar_search import (
    CalendarQueryError,
    calendar_supports_scope,
    flexible_date_window,
    parse_calendar_days,
    select_calendar_lowest,
)
from astrbot_plugin_jipiao.main import JipiaoPlugin

TODAY = date(2026, 10, 25)
CENTER = "2026-11-05"


@pytest.mark.parametrize(
    "text",
    [
        "查11月5日左右大连到上海",
        "大概11月5号去上海",
        "大约在11月5日出发",
        "差不多11月5日",
        "2026-11-05前后",
        "5号附近去上海",
        "约11月5日出发",
    ],
)
def test_calendar_requires_explicit_approximate_date(text):
    assert flexible_date_window(text, CENTER, None, today=TODAY) == ("2026-11-02", "2026-11-08")


@pytest.mark.parametrize(
    "text,airlines",
    [
        ("查11月5日机票", None),
        ("11月5日大概多少钱", None),
        ("", None),
        ("查便宜机票", None),
        ("大概什么时候便宜", None),
        ("11月5日左右国航的", None),
        ("11月5日前后CA航班", None),
        ("11月5日前后CA1234", None),
        ("11月5日左右", "MU"),
        ("11月5日前后，但必须当天走", None),
        ("大概11月5日，不改日期", None),
        ("11月5日左右，不要低价日历", None),
        ("11月5日左右，但只要11月5日", None),
        ("11月15日左右", None),
        ("12月5日左右", None),
        ("2026-12-05左右", None),
    ],
)
def test_exact_date_airline_and_unrelated_approximation_disable_calendar(text, airlines):
    assert flexible_date_window(text, CENTER, airlines, today=TODAY) is None


@pytest.mark.parametrize(
    "center,text,expected",
    [
        ("2026-11-01", "11月1日左右", ("2026-11-01", "2026-11-04")),
        ("2026-11-30", "11月30日左右", ("2026-11-27", "2026-11-30")),
        ("2026-10-26", "10月26日左右", ("2026-10-25", "2026-10-29")),
        ("2028-02-29", "2月29日左右", ("2028-02-26", "2028-02-29")),
        ("2026-12-31", "12月31日左右", ("2026-12-28", "2026-12-31")),
    ],
)
def test_seven_day_window_is_clipped_to_month_and_nonpast_dates(center, text, expected):
    assert flexible_date_window(text, center, None, today=TODAY) == expected


def test_expired_flexible_window_fails_without_requests():
    with pytest.raises(ValueError):
        flexible_date_window("10月1日左右", "2026-10-01", None, today=TODAY)


def test_parser_ignores_out_of_range_missing_zero_and_invalid_prices():
    epoch = int(datetime(2026, 11, 5, tzinfo=CHINA_TZ).timestamp()) * 1000
    payload = {
        "responseStatus": {"Ack": "Success"},
        "priceList": [
            {"departDate": f"/Date({epoch}+0800)/", "price": 260},
            {"departDate": "2026-11-05", "price": 300},
            {"departDate": "2026-11-06", "price": "292.1"},
            {"departDate": "2026-11-01", "price": 100},
            {"departDate": "2026-11-07", "price": 0},
            {"departDate": "2026-11-07", "price": "NaN"},
            {"departDate": "2026-11-07", "price": "Infinity"},
            {"departDate": "2026-11-07", "price": True},
            {"departDate": "bad", "price": 50},
            {},
            None,
        ],
    }
    assert parse_calendar_days(payload, "2026-11-02", "2026-11-08") == [
        {"date": CENTER, "price": 260},
        {"date": "2026-11-06", "price": 293},
    ]
    with pytest.raises(CalendarQueryError):
        parse_calendar_days({"responseStatus": {"Ack": "Failure"}}, CENTER, CENTER)


def test_calendar_ties_use_nearest_then_earlier_date():
    days = [
        {"date": "2026-11-03", "price": 260},
        {"date": "2026-11-06", "price": 260},
        {"date": "2026-11-04", "price": 260},
        {"date": CENTER, "price": 300},
    ]
    assert select_calendar_lowest(days, CENTER) == {"date": "2026-11-04", "price": 260}
    assert select_calendar_lowest([], CENTER) is None


async def test_calendar_http_request_uses_one_call_city_codes_and_bounded_result(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "responseStatus": {"Ack": "Success"},
                "priceList": [
                    {"departDate": "2026-11-05", "price": 300},
                    {"departDate": "2026-12-01", "price": 100},
                ],
            },
        )

    original = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        calendar_search.httpx, "AsyncClient", lambda **kw: original(transport=transport)
    )
    result = await calendar_search.fetch_low_price_calendar(
        "北京",
        "上海",
        "2026-11-02",
        "2026-11-08",
        allow_stopovers=True,
        allow_connections=True,
    )
    assert result == [{"date": CENTER, "price": 300}]
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["departNewCityCode"] == "BJS"
    assert body["arriveNewCityCode"] == "SHA"
    assert body["startDate"] == "2026-11-02"
    assert "cookie" not in requests[0].headers


@pytest.mark.parametrize("stopovers,connections", [(False, False), (True, False), (False, True)])
async def test_restricted_calendar_fetch_fails_before_http(monkeypatch, stopovers, connections):
    client = Mock()
    monkeypatch.setattr(calendar_search.httpx, "AsyncClient", client)
    assert not calendar_supports_scope(stopovers, connections)
    with pytest.raises(CalendarQueryError, match="不能分别排除"):
        await calendar_search.fetch_low_price_calendar(
            "大连",
            "上海",
            "2026-11-02",
            "2026-11-08",
            allow_stopovers=stopovers,
            allow_connections=connections,
        )
    client.assert_not_called()


async def test_calendar_fetch_defaults_to_safe_direct_scope(monkeypatch):
    client = Mock()
    monkeypatch.setattr(calendar_search.httpx, "AsyncClient", client)
    with pytest.raises(CalendarQueryError):
        await calendar_search.fetch_low_price_calendar("大连", "上海", CENTER, CENTER)
    client.assert_not_called()


class Event:
    def __init__(self, text="11月5日左右从大连去上海"):
        self.text = text

    def get_message_str(self):
        return self.text


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 25, tzinfo=CHINA_TZ).astimezone(tz)

    monkeypatch.setattr(calendar_search, "datetime", Clock)
    monkeypatch.setattr(StarTools, "get_data_dir", lambda *args: tmp_path)
    instance = JipiaoPlugin(SimpleNamespace(), {})
    instance.calendar_fetcher = AsyncMock(return_value=[{"date": "2026-11-04", "price": 260}])
    instance.scheduler.fetcher = AsyncMock(return_value=[])
    yield instance
    instance.db.close()


def quote(price=260):
    return {
        "flight_no": "CA123",
        "airline": "国航",
        "dep_time": "09:00",
        "arr_time": "11:00",
        "arr_date": "2026-11-04",
        "dep_airport": "DLC",
        "arr_airport": "SHA",
        "duration_minutes": 120,
        "price": price,
        "stops": 0,
        "itinerary_type": "nonstop",
    }


@pytest.mark.parametrize(
    "price,expected", [(260, "相同价格"), (250, "比日历参考价更低"), (320, "未成立")]
)
async def test_fuzzy_tool_verifies_only_one_date_and_never_writes_monitor_history(
    plugin, price, expected
):
    plugin.scheduler.fetcher.return_value = [quote(price)]
    result = await plugin.search_flights_tool(
        Event(), "大连", "上海", CENTER, allow_stopovers=True, allow_connections=True
    )
    assert "同月范围 2026-11-02 至 2026-11-08" in result
    assert expected in result and f"¥{price}" in result
    plugin.calendar_fetcher.assert_awaited_once_with(
        "大连",
        "上海",
        "2026-11-02",
        "2026-11-08",
        allow_stopovers=True,
        allow_connections=True,
    )
    plugin.scheduler.fetcher.assert_awaited_once_with(
        "大连",
        "上海",
        "2026-11-04",
        allow_stopovers=True,
        allow_connections=True,
    )
    assert plugin.db.get_all_tasks() == []


async def test_no_details_does_not_try_more_days_or_stop_policy_fallbacks(plugin):
    result = await plugin.search_flights_tool(
        Event(), "大连", "上海", CENTER, allow_stopovers=True, allow_connections=True
    )
    assert "未核实" in result
    assert plugin.scheduler.fetcher.await_count == 1


async def test_empty_calendar_does_not_trigger_any_detail_queries(plugin):
    plugin.calendar_fetcher.return_value = []
    result = await plugin.search_flights_tool(
        Event(), "大连", "上海", CENTER, allow_stopovers=True, allow_connections=True
    )
    assert "没有有效参考价" in result
    plugin.scheduler.fetcher.assert_not_awaited()


async def test_calendar_failure_does_not_scan_dates(plugin):
    plugin.calendar_fetcher.side_effect = CalendarQueryError("upstream unavailable")
    result = await plugin.search_flights_tool(
        Event(), "大连", "上海", CENTER, allow_stopovers=True, allow_connections=True
    )
    assert "查询失败" in result
    plugin.scheduler.fetcher.assert_not_awaited()


async def test_detail_failure_keeps_reference_explicitly_unverified(plugin):
    plugin.scheduler.fetcher.side_effect = TimeoutError("offline")
    result = await plugin.search_flights_tool(
        Event(), "大连", "上海", CENTER, allow_stopovers=True, allow_connections=True
    )
    assert "最低参考价" in result and "未核实" in result
    assert plugin.scheduler.fetcher.await_count == 1


@pytest.mark.parametrize(
    "message,airlines",
    [("11月5日去上海", "-"), ("11月5日大概多少钱", "-"), ("11月5日左右国航", "CA")],
)
async def test_exact_date_and_airline_queries_do_not_call_calendar(plugin, message, airlines):
    plugin.scheduler.fetcher.return_value = [quote()]
    result = await plugin.search_flights_tool(
        Event(message), "大连", "上海", CENTER, airlines=airlines
    )
    assert "【机票实时查询结果】" in result
    plugin.calendar_fetcher.assert_not_awaited()
    assert plugin.scheduler.fetcher.call_args.args[2] == CENTER


async def test_mock_mode_never_requests_live_calendar(plugin):
    plugin.mock = True
    plugin.scheduler.fetcher.return_value = [quote()]
    await plugin.search_flights_tool(Event(), "大连", "上海", CENTER)
    plugin.calendar_fetcher.assert_not_awaited()


async def test_calendar_reference_never_changes_existing_monitor_task(plugin):
    plugin.db.upsert_task("u", "qq:FriendMessage:1", "北京", "上海", "2099-12-01")
    plugin.db.update_lowest_3("u", '[{"p":400,"t":1}]')
    plugin.db.update_last_pushed_price("u", 390)
    before = plugin.db.get_task("u")
    plugin.scheduler.fetcher.return_value = [quote(320)]
    result = await plugin.search_flights_tool(
        Event(), "大连", "上海", CENTER, allow_stopovers=True, allow_connections=True
    )
    assert "未成立" in result
    assert plugin.db.get_task("u") == before


async def test_fuzzy_details_keep_user_time_and_stop_constraints_when_calendar_is_skipped(plugin):
    plugin.scheduler.fetcher.return_value = [quote()]
    result = await plugin.search_flights_tool(
        Event(),
        "大连",
        "上海",
        CENTER,
        time_window="12:00-18:00",
        allow_connections=True,
    )
    assert "低价日历已跳过" in result
    assert "未在原日期" in result
    plugin.calendar_fetcher.assert_not_awaited()
    plugin.scheduler.fetcher.assert_awaited_once_with(
        "大连",
        "上海",
        CENTER,
        allow_stopovers=False,
        allow_connections=True,
    )


@pytest.mark.parametrize("stopovers,connections", [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize("has_quote", [False, True])
async def test_fuzzy_calendar_cannot_pick_dates_using_forbidden_itinerary_prices(
    plugin, stopovers, connections, has_quote
):
    # The forbidden calendar would pick a different date with a cheap connection.
    plugin.calendar_fetcher.return_value = [{"date": "2026-11-02", "price": 99}]
    plugin.scheduler.fetcher.return_value = (
        [{**quote(320), "arr_date": CENTER}] if has_quote else []
    )
    plugin.db.upsert_task("u", "qq:FriendMessage:1", "DLC", "SHA", "2099-12-01")
    plugin.db.update_lowest_3("u", '[{"p":400,"t":1}]')
    before = plugin.db.get_task("u")
    result = await plugin.search_flights_tool(
        Event(),
        "大连",
        "上海",
        CENTER,
        allow_stopovers=stopovers,
        allow_connections=connections,
    )
    plugin.calendar_fetcher.assert_not_awaited()
    plugin.scheduler.fetcher.assert_awaited_once_with(
        "大连", "上海", CENTER, allow_stopovers=stopovers, allow_connections=connections
    )
    assert "低价日历已跳过" in result
    assert "2026-11-02" not in result
    assert "¥99" not in result
    assert "最低参考价" not in result
    if has_quote:
        assert "符合条件最低价: ¥320" in result
    else:
        assert "未在原日期" in result
    assert plugin.db.get_task("u") == before


async def test_fuzzy_query_defaults_to_direct_without_calendar_or_fallback(plugin):
    result = await plugin.search_flights_tool(Event(), "大连", "上海", CENTER)
    assert "低价日历已跳过" in result
    assert "未自动追加经停或中转查询" in result
    plugin.calendar_fetcher.assert_not_awaited()
    plugin.scheduler.fetcher.assert_awaited_once_with(
        "大连", "上海", CENTER, allow_stopovers=False, allow_connections=False
    )
