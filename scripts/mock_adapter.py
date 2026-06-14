#!/usr/bin/env python3
"""Local mock of the Grafana dashboards adapter (ADR 0014 + ADR 0018 envelope).

Serves the exact snapshot contract the Infinity datasource expects, so the two
open Grafana/Infinity rendering questions in context/dashboards.md can be
closed for $0 -- no AWS apply. Infinity behaves identically whether the
datasource base URL points at a live Lambda Function URL or this local server.

Run on the Windows HOST (NOT inside a container) so the Grafana container can
reach it via host.docker.internal:

    python scripts/mock_adapter.py            # binds 0.0.0.0:8899
    python scripts/mock_adapter.py 9001       # custom port

Then point Grafana at it FROM THE REPO ROOT and recreate the container:

    # PowerShell
    $env:ADAPTER_FUNCTION_URL = "http://host.docker.internal:8899/"
    docker compose up -d --force-recreate grafana

The envelope is deliberately a BREACHING fleet (alert_flag true, bearing_temp
PSI > 0.25, pumps_pooled 15, last_alert_sent_at set) AND keeps one pump at
last_alert_sent_at: null, so the null -> "never" render path is exercised in
the same view as the populated alert rows. Pure passthrough shapes only -- no
threshold logic (the adapter is not a brain; ADR 0014).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

# Row write times (snapshot contract has no history -- fixed, recent).
LATEST_TS = "2026-06-14T14:00:00.000Z"
ALERT_TS = "2026-06-14T13:55:00.000Z"


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _pump(idx, score, psi, alert=False, last_alert=None):
    return {
        "pump_id": f"P-{idx:02d}",
        "latest_ts": LATEST_TS,
        "latest_score": score,
        "psi_vibration_amp": psi[0],
        "psi_bearing_temp": psi[1],
        "psi_motor_current": psi[2],
        "psi_rpm": psi[3],
        "alert_flag": alert,
        "last_alert_sent_at": last_alert,
    }


def _build_pumps():
    pumps = []
    # P-00..P-11: healthy, all PSI < 0.1 (green), never alerted -> null page.
    for i in range(12):
        base = round(0.02 + (i % 5) * 0.012, 3)  # 0.020 .. 0.068
        pumps.append(
            _pump(
                i,
                score=round(0.03 + (i % 4) * 0.02, 3),  # 0.03 .. 0.09
                psi=(base, round(base * 0.8 + 0.01, 3),
                     round(base * 1.1, 3), round(base * 0.6, 3)),
                alert=False,
                last_alert=None,
            )
        )
    # P-12 warning band, P-13 elevated -- both still alert_flag false / null.
    pumps.append(_pump(12, 0.34, (0.08, 0.14, 0.06, 0.05), alert=False, last_alert=None))
    pumps.append(_pump(13, 0.55, (0.18, 0.12, 0.09, 0.07), alert=False, last_alert=None))
    # P-14 breaching: alert_flag true WITH a real last_alert_sent_at timestamp.
    pumps.append(_pump(14, 0.82, (0.12, 0.31, 0.16, 0.09), alert=True, last_alert=ALERT_TS))
    return pumps


# FLEET aggregate STATE row projection (ADR 0018): pump shape MINUS latest_score
# PLUS pumps_pooled. Breaching so panels 9-13 show a live ALERT row.
FLEET = {
    "latest_ts": LATEST_TS,
    "psi_vibration_amp": 0.14,
    "psi_bearing_temp": 0.31,   # > 0.25 -> red gauge + ALERT
    "psi_motor_current": 0.19,
    "psi_rpm": 0.12,
    "alert_flag": True,
    "last_alert_sent_at": ALERT_TS,
    "pumps_pooled": 15,
}


def _envelope():
    return {
        "fleet_size": 15,
        "pumps_reporting": 15,
        "as_of": _now_iso(),   # regenerated each refresh -> proves a live hit
        "pumps": _build_pumps(),
        "fleet": FLEET,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._send(200, _envelope())

    def do_POST(self):
        self._send(405, {"error": "method not allowed"})

    do_PUT = do_DELETE = do_PATCH = do_POST

    def log_message(self, fmt, *args):
        # Surface every Grafana/Infinity request -- the request path proves
        # how Infinity joined the empty panel url against the datasource base.
        sys.stderr.write("[mock_adapter] %s %s\n"
                         % (self.address_string(), fmt % args))


def main():
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    else:
        port = int(os.environ.get("MOCK_PORT", "8899"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[mock_adapter] serving ADR 0014/0018 envelope on 0.0.0.0:{port}")
    print(f"[mock_adapter] Grafana (in Docker) base URL -> "
          f"http://host.docker.internal:{port}/")
    print("[mock_adapter] Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock_adapter] stopped")
        server.server_close()


if __name__ == "__main__":
    main()
