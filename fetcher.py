"""Adapt fli one-way adult economy quotes to the plugin's small data model."""

import asyncio
import hashlib
import math
import random
import re
import time
from decimal import ROUND_CEILING, Decimal

from .proxy import EnvironmentProxyClient

CITY_AIRPORTS = {
    "北京": ("PEK", "PKX"),
    "上海": ("SHA", "PVG"),
    "成都": ("TFU", "CTU"),
    "广州": ("CAN",),
    "深圳": ("SZX",),
    "杭州": ("HGH",),
    "南京": ("NKG",),
    "重庆": ("CKG",),
    "武汉": ("WUH",),
    "西安": ("XIY",),
    "长沙": ("CSX",),
    "郑州": ("CGO",),
    "昆明": ("KMG",),
    "厦门": ("XMN",),
    "福州": ("FOC",),
    "天津": ("TSN",),
    "青岛": ("TAO",),
    "济南": ("TNA",),
    "大连": ("DLC",),
    "沈阳": ("SHE",),
    "哈尔滨": ("HRB",),
    "长春": ("CGQ",),
    "海口": ("HAK",),
    "三亚": ("SYX",),
    "南宁": ("NNG",),
    "桂林": ("KWL",),
    "贵阳": ("KWE",),
    "南昌": ("KHN",),
    "合肥": ("HFE",),
    "太原": ("TYN",),
    "石家庄": ("SJW",),
    "兰州": ("LHW",),
    "乌鲁木齐": ("URC",),
    "呼和浩特": ("HET",),
    "银川": ("INC",),
    "西宁": ("XNN",),
    "拉萨": ("LXA",),
    "宁波": ("NGB",),
    "温州": ("WNZ",),
    "泉州": ("JJN",),
    "珠海": ("ZUH",),
    "香港": ("HKG",),
    "澳门": ("MFM",),
    "台北": ("TPE", "TSA"),
}
AIRLINE_ALIASES = {
    "国航": "CA",
    "中国国航": "CA",
    "中国国际航空": "CA",
    "东航": "MU",
    "东方航空": "MU",
    "中国东方航空": "MU",
    "南航": "CZ",
    "南方航空": "CZ",
    "中国南方航空": "CZ",
    "海航": "HU",
    "海南航空": "HU",
    "厦航": "MF",
    "厦门航空": "MF",
    "川航": "3U",
    "四川航空": "3U",
    "深航": "ZH",
    "深圳航空": "ZH",
    "山航": "SC",
    "山东航空": "SC",
    "上航": "FM",
    "上海航空": "FM",
    "春秋": "9C",
    "春秋航空": "9C",
    "吉祥": "HO",
    "吉祥航空": "HO",
    "首航": "JD",
    "首都航空": "JD",
    "成航": "EU",
    "成都航空": "EU",
    "联航": "KN",
    "中国联合航空": "KN",
    "西藏航空": "TV",
}


def resolve_airports(city: str) -> tuple[str, ...]:
    value = city.strip().upper()
    if value in CITY_AIRPORTS:
        return CITY_AIRPORTS[value]
    if re.fullmatch(r"[A-Z]{3}", value):
        from fli.models import Airport

        if value in Airport.__members__:
            return (value,)
    raise ValueError(f"暂不认识城市/机场：{city}。请使用常见中文城市名或有效机场三字码。")


def normalize_airlines(airlines: str | None) -> str | None:
    if not airlines or airlines == "-":
        return None
    from fli.models import Airline

    codes = []
    for value in airlines.replace("，", ",").split(","):
        value = value.strip().upper()
        code = AIRLINE_ALIASES.get(value, value)
        if code not in Airline.__members__ and f"_{code}" not in Airline.__members__:
            raise ValueError(f"暂不认识航司：{value or '空值'}。请使用航司二字码，例如 CA,MU,3U。")
        if code not in codes:
            codes.append(code)
    return ",".join(codes)


def validate_time_window(value: str | None) -> str | None:
    if not value or value == "-":
        return None
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("时间段须为 HH:MM-HH:MM，例如 08:00-18:00。")
    start, end = value.split("-")
    if start > end:
        raise ValueError("第一版不支持跨午夜时间段，请保证开始时间不晚于结束时间。")
    return value


