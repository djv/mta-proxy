# MTA proxy

Live NYC subway board for the Garmin watch app. stdlib HTTP + `nyct-gtfs`
(direct MTA GTFS-RT, no API key for subway).

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 server.py  # 127.0.0.1:8087; MTA_PROXY_BIND / MTA_PROXY_PORT override
```

Bundled `stations.json` avoids static-feed parsing at startup. If the optional
`google_transit.zip` is newer, startup rebuilds the station cache.

## Endpoints

- `GET /mta/health` → `{"ok": true}`
- `GET /mta/board?lat=..&lon=..&limitStations=1&limitArrivals=4`
  → nearest stations + arrivals sorted by exact `arrival_at` (`route/dest/dir/mins`)
- Optional `station=<GTFS parent ID>&route=L&dir=N` selects a station and travel
  direction. Direction is `N` or `S`; filtering happens before the arrival limit.
  Each station also includes `options` with route/direction/destination labels.
- Active MTA service alerts are attached per station as
  `station.alerts = [{title, desc, routes}]` when they match the station's lines
  (or one of its stop IDs). The board also returns `alerts_available`; an alerts
  fetch failure leaves the board working with `alerts_available: false`.
  Alerts come from the public `camsys/subway-alerts` GTFS-RT feed, cached 60s.

Limits: stations 1–5, arrivals 1–8. Feed cache 25s per line group.

## Notes

- The parser's host-local datetime values are converted to Unix timestamps.
- Station complexes (e.g. Union Sq = R20 NQRW + 635 456 + L03 L)
  are separate GTFS stations — use `limitStations=3`; the proxy merges nearby
  same-name entries, including routes and arrivals.
- Sim/watch use the persistent HTTPS endpoint below. Local development remains
  available; set `MTA_PROXY_BIND=0.0.0.0` explicitly if LAN access is needed.
- Bus arrivals need an MTA BusTime key — subway only for now.

## Smoke test

```bash
python3 test_smoke.py  # needs proxy running on :8087
python3 -m unittest -v test_board  # deterministic failure/ordering/merge checks
```

## Hosted service

`https://proxy.example.net/mta/board`

Public, no authentication. This runs on an existing server;
no laptop proxy/tunnel, new domain, or paid service is required. MTA outages can
still produce partial data or HTTP 503. Feed TTL remains 25 seconds and maximum
upstream age remains 180 seconds.

Deploy from this repository with `deploy/deploy.sh [HOST]`. The script uploads
a timestamped release to `~/mta-proxy/releases`, creates its Python 3.12
venv from `requirements.lock`, runs tests, switches `current`, and enables user
systemd units. It snapshots Funnel JSON, adds only `/mta` on HTTPS 443 targeting
`http://127.0.0.1:8087/mta`, then compares all other configuration. The prefix
and query string are preserved. Existing routes on the host are untouched.
The server requires passwordless `sudo tailscale` for route mutations.
See [Tailscale CLI documentation](https://tailscale.com/docs/reference/tailscale-cli/funnel).

The service restarts after three seconds; user lingering keeps it running
without SSH. The health timer runs every five minutes, checks local health and
Union Square station L03, and logs process failures (exit 1) separately from
upstream failures/partial data (exit 2, does not restart the process).
Request logs contain method, path, status and duration, with no GPS query string.

```bash
ssh server 'systemctl --user status mta-proxy.service mta-proxy-health.timer'
ssh server 'journalctl --user -u mta-proxy -u mta-proxy-health --since "10 minutes ago"'
python3 verify_hosted.py
python3 verify_hosted.py --soak  # ten minutes, ten-second request budget
```

On-server rollback: `bash ~/mta-proxy/current/deploy/rollback.sh` swaps to the
previous release and restarts it. Add `--remove-route` only when undoing initial
hosting; this removes just `/mta` on 443, never resets Funnel. With no previous
release, rollback disables the new service and timer. Activation failures attempt
the same scoped recovery automatically. Keep release directories for rollback.
Do not reboot the shared server for deployment verification.

## Entrance coordinates

Board station objects optionally include `entrances: [{lat, lon}]`. Entries use the pinned MTA 2024 snapshot; see [data provenance](data/README.md). Merged boards union constituent entrances without changing station IDs, coordinates or ranking. Clients choose the nearest point locally. Missing data is backward compatible. Coordinates and entry permission are static, not live availability or proof of platform access.
