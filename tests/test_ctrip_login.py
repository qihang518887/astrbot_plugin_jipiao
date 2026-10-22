"""QR acquisition, confirmation and plugin lifecycle without real accounts or QQ sends."""

import asyncio
import copy
import json
import sys
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_jipiao import ctrip_login
from astrbot_plugin_jipiao.main import JipiaoPlugin
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


@pytest.fixture
def page_context():
    page = Mock()
    page.goto = AsyncMock()
    page.wait_for_url = AsyncMock()
    page.get_by_role.return_value.click = AsyncMock()
    canvas = page.get_by_test_id.return_value.locator.return_value
    canvas.wait_for = AsyncMock()
    canvas.screenshot = AsyncMock(return_value=b"official-qr-png")
    state = {
        "cookies": [
            {"domain": ".ctrip.com", "name": "session", "value": "test-private"},
            {"domain": ".example.com", "name": "unrelated", "value": "excluded"},
        ],
        "origins": [
            {"origin": "https://m.ctrip.com", "localStorage": [], "indexedDB": [{"name": "cache"}]}
        ],
    }
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        storage_state=AsyncMock(side_effect=lambda **kwargs: copy.deepcopy(state)),
        add_init_script=AsyncMock(),
        close=AsyncMock(),
    )
    return page, context


async def test_qr_requires_official_confirmation_before_replacing_session(page_context, tmp_path):
    page, context = page_context
    target = tmp_path / "cookies" / "ctrip_state.json"
    target.parent.mkdir()
    target.write_text("old-session", encoding="utf-8")
    login = ctrip_login.CtripQRLogin(context)
    assert await login.start() == b"official-qr-png"
    page.get_by_role.assert_called_once_with("link", name="扫码登录", exact=True)
    page.get_by_test_id.assert_called_with("qrCodeBox")
    assert target.read_text(encoding="utf-8") == "old-session"
    context.storage_state.assert_not_awaited()
    await login.wait_and_save(target)
    page.wait_for_url.assert_awaited_once_with(
        "https://www.ctrip.com/", timeout=ctrip_login.LOGIN_TIMEOUT_MS
    )
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert [c["name"] for c in saved["cookies"]] == ["session"]
    assert "indexedDB" not in saved["origins"][0]
    context.storage_state.assert_awaited_once_with(indexed_db=False)
    assert not target.with_suffix(".tmp").exists()


async def test_expired_or_cancelled_login_never_exports_session(page_context, tmp_path):
    page, context = page_context
    target = tmp_path / "ctrip_state.json"
    target.write_text("old-session", encoding="utf-8")
    login = ctrip_login.CtripQRLogin(context)
    await login.start()
    for error in (PlaywrightTimeoutError("expired"), asyncio.CancelledError()):
        page.wait_for_url.side_effect = error
        with pytest.raises(type(error)):
            await login.wait_and_save(target)
        assert target.read_text(encoding="utf-8") == "old-session"
    context.storage_state.assert_not_awaited()


@pytest.mark.parametrize("cancel", [False, True])
async def test_isolated_browser_always_closes_and_removes_temp_profile(
    page_context, monkeypatch, cancel
):
    from pathlib import Path

    _, context = page_context
    chromium = SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context))

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=chromium)

    monkeypatch.setitem(
        sys.modules, "playwright.async_api", SimpleNamespace(async_playwright=playwright)
    )
    with pytest.raises(asyncio.CancelledError) if cancel else nullcontext():
        async with ctrip_login.qr_login():
            args = chromium.launch_persistent_context.call_args.kwargs
            profile = Path(args["user_data_dir"])
            assert profile.is_dir()
            assert args["headless"] is True
            assert args["is_mobile"] is False
            if cancel:
                raise asyncio.CancelledError()
    context.close.assert_awaited_once()
    assert not profile.exists()


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    from astrbot.api.star import StarTools

    monkeypatch.setattr(StarTools, "get_data_dir", lambda *args: tmp_path)
    instance = JipiaoPlugin(SimpleNamespace(send_message=AsyncMock()), {})
    yield instance
    instance.db.close()


