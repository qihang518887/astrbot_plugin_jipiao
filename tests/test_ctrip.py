"""Replay public fields from the successful Ctrip response; never use login secrets."""

import copy
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_jipiao import ctrip, fetcher


@pytest.fixture
def raw():
    return json.loads(
        (Path(__file__).parent / "fixtures/ctrip_dlc_sha_2026-11-05.json").read_text(
            encoding="utf-8"
        )
    )


def normalize(raw, stopovers=False, connections=False):
    return ctrip.normalize(raw, ("DLC",), ("SHA", "PVG"), "2026-11-05", stopovers, connections)


@pytest.mark.parametrize(
    "stops,connections,count",
    [(False, False, 22), (True, False, 24), (False, True, 34), (True, True, 36)],
)
def test_real_response_replay_and_independent_scope(raw, stops, connections, count):
    quotes = normalize(raw, stops, connections)
    assert len(quotes) == count
    best = fetcher.filter_and_get_lowest(quotes, None, None)
    assert (best["flight_no"], best["price"], best["dep_time"], best["arr_time"]) == (
        "CA8911",
        320,
        "17:40",
        "19:55",
    )
    assert best["source"] == "携程 / Ctrip" and best["currency"] == "CNY"
    assert set(best) == {
        "itinerary_type",
        "flight_no",
        "airline",
        "airline_code",
        "dep_time",
        "arr_time",
        "arr_date",
        "dep_airport",
        "arr_airport",
        "duration_minutes",
        "stops",
        "legs",
        "layovers",
        "self_transfer",
        "price",
        "currency",
        "source",
    }


def test_single_segment_technical_stop_is_not_a_nonstop(raw):
    quote = next(q for q in normalize(raw, True) if q["flight_no"] == "GS6429")
    assert quote["itinerary_type"] == "stopover" and quote["stops"] == 1
    assert len(quote["legs"]) == 1  # No invented intermediate arrival/departure times.
    assert quote["layovers"][0]["arrival_airport"] == "WEH"
    assert quote["layovers"][0]["duration_minutes"] == 40
    assert isinstance(quote["layovers"][0]["overnight"], bool)
    assert "WEH" in fetcher.describe_itinerary(quote)
    assert "40 分钟" in fetcher.describe_itinerary(quote)
    assert not any(q["flight_no"] == "GS6429" for q in normalize(raw, False, True))


def test_connection_full_price_all_carriers_time_filter_and_crossday(raw):
    item = copy.deepcopy(next(i for i in raw["fltitem"] if len(i["mutilstn"]) == 2))
    item["mutilstn"][1]["basinfo"].update(flgno="MU123", aircode="MU")
    item["mutilstn"][1]["dateinfo"].update(ddate="2026-11-06 07:00:00", adate="2026-11-06 09:15:00")
    (quote,) = normalize({"fltitem": [item]}, False, True)
    assert quote["arr_date"] == "2026-11-06" and quote["arr_time"] == "09:15"
    assert quote["itinerary_type"] == "connection" and quote["self_transfer"] is None
    assert quote["layovers"][0]["overnight"] is True
    assert quote["layovers"][0]["duration_minutes"] == 785
    assert quote["price"] == 480  # Entire policy; not a sum of segment prices.
    assert fetcher.filter_and_get_lowest([quote], None, "SC") is None
    assert fetcher.filter_and_get_lowest([quote], "17:01-20:00", "SC,MU") is None
    assert fetcher.filter_and_get_lowest([quote], "17:00-17:00", "SC,MU") == quote


def test_combined_connection_and_technical_stop_requires_both_permissions(raw):
    item = copy.deepcopy(next(i for i in raw["fltitem"] if len(i["mutilstn"]) == 2))
    item["mutilstn"][0]["fsitem"] = [{"citycode": "WEH", "stopTime": 40}]
    data = {"fltitem": [item]}
    assert normalize(data, False, True) == []
    assert normalize(data, True, False) == []
    (quote,) = normalize(data, True, True)
    assert quote["itinerary_type"] == "connection" and quote["stops"] == 2


def test_airport_and_date_constraints_are_not_broadened(raw):
    hongqiao = ctrip.normalize(raw, ("DLC",), ("SHA",), "2026-11-05", True, True)
    assert hongqiao and all(q["arr_airport"] == "SHA" for q in hongqiao)
    assert not any(q["flight_no"] == "CA8911" for q in hongqiao)
    assert ctrip.normalize(raw, ("DLC",), ("PVG",), "2026-11-06", True, True) == []
    assert ctrip.normalize(raw, ("PEK",), ("PVG",), "2026-11-05", True, True) == []


