from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

# These are real AstrBot imports, not framework stubs.
from astrbot.api.event import MessageChain
from astrbot.api.star import StarTools
from astrbot.core.star.filter.command import CommandFilter
from astrbot_plugin_jipiao.algorithm import CHINA_TZ
from astrbot_plugin_jipiao.database import FlightDB
from astrbot_plugin_jipiao.fetcher import fetch_raw_flights
from astrbot_plugin_jipiao.main import JipiaoPlugin, validate_settings

FUTURE = (datetime.now(CHINA_TZ) + timedelta(days=30)).strftime("%Y-%m-%d")


class Event:
    unified_msg_origin = "qq:FriendMessage:1"

    def get_platform_id(self):
        return "qq"

    def get_sender_id(self):
        return "1"

    def plain_result(self, text):
        return text


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


def test_input_validation_and_aliases():
    settings = validate_settings("北京", "上海", FUTURE, "08:00-18:00", "国航,东航", "500")
    assert settings["airlines"] == "CA,MU"
    assert settings["target_price"] == 500
    assert settings["allow_stopovers"] is False
    assert settings["allow_connections"] is False

    stopover_settings = validate_settings("北京", "上海", FUTURE, stop_policy="接受经停")
    assert stopover_settings["allow_stopovers"] is True
    assert stopover_settings["allow_connections"] is False

    conn_settings = validate_settings("北京", "上海", FUTURE, stop_policy="接受中转")
    assert conn_settings["allow_stopovers"] is False
    assert conn_settings["allow_connections"] is True

    both_settings = validate_settings("北京", "上海", FUTURE, stop_policy="接受经停与中转")
    assert both_settings["allow_stopovers"] is True
    assert both_settings["allow_connections"] is True


@pytest.mark.parametrize(
    "dep,arr,date,price",
    [
        ("PEK", "SHA", "2000-01-01", "500"),
        ("PEK", "SHA", "2030-02-30", "500"),
        ("PEK", "SHA", "2030-1-1", "500"),
        ("北京", "PEK", FUTURE, "500"),
        ("PEK", "SHA", FUTURE, "0"),
        ("PEK", "SHA", FUTURE, "500.1"),
    ],
)
def test_invalid_commands(dep, arr, date, price):
    with pytest.raises(ValueError):
        validate_settings(dep, arr, date, target_price=price)


async def test_real_command_parser_and_create_status_cancel(plugin):
    command = CommandFilter(
        "机票监控", handler_md=SimpleNamespace(handler=JipiaoPlugin.set_monitor)
    )
    kwargs = command.validate_and_convert_params(
        ["PEK", "SHA", FUTURE, "-", "-", "500"], command.handler_params
    )
    replies = [reply async for reply in plugin.set_monitor(Event(), **kwargs)]
    assert "目标价模式" in replies[0]
    assert "尚未查询" in (await anext(plugin.my_monitor(Event())))
    assert "覆盖" in (await anext(plugin.set_monitor(Event(), **kwargs)))
    assert "已停止" in (await anext(plugin.cancel_monitor(Event())))
    assert "没有监控" in (await anext(plugin.my_monitor(Event())))


@pytest.mark.parametrize(
    "policy,expected_scope,expected_stopovers,expected_connections",
    [
        ("接受经停", "直飞、经停", 1, 0),
        ("接受中转", "直飞、中转", 0, 1),
        ("接受经停与中转", "直飞、经停、中转", 1, 1),
    ],
)
async def test_command_explicit_stop_permission(
    plugin, policy, expected_scope, expected_stopovers, expected_connections
):
    command = CommandFilter(
        "机票监控", handler_md=SimpleNamespace(handler=JipiaoPlugin.set_monitor)
    )
    kwargs = command.validate_and_convert_params(
        ["PEK", "SHA", FUTURE, "-", "-", "500", policy], command.handler_params
    )
    response = await anext(plugin.set_monitor(Event(), **kwargs))
    assert expected_scope in response
    task = plugin.db.get_all_tasks()[0]
    assert task["allow_stopovers"] == expected_stopovers
    assert task["allow_connections"] == expected_connections
    assert expected_scope in await anext(plugin.my_monitor(Event()))


async def test_invalid_replacement_preserves_existing_task(plugin):
    await anext(plugin.set_monitor(Event(), "PEK", "SHA", FUTURE, target_price="500"))
    before = plugin.db.get_all_tasks()[0]
    response = await anext(plugin.set_monitor(Event(), "PEK", "SHA", "bad"))
    assert "用法" in response
    assert plugin.db.get_all_tasks()[0] == before


