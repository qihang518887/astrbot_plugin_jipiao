import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_jipiao.database import FlightDB
from astrbot_plugin_jipiao.scheduler import MonitorScheduler

QUOTE = {
    "itinerary_type": "nonstop",
    "flight_no": "CA123",
    "airline": "Air China",
    "dep_time": "09:30",
    "arr_time": "11:45",
    "price": 400,
    "dep_airport": "PEK",
    "arr_airport": "SHA",
    "duration_minutes": 135,
    "stops": 0,
    "layovers": [],
    "self_transfer": False,
}


@pytest.fixture
def db():
    database = FlightDB(":memory:")
    database.upsert_task("u1", "qq:FriendMessage:1", "PEK", "SHA", "2099-12-01", target_price=500)
    yield database
    database.close()


def test_upsert_resets_state_and_changes_revision(db):
    before = db.get_task("u1")
    db.update_lowest_3("u1", '[{"p":400,"t":1}]')
    db.update_last_pushed_price("u1", 400)
    db.update_check("u1", 1, "查询成功", json.dumps(QUOTE))
    db.upsert_task("u1", "qq:FriendMessage:2", "CAN", "SHA", "2099-12-01")
    after = db.get_task("u1")
    assert after["revision"] != before["revision"]
    assert after["lowest_3_json"] == "[]"
    assert after["last_pushed_price"] is None
    assert after["last_checked_at"] is None
    assert len(db.get_all_tasks()) == 1


def test_persistence(tmp_path):
    path = str(tmp_path / "task.sqlite3")
    first = FlightDB(path)
    first.upsert_task("u", "s", "PEK", "SHA", "2099-12-01", allow_stopovers=True)
    first.close()
    second = FlightDB(path)
    assert second.get_task("u")["dep_city"] == "PEK"
    assert second.get_task("u")["allow_stopovers"] == 1
    assert second.delete_task("u")
    assert not second.delete_task("u")
    second.close()


async def test_expired_task_deleted_without_network(db):
    db.upsert_task("u1", "qq:FriendMessage:1", "PEK", "SHA", "2000-01-01")
    fetch, send = AsyncMock(), AsyncMock()
    scheduler = MonitorScheduler(db, send, fetcher=fetch)
    await scheduler._process_one(db.get_task("u1"))
    fetch.assert_not_awaited()
    assert db.get_task("u1") is None
    assert "已结束" in send.call_args.args[1]


@pytest.mark.parametrize("failure", [False, RuntimeError("offline")])
async def test_failed_send_does_not_consume_low_price(db, failure):
    send = AsyncMock(return_value=failure) if failure is False else AsyncMock(side_effect=failure)
    scheduler = MonitorScheduler(
        db, send, fetcher=AsyncMock(return_value=[QUOTE]), round_interval=0
    )
    await scheduler._process_one(db.get_task("u1"))
    assert db.get_task("u1")["last_pushed_price"] is None
    assert "推送失败" in db.get_task("u1")["last_status"]
    send.side_effect = None
    send.return_value = True
    await scheduler._process_one(db.get_task("u1"))
    assert db.get_task("u1")["last_pushed_price"] == 400
    await scheduler._process_one(db.get_task("u1"))
    assert send.await_count == 2


async def test_empty_results_and_network_failure_have_distinct_status(db):
    fetch = AsyncMock(return_value=[])
    scheduler = MonitorScheduler(db, AsyncMock(), fetcher=fetch, round_interval=0)
    await scheduler._process_one(db.get_task("u1"))
    assert "未查到" in db.get_task("u1")["last_status"]
    fetch.assert_awaited_once_with(
        "PEK", "SHA", "2099-12-01", allow_stopovers=False, allow_connections=False
    )
    fetch.side_effect = TimeoutError("network")
    await scheduler._process_one(db.get_task("u1"))
    assert "查询失败" in db.get_task("u1")["last_status"]
    assert db.get_task("u1")["last_pushed_price"] is None


