#!/usr/bin/env python3
"""Public API validation; optional --soak runs for ten minutes."""
import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request

BASE = 'https://proxy.example.net'
QUERY = '/mta/board?lat=40.7356&lon=-73.9910&limitStations=5&limitArrivals=8'


def get(path, status=200):
    started = time.monotonic()
    try:
        response = urllib.request.urlopen(BASE + path, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        data = json.load(response)
        assert response.status == status, (response.status, data)
    elapsed = time.monotonic() - started
    assert elapsed < 10, elapsed
    return data, elapsed


def validate():
    assert get('/mta/health')[0] == {'ok': True}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: get(QUERY), range(8)))
    assert all(r[0]['stations'] and not r[0]['failed_feeds'] for r in results)
    print('concurrent_seconds', [round(r[1], 3) for r in results], flush=True)
    for direction in ['N', 'S']:
        data, elapsed = get('/mta/board?lat=0&lon=0&station=L03&route=L&dir=' + direction)
        arrivals = data['stations'][0]['arrivals']
        assert arrivals and all(a['route'] == 'L' and a['dir'] == direction for a in arrivals)
        print('station_filter', direction, round(elapsed, 3), flush=True)
    for path, status, error in [('/mta/missing', 404, 'not found'), ('/mta/board', 400, "'lat'"), ('/mta/board?lat=91&lon=0', 400, 'lat/lon out of range'), ('/mta/board?lat=0&lon=0&station=missing', 400, 'Unknown station'), ('/mta/board?lat=0&lon=0&dir=X', 400, 'Invalid direction')]:
        assert get(path, status)[0] == {'error': error}
    print('API PASS', flush=True)


def soak():
    start = time.monotonic()
    samples = []
    while True:
        data, elapsed = get(QUERY)
        assert data['stations'] and not data['failed_feeds']
        samples.append(elapsed)
        print(json.dumps({'seconds': round(time.monotonic()-start, 1), 'latency': round(elapsed, 3), 'cached': data['cached']}), flush=True)
        if time.monotonic()-start >= 600:
            break
        time.sleep(min(15, 600-(time.monotonic()-start)))
    print('SOAK PASS', len(samples), 'requests; max', round(max(samples), 3), flush=True)


if __name__ == '__main__':
    soak() if '--soak' in sys.argv else validate()
