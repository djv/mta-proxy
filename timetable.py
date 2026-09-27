"""Scheduled subway departures from MTA's supplemented GTFS (planned work included).

`Timetable.load(zip_path, today)` keeps only trips running from yesterday to
two days ahead, stored compactly per trip. `homebound` lists scheduled trains
from a station that later stop at one of the "home" stations; `reach` lists
every station with such a one-seat ride.
"""

import csv
import sys
import datetime
import io
import logging
import os
import threading
import time
import zipfile
from array import array
from zoneinfo import ZoneInfo

import requests

LOG = logging.getLogger("mta-proxy")
NYC = ZoneInfo("America/New_York")
SUPPLEMENTED_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_supplemented.zip"
REFRESH_S = 6 * 3600
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _secs(hms):
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _parent(stop_id):
    return stop_id[:-1] if stop_id[-1:] in ("N", "S") else stop_id


def _rows(z, name):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


class Timetable:
    def __init__(self):
        self.stops = []  # index -> parent station id
        self.trip_route = []
        self.trip_service = []
        self.trip_head = []  # trip_headsign (terminal), interned
        self.trip_stops = []  # array('H') of stop indexes, in stop order
        self.trip_secs = []  # array('i') of departure seconds after service-day midnight
        self.at_stop = {}  # parent id -> [(trip index, position)]
        self.active = {}  # date -> set of service ids
        self._reach_cache = {}

    @classmethod
    def load(cls, zip_path, today=None):
        today = today or datetime.datetime.now(NYC).date()
        days = [today + datetime.timedelta(days=d) for d in (-1, 0, 1, 2)]
        tt = cls()
        z = zipfile.ZipFile(zip_path)
        for d in days:
            tt.active[d] = set()
        for row in _rows(z, "calendar.txt"):
            start = datetime.datetime.strptime(row["start_date"], "%Y%m%d").date()
            end = datetime.datetime.strptime(row["end_date"], "%Y%m%d").date()
            for d in days:
                if start <= d <= end and row[WEEKDAYS[d.weekday()]] == "1":
                    tt.active[d].add(row["service_id"])
        if "calendar_dates.txt" in z.namelist():
            for row in _rows(z, "calendar_dates.txt"):
                d = datetime.datetime.strptime(row["date"], "%Y%m%d").date()
                if d in tt.active:
                    if row["exception_type"] == "1":
                        tt.active[d].add(row["service_id"])
                    else:
                        tt.active[d].discard(row["service_id"])
        wanted = set().union(*tt.active.values())
        trip_index = {}
        for row in _rows(z, "trips.txt"):
            if row["service_id"] in wanted:
                trip_index[row["trip_id"]] = len(tt.trip_route)
                tt.trip_route.append(row["route_id"])
                tt.trip_service.append(row["service_id"])
                tt.trip_head.append(sys.intern(row.get("trip_headsign") or ""))
                tt.trip_stops.append([])
        stop_index = {}
        rows = [[] for _ in tt.trip_route]
        for row in _rows(z, "stop_times.txt"):
            t = trip_index.get(row["trip_id"])
            if t is None:
                continue
            p = _parent(row["stop_id"])
            if p not in stop_index:
                stop_index[p] = len(tt.stops)
                tt.stops.append(p)
            rows[t].append((int(row["stop_sequence"]), stop_index[p], _secs(row["departure_time"] or row["arrival_time"])))
        for t, r in enumerate(rows):
            r.sort()
            tt.trip_stops[t] = array("H", (s for _, s, _ in r))
            tt.trip_secs.append(array("i", (x for _, _, x in r)))
            for pos, s in enumerate(tt.trip_stops[t]):
                tt.at_stop.setdefault(tt.stops[s], []).append((t, pos))
        LOG.info("timetable trips=%d stops=%d days=%s", len(tt.trip_route), len(tt.stops), days[0])
        return tt

    def _midnight(self, d):
        return int(datetime.datetime.combine(d, datetime.time(), NYC).timestamp())

    def homebound(self, origins, homes, start, hours=4, limit=30):
        """Scheduled departures from any origin station that later reach a home station.

        homes maps station -> walking seconds from it to the door (a set means 0).
        Returns [(route, departure epoch, door arrival epoch, home station, headsign)], soonest first,
        using the home stop that gets you to the door earliest.
        """
        homes = homes if isinstance(homes, dict) else dict.fromkeys(homes, 0)
        origins = set(origins) - set(homes)
        end = start + hours * 3600
        out = {}
        for d, services in self.active.items():
            base = self._midnight(d)
            for o in origins:
                for t, pos in self.at_stop.get(o, ()):
                    if self.trip_service[t] not in services:
                        continue
                    dep = base + self.trip_secs[t][pos]
                    if not start <= dep <= end:
                        continue
                    stops = self.trip_stops[t]
                    for k in range(pos + 1, len(stops)):
                        name = self.stops[stops[k]]
                        if name in homes:
                            key = (self.trip_route[t], dep)
                            arr = base + self.trip_secs[t][k] + homes[name]
                            if key not in out or arr < out[key][2]:
                                out[key] = (self.trip_route[t], dep, arr, name, self.trip_head[t])
        return sorted(out.values(), key=lambda x: (x[1], x[2]))[:limit]

    def reach(self, homes, day=None, around=None):
        """Stations with a one-seat ride to a home station on `day`.

        Returns {station: {"routes": [...], "ride_min": median minutes to the door, "tph": trains/hour}}.
        tph counts departures within an hour either side of `around` (seconds after midnight,
        default 13:00).
        """
        day = day or datetime.datetime.now(NYC).date()
        around = 13 * 3600 if around is None else around
        key = (frozenset(homes.items() if isinstance(homes, dict) else homes), day, around // 1800)
        if key in self._reach_cache:
            return self._reach_cache[key]
        homes = homes if isinstance(homes, dict) else dict.fromkeys(homes, 0)
        services = self.active.get(day, set())
        rides, routes, deps = {}, {}, {}
        for t, stops in enumerate(self.trip_stops):
            if self.trip_service[t] not in services:
                continue
            names = [self.stops[s] for s in stops]
            secs = self.trip_secs[t]
            best = None  # earliest door arrival from a later home stop
            for k in range(len(names) - 1, -1, -1):
                if names[k] in homes:
                    door = secs[k] + homes[names[k]]
                    best = door if best is None else min(best, door)
                    continue
                if best is None:
                    continue
                s = names[k]
                rides.setdefault(s, []).append(best - secs[k])
                routes.setdefault(s, set()).add(self.trip_route[t])
                if abs(secs[k] - around) <= 3600:
                    deps.setdefault(s, set()).add((self.trip_route[t], secs[k]))
        out = {}
        for s, r in rides.items():
            r.sort()
            out[s] = {"routes": sorted(routes[s]), "ride_min": round(r[len(r) // 2] / 60),
                      "tph": round(len(deps.get(s, ())) / 2, 1)}
        self._reach_cache[key] = out
        return out


class Holder:
    """Keeps a Timetable fresh in the background; `get()` returns None until the first load."""

    def __init__(self, state_dir, url=SUPPLEMENTED_URL):
        self.state_dir, self.url = state_dir, url
        self.tt, self.loaded_for = None, None
        self.lock = threading.Lock()

    def zip_path(self):
        return os.path.join(self.state_dir, "gtfs_supplemented.zip")

    def refresh(self):
        os.makedirs(self.state_dir, exist_ok=True)
        path = self.zip_path()
        try:
            if not os.path.exists(path) or time.time() - os.path.getmtime(path) > REFRESH_S - 60:
                r = requests.get(self.url, timeout=(5, 120))
                r.raise_for_status()
                with open(path + ".tmp", "wb") as f:
                    f.write(r.content)
                os.replace(path + ".tmp", path)
        except Exception as exc:  # keep serving the previous zip
            LOG.error("timetable_download_failure type=%s", type(exc).__name__)
        if not os.path.exists(path):
            return
        today = datetime.datetime.now(NYC).date()
        tt = Timetable.load(path, today)
        with self.lock:
            self.tt, self.loaded_for = tt, today

    def get(self):
        with self.lock:
            return self.tt

    def run_forever(self):
        while True:
            try:
                self.refresh()
            except Exception as exc:
                LOG.error("timetable_failure type=%s", type(exc).__name__)
            # Reload at least hourly so the day window rolls over soon after midnight.
            for _ in range(REFRESH_S // 3600):
                time.sleep(3600)
                if datetime.datetime.now(NYC).date() != self.loaded_for:
                    break

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True, name="timetable").start()
