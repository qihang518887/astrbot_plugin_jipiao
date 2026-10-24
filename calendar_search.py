"""Opt-in fuzzy-date discovery; calendar quotes never become monitoring records.

The Ctrip request shape is adapted from ticket-monitoring/crawlers/special.py
(Apache-2.0). See THIRD_PARTY_NOTICES.md and LICENSE.ctrip.
"""

import re
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation

import httpx

from .algorithm import CHINA_TZ
from .ctrip import CITY_CODES
from .fetcher import AIRLINE_ALIASES, resolve_airports
from .proxy import proxy_for_url

CALENDAR_API = "https://m.ctrip.com/restapi/soa2/15380/bjjson/FlightIntlAndInlandLowestPriceSearch"


class CalendarQueryError(RuntimeError):
    """A calendar failure safe to display without session or proxy credentials."""


def flexible_date_window(
    message: str, flight_date: str, airlines: str | None, *, today: date | None = None
) -> tuple[str, str] | None:
    """Enable only explicit date approximation, without airline or exact-date constraints."""
    center = date.fromisoformat(flight_date)
    if airlines or not message:
        return None
    for match in re.finditer(r"(?:(\d{4})年)?(\d{1,2})月\s*(\d{1,2})(?:日|号)?", message):
        if (match[1] and int(match[1]) != center.year) or (int(match[2]), int(match[3])) != (
            center.month,
            center.day,
        ):
            return None
    if any(value != flight_date for value in re.findall(r"\d{4}-\d{2}-\d{2}", message)):
        return None
    if any(alias in message for alias in AIRLINE_ALIASES):
        return None
    codes = "|".join(re.escape(code) for code in sorted(set(AIRLINE_ALIASES.values())))
    if re.search(rf"(?<![A-Z0-9])(?:{codes})(?:\d{{3,4}})?(?![A-Z0-9])", message.upper()):
        return None
    if re.search(
        r"(?:不要|不用|不使用)(?:低价)?日历|不要(?:前后|左右)|"
        r"不(?:接受|考虑)(?:改期|其他日期|前后)|必须|只能|仅限|固定日期|准确日期|指定日期|不改日期",
        message,
    ):
        return None
    anchor = (
        rf"(?:{re.escape(flight_date)}|"
        rf"(?:{center.year}年)?0?{center.month}月0?{center.day}(?:日|号)?|"
        rf"(?<!\d)0?{center.day}(?:日|号))"
    )
    fuzzy = (
        rf"(?:大概|大约|大致|差不多|约莫|约)\s*(?:在|是)?\s*{anchor}|{anchor}\s*(?:左右|前后|附近)"
    )
    if not re.search(fuzzy, message):
        return None
    # Contradictory exact-date wording takes precedence over approximation.
    if re.search(rf"(?:确定|指定|固定|就定|就要|只要)\s*(?:在|为)?\s*{anchor}", message):
        return None
    today = today or datetime.now(CHINA_TZ).date()
    first = max(center - timedelta(days=3), center.replace(day=1), today)
    next_month = (center.replace(day=28) + timedelta(days=4)).replace(day=1)
    last = min(center + timedelta(days=3), next_month - timedelta(days=1))
    if first > last:
        raise ValueError("模糊日期的同月前后三天已全部过去，请提供未来的中心日期。")
    return first.isoformat(), last.isoformat()


def parse_calendar_days(payload: dict, start_date: str, end_date: str) -> list[dict]:
    """Keep positive finite daily prices in the bounded window, with UTC+8 dates."""
    if (payload.get("responseStatus") or {}).get("Ack") != "Success":
        raise CalendarQueryError("携程低价日历返回业务错误，请稍后重试。")
    by_date = {}
    for row in payload.get("priceList") or []:
        try:
            value = str(row["departDate"])
            match = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", value)
            day = (
                datetime.fromtimestamp(int(match[1]) / 1000, CHINA_TZ).date().isoformat()
                if match
                else date.fromisoformat(value[:10]).isoformat()
            )
            if not start_date <= day <= end_date or isinstance(row.get("price"), bool):
                continue
            price = Decimal(str(row["price"]))
            if not price.is_finite() or price <= 0:
                continue
            item = {"date": day, "price": int(price.to_integral_value(rounding=ROUND_CEILING))}
            if day not in by_date or item["price"] < by_date[day]["price"]:
                by_date[day] = item
        except (KeyError, TypeError, ValueError, OverflowError, OSError, InvalidOperation):
            continue
    return [by_date[day] for day in sorted(by_date)]


def select_calendar_lowest(days: list[dict], center_date: str) -> dict | None:
    """Prefer the cheapest date, then the nearest date, then the earlier date."""
    center = date.fromisoformat(center_date)
    return min(
        days,
        key=lambda item: (
            item["price"],
            abs((date.fromisoformat(item["date"]) - center).days),
            item["date"],
        ),
        default=None,
    )


async def fetch_low_price_calendar(
    dep_city: str, arr_city: str, start_date: str, end_date: str
) -> list[dict]:
    """Request Ctrip once and discard dates outside the requested same-month range."""
    departure, arrival = resolve_airports(dep_city)[0], resolve_airports(arr_city)[0]
    payload = {
        "searchType": 1,
        "departNewCityCode": CITY_CODES.get(departure, departure),
        "arriveNewCityCode": CITY_CODES.get(arrival, arrival),
        "passengerList": [{"passengerCount": 1, "passengerType": "Adult"}],
        "grade": 1,
        "calendarSelections": [{"selectionType": 8, "selectionContent": ["1"]}],
        "startDate": start_date,
        "flag": 0,
        "channelName": "MobileH5",
        "lowPriceExtendInfo": [],
    }
    try:
        async with httpx.AsyncClient(
            proxy=proxy_for_url(CALENDAR_API) or None,
            trust_env=False,
            timeout=25,
            headers={"Referer": "https://m.ctrip.com/", "User-Agent": "Mozilla/5.0"},
        ) as client:
            response = await client.post(CALENDAR_API, json=payload)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CalendarQueryError("携程低价日历请求失败，本次不继续批量查价。") from exc
    return parse_calendar_days(data, start_date, end_date)
