import datetime
import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import server
import timetable
from timetable import NYC, Timetable

# 2026-09-28 is a Monday.
MON = datetime.date(2026, 9, 28)


def epoch(d, hms):
    h, m = map(int, hms.split(":")[:2])
    return int(datetime.datetime.combine(d, datetime.time(), NYC).timestamp()) + h * 3600 + m * 60


def fixture_zip(path):
    """Line X: A -> B -> HOME (northbound) and HOME -> B -> A; one after-midnight trip; one removed date."""
    files = {
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                        "Weekday,1,1,1,1,1,0,0,20260101,20261231\n"
                        "Sunday,0,0,0,0,0,0,1,20260101,20261231\n",
        "calendar_dates.txt": "service_id,date,exception_type\n"
                              "Weekday,20260929,2\n"
                              "Work,20260929,1\n",
        "trips.txt": "route_id,trip_id,service_id,trip_headsign,direction_id,shape_id\n"
                     "X,t_north,Weekday,Home,0,\n"
                     "X,t_south,Weekday,A,1,\n"
                     "X,t_late,Weekday,Home,0,\n"
                     "Y,t_work,Work,Home,0,\n"
                     "X,t_sun,Sunday,Home,0,\n",
        "stop_times.txt": "trip_id,stop_id,arrival_time,departure_time,stop_sequence\n"
                          "t_north,AN,08:00:00,08:00:00,1\n"
                          "t_north,BN,08:05:00,08:05:00,2\n"
                          "t_north,HN,08:12:00,08:12:00,3\n"
                          "t_south,HS,09:00:00,09:00:00,1\n"
                          "t_south,BS,09:07:00,09:07:00,2\n"
                          "t_south,AS,09:12:00,09:12:00,3\n"
                          "t_late,AN,24:30:00,24:30:00,1\n"
                          "t_late,HN,24:42:00,24:42:00,2\n"
                          "t_work,AN,10:00:00,10:00:00,1\n"
                          "t_work,HN,10:20:00,10:20:00,2\n"
                          "t_sun,AN,11:00:00,11:00:00,1\n"
                          "t_sun,HN,11:10:00,11:10:00,2\n",
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)


class TimetableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.zip = os.path.join(cls.tmp.name, "gtfs.zip")
        fixture_zip(cls.zip)
        cls.tt = Timetable.load(cls.zip, MON)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_homebound_only_toward_home(self):
        deps = self.tt.homebound({"A"}, {"H"}, epoch(MON, "07:00"), hours=4)
        self.assertEqual([(r, d, a) for r, d, a, _, _ in deps], [("X", epoch(MON, "08:00"), epoch(MON, "08:12"))])
        self.assertEqual(deps[0][4], "Home")  # headsign
        self.assertEqual(self.tt.homebound({"B"}, {"H"}, epoch(MON, "08:50"), hours=1), [])  # southbound only

    def test_walk_time_added_to_door_arrival(self):
        deps = self.tt.homebound({"A"}, {"H": 300}, epoch(MON, "07:00"), hours=2)
        self.assertEqual(deps[0][2], epoch(MON, "08:17"))

    def test_after_midnight_trip_belongs_to_previous_service_day(self):
        deps = self.tt.homebound({"A"}, {"H"}, epoch(MON + datetime.timedelta(days=1), "00:00"), hours=1)
        self.assertEqual([d for _, d, _, _, _ in deps], [epoch(MON, "24:30")])

    def test_calendar_dates_remove_and_add(self):
        tue = MON + datetime.timedelta(days=1)
        deps = self.tt.homebound({"A"}, {"H"}, epoch(tue, "07:00"), hours=4)
        self.assertEqual([r for r, _, _, _, _ in deps], ["Y"])  # Weekday removed, Work added

    def test_sunday_service(self):
        # Load for a Sunday so it is in the window.
        tt = Timetable.load(self.zip, datetime.date(2026, 10, 4))
        deps = tt.homebound({"A"}, {"H"}, epoch(datetime.date(2026, 10, 4), "10:00"), hours=2)
        self.assertEqual([r for r, _, _, _, _ in deps], ["X"])

    def test_reach_ride_and_tph(self):
        r = self.tt.reach({"H": 120}, MON, 8 * 3600)
        self.assertEqual(set(r), {"A", "B"})
        self.assertEqual(r["B"]["ride_min"], 9)  # 7 min ride + 2 min walk
        self.assertEqual(r["A"]["routes"], ["X"])
        self.assertEqual(r["A"]["tph"], 0.5)


class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        zip_path = os.path.join(self.tmp.name, "gtfs.zip")
        fixture_zip(zip_path)
        tt = Timetable.load(zip_path, MON)
        self.patches = [
            patch.object(server, "STATE_DIR", self.tmp.name),
            patch.object(server, "stations", [
                dict(id="A", name="Alpha", lat=40.70, lon=-73.99, routes=["X", "Y"]),
                dict(id="B", name="Beta", lat=40.69, lon=-73.985, routes=["X"]),
                dict(id="H", name="Home", lat=40.686, lon=-73.978, routes=["X", "Y"]),
            ]),
            patch.object(server, "entrances", {"A": [dict(lat=40.7001, lon=-73.9901)]}),
            patch.object(server, "get_alerts", return_value=([], False)),
            patch.object(server.timetable, "get", return_value=tt),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def body(self, **kw):
        return {"key": "abcdefghij12", "lat": 40.6862, "lon": -73.9776, "stations": ["A", "B"], **kw}

    def test_roundtrip(self):
        now = epoch(MON, "07:30")
        server.save_pack(self.body(), now)
        pack = server.get_pack("abcdefghij12", 4, now)
        a = pack["stations"][0]
        self.assertEqual((a["id"], a["routes"], a["entrances"]), ("A", ["X"], [dict(lat=40.7001, lon=-73.9901)]))
        self.assertEqual(a["d"][:2], [epoch(MON, "08:00"), 0])
        self.assertGreater(a["d"][2], 12)  # ride plus walk from H
        self.assertEqual(len(a["h"]), len(a["d"]) // 3)
        self.assertTrue(all(a["heads"][i] for i in a["h"]))
        self.assertLess(len(json.dumps(pack)), 4096)

    def test_bad_key_and_unknown(self):
        with self.assertRaises(ValueError):
            server.save_pack(self.body(key="SHORT"))
        with self.assertRaises(ValueError):
            server.save_pack(self.body(stations=["ZZZ"]))
        self.assertIsNone(server.get_pack("nosuchkey123"))
        self.assertIsNone(server.get_pack("../../etc"))

    def test_expiry(self):
        now = epoch(MON, "07:30")
        server.save_pack(self.body(), now)
        self.assertIsNone(server.get_pack("abcdefghij12", 4, now + server.PACK_TTL_S))

    def test_station_cap_and_dedupe(self):
        res = server.save_pack(self.body(stations=["A", "A", "B", "H", "B"]), epoch(MON, "07:00"))
        self.assertEqual(res["stations"], ["A", "B", "H"])

    def test_oldest_packs_evicted(self):
        with patch.object(server, "PACK_MAX", 2):
            for i, k in enumerate(["aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc"]):
                server.save_pack(self.body(key=k), epoch(MON, "07:00") + i)
            self.assertIsNone(server.get_pack("aaaaaaaaaa", 4, epoch(MON, "07:10")))
            self.assertIsNotNone(server.get_pack("cccccccccc", 4, epoch(MON, "07:10")))


if __name__ == "__main__":
    unittest.main()
