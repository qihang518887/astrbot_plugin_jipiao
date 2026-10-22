"""Ctrip H5 browser acquisition adapted from ticket-monitoring (Apache-2.0).

Modified 2026-10-22: serial browser access, portable session import, fail-fast
responses and conversion to the existing monitoring schema. See THIRD_PARTY_NOTICES.md.
"""

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .proxy import proxy_for_url

SOURCE = "携程 / Ctrip"
LIST_API = "/restapi/soa2/14488/flightListSearchForH5"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)
CITY_CODES = {"PEK": "BJS", "PKX": "BJS", "PVG": "SHA", "TFU": "CTU", "XIY": "SIA"}
_BROWSER_LOCK = threading.Lock()


class CtripQueryError(RuntimeError):
    """Safe upstream/session failure for the monitor status."""


def save_state(context, path: Path):
    """Export only Ctrip browser storage; never print session contents."""
    state = context.storage_state(indexed_db=True)
    write_state(state, path)


def write_state(state: dict, path: Path):
    """Atomically replace the Ctrip session after a successful login or query."""
    state["cookies"] = [
        item
        for item in state["cookies"]
        if item["domain"].lstrip(".") == "ctrip.com" or item["domain"].endswith(".ctrip.com")
    ]
    state["origins"] = [
        item
        for item in state["origins"]
        if (urlsplit(item["origin"]).hostname or "").endswith(".ctrip.com")
        or urlsplit(item["origin"]).hostname == "ctrip.com"
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def browser_options(*, headless=True, mobile=True):
    proxy_url = proxy_for_url("https://m.ctrip.com/")
    proxy = None
    if proxy_url:
        parts = urlsplit(proxy_url)
        proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
        if parts.username:
            from urllib.parse import unquote

            proxy.update(username=unquote(parts.username), password=unquote(parts.password or ""))
        bypass = os.environ.get("no_proxy") or os.environ.get("NO_PROXY")
        if bypass:
            proxy["bypass"] = bypass
    return dict(
        headless=headless,
        user_agent=MOBILE_UA if mobile else None,
        viewport={"width": 390, "height": 844} if mobile else {"width": 1366, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        is_mobile=mobile,
        has_touch=mobile,
        proxy=proxy,
        args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
    )


@contextmanager
def browser(profile_dir: Path, storage_state: Path | None, *, headless=True, mobile=True):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir), **browser_options(headless=headless, mobile=mobile)
        )
        try:
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            if storage_state is not None and storage_state.is_file():
                context.set_storage_state(storage_state)
            yield context
        finally:
            context.close()


def query(dep_code: str, arr_code: str, date: str, *, profile_dir: Path, storage_state: Path):
    """Load the official mobile page using the saved profile, once, without retries."""
    with _BROWSER_LOCK:
        if not storage_state.is_file() and not (profile_dir / "Default").is_dir():
            raise CtripQueryError("未保存携程会话，请管理员私聊发送 /携程登录 并扫码确认")
        with browser(profile_dir, storage_state) as context:
            page = context.new_page()
            page.set_default_timeout(45000)
            page.goto("https://m.ctrip.com/html5/", wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            url = "https://m.ctrip.com/html5/flight/taro/first?" + urlencode(
                {
                    "from": "inner",
                    "tripType": "ONE_WAY",
                    "dcity": dep_code,
                    "dcityName": dep_code,
                    "acity": arr_code,
                    "acityName": arr_code,
                    "ddate": date,
                }
            )
            with page.expect_response(lambda response: LIST_API in response.url) as pending:
                page.goto(url, wait_until="domcontentloaded")
            response = pending.value
            if response.status != 200:
                raise CtripQueryError(
                    f"携程航班接口拒绝访问（HTTP {response.status}），请重新登录验证"
                )
            data = response.json()
            if data["ResponseStatus"]["Ack"] != "Success" or data["ResponseStatus"]["Errors"]:
                raise CtripQueryError("携程航班接口返回业务错误，请重新登录验证")
            if data["rlt"] != 0 or data["rltstatus"] != 0:
                raise CtripQueryError("携程未返回有效查询结果，请重新登录验证")
            # Read the flight list before refreshing the session file. No raw response is logged.
            data["fltitem"]
            save_state(context, storage_state)
            return data


def _minutes(start: str, end: str) -> int:
    return int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60)


