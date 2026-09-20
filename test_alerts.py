import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from nyct_gtfs.compiled_gtfs import gtfs_realtime_pb2

import server
from alerts import for_station, parse_alerts


def feed_bytes(alerts):
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    for index, spec in enumerate(alerts):
        entity = message.entity.add()
        entity.id = str(index)
        alert = entity.alert
        alert.header_text.translation.add().text = spec.get("header", "")
        if spec.get("desc"):
            alert.description_text.translation.add().text = spec["desc"]
        for route in spec.get("routes", []):
            alert.informed_entity.add().route_id = route
        for stop in spec.get("stops", []):
            alert.informed_entity.add().stop_id = stop
        for start, end in spec.get("periods", []):
            period = alert.active_period.add()
            if start:
                period.start = start
            if end:
                period.end = end
    return message.SerializeToString()


class ParseTests(unittest.TestCase):
    def test_strips_markup_and_entities(self):
        payload = feed_bytes(
            [
                {
                    "header": "Downtown [A][C][D] trains are delayed",
                    "desc": "<b>Delays</b><br/>due to &amp; signal work",
                }
            ]
        )
        alert = parse_alerts(payload, now=1000)[0]
        self.assertEqual(alert["title"], "Downtown A C D trains are delayed")
        self.assertEqual(alert["desc"], "Delays due to & signal work")

    def test_active_period_filtering(self):
        payload = feed_bytes(
            [
                {"header": "Open ended", "routes": ["L"], "periods": [(500, 0)]},
                {"header": "Expired", "routes": ["L"], "periods": [(100, 200)]},
                {"header": "Future", "routes": ["L"], "periods": [(2000, 3000)]},
                {"header": "No period", "routes": ["L"]},
            ]
        )
        titles = [a["title"] for a in parse_alerts(payload, now=1000)]
        self.assertEqual(titles, ["Open ended", "No period"])

    def test_empty_translation_skipped(self):
        message = gtfs_realtime_pb2.FeedMessage()
        message.header.gtfs_realtime_version = "2.0"
        entity = message.entity.add()
        entity.id = "0"
        entity.alert.informed_entity.add().route_id = "L"
        self.assertEqual(parse_alerts(message.SerializeToString(), now=1), [])


class StationMatchTests(unittest.TestCase):
    def test_route_and_stop_matching(self):
        alerts = [
            {"title": "L delays", "routes": ["L"], "stops": []},
            {"title": "L03 entrance closed", "routes": [], "stops": ["L03N"]},
            {"title": "Other line", "routes": ["G"], "stops": []},
            {"title": "L other station", "routes": [], "stops": ["A12S"]},
            {"title": "L delays", "routes": ["L"], "stops": []},
        ]
        hits = for_station(alerts, ["L"], "L03")
        self.assertEqual(
            [h["title"] for h in hits], ["L delays", "L03 entrance closed"]
        )

    def test_route_must_match_when_station_specific(self):
        alerts = [{"title": "G only", "routes": ["G"], "stops": ["L03N"]}]
        self.assertEqual(for_station(alerts, ["L"], "L03"), [])


class BoardIntegrationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(server, "get_alerts", return_value=([], False))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_alerts_attached_and_flag(self):
        now = 1789080000
        payload = feed_bytes([{"header": "[L] delays", "routes": ["L"]}])
        parsed = parse_alerts(payload, now=now)
        station = dict(id="L03", name="Union", lat=40.735, lon=-73.991, routes=["L"])
        with (
            patch.object(server, "stations", [station]),
            patch.object(
                server, "get_feed", return_value=(SimpleNamespace(trips=[]), True)
            ),
            patch.object(server, "get_alerts", return_value=(parsed, True)),
        ):
            result = server.board(40.735, -73.991)
        alerts = result["stations"][0]["station"]["alerts"]
        self.assertEqual([a["title"] for a in alerts], ["L delays"])
        self.assertTrue(result["alerts_available"])

    def test_alerts_failure_does_not_fail_board(self):
        station = dict(id="L03", name="Union", lat=40.735, lon=-73.991, routes=["L"])
        with (
            patch.object(server, "stations", [station]),
            patch.object(
                server, "get_feed", return_value=(SimpleNamespace(trips=[]), True)
            ),
            patch.object(server, "get_alerts", return_value=([], False)),
        ):
            result = server.board(40.735, -73.991)
        self.assertNotIn("alerts", result["stations"][0]["station"])
        self.assertFalse(result["alerts_available"])


class GetAlertsTests(unittest.TestCase):
    def test_get_alerts_caches_and_degrades(self):
        server.alerts_cache = (0.0, [], False)
        calls = []

        def fetch():
            calls.append(1)
            raise RuntimeError("down")

        with patch.object(server, "_fetch_alerts", side_effect=fetch):
            self.assertEqual(server.get_alerts(), ([], False))
            self.assertEqual(server.get_alerts(), ([], False))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
