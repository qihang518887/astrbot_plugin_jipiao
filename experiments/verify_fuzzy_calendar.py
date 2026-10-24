"""Verify one same-month calendar candidate using a disposable Ctrip session copy."""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_jipiao.algorithm import CHINA_TZ
from astrbot_plugin_jipiao.calendar_search import (
    fetch_low_price_calendar,
    flexible_date_window,
    select_calendar_lowest,
)
from astrbot_plugin_jipiao.fetcher import fetch_raw_flights, filter_and_get_lowest


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dep", default="大连")
    parser.add_argument("--arr", default="上海")
    parser.add_argument("--center", default="2026-11-05")
    parser.add_argument("--message", default="11月5日左右从大连到上海")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    window = flexible_date_window(args.message, args.center, None)
    if window is None:
        raise ValueError("This request is not eligible for calendar discovery")
    report = {
        "started_at": datetime.now(CHINA_TZ).isoformat(),
        "route": [args.dep, args.arr],
        "center": args.center,
        "window": list(window),
        "calendar_calls": 1,
        "detail_calls": 0,
        "monitor_database_accessed": False,
        "qq_message_sent": False,
        "original_session_modified": False,
    }
    try:
        days = await fetch_low_price_calendar(args.dep, args.arr, *window)
        report["calendar_days"] = days
        candidate = select_calendar_lowest(days, args.center)
        report["calendar_candidate"] = candidate
        if candidate is not None:
            with tempfile.TemporaryDirectory(prefix="jipiao-calendar-check-") as temporary:
                path = Path(temporary)
                state = path / "state.json"
                shutil.copyfile(args.state, state)
                report["detail_calls"] = 1
                flights = await fetch_raw_flights(
                    args.dep,
                    args.arr,
                    candidate["date"],
                    profile_dir=path / "profile",
                    storage_state=state,
                )
                best = filter_and_get_lowest(flights, None, None)
                report["detail_quote_count"] = len(flights)
                report["verified_best"] = best
                report["calendar_price_matched"] = bool(
                    best and best["price"] == candidate["price"]
                )
        report["completed"] = True
    except Exception as exc:
        report["completed"] = False
        report["error_type"] = type(exc).__name__
        # Ctrip/Calendar query errors are intentionally safe user-facing errors.
        if type(exc).__name__ in {"CtripQueryError", "CalendarQueryError"}:
            report["error"] = str(exc)
    report["finished_at"] = datetime.now(CHINA_TZ).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