@pytest.mark.parametrize("replace", [False, True])
async def test_cancel_or_replace_during_fetch_prevents_stale_notification(db, replace):
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return [QUOTE]

    send = AsyncMock()
    scheduler = MonitorScheduler(db, send, fetcher=fetch)
    job = asyncio.create_task(scheduler._process_one(db.get_task("u1")))
    await entered.wait()
    async with scheduler.user_lock("u1"):
        if replace:
            db.upsert_task("u1", "s2", "CAN", "SHA", "2099-12-01", target_price=100)
        else:
            db.delete_task("u1")
    release.set()
    await job
    send.assert_not_awaited()
    if replace:
        assert db.get_task("u1")["last_checked_at"] is None


async def test_start_is_idempotent_and_stop_interrupts_idle_sleep(db):
    db.delete_task("u1")
    scheduler = MonitorScheduler(db, AsyncMock(), round_interval=1800)
    scheduler.start()
    first = scheduler._task
    scheduler.start()
    assert scheduler._task is first
    await asyncio.sleep(0)
    await asyncio.wait_for(scheduler.stop(), 1)
    assert first.done() and not scheduler.running


async def test_wake_does_not_requery_recent_tasks(db):
    fetch = AsyncMock(return_value=[])
    scheduler = MonitorScheduler(db, AsyncMock(), fetcher=fetch)
    await scheduler._process_one(db.get_task("u1"))
    await scheduler._process_one(db.get_task("u1"))
    assert fetch.await_count == 1


async def test_notification_preserves_connection_details(db, connecting_quote, monkeypatch):
    from astrbot_plugin_jipiao.algorithm import flight_timestamp

    monkeypatch.setattr(
        "astrbot_plugin_jipiao.scheduler.time.time", lambda: flight_timestamp("2026-11-01")
    )
    db.upsert_task(
        "u1",
        "qq:FriendMessage:1",
        "北京",
        "上海",
        "2026-12-01",
        target_price=2000,
        allow_connections=True,
    )
    send = AsyncMock(return_value=True)
    scheduler = MonitorScheduler(db, send, fetcher=AsyncMock(return_value=[connecting_quote]))
    await scheduler._process_one(db.get_task("u1"))
    send.assert_awaited_once()
    message = send.call_args.args[1]
    assert "KE2224 / KE2071" in message
    assert "到达 2026-12-02 09:40" in message
    assert "PUS 中转，停留 1110 分钟，过夜" in message
    assert "¥1718" in message
    assert db.get_task("u1")["last_pushed_price"] == 1718
    scheduler.fetcher.assert_awaited_once_with(
        "北京", "上海", "2026-12-01", allow_stopovers=False, allow_connections=True
    )


def test_old_database_defaults_to_direct_and_resets_mixed_price_history(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE tasks (
            user_id TEXT PRIMARY KEY, session_id TEXT, dep_city TEXT, arr_city TEXT,
            flight_date TEXT, time_window TEXT, airlines TEXT, target_price INTEGER,
            created_at INTEGER, lowest_3_json TEXT, last_pushed_price INTEGER
        )""")
        connection.execute("""INSERT INTO tasks VALUES (
            'u', 'session', 'PEK', 'SHA', '2099-12-01', NULL, NULL, 2000,
            1, '[{"p":1718,"t":1}]', 1718
        )""")
    database = FlightDB(str(path))
    task = database.get_task("u")
    assert task["allow_stopovers"] == 0
    assert task["session_id"] == "session"
    assert task["target_price"] == 2000
    assert task["lowest_3_json"] == "[]"
    assert task["last_pushed_price"] is None
    database.update_lowest_3("u", '[{"p":1770,"t":2}]')
    database.close()
    reopened = FlightDB(str(path))
    assert reopened.get_task("u")["lowest_3_json"] == '[{"p":1770,"t":2}]'
    reopened.close()