def _ordinary_policy(policy: dict) -> bool:
    if not policy["quantity"] or policy["quantity"] <= 0:
        return False
    if not policy["classinfor"] or any(c["cgrd"] != 0 for c in policy["classinfor"]):
        return False
    # nt=20 is a purchase eligibility restriction (age/student/group).
    if any(note["notetype"] == 20 for note in policy.get("fnotelst", [])):
        return False
    for cabin in policy["classinfor"]:
        for note in cabin.get("classNoteList") or []:
            if note["notetype"] == 3 and any(
                token.strip().startswith("Limited") or token.strip() == "HighRisk"
                for token in (note.get("notecnt") or "").split(",")
            ):
                return False
    price = Decimal(str(policy["tprice"]))
    return price.is_finite() and price > 0


def normalize(data, departures, arrivals, date, allow_stopovers=False, allow_connections=False):
    """Keep the plugin's quote fields, exact airports and independent stop permissions."""
    from .fetcher import AIRLINE_ALIASES, classify_itinerary

    airline_names = {code: name for name, code in AIRLINE_ALIASES.items()}
    quotes = []
    for item in data["fltitem"]:
        policies = [p for p in item["policyinfo"] if _ordinary_policy(p)]
        if not policies:
            continue
        segments = item["mutilstn"]
        legs = [
            {
                "flight_no": s["basinfo"]["flgno"],
                "airline": airline_names.get(s["basinfo"]["aircode"], s["basinfo"]["aircode"]),
                "airline_code": s["basinfo"]["aircode"],
                "dep_airport": s["dportinfo"]["aport"],
                "arr_airport": s["aportinfo"]["aport"],
                "dep_datetime": s["dateinfo"]["ddate"].replace(" ", "T"),
                "arr_datetime": s["dateinfo"]["adate"].replace(" ", "T"),
                "duration_minutes": _minutes(s["dateinfo"]["ddate"], s["dateinfo"]["adate"]),
            }
            for s in segments
        ]
        first, last = legs[0], legs[-1]
        if (
            first["dep_airport"] not in departures
            or last["arr_airport"] not in arrivals
            or first["dep_datetime"][:10] != date
        ):
            continue
        layovers = []
        for index, segment in enumerate(segments):
            for stop in segment["fsitem"] or []:
                # The source gives duration/city only. Do not invent intermediate timestamps.
                layovers.append(
                    {
                        "arrival_airport": stop["citycode"],
                        "departure_airport": stop["citycode"],
                        "duration_minutes": stop["stopTime"],
                        "overnight": False,
                        "change_of_airport": False,
                        "kind": "same_flight_number",
                    }
                )
            if index + 1 < len(legs):
                previous, following = legs[index], legs[index + 1]
                change = previous["arr_airport"] != following["dep_airport"]
                layovers.append(
                    {
                        "arrival_airport": previous["arr_airport"],
                        "departure_airport": following["dep_airport"],
                        "duration_minutes": _minutes(
                            previous["arr_datetime"], following["dep_datetime"]
                        ),
                        "overnight": previous["arr_datetime"][:10]
                        != following["dep_datetime"][:10],
                        "change_of_airport": change,
                        "kind": "same_flight_number"
                        if previous["flight_no"] == following["flight_no"] and not change
                        else "connection",
                    }
                )
        itinerary_type = classify_itinerary(legs, layovers, None)
        if len(legs) == 1 and layovers:
            itinerary_type = "stopover"
        has_stopovers = any(s["kind"] == "same_flight_number" for s in layovers)
        if has_stopovers and not allow_stopovers:
            continue
        if itinerary_type == "connection" and not allow_connections:
            continue
        quotes.append(
            {
                "itinerary_type": itinerary_type,
                "flight_no": " / ".join(leg["flight_no"] for leg in legs),
                "airline": " / ".join(dict.fromkeys(leg["airline"] for leg in legs)),
                "airline_code": ",".join(dict.fromkeys(leg["airline_code"] for leg in legs)),
                "dep_time": first["dep_datetime"][11:16],
                "arr_time": last["arr_datetime"][11:16],
                "arr_date": last["arr_datetime"][:10],
                "dep_airport": first["dep_airport"],
                "arr_airport": last["arr_airport"],
                "duration_minutes": _minutes(first["dep_datetime"], last["arr_datetime"]),
                "stops": len(layovers),
                "legs": legs,
                "layovers": layovers,
                "self_transfer": None if itinerary_type == "connection" else False,
                "price": int(
                    min(Decimal(str(p["tprice"])) for p in policies).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                ),
                "currency": "CNY",
                "source": SOURCE,
            }
        )
    return quotes
