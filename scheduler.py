"""Serial background polling with revision checks and cancellable lifecycle."""

import asyncio
import json
import logging
import random
import time

from .algorithm import evaluate_flight, flight_timestamp, task_phase
from .fetcher import describe_itinerary, fetch_raw_flights, filter_and_get_lowest

logger = logging.getLogger(__name__)


class NotificationNotReady(RuntimeError):
    """A known delivery precondition failed; its message is safe for users."""


class MonitorScheduler:
    def __init__(
        self,
        db,
        send_msg_callback,
        *,
        fetcher=fetch_raw_flights,
        round_interval=1800,
        task_delay=30,
        mock=False,
    ):
        self.db = db
        self.send_msg_callback = send_msg_callback
        self.fetcher = fetcher
        self.round_interval = round_interval
        self.task_delay = task_delay
        self.mock = mock
        self.running = False
        self._task = None
        self._locks = {}
        self._wake = asyncio.Event()

    def user_lock(self, user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    def start(self):
        if self._task is None or self._task.done():
            self.running = True
            self._task = asyncio.create_task(self._loop(), name="jipiao-monitor")

    async def stop(self):
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def wake(self):
        # Only idle polling is interrupted; inter-task rate limiting stays intact.
        self._wake.set()

    async def _loop(self):
        while self.running:
            self._wake.clear()
            try:
                tasks = self.db.get_all_tasks()
                for task in tasks:
                    if not self.running:
                        return
                    try:
                        await self._process_one(task)
                    except Exception:
                        logger.exception("Flight monitor task failed")
                    await asyncio.sleep(
                        random.uniform(max(0, self.task_delay - 1), self.task_delay + 2)
                    )
            except Exception:
                logger.exception("Flight monitor round failed")
            # New tasks can wake an idle scheduler without causing existing users
            # to be polled faster: last_checked_at is checked in _process_one.
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.round_interval)
            except TimeoutError:
                pass

    async def _process_one(self, task: dict):
        user_id = task["user_id"]
        async with self.user_lock(user_id):
            current = self.db.get_task(user_id)
            if current is None or current["revision"] != task["revision"]:
                return
            task = current
            now = int(time.time())
            if now >= flight_timestamp(task["flight_date"]):
                self.db.delete_task(user_id)
                await self.send_msg_callback(
                    task["session_id"],
                    ("【模拟模式】\n" if self.mock else "")
                    + f"机票监控已结束：{task['dep_city']} → {task['arr_city']}，{task['flight_date']}。\n"
                    + "已到出发日北京时间零点，任务已删除。",
                )
                return
            if (
                task["last_checked_at"] is not None
                and now - task["last_checked_at"] < self.round_interval
            ):
                return
            if task_phase(task, now)[0].startswith("等待期"):
                self.db.update_check(user_id, now, "等待起飞前60天，暂不查询", None)
                return

        # No lock is held during I/O, so users can replace or cancel immediately.
        try:
            flights = await self.fetcher(
                task["dep_city"],
                task["arr_city"],
                task["flight_date"],
                allow_stopovers=bool(task["allow_stopovers"]),
                allow_connections=bool(task["allow_connections"]),
            )
            best = filter_and_get_lowest(flights, task["time_window"], task["airlines"])
        except Exception as exc:
            logger.exception("Flight data query failed")
            async with self.user_lock(user_id):
                current = self.db.get_task(user_id)
                if current and current["revision"] == task["revision"]:
                    self.db.update_check(
                        user_id,
                        int(time.time()),
                        f"查询失败（{type(exc).__name__}），下轮重试",
                        None,
                    )
            return

        async with self.user_lock(user_id):
            current = self.db.get_task(user_id)
            if current is None or current["revision"] != task["revision"]:
                return
            now = int(time.time())
            decision = evaluate_flight(current, best, now)
            if decision["is_expired"]:
                self.db.delete_task(user_id)
                await self.send_msg_callback(task["session_id"], "机票监控已到期结束，任务已删除。")
                return
            best_json = json.dumps(best, ensure_ascii=False) if best else None
            self.db.update_check(
                user_id, now, "查询成功" if best else "本次未查到符合条件的报价", best_json
            )
            if decision["new_lowest_3_json"] is not None:
                self.db.update_lowest_3(user_id, decision["new_lowest_3_json"])
            if not decision["should_notify"]:
                return
            prefix = "【模拟数据测试，不可用于购票】" if self.mock else "【机票低价提醒】"
            text = (
                f"{prefix}\n{task['dep_city']} → {task['arr_city']}｜{task['flight_date']}\n"
                f"{best['flight_no']} {best['airline']}\n"
                f"起飞 {best['dep_time']} → 到达 {best.get('arr_date', task['flight_date'])} {best['arr_time']}\n"
                f"{describe_itinerary(best)}\n"
                f"本次匹配最低价：¥{best['price']} / 成人\n"
                f"{decision['notify_reason']}\n"
                f"数据来源：{best.get('source', 'Google Flights / fli')}\n"
                "单程经济舱；报价向上取整，实际价格和税费以购票页面为准。\n"
                "仅价格进一步降低时再次提醒；/取消监控 可停止。"
            )
            try:
                sent = await self.send_msg_callback(task["session_id"], text)
                if sent is False:
                    raise RuntimeError("Message platform was not found")
            except Exception as exc:
                logger.exception("Flight notification failed")
                detail = str(exc) if isinstance(exc, NotificationNotReady) else type(exc).__name__
                self.db.update_check(
                    user_id,
                    now,
                    f"报价已查到，推送失败（{detail}），下轮重试",
                    best_json,
                )
                return
            # Sending and updating share the same lock as replace/cancel commands.
            # A callback success means handed to the adapter, not a read receipt.
            self.db.update_last_pushed_price(user_id, decision["new_pushed_price"])
