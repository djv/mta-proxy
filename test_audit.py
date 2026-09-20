import datetime
import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import server


def setUpModule():
    patcher = patch.object(server, "get_alerts", return_value=([], False))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def _station(sid, name="Union", lat=40.735, lon=-73.991, routes=("L",)):
    return dict(id=sid, name=name, lat=lat, lon=lon, routes=list(routes))


def _trip(route, stop_id, now, offset, dest="8 Av"):
    return SimpleNamespace(
        route_id=route,
        headsign_text=dest,
        stop_time_updates=[
            SimpleNamespace(
                stop_id=stop_id,
                arrival=datetime.datetime.fromtimestamp(now + offset),
            )
        ],
    )


class AuditTests(unittest.TestCase):
    def test_gs_shuttle_uses_numbered_feed(self):
        now = 1789080000
        feed = SimpleNamespace(
            trips=[_trip("GS", "901N", now, 60, "Times Sq")],
            last_generated=datetime.datetime.fromtimestamp(now),
        )
        seen = []

        def fetch(key):
            seen.append(key)
            return feed, False

        with (
            patch.object(server, "stations", [_station("901", routes=("GS",))]),
            patch.object(server.time, "time", return_value=now),
            patch.object(server, "get_feed", side_effect=fetch),
        ):
            result = server.board(40.75, -73.98, station_id="901")
        self.assertEqual(seen, ["gtfs"])
        arrivals = result["stations"][0]["arrivals"]
        self.assertEqual(len(arrivals), 1)
        self.assertFalse(result["stations"][0]["partial"])
        self.assertEqual(result["stations"][0]["station"]["routes"], ["GS"])

    def test_express_variant_falls_back_to_base_feed(self):
        now = 1789080000
        feed = SimpleNamespace(
            trips=[_trip("6X", "601N", now, 60, "Pelham")],
            last_generated=datetime.datetime.fromtimestamp(now),
        )
        seen = []

        def fetch(key):
            seen.append(key)
            return feed, False

        with (
            patch.object(server, "stations", [_station("601", routes=("6X",))]),
            patch.object(server.time, "time", return_value=now),
            patch.object(server, "get_feed", side_effect=fetch),
        ):
            result = server.board(40.8, -73.8)
        self.assertEqual(seen, ["gtfs"])
        self.assertEqual(result["stations"][0]["arrivals"][0]["route"], "6X")

    def test_sir_maps_to_si_feed(self):
        self.assertEqual(server._feed_for("SIR"), "gtfs-si")

    def test_feed_for_unknown_and_degenerate(self):
        self.assertIsNone(server._feed_for("ZZ"))
        self.assertIsNone(server._feed_for("X"))
        self.assertIsNone(server._feed_for(""))

    def test_anchor_id_preserved_after_complex_merge(self):
        now = 1789080000
        trips = [
            _trip("L", "L03N", now, 60, "8 Av"),
            _trip("4", "635N", now, 120, "Woodlawn"),
        ]
        feed = SimpleNamespace(
            trips=trips, last_generated=datetime.datetime.fromtimestamp(now)
        )
        stations = [
            _station("635", routes=("4", "5", "6")),
            _station("L03", routes=("L",)),
        ]
        with (
            patch.object(server, "stations", stations),
            patch.object(server.time, "time", return_value=now),
            patch.object(server, "get_feed", return_value=(feed, True)),
        ):
            result = server.board(0, 0, station_id="L03")
        self.assertEqual(len(result["stations"]), 1)
        st = result["stations"][0]["station"]
        self.assertEqual(st["id"], "L03")
        self.assertIn("L", st["routes"])
        self.assertIn("4", st["routes"])

    def test_anchor_merge_splits_distant_namesakes(self):
        # Members on opposite sides of the anchor (>300m apart) form two groups;
        # the anchor group keeps the requested id, the other keeps its own.
        anchor = _station("a", lat=40.0, lon=-74.0)
        far_west = _station("b", lat=40.0, lon=-74.0018)
        far_east = _station("c", lat=40.0, lon=-73.9982)
        for s in (far_west, far_east):
            s["distance_m"] = 10
        anchor["distance_m"] = 5
        entries = [
            dict(station=far_west, arrivals=[], partial=False),
            dict(station=anchor, arrivals=[], partial=False),
            dict(station=far_east, arrivals=[], partial=False),
        ]
        merged = server.merge_complexes(entries, 4)
        self.assertEqual(len(merged), 2)

    def test_station_with_only_unknown_routes_is_not_cached(self):
        with (
            patch.object(server, "stations", [_station("ZZ1", routes=("ZZ",))]),
            patch.object(server, "get_feed") as get,
        ):
            result = server.board(40.7, -74.0)
        get.assert_not_called()
        self.assertFalse(result["cached"])
        self.assertEqual(result["stations"][0]["arrivals"], [])
        self.assertEqual(result["failed_feeds"], [])

    def test_epoch_roundtrips_naive_host_local(self):
        ts = 1789247076
        naive = datetime.datetime.fromtimestamp(ts)
        self.assertEqual(server._epoch(naive), ts)


