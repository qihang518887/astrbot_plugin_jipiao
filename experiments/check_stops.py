"""Compare nonstop and unrestricted fli results without creating monitor tasks."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_jipiao.fetcher import resolve_airports
from astrbot_plugin_jipiao.proxy import EnvironmentProxyClient

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dep", default="北京")
    parser.add_argument("--arr", default="上海")
    parser.add_argument("--date", default="2026-12-01")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "departure": args.dep,
        "arrival": args.arr,
        "date": args.date,
        "queries": {},
    }
    for stops in (MaxStops.NON_STOP, MaxStops.ANY):
        filters = FlightSearchFilters(
            passenger_info=PassengerInfo(adults=1),
            flight_segments=[
                FlightSegment(
                    departure_airport=[[Airport[c], 0] for c in resolve_airports(args.dep)],
                    arrival_airport=[[Airport[c], 0] for c in resolve_airports(args.arr)],
                    travel_date=args.date,
                )
            ],
            seat_type=SeatType.ECONOMY,
            stops=stops,
            sort_by=SortBy.CHEAPEST,
        )
        search = SearchFlights()
        search.client = EnvironmentProxyClient(search.client)
        flights = search.search(filters, currency="CNY", language="zh-CN", country="CN") or []
        report["queries"][stops.name] = [f.model_dump(mode="json") for f in flights]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(stops.name, "count=", len(flights), flush=True)
        for f in flights[:8]:
            print(
                f.price,
                f.currency,
                f.stops,
                f.duration,
                [
                    (
                        leg.airline.name,
                        leg.flight_number,
                        leg.departure_airport.name,
                        leg.arrival_airport.name,
                    )
                    for leg in f.legs
                ],
                flush=True,
            )


if __name__ == "__main__":
    main()
