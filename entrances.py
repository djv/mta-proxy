"""Normalize the pinned MTA snapshot; no network dependency at runtime."""
import json
import math
from pathlib import Path


def index_entrances(rows, station_ids):
    result = {}
    for row in rows:
        if row.get('entry_allowed') != 'YES':
            continue
        try:
            lat, lon = float(row['entrance_latitude']), float(row['entrance_longitude'])
        except (KeyError, ValueError, TypeError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        for sid in row.get('gtfs_stop_id', '').split(';'):
            sid = sid.strip()
            if sid in station_ids:
                result.setdefault(sid, set()).add((lat, lon))
    return {sid: [dict(lat=lat, lon=lon) for lat, lon in sorted(points)]
            for sid, points in sorted(result.items())}


def load_entrances(stations):
    path = Path(__file__).parent / 'data/entrances-2024.json'
    if not path.exists():
        return {}
    return index_entrances(json.loads(path.read_text()), {s['id'] for s in stations})


def combine(a, b):
    return [dict(lat=lat, lon=lon) for lat, lon in sorted(
        {(p['lat'], p['lon']) for p in a + b})]
