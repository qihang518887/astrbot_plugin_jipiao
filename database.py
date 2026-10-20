"""SQLite persistence; all access stays on the event-loop thread."""

import sqlite3
import time
import uuid
from pathlib import Path


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
                created_at INTEGER NOT NULL,
                lowest_3_json TEXT DEFAULT '[]',
                last_pushed_price INTEGER DEFAULT NULL,
                revision TEXT NOT NULL DEFAULT '',
                last_checked_at INTEGER DEFAULT NULL,
                last_status TEXT DEFAULT NULL,
                last_best_json TEXT DEFAULT NULL
            )
        """)
        # Accept databases created with the original eleven-column proposal.
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        for name, sql_type in {
            "revision": "TEXT NOT NULL DEFAULT ''",
            "last_checked_at": "INTEGER DEFAULT NULL",
            "last_status": "TEXT DEFAULT NULL",
            "last_best_json": "TEXT DEFAULT NULL",
        }.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}")
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
    ):
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO tasks
                (user_id, session_id, dep_city, arr_city, flight_date,
                 time_window, airlines, target_price, created_at, revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    session_id,
                    dep_city,
                    arr_city,
                    flight_date,
                    time_window,
                    airlines,
                    target_price,
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
