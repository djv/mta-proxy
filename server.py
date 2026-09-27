#!/usr/bin/env python3
"""MTA proxy for Garmin watches. stdlib only (+nyct-gtfs).

GET /mta/health
GET /mta/board?lat=..&lon=..&limitStations=1&limitArrivals=4
GET /mta/reach?lat=..&lon=..&radius=700[&at=EPOCH]
POST /mta/pack {key, lat, lon, stations}   GET /mta/pack?key=..
"""

import csv
import datetime
import io
import json
import math
import re
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
from alerts import ALERTS_URL, for_station, parse_alerts
from timetable import NYC, Holder


LOG = logging.getLogger("mta-proxy")


def bind_address():
    return os.environ.get("MTA_PROXY_BIND", "127.0.0.1")


PORT = int(os.environ.get("MTA_PROXY_PORT", "8087"))
STATIC_ZIP = "google_transit.zip"
FEED_TTL_S = 25
FEED_MAX_AGE_S = 180
ALERTS_TTL_S = 60
STATE_DIR = os.environ.get("MTA_PROXY_STATE", os.path.expanduser("~/mta-proxy/state"))
PACK_TTL_S = 36 * 3600
PACK_MAX = 200  # stored packs; oldest dropped first
PACK_STATIONS = 4
PACK_DEPS = 30
PACK_KEY_RE = re.compile(r"^[a-z0-9]{10,32}$")
WALK_MPS = 4.9 / 3.6 / 1.3  # 4.9 km/h over the straight line with a 1.3x street-grid detour

stations = []  # [{id,name,lat,lon,routes[]}]
entrances = {}
feeds = {}  # feed_key -> (fetched_at, NYCTFeed)
feeds_lock = threading.Lock()
feeds_inflight = {}  # feed_key -> _Inflight
alerts_cache = (0.0, [], False)  # (fetched_at, alerts, available)
alerts_lock = threading.Lock()
timetable = Holder(STATE_DIR)
packs_lock = threading.Lock()


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


def _fetch_alerts():
    response = requests.get(ALERTS_URL, timeout=(2, 4))
    response.raise_for_status()
    return parse_alerts(response.content)


def get_alerts():
    """Return (alerts, available); failures degrade to empty, never raise."""
    global alerts_cache
    now = time.time()
    with alerts_lock:
        fetched_at, data, available = alerts_cache
        if fetched_at and now - fetched_at < ALERTS_TTL_S:
            return data, available
    try:
        data, available = _fetch_alerts(), True
    except Exception as exc:
        LOG.error("alerts_failure type=%s", type(exc).__name__)
        data, available = [], False
    with alerts_lock:
        alerts_cache = (now, data, available)
    return data, available


def board(
    lat,
    lon,
    limit_stations=1,
    limit_arrivals=4,
    station_id=None,
    route=None,
    dest=None,
    direction=None,
):
    if direction not in (None, "N", "S"):
        raise ValueError("Invalid direction")
    anchor = None
    if station_id:
        anchor = next((s for s in stations if s["id"] == station_id), None)
        if anchor is None:
            raise ValueError("Unknown station")
        selected = [
            s
            for s in stations
            if s["id"] == station_id
            or (
                s["name"] == anchor["name"]
                and haversine_m(s["lat"], s["lon"], anchor["lat"], anchor["lon"]) < 300
            )
        ]
    else:
        ranked = sorted(
            stations, key=lambda s: haversine_m(lat, lon, s["lat"], s["lon"])
        )
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
                    **(
                        {"entrances": entrances[s["id"]]}
                        if s["id"] in entrances
                        else {}
                    ),
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
        first["station"]["distance_m"] = round(
            haversine_m(lat, lon, anchor["lat"], anchor["lon"])
        )
    for e in result_stations:
        e["options"] = [
            dict(route=r, dest=d, dir=di)
            for r, d, di in sorted(
                {(a["route"], a["dest"], a["dir"]) for a in e["arrivals"]}
            )
        ]
        e["arrivals"] = [
            a
            for a in e["arrivals"]
            if (not route or a["route"] == route)
            and (not dest or a["dest"] == dest)
            and (not direction or a["dir"] == direction)
        ][:limit_arrivals]
    alerts, alerts_available = get_alerts()
    for e in result_stations:
        hits = for_station(alerts, e["station"]["routes"], e["station"]["id"])
        if hits:
            e["station"]["alerts"] = [
                {"title": hit["title"], "desc": hit["desc"], "routes": hit["routes"]}
                for hit in hits
            ]
    return {
        "stations": result_stations,
        "cached": used_cache,
        "failed_feeds": failures,
        "alerts_available": alerts_available,
    }