def event(private=True):
    return SimpleNamespace(
        is_private_chat=lambda: private,
        plain_result=lambda text: text,
        chain_result=lambda chain: chain,
    )


@pytest.fixture
def login_mock(monkeypatch):
    login = SimpleNamespace(start=AsyncMock(return_value=b"qr-image"), wait_and_save=AsyncMock())
    closed = Mock()

    @asynccontextmanager
    async def browser():
        try:
            yield login
        finally:
            closed()

    factory = Mock(side_effect=browser)
    monkeypatch.setattr("astrbot_plugin_jipiao.main.qr_login", factory)
    return login, closed, factory


async def test_group_and_busy_login_do_not_start_browser(plugin, login_mock):
    _, _, factory = login_mock
    replies = [r async for r in plugin.login_ctrip(event(private=False))]
    assert "私聊" in replies[0]
    async with plugin.login_lock:
        replies = [r async for r in plugin.login_ctrip(event())]
    assert "进行中" in replies[0]
    factory.assert_not_called()


async def test_login_sends_image_and_retains_every_monitor_field(plugin, login_mock):
    login, closed, _ = login_mock
    plugin.db.upsert_task(
        "u", "qq:GroupMessage:original", "大连", "上海", "2099-12-01", target_price=500
    )
    before = plugin.db.get_all_tasks()
    replies = [r async for r in plugin.login_ctrip(event())]
    assert "携程 App" in replies[0]
    assert replies[1][0].type == "Image"
    assert "登录成功" in replies[-1]
    login.wait_and_save.assert_awaited_once_with(plugin.ctrip_state)
    closed.assert_called_once()
    assert plugin.login_task is None
    assert not plugin.login_lock.locked()
    assert plugin.db.get_all_tasks() == before
    plugin.context.send_message.assert_not_awaited()


async def test_timeout_closes_browser_and_reports_failure(plugin, login_mock):
    login, closed, _ = login_mock
    login.wait_and_save.side_effect = PlaywrightTimeoutError("expired")
    replies = [r async for r in plugin.login_ctrip(event())]
    assert "超时" in replies[-1]
    assert "未替换原会话" in replies[-1]
    assert plugin.login_task is None
    closed.assert_called_once()


async def test_plugin_unload_cancels_pending_login_and_closes_browser(plugin, login_mock):
    login, closed, _ = login_mock
    waiting = asyncio.Event()

    async def wait(path):
        waiting.set()
        await asyncio.Future()

    login.wait_and_save.side_effect = wait

    async def consume():
        return [r async for r in plugin.login_ctrip(event())]

    task = asyncio.create_task(consume())
    await waiting.wait()
    await plugin.terminate()
    assert task.cancelled()
    assert plugin.login_task is None
    closed.assert_called_once()


async def test_closing_command_after_qr_send_closes_browser(plugin, login_mock):
    _, closed, _ = login_mock
    command = plugin.login_ctrip(event())
    await anext(command)
    await anext(command)
    await command.aclose()
    closed.assert_called_once()
    assert plugin.login_task is None
    assert not plugin.login_lock.locked()


async def test_query_and_login_share_the_same_confirmed_state(page_context, monkeypatch, tmp_path):
    """Exercise the actual browser storage importer, not only a path assertion."""
    from astrbot_plugin_jipiao import ctrip

    _, context = page_context
    path = tmp_path / "ctrip_state.json"
    login = ctrip_login.CtripQRLogin(context)
    await login.start()
    await login.wait_and_save(path)
    imported = []
    sync_context = Mock()
    sync_context.set_storage_state.side_effect = lambda state: imported.append(state)
    playwright = Mock()
    playwright.chromium.launch_persistent_context.return_value = sync_context
    manager = Mock()
    manager.__enter__ = Mock(return_value=playwright)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setitem(
        sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=lambda: manager)
    )
    with ctrip.browser(tmp_path / "query_profile", path):
        pass
    assert imported[0]["cookies"][0]["name"] == "session"
    sync_context.close.assert_called_once()