async def test_adapter_wraps_text_as_real_message_chain(plugin):
    await plugin._send_message(Event.unified_msg_origin, "test")
    chain = plugin.context.send_message.call_args.args[1]
    assert isinstance(chain, MessageChain)
    assert chain.chain[0].text == "test"
    assert chain.use_markdown_ is False
    plugin.context.send_message.return_value = False
    with pytest.raises(RuntimeError):
        await plugin._send_message(Event.unified_msg_origin, "test")


async def test_qq_group_restart_does_not_silently_consume_notification(plugin):
    from astrbot_plugin_jipiao.scheduler import NotificationNotReady

    platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="qq", name="qq_official"),
        _session_scene={},
        _allow_group_proactive_send=True,
    )
    plugin.context.platform_manager.get_insts = lambda: [platform]
    with pytest.raises(NotificationNotReady):
        await plugin._send_message("qq:GroupMessage:user_group", "test")
    plugin.context.send_message.assert_not_awaited()
    platform._session_scene["group"] = "group"
    await plugin._send_message("qq:GroupMessage:user_group", "test")
    assert plugin.context.send_message.await_count == 1


@pytest.mark.parametrize("config", [{}, {"data_source": "ctrip"}])
async def test_ctrip_upgrade_preserves_complete_existing_tasks(monkeypatch, tmp_path, config):
    """Open an existing v0.3.3 database, rather than creating replacement tasks."""
    import json

    old_db = FlightDB(str(tmp_path / "flights.sqlite3"))
    for user_id, target in [('["qq","1"]', 500), ('["qq","2"]', None)]:
        old_db.upsert_task(
            user_id,
            "qq:GroupMessage:existing_group",
            "北京",
            "上海",
            FUTURE,
            time_window="08:00-18:00",
            airlines="CA,MU",
            target_price=target,
            allow_stopovers=True,
            allow_connections=False,
        )
        old_db.update_lowest_3(user_id, '[{"p":400,"t":1770000000},{"p":420,"t":1770030000}]')
        old_db.update_last_pushed_price(user_id, 450)
        old_db.update_check(
            user_id,
            1770040000,
            "查询成功",
            json.dumps({"price": 440, "source": "Google Flights / fli"}),
        )
    before = old_db.get_all_tasks()
    old_db.close()

    monkeypatch.setattr(StarTools, "get_data_dir", lambda *args: tmp_path)
    context = SimpleNamespace(
        send_message=AsyncMock(return_value=True),
        platform_manager=SimpleNamespace(get_insts=lambda: []),
    )
    instance = JipiaoPlugin(context, config)
    assert instance.mock is False
    assert instance.scheduler.fetcher.func is fetch_raw_flights
    assert instance.scheduler.fetcher.keywords == {
        "profile_dir": tmp_path / "browser" / "ctrip",
        "storage_state": tmp_path / "cookies" / "ctrip_state.json",
    }
    assert instance.db.get_all_tasks() == before  # Every setting, state, revision and timestamp.
    assert not (tmp_path / "mock.sqlite3").exists()

    # Continue the existing target-price task, including the existing session/scope
    # and lower-only push rule. No actual QQ message is sent.
    quote = {
        "price": 440,
        "flight_no": "CA123",
        "airline": "国航",
        "airline_code": "CA",
        "dep_time": "09:00",
        "arr_time": "11:00",
        "arr_date": FUTURE,
        "dep_airport": "PEK",
        "arr_airport": "PVG",
        "itinerary_type": "nonstop",
        "duration_minutes": 120,
        "stops": 0,
        "source": "携程 / Ctrip",
    }
    instance.scheduler.fetcher = AsyncMock(return_value=[quote])
    await instance.scheduler._process_one(before[0])
    instance.scheduler.fetcher.assert_awaited_once_with(
        "北京", "上海", FUTURE, allow_stopovers=True, allow_connections=False
    )
    assert context.send_message.call_args.args[0] == "qq:GroupMessage:existing_group"
    assert instance.db.get_task(before[0]["user_id"])["last_pushed_price"] == 440
    assert instance.db.get_task(before[0]["user_id"])["lowest_3_json"] == before[0]["lowest_3_json"]
    instance.db.close()


@pytest.mark.parametrize("source", ["fli", "tuniu"])
def test_legacy_source_requires_explicit_configuration(monkeypatch, source):
    lookup = Mock()
    monkeypatch.setattr(StarTools, "get_data_dir", lookup)
    with pytest.raises(ValueError, match="ctrip"):
        JipiaoPlugin(SimpleNamespace(), {"data_source": source})
    lookup.assert_not_called()
