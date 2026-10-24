"""Observation transition delivery uses isolated databases and mocked adapters."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_jipiao.algorithm import DAY, flight_timestamp, observation_finished, task_phase
from astrbot_plugin_jipiao.database import FlightDB
from astrbot_plugin_jipiao.scheduler import MonitorScheduler

FLIGHT = flight_timestamp("2030-12-01")
LOWEST = '[{"p":400,"t":1},{"p":450,"t":2},{"p":500,"t":3}]'
QUOTE = {
    "price": 420,
    "flight_no": "CA123",
    "airline": "国航",
    "dep_time": "09:00",
    "arr_time": "11:00",
    "itinerary_type": "nonstop",
    "dep_airport": "PEK",
    "arr_airport": "SHA",
}


@pytest.fixture
def observed(tmp_path, monkeypatch):
    clock = [FLIGHT - 60 * DAY]
    monkeypatch.setattr("astrbot_plugin_jipiao.scheduler.time.time", lambda: clock[0])
    db = FlightDB(str(tmp_path / "observed.sqlite3"))
    db.upsert_task('["qq","1"]', "qq:GroupMessage:group", "PEK", "SHA", "2030-12-01")
    db.update_lowest_3('["qq","1"]', LOWEST)
    _, deadline = task_phase(db.get_all_tasks()[0], clock[0])
    clock[0] = deadline + 1
    yield db, clock
    db.close()


async def test_summary_before_fetch_and_recent_check_does_not_block_it(observed):
    db, clock = observed
    task = db.get_all_tasks()[0]
    db.update_check(task["user_id"], clock[0] - 1, "查询成功", None)
    send, summary, fetch = AsyncMock(), AsyncMock(), AsyncMock()
    scheduler = MonitorScheduler(db, send, send_observation_callback=summary, fetcher=fetch)
    await scheduler._process_one(task)
    summary.assert_awaited_once()
    assert summary.call_args.args[0]["user_id"] == task["user_id"]
    message = summary.call_args.args[1]
    assert "观察期已结束，已进入决策期" in message
    assert "1. ¥400\n2. ¥450\n3. ¥500" in message
    assert "低于第二低价 ¥450" in message
    fetch.assert_not_awaited()
    send.assert_not_awaited()
    current = db.get_task(task["user_id"])
    assert current["observation_notified_at"] == clock[0]
    assert current["last_pushed_price"] is None
    assert current["lowest_3_json"] == LOWEST


async def test_summary_persisted_across_scheduler_and_database_restart(observed):
    db, clock = observed
    send = AsyncMock()
    await MonitorScheduler(db, send, fetcher=AsyncMock(return_value=[]))._process_one(
        db.get_all_tasks()[0]
    )
    path = db.conn.execute("PRAGMA database_list").fetchone()[2]
    reopened = FlightDB(path)
    try:
        clock[0] += 7200
        await MonitorScheduler(reopened, send, fetcher=AsyncMock(return_value=[]))._process_one(
            reopened.get_all_tasks()[0]
        )
        send.assert_awaited_once()
        assert reopened.get_all_tasks()[0]["observation_notified_at"] is not None
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", [False, RuntimeError("offline")])
async def test_summary_failure_retries_once_per_visit_and_does_not_consume_price(observed, failure):
    db, _ = observed
    summary = AsyncMock(return_value=False) if failure is False else AsyncMock(side_effect=failure)
    send = AsyncMock()
    scheduler = MonitorScheduler(
        db,
        send,
        send_observation_callback=summary,
        fetcher=AsyncMock(return_value=[]),
        round_interval=0,
    )
    await scheduler._process_one(db.get_all_tasks()[0])
    summary.assert_awaited_once()
    assert db.get_all_tasks()[0]["observation_notified_at"] is None
    assert db.get_all_tasks()[0]["last_pushed_price"] is None
    summary.side_effect = None
    summary.return_value = True
    await scheduler._process_one(db.get_all_tasks()[0])
    await scheduler._process_one(db.get_all_tasks()[0])
    assert summary.await_count == 2
    assert db.get_all_tasks()[0]["observation_notified_at"] is not None
    send.assert_not_awaited()


@pytest.mark.parametrize("count", [0, 1, 2])
async def test_summary_missing_prices_are_explicit(observed, count):
    db, _ = observed
    task = db.get_all_tasks()[0]
    db.update_lowest_3(task["user_id"], json.dumps(json.loads(LOWEST)[:count]))
    send = AsyncMock()
    await MonitorScheduler(db, send, fetcher=AsyncMock(return_value=[]))._process_one(task)
    message = send.call_args.args[1]
    assert message.count("暂无记录") == 3 - count
    if count == 0:
        assert "没有有效报价" in message
    if count == 1:
        assert "唯一已记录价格" in message
        assert "¥450" not in message


@pytest.mark.parametrize("fetch_failure", [False, True])
async def test_transition_during_fetch_still_sends_summary(observed, fetch_failure):
    db, clock = observed
    clock[0] -= 1  # Exact deadline is still observation.
    order = []

    async def fetch(*args, **kwargs):
        order.append("fetch")
        clock[0] += 1
        if fetch_failure:
            raise TimeoutError("network")
        return []

    async def send(*args):
        order.append("summary")

    await MonitorScheduler(db, send, fetcher=fetch)._process_one(db.get_all_tasks()[0])
    assert order == ["fetch", "summary"]
    assert db.get_all_tasks()[0]["observation_notified_at"] == clock[0]
    assert db.get_all_tasks()[0]["lowest_3_json"] == LOWEST


async def test_quote_error_does_not_prevent_already_due_summary(observed):
    db, _ = observed
    send = AsyncMock()

    async def fetch(*args, **kwargs):
        send.assert_awaited_once()
        raise TimeoutError("network")

    await MonitorScheduler(db, send, fetcher=fetch)._process_one(db.get_all_tasks()[0])
    assert db.get_all_tasks()[0]["observation_notified_at"] is not None
    assert "查询失败" in db.get_all_tasks()[0]["last_status"]


@pytest.mark.parametrize(
    "mode", ["target", "expired", "created_in_fallback", "observing", "waiting"]
)
async def test_tasks_without_completed_observation_do_not_send_summary(observed, mode):
    db, clock = observed
    task = db.get_all_tasks()[0]
    if mode == "expired":
        clock[0] = FLIGHT
    elif mode == "created_in_fallback":
        clock[0] = FLIGHT - 6 * DAY
        with db.conn:
            db.conn.execute("UPDATE tasks SET created_at=?", (clock[0],))
    elif mode == "target":
        with db.conn:
            db.conn.execute("UPDATE tasks SET target_price=500")
    elif mode == "observing":
        clock[0] = task_phase(task, FLIGHT - 60 * DAY)[1]
    else:
        clock[0] = FLIGHT - 61 * DAY
    summary = AsyncMock()
    await MonitorScheduler(
        db, AsyncMock(), send_observation_callback=summary, fetcher=AsyncMock(return_value=[])
    )._process_one(db.get_all_tasks()[0])
    summary.assert_not_awaited()


async def test_seven_day_boundary_ends_short_observation(observed):
    db, clock = observed
    clock[0] = FLIGHT - 7 * DAY - 3600
    task = db.get_all_tasks()[0]
    with db.conn:
        db.conn.execute("UPDATE tasks SET created_at=?", (clock[0],))
    assert not observation_finished(db.get_all_tasks()[0], clock[0])
    clock[0] += 3600
    send = AsyncMock()
    await MonitorScheduler(db, send, fetcher=AsyncMock(return_value=[]))._process_one(task)
    assert "已进入起飞前7天兜底期" in send.call_args.args[1]
    assert db.get_all_tasks()[0]["observation_notified_at"] is not None


@pytest.mark.parametrize("replace", [False, True])
async def test_transition_after_cancel_or_replace_does_not_send_stale_summary(observed, replace):
    db, clock = observed
    clock[0] -= 1
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return [QUOTE]

    summary, send = AsyncMock(), AsyncMock()
    scheduler = MonitorScheduler(db, send, send_observation_callback=summary, fetcher=fetch)
    task = db.get_all_tasks()[0]
    job = asyncio.create_task(scheduler._process_one(task))
    await entered.wait()
    clock[0] += 1
    async with scheduler.user_lock(task["user_id"]):
        if replace:
            db.upsert_task(task["user_id"], "s2", "CAN", "SHA", "2030-12-01")
        else:
            db.delete_task(task["user_id"])
    release.set()
    await job
    summary.assert_not_awaited()
    send.assert_not_awaited()


async def test_decision_summary_and_changed_qualifying_prices_are_independent(observed):
    db, _ = observed
    send, summary = AsyncMock(), AsyncMock()
    fetch = AsyncMock(return_value=[QUOTE])
    scheduler = MonitorScheduler(
        db, send, send_observation_callback=summary, fetcher=fetch, round_interval=0
    )
    for price in [400, 420, 420, 450]:
        fetch.return_value = [{**QUOTE, "price": price}]
        await scheduler._process_one(db.get_all_tasks()[0])
    summary.assert_awaited_once()
    assert send.await_count == 2
    assert "¥400" in send.await_args_list[0].args[1]
    assert "¥420" in send.await_args_list[1].args[1]
    assert db.get_all_tasks()[0]["last_pushed_price"] == 420


def test_completion_marker_migrates_without_resetting_task_and_resets_on_replacement(observed):
    db, clock = observed
    task = db.get_all_tasks()[0]
    # Reproduce the immediately preceding schema without this new column.
    with db.conn:
        db.conn.execute("ALTER TABLE tasks DROP COLUMN observation_notified_at")
    path = db.conn.execute("PRAGMA database_list").fetchone()[2]
    migrated = FlightDB(path)
    try:
        assert migrated.get_all_tasks()[0] == task
        migrated.mark_observation_notified(task["user_id"], clock[0])
        assert migrated.get_all_tasks()[0]["observation_notified_at"] == clock[0]
        migrated.upsert_task(task["user_id"], "new-session", "CAN", "SHA", "2030-12-01")
        assert migrated.get_all_tasks()[0]["observation_notified_at"] is None
    finally:
        migrated.close()