@pytest.mark.parametrize("price", [0, -1, "NaN", "Infinity"])
def test_invalid_price_is_not_a_free_ticket(raw, price):
    item = next(i for i in raw["fltitem"] if i["mutilstn"][0]["basinfo"]["flgno"] == "CA8911")
    item["policyinfo"] = [copy.deepcopy(item["policyinfo"][0])]
    item["policyinfo"][0]["tprice"] = price
    assert normalize({"fltitem": [item]}) == []


def test_choose_available_ordinary_economy_policy_and_round_up(raw):
    item = next(i for i in raw["fltitem"] if i["mutilstn"][0]["basinfo"]["flgno"] == "CA8911")
    ordinary = copy.deepcopy(item["policyinfo"][0])
    ordinary["tprice"] = 420.2
    student = copy.deepcopy(ordinary)
    student.update(tprice=200, fnotelst=[{"notetype": 20, "notecnt": "200_LimitedStudentPolicy"}])
    member = copy.deepcopy(ordinary)
    member["tprice"] = 180
    member["classinfor"][0]["classNoteList"] = [
        {"notetype": 3, "notecnt": "FreeLuggage,LimitedAirlineMembership"}
    ]
    unavailable = copy.deepcopy(ordinary)
    unavailable.update(tprice=100, quantity=None)
    business = copy.deepcopy(ordinary)
    business["tprice"] = 80
    business["classinfor"][0]["cgrd"] = 2
    item["policyinfo"] = [student, member, unavailable, business, ordinary]
    assert normalize({"fltitem": [item]})[0]["price"] == 421
    item["policyinfo"].pop()
    assert normalize({"fltitem": [item]}) == []


def test_fetcher_queries_city_but_filters_actual_airport(monkeypatch, raw, tmp_path):
    requests = []
    monkeypatch.setattr(ctrip, "query", lambda *a, **kw: requests.append((a, kw)) or raw)
    quotes = fetcher._fetch_sync(
        "大连", "上海", "2026-11-05", profile_dir=tmp_path, storage_state=tmp_path / "state.json"
    )
    assert requests[0][0] == ("DLC", "SHA", "2026-11-05")
    assert fetcher.filter_and_get_lowest(quotes, None, None)["price"] == 320
    hongqiao = fetcher._fetch_sync("DLC", "SHA", "2026-11-05")
    assert hongqiao and all(q["arr_airport"] == "SHA" for q in hongqiao)
    assert not any(q["flight_no"] == "CA8911" for q in hongqiao)


def fake_browser(monkeypatch, response):
    calls = []
    pending = SimpleNamespace(value=response, __enter__=lambda: None)

    @contextmanager
    def expect(predicate):
        calls.append("expect")
        assert predicate(SimpleNamespace(url="https://m.ctrip.com" + ctrip.LIST_API))
        yield pending

    page = SimpleNamespace(
        set_default_timeout=lambda value: None,
        goto=lambda url, **kw: calls.append(url),
        wait_for_timeout=lambda value: calls.append(value),
        expect_response=expect,
    )

    @contextmanager
    def browser(*args, **kwargs):
        yield SimpleNamespace(new_page=lambda: page)

    monkeypatch.setattr(ctrip, "browser", browser)
    monkeypatch.setattr(ctrip, "save_state", lambda *args: calls.append("save"))
    return calls


@pytest.mark.parametrize("failure", ["http", "ack", "rlt"])
def test_upstream_rejection_fails_once_without_saving_session(monkeypatch, tmp_path, failure):
    data = {
        "ResponseStatus": {"Ack": "Failure" if failure == "ack" else "Success", "Errors": []},
        "rlt": 1 if failure == "rlt" else 0,
        "rltstatus": 0,
    }
    calls = fake_browser(
        monkeypatch, SimpleNamespace(status=432 if failure == "http" else 200, json=lambda: data)
    )
    state = tmp_path / "state.json"
    state.write_text("{}")
    with pytest.raises(ctrip.CtripQueryError):
        ctrip.query("DLC", "SHA", "2026-11-05", profile_dir=tmp_path, storage_state=state)
    assert calls.count("expect") == 1 and "save" not in calls
    assert calls[0] == "https://m.ctrip.com/html5/" and calls[1] == 4000


def test_missing_session_does_not_start_anonymous_monitor(monkeypatch, tmp_path):
    monkeypatch.setattr(ctrip, "browser", lambda *a, **kw: pytest.fail("must not launch"))
    with pytest.raises(ctrip.CtripQueryError, match="未保存携程会话"):
        ctrip.query(
            "DLC", "SHA", "2026-11-05", profile_dir=tmp_path, storage_state=tmp_path / "state.json"
        )