class CacheTests(unittest.TestCase):
    def _write(self, path, obj, mtime):
        path.write_text(json.dumps(obj))
        os.utime(path, (mtime, mtime))

    def test_cache_newer_than_zip_wins(self):
        with patch.object(server, "load_stations") as loader:
            with tempfile.TemporaryDirectory() as tmp:
                cache = pathlib.Path(tmp) / "stations.json"
                zipf = pathlib.Path(tmp) / "static.zip"
                self._write(cache, [{"id": "X"}], 2000)
                zipf.write_bytes(b"zip")
                os.utime(zipf, (1000, 1000))
                stations, source = server.load_station_cache(str(cache), str(zipf))
        loader.assert_not_called()
        self.assertEqual(source, "cache")
        self.assertEqual(stations, [{"id": "X"}])

    def test_stale_cache_rebuilds_from_zip(self):
        sentinel = [{"id": " rebuilt"}]
        with patch.object(server, "load_stations", return_value=sentinel) as loader:
            with tempfile.TemporaryDirectory() as tmp:
                cache = pathlib.Path(tmp) / "stations.json"
                zipf = pathlib.Path(tmp) / "static.zip"
                self._write(cache, [{"id": "old"}], 1000)
                zipf.write_bytes(b"zip")
                os.utime(zipf, (2000, 2000))
                stations, source = server.load_station_cache(str(cache), str(zipf))
                self.assertEqual(cache.read_text(), '[{"id": " rebuilt"}]')
        loader.assert_called_once_with(str(zipf))
        self.assertEqual(source, "zip")
        self.assertEqual(stations, sentinel)

    def test_missing_zip_with_cache_is_expected_server_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = pathlib.Path(tmp) / "stations.json"
            self._write(cache, [{"id": "X"}], 1000)
            stations, source = server.load_station_cache(
                str(cache), str(pathlib.Path(tmp) / "absent.zip")
            )
        self.assertEqual(source, "cache")
        self.assertEqual(stations, [{"id": "X"}])

    def test_mtime_str(self):
        self.assertEqual(server._mtime_str("/nonexistent/path/xyz"), "missing")
        self.assertRegex(server._mtime_str(__file__), r"^20\d\d-")


class SingleflightTests(unittest.TestCase):
    def setUp(self):
        server.feeds.clear()
        if hasattr(server, "feeds_inflight"):
            server.feeds_inflight.clear()
            self.addCleanup(server.feeds_inflight.clear)
        self.addCleanup(server.feeds.clear)

    def _spawn(self, n, fn):
        results, errors = [None] * n, [None] * n
        start = threading.Barrier(n + 1)

        def work(i):
            start.wait(timeout=10)
            try:
                results[i] = fn()
            except Exception as exc:  # noqa: BLE001
                errors[i] = exc

        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        start.wait(timeout=10)
        return threads, results, errors

    def _join(self, threads):
        for t in threads:
            t.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads))

    def _wait_for_calls(self, calls, n=1, deadline_s=10):
        end = time.monotonic() + deadline_s
        while len(calls) < n and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(len(calls), n)

    def _mock_fetch(self, now, calls, release, fail=None):
        feed_inst = SimpleNamespace(
            load_gtfs_bytes=lambda b: None,
            last_generated=datetime.datetime.fromtimestamp(now),
        )

        def fake_get(url, timeout):
            calls.append(url)
            assert release.wait(timeout=10), "leader released"
            if fail is not None:
                raise fail
            resp = SimpleNamespace(content=b"bytes")
            resp.raise_for_status = lambda: None
            return resp

        return feed_inst, fake_get

    @contextmanager
    def _patched(self, now, feed_inst, fake_get):
        with (
            patch.object(server.requests, "get", side_effect=fake_get),
            patch.object(server, "NYCTFeed", return_value=feed_inst),
            patch.object(server.time, "time", return_value=now),
        ):
            yield

    def test_concurrent_misses_share_one_fetch(self):
        now = 1789080000
        calls, release = [], threading.Event()
        feed_inst, fake_get = self._mock_fetch(now, calls, release)
        with self._patched(now, feed_inst, fake_get):
            threads, results, errors = self._spawn(8, lambda: server.get_feed("gtfs-l"))
            self._wait_for_calls(calls)
            time.sleep(0.3)  # let waiters pile onto the in-flight fetch
            release.set()
            self._join(threads)
        self.assertEqual(errors, [None] * 8)
        for feed, cached in results:
            self.assertIs(feed, feed_inst)
            self.assertFalse(cached)

    def test_shared_failure_reaches_all_waiters_once(self):
        now = 1789080000
        calls, release = [], threading.Event()
        feed_inst, fake_get = self._mock_fetch(
            now, calls, release, fail=RuntimeError("down")
        )
        with self._patched(now, feed_inst, fake_get):
            threads, results, errors = self._spawn(4, lambda: server.get_feed("gtfs-l"))
            self._wait_for_calls(calls)
            time.sleep(0.3)
            release.set()
            self._join(threads)
        self.assertTrue(all(r is None for r in results))
        self.assertTrue(all(isinstance(e, RuntimeError) for e in errors))

    def test_hit_after_shared_fetch_is_cached(self):
        now = 1789080000
        calls, release = [], threading.Event()
        release.set()
        feed_inst, fake_get = self._mock_fetch(now, calls, release)
        with self._patched(now, feed_inst, fake_get):
            server.get_feed("gtfs-l")
            feed, cached = server.get_feed("gtfs-l")
        self.assertEqual(len(calls), 1)
        self.assertIs(feed, feed_inst)
        self.assertTrue(cached)

    def test_stale_feed_rejected(self):
        now = 1789080000
        calls, release = [], threading.Event()
        release.set()
        old = SimpleNamespace(
            load_gtfs_bytes=lambda b: None,
            last_generated=datetime.datetime.fromtimestamp(now - 9999),
        )
        _, fake_get = self._mock_fetch(now, calls, release)
        with (
            patch.object(server.requests, "get", side_effect=fake_get),
            patch.object(server, "NYCTFeed", return_value=old),
            patch.object(server.time, "time", return_value=now),
        ):
            with self.assertRaisesRegex(RuntimeError, "stale"):
                server.get_feed("gtfs-l")
        self.assertEqual(len(calls), 1)
        self.assertEqual(server.feeds, {})


if __name__ == "__main__":
    unittest.main()
