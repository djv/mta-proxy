# MTA proxy

Python HTTP backend for [garmin-nyc-mta](https://github.com/djv/garmin-nyc-mta).
It turns MTA subway feeds into nearby arrival boards and compact scheduled
departure packs that a watch can download before going offline.

## Architecture

```text
Garmin watch app → HTTP(S) → this proxy → MTA GTFS-realtime arrivals and alerts
                                     → MTA supplemented GTFS schedules
                                     → bundled stations and entrance snapshot
```

`server.py` uses the standard library HTTP server and `nyct-gtfs` for feed
parsing. Arrival feeds are cached for 25 seconds per line group. Singleflight
shares one upstream fetch among concurrent misses for the same feed. Newly
fetched feeds older than 180 seconds are rejected. A board can return partial
results with `failed_feeds`; if all required feeds fail, it returns HTTP 503.
Alerts have a separate 60-second cache. Alert failure leaves the board working
with `alerts_available: false`.

`timetable.py` downloads supplemented GTFS into the state directory and refreshes
it every six hours. It indexes service dates from yesterday through two days
ahead, including calendar exceptions and times past midnight. It retains the
previous ZIP on download failure, so schedules can be stale. Schedule endpoints
return 503 until the first timetable loads. Offline packs contain scheduled
trains, not realtime predictions. Walking time uses straight-line distance with
a 1.3 detour factor at 4.9 km/h.

`stations.json` supplies station metadata. A newer bundled `google_transit.zip`
causes a station-cache rebuild at startup. Entrances use a pinned MTA snapshot;
see [data provenance](data/README.md). They do not establish live availability
or access to a selected platform. Nearby same-name stations are merged into
complexes, including their routes, arrivals and entrances.

## Endpoints

Documented endpoint responses are JSON. No authentication is implemented.

- `GET /mta/health`: process health, `{"ok": true}`; does not check upstreams.
- `GET /mta/board?lat=40.7527&lon=-73.9772&limitStations=1&limitArrivals=4`:
  nearby stations and arrivals ordered by `arrival_at`. Station limits clamp
  to 1–5; arrival limits to 1–8. Optional `station=<GTFS parent ID>`, `route`,
  `dest` and `dir=N|S` filter before the arrival limit. Coordinates are still
  required with `station`. Stations include travel `options` and matching alerts.
- `GET /mta/reach?lat=40.7527&lon=-73.9772&radius=700[&at=EPOCH]`:
  destination-area stations (`home`, with walking seconds) and stations with
  a one-seat scheduled ride there. Includes routes, median `ride_min` including
  the walk, and `tph` in the two-hour window around `at`. Radius clamps to
  100–1500 metres.
- `POST /mta/pack`: JSON `{"key":"examplepack01","lat":40.7527,"lon":-73.9772,
  "stations":["L03"],"radius":700}`. Stores destination coordinates and up to
  four known station IDs for 36 hours. Keys must be 10–32 lowercase letters or
  digits. Bodies are limited to 4096 bytes. Returns accepted stations and expiry.
- `GET /mta/pack?key=examplepack01&hours=4`: generated scheduled departures
  for a saved pack; hours clamps to 1–6. Missing or expired packs return 404.
  Each station has `routes`, `heads` (terminals), `h` (one headsign index per
  departure), and flat `d` triples of departure epoch, route index and minutes
  to the destination door. At most 30 departures, six entrances and two alert
  titles are returned per station. Alerts may include the opposite direction.

## Privacy and security

The server receives destination/location coordinates, station selections and,
for packs, a client-chosen key. The HTTP connection also exposes the connecting
peer's IP address to the server or its reverse proxy. Upstream requests fetch
shared MTA feeds; coordinates and pack keys are not forwarded to MTA.

Application request logs contain method, URL path, status and duration. They omit
query strings, request bodies and client IPs. Failure logs record exception
types. This does not control logs kept by a reverse proxy, hosting provider or
client. Query URLs can contain coordinates or pack keys.

Board and reach locations are not written as request records. The timetable's
in-memory reach cache can retain derived destination station/walking-time sets.
Pack POSTs persist exact coordinates, radius, selected stations, key and creation
time as plaintext in `$MTA_PROXY_STATE/packs.json`. The default state directory
is `~/mta-proxy/state`. The schedule ZIP is stored there too. Pack GETs return
derived departures and public station coordinates, rather than the stored
exact destination coordinates.

Pack keys are **unauthenticated client keys**, not accounts or enforced access
controls. Anyone knowing or guessing a key can read its derived pack or overwrite
it. Anyone can create packs. The global cap of 200 evicts the oldest entries,
so one client can evict another client's data. There are no per-client quotas
or rate limits. Do not use this API as confidential location storage or present
an unrestricted deployment as a privacy-hardened public demo.

Expiry blocks reads after 36 hours. Expired records are physically pruned only
on a later successful POST; there is no scheduled deletion or explicit delete
endpoint. File permissions follow the process umask; the application does not
set restrictive modes or encrypt the file. Backups can retain older records.
Responses do not set `Cache-Control: no-store`. Request threads and body-read
deadlines are not bounded by the application. Deployment exposure and access
restrictions are the operator's responsibility.

## Run and test

Python 3.12 is the recorded deployment version. From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
python3 server.py
```

Defaults: `MTA_PROXY_BIND=127.0.0.1`, `MTA_PROXY_PORT=8087`,
`MTA_PROXY_STATE=~/mta-proxy/state`. Set these environment variables to override
them. Startup launches the schedule downloader. Board requests need network
access to MTA; subway feeds require no API key. Bus arrivals are not supported.

Deterministic tests use fixtures and mocked upstreams:

```bash
OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 \
  python3 -m unittest -v test_board test_hosting test_audit test_entrances test_alerts test_timetable test_deploy_config
```

There is no configured lint, static type-check or package-build target. This is
a script-based service. `test_smoke.py` is a separate live integration script,
not an offline discovery target: it executes HTTP requests on import. With a
server running, `python3 test_smoke.py` checks live boards. `verify_hosted.py`
checks concurrency, direction filters and error responses; `--soak` runs for ten
minutes. Both read `MTA_PROXY_BASE` (origin only, without `/mta`), defaulting to
`http://127.0.0.1:8087`. The verifier uses eight concurrent requests.

Historical measurements are in [QA.md](QA.md). They are small dated samples,
not availability or latency guarantees.

## Deployment

Copy `.env.deployment.example` to `.env.deployment` (gitignored) and set
`MTA_PROXY_SSH_HOST`, or pass the SSH host as the first argument to
`bash deploy/deploy.sh`. No deployment host is built into the scripts.

Optional settings:

- `MTA_PROXY_ROOT`: absolute deployment path without spaces or shell metacharacters.
  Defaults to the remote SSH account's `$HOME/mta-proxy`. Release, current,
  previous and snapshot paths live under it. Generated systemd units use it.
  This does not change the server's separate `MTA_PROXY_STATE` default.
- `MTA_PROXY_PUBLIC_HOST`: Funnel DNS hostname. Defaults to `Self.DNSName` from
  `tailscale status --json` on the server. Activation uses it for route checks
  and HTTPS verification.
- `MTA_PROXY_BASE`: optional origin for validation scripts. Export it, or source
  `.env.deployment` before running them. Deployment reads the file automatically.

The deployer creates a timestamped release, installs pinned dependencies, runs
tests, switches `current`, installs user systemd units, and adds only `/mta` on
Funnel HTTPS 443 pointing to `http://127.0.0.1:8087/mta`. It compares before/after
routing snapshots to ensure other routes stay unchanged. Snapshots can contain
private routing details and must stay local. Requirements: SSH, Python with venv,
user systemd with lingering, Tailscale Funnel, and passwordless `sudo tailscale`.

The service restarts after three seconds. A five-minute health timer distinguishes
process/protocol failures (exit 1) from upstream unavailability/partial data
(exit 2). It does not restart a healthy process for an upstream failure.

On the server, `bash "$HOME/mta-proxy/current/deploy/rollback.sh"` swaps to the
previous release. For a custom root, export `MTA_PROXY_ROOT` and invoke the script
under that root. `--remove-route` removes only `/mta` on 443. With no previous
release, rollback disables the service and timer. Activation failures attempt
scoped recovery. Keep previous releases for rollback; do not commit runtime state,
releases or routing snapshots. This source polish does not deploy anything.

## License

Code is licensed under [MIT](LICENSE). Bundled MTA data has its own provenance;
the code license does not replace upstream data terms.
