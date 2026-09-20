"""Parse MTA GTFS-RT subway alerts into compact records.

Deterministic and network-free at import: `parse_alerts` takes raw feed bytes.
The upstream URL is the public MTA alerts feed (no API key for the subway).
"""

import re
import time

from nyct_gtfs.compiled_gtfs import gtfs_realtime_pb2

ALERTS_URL = (
    "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts"
)

_TAG_RE = re.compile(r"<[^>]+>")
_MARKUP_RE = re.compile(r"\[([^\]]*)\]")
_WS_RE = re.compile(r"\s+")
_ENTITIES = {
    "&amp;": "&",
    "&nbsp;": " ",
    "&lt;": "<",
    "&gt;": ">",
    "&#39;": "'",
    "&quot;": '"',
}


def _clean(text):
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = _TAG_RE.sub(" ", text)
    text = _MARKUP_RE.sub(r"\1 ", text)
    return _WS_RE.sub(" ", text).strip()


def _text(field):
    for translation in field.translation:
        if translation.text:
            return _clean(translation.text)
    return ""


def _is_active(periods, now):
    # No active_period means the alert is always active.
    if not periods:
        return True
    for period in periods:
        start = period.start if period.HasField("start") else 0
        end = period.end if period.HasField("end") else 0
        if start and now < start:
            continue
        if end and now > end:
            continue
        return True
    return False


def parse_alerts(payload, now=None):
    now = int(time.time() if now is None else now)
    message = gtfs_realtime_pb2.FeedMessage()
    message.ParseFromString(payload)
    alerts = []
    for entity in message.entity:
        alert = entity.alert
        if not alert.header_text.translation and not alert.description_text.translation:
            continue
        if not _is_active(alert.active_period, now):
            continue
        routes, stops = set(), set()
        for informed in alert.informed_entity:
            if informed.route_id:
                routes.add(informed.route_id)
            if informed.stop_id:
                stops.add(informed.stop_id)
        alerts.append(
            {
                "title": _text(alert.header_text),
                "desc": _text(alert.description_text),
                "routes": sorted(routes),
                "stops": sorted(stops),
                "effect": alert.effect,
            }
        )
    return alerts


def for_station(alerts, routes, station_id):
    """Alerts relevant to a station, deduplicated by title."""
    wanted_routes = set(routes or [])
    wanted_stops = {station_id + "N", station_id + "S", station_id}
    hits = []
    seen = set()
    for alert in alerts:
        if alert["routes"] and not (set(alert["routes"]) & wanted_routes):
            continue
        if alert["stops"] and not (set(alert["stops"]) & wanted_stops):
            continue
        if not alert["title"] or alert["title"] in seen:
            continue
        seen.add(alert["title"])
        hits.append(alert)
    return hits
