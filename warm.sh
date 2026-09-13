#!/bin/bash
# Keep MTA feed cache warm so watch/sim requests return in ~1s.
# Run once: setsid nohup ./warm.sh > warm.log 2>&1 < /dev/null &
while true; do
  python3 -c "
import urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8087/mta/board?lat=40.7140&lon=-73.9507&limitStations=1&limitArrivals=4', timeout=55).read()
except Exception as e:
    print('warm fail:', e, flush=True)
"
  sleep 20
done
