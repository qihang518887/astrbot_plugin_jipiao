import json

import pytest
from astrbot_plugin_jipiao.algorithm import DAY, evaluate_flight, flight_timestamp, task_phase

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


def test_observation_lowest_three_and_six_hour_deduplication():
    now = FLIGHT - 50 * DAY
    row = task(
        lowest_3_json=json.dumps(
            [{"p": 400, "t": now - 21599}, {"p": 500, "t": now - DAY}, {"p": 600, "t": now - DAY}]
        )
    )
    assert evaluate_flight(row, {"price": 400}, now)["new_lowest_3_json"] is None
    result = evaluate_flight(row, {"price": 400}, now + 1)
    assert [x["p"] for x in json.loads(result["new_lowest_3_json"])] == [400, 400, 500]
    assert len(json.loads(row["lowest_3_json"])) == 3


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
