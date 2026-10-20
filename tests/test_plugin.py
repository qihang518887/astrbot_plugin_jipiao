from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# These are real AstrBot imports, not framework stubs.
from astrbot.api.event import MessageChain
from astrbot.api.star import StarTools
from astrbot.core.star.filter.command import CommandFilter
from astrbot_plugin_jipiao.algorithm import CHINA_TZ
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
