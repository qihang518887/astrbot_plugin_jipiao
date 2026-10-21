import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest
from astrbot_plugin_jipiao import fetcher

from fli.models import Airline, Airport


@pytest.mark.parametrize(
    "value,expected", [("北京", ("PEK", "PKX")), ("上海", ("SHA", "PVG")), ("pek", ("PEK",))]
)
def test_airport_mapping(value, expected):
    assert fetcher.resolve_airports(value) == expected


@pytest.mark.parametrize("value", ["不存在", "ZZZ", "北京;DROP TABLE tasks"])
def test_invalid_airport(value):
    with pytest.raises(ValueError):
        fetcher.resolve_airports(value)


def test_chinese_airline_mapping_and_numeric_code():
    assert fetcher.normalize_airlines("国航，东航,3U,CA") == "CA,MU,3U"


@pytest.mark.parametrize("window", ["24:00-25:00", "8:00-18:00", "18:00-08:00", "12:61-13:00"])
def test_invalid_time_window(window):
    with pytest.raises(ValueError):
        fetcher.validate_time_window(window)


def test_filter_edges_empty_and_airline_aliases():
    flights = [
        {"flight_no": "CA123", "airline": "Air China", "dep_time": "08:00", "price": 500},
        {"flight_no": "MU456", "airline": "China Eastern", "dep_time": "18:00", "price": 400},
        {"flight_no": "CA456", "airline": "Air China", "dep_time": "18:01", "price": 200},
    ]
    assert fetcher.filter_and_get_lowest(flights, "08:00-18:00", "国航")["price"] == 500
    assert fetcher.filter_and_get_lowest(flights, "08:00-18:00", "CA,MU")["price"] == 400
    assert fetcher.filter_and_get_lowest(flights, None, "CZ") is None


def test_fli_conversion_uses_cny_and_skips_missing_foreign_and_invalid_prices(monkeypatch):
    from fli.search import SearchFlights

    leg = SimpleNamespace(
        airline=Airline.CA,
        flight_number="123",
        departure_airport=Airport.PEK,
        arrival_airport=Airport.SHA,
        departure_datetime=datetime(2030, 12, 1, 8, 0),
        arrival_datetime=datetime(2030, 12, 1, 10, 0),
        duration=120,
    )
    captured = {}

    def fake_search(self, filters, **kwargs):
        assert isinstance(self.client, fetcher.EnvironmentProxyClient)
        captured.update(kwargs)
        assert len(filters.flight_segments[0].departure_airport) == 2
        assert filters.stops.name == "NON_STOP"
        return [
            SimpleNamespace(
                legs=legs,
                price=price,
                currency=currency,
                duration=120,
                layovers=None,
                self_transfer=None,
            )
            for legs, price, currency in [
                ([leg], 500.01, "CNY"),
                ([leg], None, "CNY"),
                ([leg], 100, "USD"),
                ([leg], 0, "CNY"),
            ]
        ]

    monkeypatch.setattr(SearchFlights, "search", fake_search)
    flights = fetcher._fetch_sync("北京", "上海", "2030-12-01")
    assert captured["currency"] == "CNY"
    assert len(flights) == 1
    assert flights[0]["price"] == 501
    assert flights[0]["flight_no"] == "CA123"
    assert flights[0]["stops"] == 0
    assert "接口单航段" in fetcher.describe_itinerary(flights[0])


@pytest.mark.parametrize("same_number", [False, True])
def test_multileg_total_fare_last_arrival_and_stop_kind(monkeypatch, same_number):
    from fli.models import FlightLeg, FlightResult, Layover
    from fli.search import SearchFlights

    flight = FlightResult(
        price=420.2,
        currency="CNY",
        duration=1515,
        stops=1,
        self_transfer=not same_number,
        legs=[
            FlightLeg(
                airline=Airline.CA,
                flight_number="123",
                departure_airport=Airport.PEK,
                arrival_airport=Airport.CKG,
                departure_datetime=datetime(2030, 12, 1, 8),
                arrival_datetime=datetime(2030, 12, 1, 11),
                duration=180,
            ),
            FlightLeg(
                airline=Airline.CA if same_number else Airline.MU,
                flight_number="123" if same_number else "456",
                departure_airport=Airport.CKG,
                arrival_airport=Airport.PVG,
                departure_datetime=datetime(2030, 12, 2, 7),
                arrival_datetime=datetime(2030, 12, 2, 9, 15),
                duration=135,
            ),
        ],
        layovers=[Layover(airport=Airport.CKG, duration=1200, overnight=True)],
    )
    monkeypatch.setattr(SearchFlights, "search", lambda *a, **kw: [flight])
    if same_number:
        (quote,) = fetcher._fetch_sync("北京", "上海", "2030-12-01", allow_stopovers=True)
        assert fetcher._fetch_sync("北京", "上海", "2030-12-01", allow_connections=True) == []
    else:
        (quote,) = fetcher._fetch_sync("北京", "上海", "2030-12-01", allow_connections=True)
        assert fetcher._fetch_sync("北京", "上海", "2030-12-01", allow_stopovers=True) == []
    assert fetcher._fetch_sync("北京", "上海", "2030-12-01") == []
    assert quote["price"] == 421  # Whole itinerary, not a sum of per-leg fares.
    assert quote["dep_airport"] == "PEK"
    assert quote["arr_airport"] == "PVG"
    assert quote["dep_time"] == "08:00"
    assert (quote["arr_date"], quote["arr_time"]) == ("2030-12-02", "09:15")
    assert quote["duration_minutes"] == 1515
    assert quote["stops"] == 1
    assert len(quote["legs"]) == 2
    assert quote["layovers"][0]["kind"] == ("same_flight_number" if same_number else "connection")
    description = fetcher.describe_itinerary(quote)
    assert "CKG" in description and "1200 分钟，过夜" in description
    if same_number:
        assert "是否换机待核实" in description
        assert "同航班号经停" in description
        assert fetcher.filter_and_get_lowest([quote], "08:00-08:00", "CA") == quote
    else:
        assert "需自行中转" in description
        assert "中转行程（换航班）" in description
        assert quote["flight_no"] == "CA123 / MU456"
        assert fetcher.filter_and_get_lowest([quote], None, "CA") is None
    assert fetcher.filter_and_get_lowest([quote], "08:01-18:00", None) is None
    assert fetcher.filter_and_get_lowest([quote], None, "CA,MU") == quote


async def test_worker_does_not_block_loop_and_is_drained_on_cancel(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def fake(*args):
        started.set()
        release.wait(3)
        finished.set()
        return []

    monkeypatch.setattr(fetcher, "_fetch_sync", fake)
    work = asyncio.create_task(fetcher.fetch_raw_flights("PEK", "SHA", "2030-12-01"))
    for _ in range(200):
        if started.is_set():
            break
        await asyncio.sleep(0.005)
    assert started.is_set()
    work.cancel()
    await asyncio.sleep(0)
    assert not work.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert finished.is_set()


async def test_mock_is_explicitly_marked():
    flights = await fetcher.fetch_mock_flights("PEK", "SHA", "2030-12-01")
    assert flights and all("模拟" in f["source"] for f in flights)
