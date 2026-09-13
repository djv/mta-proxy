#!/usr/bin/env python3
"""MTA proxy for Garmin watches. stdlib only (+nyct-gtfs).

GET /mta/health
GET /mta/board?lat=..&lon=..&limitStations=1&limitArrivals=4
"""

import csv
import io
import json
import math
import logging
import os
import threading
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nyct_gtfs import NYCTFeed
from entrances import load_entrances, combine


LOG = logging.getLogger("mta-proxy")


def bind_address():
    return os.environ.get("MTA_PROXY_BIND", "127.0.0.1")


PORT = int(os.environ.get("MTA_PROXY_PORT", "8087"))
STATIC_ZIP = "google_transit.zip"
FEED_TTL_S = 25
FEED_MAX_AGE_S = 180

stations = []  # [{id,name,lat,lon,routes[]}]
entrances = {}
feeds = {}  # feed_key -> (fetched_at, NYCTFeed)
feeds_lock = threading.Lock()
feeds_inflight = {}  # feed_key -> _Inflight


class _Inflight:
    """Shared state for one in-flight feed fetch (singleflight)."""

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error: BaseException | None = None

FEED_FOR_ROUTE = {}
for _r in ["1", "2", "3", "4", "5", "6", "7", "S"]:
    FEED_FOR_ROUTE[_r] = "gtfs"
for _r in ["A", "C", "E", "FS", "H"]:
    FEED_FOR_ROUTE[_r] = "gtfs-ace"
for _r in ["B", "D", "F", "M"]:
    FEED_FOR_ROUTE[_r] = "gtfs-bdfm"
FEED_FOR_ROUTE["G"] = "gtfs-g"
for _r in ["J", "Z"]:
    FEED_FOR_ROUTE[_r] = "gtfs-jz"
FEED_FOR_ROUTE["L"] = "gtfs-l"
for _r in ["N", "Q", "R", "W"]:
    FEED_FOR_ROUTE[_r] = "gtfs-nqrw"
FEED_FOR_ROUTE["SI"] = "gtfs-si"
FEED_FOR_ROUTE["SIR"] = "gtfs-si"
FEED_FOR_ROUTE["GS"] = "gtfs"  # 42nd St shuttle shares the numbered-lines feed

# one representative route per feed (nyct-gtfs maps line -> feed url)
FEED_SEED_ROUTE = {
    "gtfs": "1",
    "gtfs-ace": "A",
    "gtfs-bdfm": "B",
    "gtfs-g": "G",
    "gtfs-jz": "J",
    "gtfs-l": "L",
    "gtfs-nqrw": "N",
    "gtfs-si": "SI",
}


def _epoch(dt):
    # nyct-gtfs builds naive datetimes via datetime.fromtimestamp (host-local).
    # datetime.timestamp() on naive values assumes the same host-local zone,
    # so this round-trips to the original feed epoch on any host TZ.
    return int(dt.timestamp())


def _feed_for(route):
    if route in FEED_FOR_ROUTE:
        return FEED_FOR_ROUTE[route]
    base = route[:-1] if route.endswith("X") else None
    if base and base in FEED_FOR_ROUTE:
        return FEED_FOR_ROUTE[base]
    return None


def load_stations(zip_path=STATIC_ZIP):
    z = zipfile.ZipFile(zip_path)
    with z.open("stops.txt") as f:
        stops = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))
    parents = [s for s in stops if not s.get("parent_station")]
    # routes per parent: child ids are "<parent>N/S", trips give route per child
    with z.open("stop_times.txt") as f:
        st = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        trip_routes = {}
        with z.open("trips.txt") as f2:
            for t in csv.DictReader(io.TextIOWrapper(f2, encoding="utf-8-sig")):
                trip_routes[t["trip_id"]] = t["route_id"]
        parent_routes = {}
        for row in st:
            sid = row["stop_id"]
            pid = sid[:-1] if sid[-1:] in ("N", "S") else None
            if pid and row["trip_id"] in trip_routes:
                parent_routes.setdefault(pid, set()).add(trip_routes[row["trip_id"]])
    out = []
    for p in parents:
        out.append(
            {
                "id": p["stop_id"],
                "name": p["stop_name"],
                "lat": float(p["stop_lat"]),
                "lon": float(p["stop_lon"]),
                "routes": sorted(parent_routes.get(p["stop_id"], [])),
            }
        )
    return out


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _fetch_feed(feed_key):
    url = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2F" + feed_key
    response = requests.get(url, timeout=(2, 4))
    response.raise_for_status()
    feed = NYCTFeed(FEED_SEED_ROUTE[feed_key], fetch_immediately=False)
    feed.load_gtfs_bytes(response.content)
    if time.time() - feed.last_generated.timestamp() > FEED_MAX_AGE_S:
        raise RuntimeError("MTA feed is stale")
    return feed