def merge_complexes(entries, limit_arrivals):
    """Merge same-name GTFS stations (e.g. Union Sq = R20 + 635 + L03)."""
    merged = []
    for e in entries:
        m = next(
            (
                item
                for item in merged
                if item["station"]["name"] == e["station"]["name"]
                and haversine_m(
                    item["station"]["lat"],
                    item["station"]["lon"],
                    e["station"]["lat"],
                    e["station"]["lon"],
                )
                < 300
            ),
            None,
        )
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
                m["station"]["entrances"] = combine(
                    m["station"].get("entrances", []), e["station"].get("entrances", [])
                )
            m["partial"] = m.get("partial", False) or e.get("partial", False)
        else:
            merged.append(e)
    return merged


def complex_ids(station_id):
    """GTFS parent ids of the station complex containing station_id (same name within 300 m)."""
    anchor = next((s for s in stations if s["id"] == station_id), None)
    if anchor is None:
        raise ValueError("Unknown station")
    return [s["id"] for s in stations if s["name"] == anchor["name"]
            and haversine_m(s["lat"], s["lon"], anchor["lat"], anchor["lon"]) < 300], anchor


def home_stations(lat, lon, radius):
    """Stations within radius of home -> walking seconds from them to the door."""
    return {s["id"]: round(haversine_m(lat, lon, s["lat"], s["lon"]) / WALK_MPS)
            for s in stations if haversine_m(lat, lon, s["lat"], s["lon"]) <= radius}


def reach(lat, lon, radius=700, at=None):
    tt = timetable.get()
    if tt is None:
        raise RuntimeError("timetable loading")
    homes = home_stations(lat, lon, radius)
    when = datetime.datetime.fromtimestamp(at or time.time(), NYC)
    table = tt.reach(homes, when.date(), when.hour * 3600 + when.minute * 60)
    by_id = {s["id"]: s for s in stations}
    out = []
    for sid, info in table.items():
        s = by_id.get(sid)
        if s is None:
            continue
        out.append({"id": sid, "name": s["name"], "lat": s["lat"], "lon": s["lon"],
                    **({"entrances": entrances[sid]} if sid in entrances else {}), **info})
    out.sort(key=lambda x: haversine_m(lat, lon, x["lat"], x["lon"]))
    home = [{"id": sid, "name": by_id[sid]["name"], "walk_s": w} for sid, w in sorted(homes.items(), key=lambda x: x[1])]
    return {"home": home, "stations": out}


def _packs_path():
    return os.path.join(STATE_DIR, "packs.json")