def test_portable_session_export_only_includes_ctrip_storage(tmp_path):
    state = {
        "cookies": [
            {"domain": ".ctrip.com", "name": "session", "value": "test"},
            {"domain": "unrelated.example", "name": "other", "value": "test"},
        ],
        "origins": [
            {"origin": "https://m.ctrip.com", "localStorage": []},
            {"origin": "https://unrelated.example", "localStorage": []},
        ],
    }
    context = SimpleNamespace(storage_state=lambda **kw: copy.deepcopy(state))
    target = tmp_path / "cookies" / "state.json"
    ctrip.save_state(context, target)
    exported = json.loads(target.read_text(encoding="utf-8"))
    assert [c["domain"] for c in exported["cookies"]] == [".ctrip.com"]
    assert [o["origin"] for o in exported["origins"]] == ["https://m.ctrip.com"]
    assert not target.with_suffix(".tmp").exists()


def test_browser_imports_portable_state_and_closes_on_failure(monkeypatch, tmp_path):
    import playwright.sync_api as playwright_api

    calls = []
    state = tmp_path / "state.json"
    state.write_text("{}")
    context = SimpleNamespace(
        add_init_script=lambda script: None,
        set_storage_state=lambda path: calls.append(path),
        close=lambda: calls.append("closed"),
    )
    launches = []

    @contextmanager
    def playwright():
        yield SimpleNamespace(
            chromium=SimpleNamespace(
                launch_persistent_context=lambda **kw: launches.append(kw) or context
            )
        )

    monkeypatch.setattr(playwright_api, "sync_playwright", playwright)
    monkeypatch.setenv("HTTPS_PROXY", "http://name:secret@127.0.0.1:7897")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    with pytest.raises(ValueError, match="test failure"):
        with ctrip.browser(tmp_path / "new-profile", state):
            raise ValueError("test failure")
    assert calls == [state, "closed"]
    assert launches[0]["headless"] is True and launches[0]["is_mobile"] is True
    assert launches[0]["user_data_dir"] == str(tmp_path / "new-profile")
    assert launches[0]["proxy"] == {
        "server": "http://127.0.0.1:7897",
        "username": "name",
        "password": "secret",
        "bypass": "localhost,127.0.0.1",
    }


def test_export_uses_saved_profile_instead_of_reimporting_old_state(monkeypatch, tmp_path):
    from astrbot_plugin_jipiao import ctrip_session

    (tmp_path / "Default").mkdir()
    target = tmp_path / "state.json"
    target.write_text('{"cookies":[],"origins":[]}')
    received = []
    state = {
        "cookies": [{"domain": ".ctrip.com", "name": "test", "value": "refreshed"}],
        "origins": [],
    }

    @contextmanager
    def browser(profile, storage, **kwargs):
        received.append(storage)
        yield SimpleNamespace(storage_state=lambda **kw: copy.deepcopy(state))

    monkeypatch.setattr(ctrip_session, "browser", browser)
    monkeypatch.setattr(
        "sys.argv", ["ctrip_session", "--profile-dir", str(tmp_path), "--state-file", str(target)]
    )
    ctrip_session.main()
    assert received == [None]
    assert json.loads(target.read_text(encoding="utf-8"))["cookies"][0]["value"] == "refreshed"


async def test_ctrip_failure_preserves_baseline_and_push_state(tmp_path):
    from unittest.mock import AsyncMock

    from astrbot_plugin_jipiao.database import FlightDB
    from astrbot_plugin_jipiao.scheduler import MonitorScheduler

    db = FlightDB(str(tmp_path / "flights.sqlite3"))
    db.upsert_task("u", "qq:FriendMessage:1", "大连", "上海", "2099-12-01", target_price=500)
    db.update_lowest_3("u", '[{"p":400,"t":1}]')
    db.update_last_pushed_price("u", 450)
    before = db.get_task("u")
    sender = AsyncMock()
    scheduler = MonitorScheduler(
        db,
        sender,
        fetcher=AsyncMock(
            side_effect=ctrip.CtripQueryError("携程航班接口拒绝访问（HTTP 432），请重新登录验证")
        ),
    )
    await scheduler._process_one(before)
    after = db.get_task("u")
    for name in ("lowest_3_json", "last_pushed_price", "created_at", "revision", "session_id"):
        assert after[name] == before[name]
    assert "HTTP 432" in after["last_status"]
    sender.assert_not_awaited()
    db.close()
