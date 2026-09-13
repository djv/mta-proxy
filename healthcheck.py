#!/usr/bin/env python3
"""Exit 1: local process/protocol failure; exit 2: MTA unavailable/partial."""
import json
import logging
import os
import urllib.error
import urllib.request


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def check(base, get=fetch):
    try:
        status, data = get(base + '/mta/health')
        if status != 200 or data.get('ok') is not True:
            return 1, 'process_failure health'
    except Exception:
        return 1, 'process_failure health_transport'
    try:
        status, data = get(base + '/mta/board?lat=0&lon=0&station=L03&route=L&dir=N')
        if status == 503:
            return 2, 'upstream_failure unavailable'
        if status != 200 or not data.get('stations'):
            return 1, 'process_failure board_protocol'
        if data.get('failed_feeds') or any(s.get('partial') for s in data['stations']):
            return 2, 'upstream_failure partial'
        return 0, 'healthy station=L03'
    except Exception:
        return 1, 'process_failure board_transport'


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    code, message = check('http://127.0.0.1:' + os.environ.get('MTA_PROXY_PORT', '8087'))
    logging.log(logging.INFO if code == 0 else logging.ERROR, message)
    raise SystemExit(code)
