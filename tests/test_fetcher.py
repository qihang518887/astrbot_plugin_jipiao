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
