# Third-party notices

`ctrip.py` and `ctrip_session.py` adapt the H5 browser flow, persistent-profile
login interaction and flight response field mapping from `crawlers/ctrip.py`
and `crawlers/base.py` in `ticket-monitoring` by dengmeiluan:
https://github.com/dengmeiluan/ticket-monitoring (Apache License 2.0).
The upstream license is reproduced in `LICENSE.ctrip`.
Modifications on 2026-10-22: plugin-local serialized browser queries, portable
Ctrip storage export/import, fail-fast response handling, ordinary economy
policy selection, independent stopover/connection permissions and conversion
to the existing plugin schema. No upstream account credentials are included.
The Ctrip source requires Playwright on the query host, including the server.

`calendar_search.py` adapts the Ctrip 15380 calendar request shape from
`crawlers/special.py` in the same upstream project (Apache-2.0).
Modifications on 2026-10-25: async plugin-local requests, same-month seven-day
filtering, explicit fuzzy-date eligibility and single-day verification. Calendar
reference prices are never written into monitoring observation or push history.

The official QR login lifecycle in `ctrip_login.py` is plugin-specific; it uses
the same browser launch settings and saves the confirmed session on the query host.

v0.6.1 excludes IndexedDB caches from both session restore and export, requires
explicit fetcher session paths, and accepts the exact LimitedCtripMemberShip
fare marker while preserving other purchase restrictions and stop permissions.