def get_feed(feed_key):
    # Singleflight: concurrent misses for one feed share a single fetch.
    # The lock is never held during HTTP; waiters get (feed, False) since
    # the data is fresh, not cached.
    for _ in range(2):
        now = time.time()
        with feeds_lock:
            hit = feeds.get(feed_key)
            if hit and now - hit[0] < FEED_TTL_S:
                return hit[1], True
            inflight = feeds_inflight.get(feed_key)
            if inflight is None:
                inflight = _Inflight()
                feeds_inflight[feed_key] = inflight
                leader = True
            else:
                leader = False
        if not leader:
            inflight.event.wait(timeout=30)
            if inflight.error is not None:
                raise inflight.error
            if inflight.result is not None:
                return inflight.result
            continue  # leader vanished; retry as a new contender
        try:
            feed = _fetch_feed(feed_key)
        except Exception as exc:
            with feeds_lock:
                del feeds_inflight[feed_key]
                inflight.error = exc
                inflight.event.set()
            raise
        with feeds_lock:
            feeds[feed_key] = (now, feed)
            del feeds_inflight[feed_key]
            inflight.result = (feed, False)
            inflight.event.set()
        return feed, False
    feed = _fetch_feed(feed_key)
    with feeds_lock:
        feeds[feed_key] = (time.time(), feed)
    return feed, False


