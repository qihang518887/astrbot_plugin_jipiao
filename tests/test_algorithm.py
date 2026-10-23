import json

import pytest
from astrbot_plugin_jipiao.algorithm import (
    DAY,
    evaluate_flight,
    flight_timestamp,
    normalize_lowest_3,
    task_phase,
)

FLIGHT = flight_timestamp("2030-12-01")


def task(**changes):
    row = {
        "flight_date": "2030-12-01",
        "created_at": FLIGHT - 60 * DAY,
        "lowest_3_json": "[]",
        "target_price": None,
        "last_pushed_price": None,
    }
    row.update(changes)
    return row


def test_expiry_has_priority_without_quote():
    assert evaluate_flight(task(), None, FLIGHT)["is_expired"]
    assert not evaluate_flight(task(), None, FLIGHT - 1)["is_expired"]


def test_timestamp_is_midnight_utc_plus_eight():
    from datetime import datetime, timezone

    assert datetime.fromtimestamp(FLIGHT, timezone.utc).isoformat() == "2030-11-30T16:00:00+00:00"


@pytest.mark.parametrize(
    "price,last,expected",
    [
        (499, None, True),
        (500, None, True),
        (501, None, False),
        (500, 500, False),
        (499, 500, True),
        (501, 499, False),
    ],
)
def test_target_and_monotonic_notifications(price, last, expected):
    result = evaluate_flight(
        task(target_price=500, last_pushed_price=last), {"price": price}, FLIGHT - 90 * DAY
    )
    assert result["should_notify"] is expected
    assert result["new_pushed_price"] == (price if expected else None)


def test_wait_period_and_first_observation_boundary():
    row = task()
    assert evaluate_flight(row, {"price": 400}, FLIGHT - 60 * DAY - 1)["new_lowest_3_json"] is None
    result = evaluate_flight(row, {"price": 400}, FLIGHT - 60 * DAY)
    assert json.loads(result["new_lowest_3_json"])[0]["p"] == 400
    assert not result["should_notify"]


@pytest.mark.parametrize("elapsed", [0, 21599, 21600, DAY, 3 * DAY])
def test_repeated_price_never_occupies_another_observation_slot(elapsed):
    now = FLIGHT - 50 * DAY
    row = task(
        lowest_3_json=json.dumps(
            [{"p": 400, "t": now - 21599}, {"p": 500, "t": now - DAY}, {"p": 600, "t": now - DAY}]
        )
    )
    result = evaluate_flight(row, {"price": 400}, now + elapsed)
    assert result["new_lowest_3_json"] is None
    assert not result["should_notify"]
    assert [x["p"] for x in json.loads(row["lowest_3_json"])] == [400, 500, 600]


def test_observation_keeps_three_distinct_lowest_prices_across_queries():
    row = task()
    now = FLIGHT - 50 * DAY
    for index, price in enumerate([600, 500, 400, 400, 450, 300, 600]):
        result = evaluate_flight(row, {"price": price}, now + index * 21600)
        if result["new_lowest_3_json"] is not None:
            row["lowest_3_json"] = result["new_lowest_3_json"]
        assert not result["should_notify"]
    assert json.loads(row["lowest_3_json"]) == [
        {"p": 300, "t": now + 5 * 21600},
        {"p": 400, "t": now + 2 * 21600},
        {"p": 450, "t": now + 4 * 21600},
    ]


def test_legacy_duplicate_prices_keep_earliest_time_and_are_repaired_in_observation():
    records = [{"p": 400, "t": 3}, {"p": 500, "t": 2}, {"p": 400, "t": 1}]
    expected = [{"p": 400, "t": 1}, {"p": 500, "t": 2}]
    assert normalize_lowest_3(records) == expected
    row = task(lowest_3_json=json.dumps(records))
    result = evaluate_flight(row, {"price": 400}, FLIGHT - 50 * DAY)
    assert json.loads(result["new_lowest_3_json"]) == expected
    assert json.loads(row["lowest_3_json"]) == records


@pytest.mark.parametrize("price,notify", [(400, True), (450, False)])
def test_decision_repairs_legacy_duplicates_and_uses_available_distinct_prices(price, notify):
    row = task(lowest_3_json='[{"p":400,"t":1},{"p":400,"t":2},{"p":400,"t":3}]')
    result = evaluate_flight(row, {"price": price}, FLIGHT - 20 * DAY)
    assert json.loads(result["new_lowest_3_json"]) == [{"p": 400, "t": 1}]
    assert result["should_notify"] is notify


def test_higher_new_price_does_not_change_full_observation_list():
    row = task(lowest_3_json='[{"p":400,"t":1},{"p":450,"t":2},{"p":500,"t":3}]')
    result = evaluate_flight(row, {"price": 600}, FLIGHT - 50 * DAY)
    assert result["new_lowest_3_json"] is None


def test_exact_observation_deadline_is_still_observation():
    row = task(lowest_3_json='[{"p": 500, "t": 1}]')
    _, deadline = task_phase(row, FLIGHT - 50 * DAY)
    assert not evaluate_flight(row, {"price": 400}, deadline)["should_notify"]
    assert evaluate_flight(row, {"price": 400}, deadline + 1)["should_notify"]


@pytest.mark.parametrize(
    "price,notify,word",
    [(501, False, ""), (500, True, "基准价"), (400, True, "追平"), (399, True, "刷新")],
)
def test_decision_threshold_and_wording(price, notify, word):
    row = task(lowest_3_json='[{"p": 400, "t": 1}, {"p": 450, "t": 2}, {"p": 500, "t": 3}]')
    result = evaluate_flight(row, {"price": price}, FLIGHT - 20 * DAY)
    assert result["should_notify"] is notify
    assert word in result["notify_reason"]


def test_no_observations_waits_until_seven_day_fallback():
    assert not evaluate_flight(task(), {"price": 900}, FLIGHT - 10 * DAY)["should_notify"]
    assert evaluate_flight(task(), {"price": 900}, FLIGHT - 7 * DAY)["should_notify"]


def test_late_creation_minimum_observation_and_fallback_priority():
    start = FLIGHT - 7 * DAY - 3600
    row = task(created_at=start)
    assert task_phase(row, start) == ("观察期", start + 43200)
    assert evaluate_flight(row, {"price": 900}, FLIGHT - 7 * DAY)["should_notify"]
