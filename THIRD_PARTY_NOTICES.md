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

The official QR login lifecycle in `ctrip_login.py` is plugin-specific; it uses
the same browser launch settings and saves the confirmed session on the query host.
