"""Run one explicit query without creating tasks or sending any messages."""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from .algorithm import CHINA_TZ
from .fetcher import (
    fetch_mock_flights,
    fetch_raw_flights,
    filter_and_get_lowest,
    normalize_airlines,
    validate_time_window,
)


async def main():
    parser = argparse.ArgumentParser(description="机票插件单次查询；不会发送 QQ 消息")
    parser.add_argument("--dep", default="PEK")
    parser.add_argument("--arr", default="SHA")
    parser.add_argument(
        "--date", default=(datetime.now(CHINA_TZ) + timedelta(days=30)).strftime("%Y-%m-%d")
    )
    parser.add_argument("--time-window", default=None)
    parser.add_argument("--airlines", default=None)
    parser.add_argument("--mock", action="store_true", help="使用明确标注的模拟报价")
    parser.add_argument("--profile-dir", type=Path, help="携程持久化浏览器目录")
    parser.add_argument("--storage-state", type=Path, help="携程导出的会话文件")
    parser.add_argument("--allow-stopovers", action="store_true", help="允许经停，默认仅直飞")
    parser.add_argument("--allow-connections", action="store_true", help="允许中转，默认仅直飞")
    parser.add_argument("--allow-stops", action="store_true", help="允许经停与中转，默认仅直飞")
    args = parser.parse_args()
    window = validate_time_window(args.time_window)
    airlines = normalize_airlines(args.airlines)
    fetcher = fetch_mock_flights if args.mock else fetch_raw_flights
    if not args.mock:
        fetcher = partial(
            fetch_raw_flights, profile_dir=args.profile_dir, storage_state=args.storage_state
        )
    if args.allow_stops:
        args.allow_stopovers = True
        args.allow_connections = True
    flights = await fetcher(
        args.dep,
        args.arr,
        args.date,
        allow_stopovers=args.allow_stopovers,
        allow_connections=args.allow_connections,
    )
    print(
        json.dumps(
            {
                "mode": "mock" if args.mock else "real",
                "route": [args.dep, args.arr],
                "date": args.date,
                "checked_at": datetime.now(CHINA_TZ).isoformat(),
                "returned_count": len(flights),
                "allow_stopovers": args.allow_stopovers,
                "allow_connections": args.allow_connections,
                "best": filter_and_get_lowest(flights, window, airlines),
                "note": "模拟数据，不可购票"
                if args.mock
                else "本次返回结果中的最低价，不代表全网最低价",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
