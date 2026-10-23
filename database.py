"""SQLite persistence; all access stays on the event-loop thread."""

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .algorithm import normalize_lowest_3


class FlightDB:
    """Persist one task per platform-scoped user."""

    def __init__(self, db_path: str):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                user_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                dep_city TEXT NOT NULL,
                arr_city TEXT NOT NULL,
                flight_date TEXT NOT NULL,
                time_window TEXT DEFAULT NULL,
                airlines TEXT DEFAULT NULL,
                target_price INTEGER DEFAULT NULL,
                allow_stopovers INTEGER NOT NULL DEFAULT 0,
                allow_connections INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                lowest_3_json TEXT DEFAULT '[]',
                last_pushed_price INTEGER DEFAULT NULL,
                revision TEXT NOT NULL DEFAULT '',
                last_checked_at INTEGER DEFAULT NULL,
                last_status TEXT DEFAULT NULL,
                last_best_json TEXT DEFAULT NULL
            )
        """)
        # Add task settings to existing installations as the schema evolves.
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        for name, sql_type in {
            "revision": "TEXT NOT NULL DEFAULT ''",
            "last_checked_at": "INTEGER DEFAULT NULL",
            "last_status": "TEXT DEFAULT NULL",
            "last_best_json": "TEXT DEFAULT NULL",
            "allow_stopovers": "INTEGER NOT NULL DEFAULT 0",
            "allow_connections": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}")
        if "allow_stopovers" not in columns:
            # Earlier '接受经停' also allowed flight changes. Preserve the user's
            # stopover choice, disable connections, and discard the mixed baseline.
            if "allow_stops" in columns:
                self.conn.execute("UPDATE tasks SET allow_stopovers=allow_stops")
            reset_where = "WHERE allow_stops=1" if "allow_stops" in columns else ""
            self.conn.execute(
                """UPDATE tasks SET lowest_3_json='[]', last_pushed_price=NULL,
                last_best_json=NULL, last_checked_at=NULL,
                last_status='已分开经停与中转，按新范围重新观察',
                created_at=?, revision=lower(hex(randomblob(16))) """
                + reset_where,
                (int(time.time()),),
            )
        # Repair old six-hour deduplication results without resetting the task.
        for row in self.conn.execute("SELECT user_id, lowest_3_json FROM tasks").fetchall():
            recorded = json.loads(row["lowest_3_json"])
            lowest = normalize_lowest_3(recorded)
            if lowest != recorded:
                self.conn.execute(
                    "UPDATE tasks SET lowest_3_json=? WHERE user_id=?",
                    (json.dumps(lowest), row["user_id"]),
                )
        self.conn.commit()

    def upsert_task(
        self,
        user_id,
        session_id,
        dep_city,
        arr_city,
        flight_date,
        time_window=None,
        airlines=None,
        target_price=None,
        allow_stopovers=False,
        allow_connections=False,
    ):
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO tasks
                (user_id, session_id, dep_city, arr_city, flight_date,
                 time_window, airlines, target_price, allow_stopovers, allow_connections,
                 created_at, revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    session_id,
                    dep_city,
                    arr_city,
                    flight_date,
                    time_window,
                    airlines,
                    target_price,
                    int(allow_stopovers),
                    int(allow_connections),
                    int(time.time()),
                    uuid.uuid4().hex,
                ),
            )

    def get_task(self, user_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_all_tasks(self) -> list[dict]:
        return [
            dict(row)
            for row in self.conn.execute("SELECT * FROM tasks ORDER BY created_at, user_id")
        ]

    def delete_task(self, user_id: str) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM tasks WHERE user_id=?", (user_id,)).rowcount > 0

    def update_lowest_3(self, user_id: str, lowest_3_json: str):
        recorded = json.loads(lowest_3_json)
        lowest = normalize_lowest_3(recorded)
        if lowest != recorded:
            lowest_3_json = json.dumps(lowest)
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET lowest_3_json=? WHERE user_id=?", (lowest_3_json, user_id)
            )

    def update_last_pushed_price(self, user_id: str, price: int):
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET last_pushed_price=? WHERE user_id=?", (price, user_id)
            )

    def update_check(self, user_id: str, now_ts: int, status: str, best_json: str | None):
        with self.conn:
            self.conn.execute(
                "UPDATE tasks SET last_checked_at=?, last_status=?, last_best_json=? WHERE user_id=?",
                (now_ts, status, best_json, user_id),
            )

    def close(self):
        self.conn.close()
