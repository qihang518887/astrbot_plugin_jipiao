import asyncio
import threading

import pytest
from astrbot_plugin_jipiao import fetcher


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


@pytest.mark.parametrize(
    "value,expected",
    [
        ("!春秋,!联航", "!9C,!KN"),
        (" !9c，！春秋航空,!kn ", "!9C,!KN"),
        ("!川航,!3u", "!3U"),
        ("CA,MU,!东航,!春秋", "CA,MU,!MU,!9C"),
        (" - ", None),
    ],
)
def test_airline_blacklist_normalization(value, expected):
    assert fetcher.normalize_airlines(value) == expected


@pytest.mark.parametrize("value", ["!", "!!CA", "!不存在", "!ZZZ", "!CA,", "!CA,,!MU"])
def test_invalid_blacklist_is_rejected(value):
    with pytest.raises(ValueError):
        fetcher.normalize_airlines(value)


def test_blacklist_only_allows_other_airlines_and_preserves_time_filter():
    flights = [
        {"flight_no": "9C123", "dep_time": "10:00", "price": 100},
        {"flight_no": "KN123", "dep_time": "10:00", "price": 200},
        {"flight_no": "CA123", "dep_time": "18:01", "price": 250},
        {"flight_no": "MU123", "dep_time": "18:00", "price": 300},
    ]
    assert fetcher.filter_and_get_lowest(flights, "08:00-18:00", "!春秋,!联航") == flights[3]
    assert fetcher.filter_and_get_lowest(flights, None, "!9C,!KN") == flights[2]
    assert fetcher.filter_and_get_lowest(flights[:2], None, "!9C,!KN") is None


@pytest.mark.parametrize(
    "fields",
    [
        {"airline_code": "CA,9C", "flight_no": "CA123 / 9C456"},
        {"flight_no": "CA123 / 9C456"},
        {"airline_code": "CA", "legs": [{"airline_code": "CA"}, {"airline_code": "9c"}]},
        {"airline_code": "CA", "legs": [{"flight_no": "CA123"}, {"flight_no": "9C456"}]},
    ],
)
def test_blacklisted_later_leg_excludes_entire_connection(fields):
    flight = {"flight_no": "CA123", "dep_time": "10:00", "price": 100, **fields}
    assert fetcher.filter_and_get_lowest([flight], None, "!9C") is None


def test_blacklist_takes_priority_when_combined_with_whitelist():
    flights = [
        {"flight_no": "MU123", "dep_time": "10:00", "price": 100},
        {"flight_no": "CA123", "dep_time": "10:00", "price": 200},
        {"flight_no": "CZ123", "dep_time": "10:00", "price": 50},
    ]
    assert fetcher.filter_and_get_lowest(flights, None, "CA,MU,!MU") == flights[1]
    assert fetcher.filter_and_get_lowest(flights[:1], None, "MU,!MU") is None


def test_airline_constraints_reject_quotes_without_carrier_identity():
    assert fetcher.filter_and_get_lowest([{"dep_time": "10:00", "price": 50}], None, "!9C") is None


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


async def test_worker_does_not_block_loop_and_is_drained_on_cancel(monkeypatch, tmp_path):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def fake(*args, **kwargs):
        started.set()
        release.wait(3)
        finished.set()
        return []

    monkeypatch.setattr(fetcher, "_fetch_sync", fake)
    work = asyncio.create_task(
        fetcher.fetch_raw_flights(
            "PEK", "SHA", "2030-12-01", profile_dir=tmp_path, storage_state=tmp_path / "state.json"
        )
    )
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
