import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import server
from entrances import index_entrances, load_entrances


def setUpModule():
    patcher = patch.object(server, "get_alerts", return_value=([], False))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class EntranceTests(unittest.TestCase):
    def test_filter_match_and_deduplicate(self):
        row = dict(
            entry_allowed="YES",
            entrance_latitude="40.7",
            entrance_longitude="-74",
            gtfs_stop_id="A12; D13",
        )
        rows = [
            row,
            row.copy(),
            dict(row, entry_allowed="NO"),
            dict(row, entrance_latitude="nan"),
            dict(row, entrance_longitude="181"),
            dict(row, entrance_latitude=None),
            dict(row, gtfs_stop_id="A1"),
        ]
        self.assertEqual(
            index_entrances(rows, {"A12", "D13"}),
            {"A12": [dict(lat=40.7, lon=-74.0)], "D13": [dict(lat=40.7, lon=-74.0)]},
        )

    def test_merged_board_preserves_anchor_and_combines(self):
        stations = [
            dict(id=sid, name="Union", lat=40.735, lon=-73.991, routes=[])
            for sid in ["L03", "R20"]
        ]
        points = {
            "L03": [dict(lat=40.734, lon=-73.991)],
            "R20": [dict(lat=40.734, lon=-73.991), dict(lat=40.736, lon=-73.992)],
        }
        with (
            patch.object(server, "stations", stations),
            patch.object(server, "entrances", points),
        ):
            s = server.board(40.735, -73.991, station_id="L03", direction="N")[
                "stations"
            ][0]["station"]
        self.assertEqual(s["id"], "L03")
        self.assertEqual(s["lat"], 40.735)
        self.assertEqual(len(s["entrances"]), 2)
        self.assertNotIn("entrances", stations[0])

    def test_snapshot_coverage(self):
        stations = json.loads(__import__("pathlib").Path("stations.json").read_text())
        data = load_entrances(stations)
        self.assertGreater(len(data), 450)
        self.assertIn("R20", data)
        self.assertEqual(data["A12"], data["D13"])
