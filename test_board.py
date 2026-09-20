import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import server


def setUpModule():
    # Board tests do not exercise alerts; keep them offline.
    patcher = patch.object(server, "get_alerts", return_value=([], False))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def station(sid, name="Union", lon=-73.991):
    return dict(id=sid, name=name, lat=40.735, lon=lon, routes=["L"])


class BoardTests(unittest.TestCase):
    def test_direction_keeps_different_terminals_and_excludes_opposite(self):
        now = 1789080000
        trips = [
            SimpleNamespace(
                route_id="L",
                headsign_text=dest,
                stop_time_updates=[
                    SimpleNamespace(
                        stop_id="L03" + direction,
                        arrival=datetime.datetime.fromtimestamp(now + n),
                    )
                ],
            )
            for dest, direction, n in [
                ("Short turn", "N", 10),
                ("8 Av", "N", 20),
                ("Canarsie", "S", 5),
            ]
        ]
        with (
            patch.object(server, "stations", [station("L03")]),
            patch.object(server.time, "time", return_value=now),
            patch.object(
                server, "get_feed", return_value=(SimpleNamespace(trips=trips), True)
            ),
        ):
            result = server.board(0, 0, station_id="L03", route="L", direction="N")[
                "stations"
            ][0]
        self.assertEqual(
            [a["dest"] for a in result["arrivals"]], ["Short turn", "8 Av"]
        )
        self.assertEqual({o["dir"] for o in result["options"]}, {"N", "S"})

    def test_filter_before_limit_and_all_destination_options(self):
        now = 1789080000
        trips = [
            SimpleNamespace(
                route_id="L",
                headsign_text=dest,
                stop_time_updates=[
                    SimpleNamespace(
                        stop_id="L03N",
                        arrival=datetime.datetime.fromtimestamp(now + offset),
                    )
                ],
            )
            for dest, offset in [("8 Av", n) for n in range(1, 12)]
            + [("Canarsie", 100)]
        ]
        with (
            patch.object(server, "stations", [station("L03")]),
            patch.object(server.time, "time", return_value=now),
            patch.object(
                server, "get_feed", return_value=(SimpleNamespace(trips=trips), True)
            ),
        ):
            result = server.board(0, 0, 1, 4, "L03", "L", "Canarsie")["stations"][0]
        self.assertEqual([a["dest"] for a in result["arrivals"]], ["Canarsie"])
        self.assertEqual(len(result["options"]), 2)

    def test_unknown_station_rejected(self):
        with patch.object(server, "stations", [station("L03")]):
            with self.assertRaises(ValueError):
                server.board(0, 0, station_id="missing")

    def test_explicit_station_includes_complex_but_not_remote_namesake(self):
        with (
            patch.object(
                server,
                "stations",
                [station("a"), station("b"), station("c", lon=-74.1)],
            ),
            patch.object(
                server, "get_feed", return_value=(SimpleNamespace(trips=[]), True)
            ),
        ):
            result = server.board(0, 0, station_id="a")
        self.assertEqual(len(result["stations"]), 1)

    def test_past_arrivals_removed_and_exact_order(self):
        now = 1789080000
        updates = [
            SimpleNamespace(
                stop_id="L03N", arrival=datetime.datetime.fromtimestamp(now + n)
            )
            for n in [-40, 50, 10, 120]
        ]
        feed = SimpleNamespace(
            trips=[
                SimpleNamespace(
                    route_id="L", headsign_text="8 Av", stop_time_updates=updates
                )
            ]
        )
        with (
            patch.object(server, "stations", [station("L03")]),
            patch.object(server.time, "time", return_value=now),
            patch.object(server, "get_feed", return_value=(feed, True)),
        ):
            result = server.board(40.735, -73.991)
        arrivals = result["stations"][0]["arrivals"]
        self.assertEqual([a["arrival_at"] - now for a in arrivals], [10, 50, 120])
        self.assertEqual([a["mins"] for a in arrivals], [0, 0, 2])

    def test_total_feed_failure_is_error(self):
        with (
            patch.object(server, "stations", [station("L03")]),
            patch.object(server, "get_feed", side_effect=RuntimeError("down")),
        ):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                server.board(40.735, -73.991)

    def test_partial_feeds_are_explicit(self):
        s = station("L03")
        s["routes"] = ["L", "N"]

        def fetch(key):
            if key == "gtfs-l":
                raise RuntimeError("down")
            return SimpleNamespace(trips=[]), True

        with (
            patch.object(server, "stations", [s]),
            patch.object(server, "get_feed", side_effect=fetch),
        ):
            result = server.board(40.735, -73.991)
        self.assertTrue(result["stations"][0]["partial"])
        self.assertEqual(result["failed_feeds"], ["gtfs-l"])

    def test_nonadjacent_complex_merge_and_distant_names(self):
        entries = []
        for s in [
            station("a"),
            station("b", "Other"),
            station("c"),
            station("d", lon=-74.1),
        ]:
            s["distance_m"] = 10
            entries.append(dict(station=s, arrivals=[], partial=False))
        merged = server.merge_complexes(entries, 4)
        self.assertEqual(len(merged), 3)


if __name__ == "__main__":
    unittest.main()