def board(lat, lon, limit_stations=1, limit_arrivals=4, station_id=None, route=None, dest=None, direction=None):
    if direction not in (None, 'N', 'S'):
        raise ValueError('Invalid direction')
    anchor = None
    if station_id:
        anchor = next((s for s in stations if s['id'] == station_id), None)
        if anchor is None:
            raise ValueError('Unknown station')
        selected = [s for s in stations if s['id'] == station_id or
                    (s['name'] == anchor['name'] and
                     haversine_m(s['lat'], s['lon'], anchor['lat'], anchor['lon']) < 300)]
    else:
        ranked = sorted(stations, key=lambda s: haversine_m(lat, lon, s["lat"], s["lon"]))
        selected = ranked[:limit_stations]
    needed_feeds = {_feed_for(r) for s in selected for r in s["routes"]}
    needed_feeds.discard(None)
    loaded = {}
    failures = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = {fk: pool.submit(get_feed, fk) for fk in needed_feeds}
        for fk, future in pending.items():
            try:
                loaded[fk] = future.result()
            except Exception as exc:
                failures.append(fk)
                LOG.error("upstream_failure feed=%s type=%s", fk, type(exc).__name__)
    if needed_feeds and not loaded:
        raise RuntimeError("MTA feeds unavailable")
    now = time.time()
    result_stations = []
    used_cache = bool(needed_feeds)
    for s in selected:
        needed = {_feed_for(r) for r in s["routes"]}
        needed.discard(None)
        arrs = []
        for fk in needed:
            if fk not in loaded:
                continue
            feed, cached = loaded[fk]
            used_cache = used_cache and cached
            for trip in feed.trips:
                if trip.route_id not in s["routes"]:
                    continue
                for stu in trip.stop_time_updates:
                    if stu.stop_id not in (s["id"] + "N", s["id"] + "S"):
                        continue
                    if stu.arrival is None:
                        continue
                    arrival_at = _epoch(stu.arrival)
                    seconds = arrival_at - now
                    if seconds < 0:
                        continue
                    arrs.append(
                        {
                            "route": trip.route_id,
                            "dest": (trip.headsign_text or "").strip(),
                            "dir": "N" if stu.stop_id.endswith("N") else "S",
                            "mins": int(seconds // 60),
                            "arrival_at": arrival_at,
                        }
                    )
        arrs.sort(key=lambda a: a["arrival_at"])
        result_stations.append(
            {
                "station": {
                    "id": s["id"],
                    "name": s["name"],
                    "lat": s["lat"],
                    "lon": s["lon"],
                    "distance_m": round(haversine_m(lat, lon, s["lat"], s["lon"])),
                    "routes": s["routes"],
                    **({"entrances": entrances[s["id"]]} if s["id"] in entrances else {}),
                },
                "arrivals": arrs,
                "partial": any(fk not in loaded for fk in needed),
            }
        )
    result_stations = merge_complexes(result_stations, 100000)
    if anchor is not None and result_stations:
        # Keep the requested station identity; arrivals/routes are merged
        # from the whole complex but the id must not depend on file order.
        first = result_stations[0]
        first["station"]["id"] = anchor["id"]
        first["station"]["name"] = anchor["name"]
        first["station"]["lat"] = anchor["lat"]
        first["station"]["lon"] = anchor["lon"]
        first["station"]["distance_m"] = round(haversine_m(lat, lon, anchor["lat"], anchor["lon"]))
    for e in result_stations:
        e['options'] = [dict(route=r, dest=d, dir=di) for r, d, di in sorted({
            (a['route'], a['dest'], a['dir']) for a in e['arrivals']})]
        e["arrivals"] = [a for a in e['arrivals'] if
                         (not route or a['route'] == route) and
                         (not dest or a['dest'] == dest) and
                         (not direction or a['dir'] == direction)][:limit_arrivals]
    return {
        "stations": result_stations,
        "cached": used_cache,
        "failed_feeds": failures,
    }


def merge_complexes(entries, limit_arrivals):
    """Merge same-name GTFS stations (e.g. Union Sq = R20 + 635 + L03)."""
    merged = []
    for e in entries:
        m = next((item for item in merged
                  if item["station"]["name"] == e["station"]["name"]
                  and haversine_m(item["station"]["lat"], item["station"]["lon"],
                                  e["station"]["lat"], e["station"]["lon"]) < 300), None)
        if m is not None:
            m["arrivals"] = sorted(
                m["arrivals"] + e["arrivals"], key=lambda a: a["arrival_at"]
            )[:limit_arrivals]
            m["station"]["routes"] = sorted(
                set(m["station"]["routes"]) | set(e["station"]["routes"])
            )
            m["station"]["distance_m"] = min(
                m["station"]["distance_m"], e["station"]["distance_m"]
            )
            if "entrances" in m["station"] or "entrances" in e["station"]:
                m["station"]["entrances"] = combine(m["station"].get("entrances", []), e["station"].get("entrances", []))
            m["partial"] = m.get("partial", False) or e.get("partial", False)
        else:
            merged.append(e)
    return merged


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Base handler includes raw URLs (GPS); use structured logs below.

    def log_request(self, code="-", size="-"):
        LOG.info("request method=%s path=%s status=%s duration_ms=%.1f",
                 self.command, urllib.parse.urlsplit(self.path).path, code,
                 (time.monotonic() - getattr(self, "started", time.monotonic())) * 1000)

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.started = time.monotonic()
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/mta/health":
            self.send_json({"ok": True})
        elif u.path == "/mta/board":
            try:
                lat = float(q["lat"][0])
                lon = float(q["lon"][0])
                ls = int(q.get("limitStations", ["1"])[0])
                la = int(q.get("limitArrivals", ["4"])[0])
                if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                    raise ValueError("lat/lon out of range")
                ls = min(max(ls, 1), 5)
                la = min(max(la, 1), 8)
                self.send_json(board(lat, lon, ls, la, q.get('station', [None])[0],
                                     q.get('route', [None])[0], q.get('dest', [None])[0],
                                     q.get('dir', [None])[0]))
            except (ValueError, KeyError) as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as exc:
                LOG.error("board_failure type=%s", type(exc).__name__)
                self.send_json({"error": "MTA feeds unavailable"}, 503)
        else:
            self.send_json({"error": "not found"}, 404)


def _mtime_str(path):
    if not os.path.exists(path):
        return "missing"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path)))


def load_station_cache(cache_path="stations.json", zip_path=STATIC_ZIP):
    """Return (stations, source) with source 'cache' or 'zip'.

    The release tarball ships stations.json without the static zip, so a
    missing zip with a present cache is the expected server layout, not an
    error. A stale cache (older than the zip) triggers a rebuild.
    """
    use_cache = os.path.exists(cache_path) and (
        not os.path.exists(zip_path)
        or os.path.getmtime(cache_path) >= os.path.getmtime(zip_path)
    )
    if use_cache:
        with open(cache_path) as f:
            return json.load(f), "cache"
    stations = load_stations(zip_path)
    with open(cache_path, "w") as f:
        json.dump(stations, f)
    return stations, "zip"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    LOG.info("loading stations")
    stations, source = load_station_cache()
    LOG.info(
        "stations source=%s count=%d cache_mtime=%s zip_mtime=%s",
        source,
        len(stations),
        _mtime_str("stations.json"),
        _mtime_str(STATIC_ZIP),
    )
    entrances = load_entrances(stations)
    print(f"{len(stations)} stations", flush=True)
    srv = ThreadingHTTPServer((bind_address(), PORT), Handler)
    print(f"listening on {bind_address()}:{PORT}", flush=True)
    srv.serve_forever()