def _fetch_sync(dep_city: str, arr_city: str, flight_date: str) -> list[dict]:
    from fli.models import (
        Airport,
        FlightSearchFilters,
        FlightSegment,
        MaxStops,
        PassengerInfo,
        SeatType,
        SortBy,
    )
    from fli.search import SearchFlights

    filters = FlightSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport[code], 0] for code in resolve_airports(dep_city)],
                arrival_airport=[[Airport[code], 0] for code in resolve_airports(arr_city)],
                travel_date=flight_date,
            )
        ],
        seat_type=SeatType.ECONOMY,
        stops=MaxStops.NON_STOP,
        sort_by=SortBy.CHEAPEST,
    )
    search = SearchFlights()
    search.client = EnvironmentProxyClient(search.client)
    flights = search.search(filters, currency="CNY", language="zh-CN", country="CN")
    normalized = []
    for flight in flights or []:
        if len(flight.legs) != 1 or flight.price is None:
            continue
        if not math.isfinite(flight.price) or flight.price <= 0:
            continue
        if flight.currency and flight.currency.upper() != "CNY":
            continue
        leg = flight.legs[0]
        if leg.departure_datetime.strftime("%Y-%m-%d") != flight_date:
            continue
        code = leg.airline.name.removeprefix("_")
        number = str(leg.flight_number)
        normalized.append(
            {
                "flight_no": number if number.startswith(code) else f"{code}{number}",
                "airline": str(leg.airline.value),
                "airline_code": code,
                "dep_time": leg.departure_datetime.strftime("%H:%M"),
                "arr_time": leg.arrival_datetime.strftime("%H:%M"),
                "arr_date": leg.arrival_datetime.strftime("%Y-%m-%d"),
                "dep_airport": leg.departure_airport.name,
                "arr_airport": leg.arrival_airport.name,
                # Round up so a fractional quote above a whole-yuan target never triggers it.
                "price": int(Decimal(str(flight.price)).to_integral_value(rounding=ROUND_CEILING)),
                "currency": "CNY",
                "source": "Google Flights / fli",
            }
        )
    return normalized


async def fetch_raw_flights(dep_city: str, arr_city: str, flight_date: str) -> list[dict]:
    """Run blocking fli I/O outside the event loop and drain it on cancellation.

    Args:
        dep_city: Supported city alias or IATA airport code.
        arr_city: Supported city alias or IATA airport code.
        flight_date: Local departure date in YYYY-MM-DD format.

    Returns:
        Priced nonstop adult economy flights returned by fli.
    """
    worker = asyncio.create_task(asyncio.to_thread(_fetch_sync, dep_city, arr_city, flight_date))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Python cannot kill an HTTP worker thread. Drain before allowing reload.
        try:
            await worker
        except Exception:
            pass
        raise


async def fetch_mock_flights(dep_city: str, arr_city: str, flight_date: str) -> list[dict]:
    seed = hashlib.sha256(
        f"{dep_city}|{arr_city}|{flight_date}|{int(time.time()) // 1800}".encode()
    ).hexdigest()
    rng = random.Random(seed)
    return [
        {
            "flight_no": f"{code}{5100 + i}",
            "airline": name,
            "airline_code": code,
            "dep_time": f"{8 + i * 2:02d}:30",
            "arr_time": f"{10 + i * 2:02d}:30",
            "arr_date": flight_date,
            "price": rng.randint(350, 950),
            "currency": "CNY",
            "source": "模拟数据（不可购票）",
        }
        for i, (code, name) in enumerate(
            [
                ("CA", "中国国际航空"),
                ("MU", "东方航空"),
                ("CZ", "南方航空"),
                ("HU", "海南航空"),
                ("MF", "厦门航空"),
            ]
        )
    ]


def filter_and_get_lowest(
    flights: list[dict], time_window: str | None, airlines: str | None
) -> dict | None:
    window = validate_time_window(time_window)
    valid = []
    keywords = [
        AIRLINE_ALIASES.get(v.strip().upper(), v.strip().upper())
        for v in (airlines or "").replace("，", ",").split(",")
        if v.strip()
    ]
    for flight in flights:
        if window:
            start, end = window.split("-")
            if not start <= flight["dep_time"] <= end:
                continue
        if keywords:
            code = flight.get("airline_code", flight["flight_no"][:2]).upper()
            if not any(k == code or k in flight["airline"].upper() for k in keywords):
                continue
        valid.append(flight)
    return min(valid, key=lambda flight: flight["price"]) if valid else None