def _load_packs():
    try:
        with open(_packs_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_pack(body, now=None):
    """Store the stations of a planned run under a client-chosen key."""
    now = int(now or time.time())
    key = body.get("key")
    if not isinstance(key, str) or not PACK_KEY_RE.match(key):
        raise ValueError("key must be 10-32 lowercase letters/digits")
    lat, lon = float(body["lat"]), float(body["lon"])
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("lat/lon out of range")
    ids = body.get("stations")
    if not isinstance(ids, list) or not ids:
        raise ValueError("stations must be a non-empty list")
    known = {s["id"] for s in stations}
    ids = [i for i in dict.fromkeys(ids) if i in known][:PACK_STATIONS]
    if not ids:
        raise ValueError("Unknown station")
    radius = min(max(int(body.get("radius", 700)), 100), 1500)
    with packs_lock:
        packs = {k: v for k, v in _load_packs().items() if now - v["created"] < PACK_TTL_S}
        packs[key] = {"created": now, "lat": lat, "lon": lon, "radius": radius, "stations": ids}
        for k in sorted(packs, key=lambda k: packs[k]["created"])[:-PACK_MAX]:
            del packs[k]
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = _packs_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(packs, f)
        os.replace(tmp, _packs_path())
    return {"ok": True, "stations": ids, "expires": now + PACK_TTL_S}


def get_pack(key, hours=4, now=None):
    """Scheduled homebound departures for a stored pack, compact enough for a watch background task.

    Each station's `d` is a flat list [departure epoch, route index, minutes to the door, ...];
    `h` holds one index into `heads` (the train's terminal) per departure.
    """
    now = int(now or time.time())
    with packs_lock:
        pack = _load_packs().get(key) if isinstance(key, str) and PACK_KEY_RE.match(key) else None
    if pack is None or now - pack["created"] >= PACK_TTL_S:
        return None
    tt = timetable.get()
    if tt is None:
        raise RuntimeError("timetable loading")
    homes = home_stations(pack["lat"], pack["lon"], pack["radius"])
    alerts, _ = get_alerts()
    out = []
    for sid in pack["stations"]:
        ids, anchor = complex_ids(sid)
        deps = tt.homebound(ids, homes, now - 60, hours, PACK_DEPS)
        routes = sorted({d[0] for d in deps})
        heads = sorted({d[4] for d in deps})
        flat, h = [], []
        for route, dep, door, _, head in deps:
            flat += [dep, routes.index(route), round((door - dep) / 60)]
            h.append(heads.index(head))
        ent = []
        for i in ids:
            ent = combine(ent, entrances.get(i, []))
        hits = for_station(alerts, sorted({r for s in stations if s["id"] in ids for r in s["routes"]}), sid)
        out.append({"id": sid, "name": anchor["name"], "lat": anchor["lat"], "lon": anchor["lon"],
                    "routes": routes, "entrances": ent[:6],
                    "alerts": [{"title": a["title"][:100]} for a in hits[:2]], "d": flat,
                    "heads": [x[:24] for x in heads], "h": h})
    return {"v": 1, "generated": now, "expires": pack["created"] + PACK_TTL_S, "stations": out}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Base handler includes raw URLs (GPS); use structured logs below.

    def log_request(self, code="-", size="-"):
        LOG.info(
            "request method=%s path=%s status=%s duration_ms=%.1f",
            self.command,
            urllib.parse.urlsplit(self.path).path,
            code,
            (time.monotonic() - getattr(self, "started", time.monotonic())) * 1000,
        )

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
                self.send_json(
                    board(
                        lat,
                        lon,
                        ls,
                        la,
                        q.get("station", [None])[0],
                        q.get("route", [None])[0],
                        q.get("dest", [None])[0],
                        q.get("dir", [None])[0],
                    )
                )
            except (ValueError, KeyError) as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as exc:
                LOG.error("board_failure type=%s", type(exc).__name__)
                self.send_json({"error": "MTA feeds unavailable"}, 503)
        elif u.path == "/mta/reach":
            try:
                lat, lon = float(q["lat"][0]), float(q["lon"][0])
                radius = min(max(int(q.get("radius", ["700"])[0]), 100), 1500)
                at = int(q["at"][0]) if "at" in q else None
                self.send_json(reach(lat, lon, radius, at))
            except (ValueError, KeyError) as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as exc:
                LOG.error("reach_failure type=%s", type(exc).__name__)
                self.send_json({"error": "timetable unavailable"}, 503)
        elif u.path == "/mta/pack":
            try:
                hours = min(max(float(q.get("hours", ["4"])[0]), 1), 6)
                pack = get_pack(q.get("key", [""])[0], hours)
                if pack is None:
                    self.send_json({"error": "no pack"}, 404)
                else:
                    self.send_json(pack)
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as exc:
                LOG.error("pack_failure type=%s", type(exc).__name__)
                self.send_json({"error": "timetable unavailable"}, 503)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        self.started = time.monotonic()
        u = urllib.parse.urlparse(self.path)
        if u.path != "/mta/pack":
            self.send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("body must be 1-4096 bytes")
            self.send_json(save_pack(json.loads(self.rfile.read(length))))
        except (ValueError, KeyError, TypeError) as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as exc:
            LOG.error("pack_save_failure type=%s", type(exc).__name__)
            self.send_json({"error": "could not save"}, 500)


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
    timetable.start()
    print(f"{len(stations)} stations", flush=True)
    srv = ThreadingHTTPServer((bind_address(), PORT), Handler)
    print(f"listening on {bind_address()}:{PORT}", flush=True)
    srv.serve_forever()
