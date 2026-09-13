#!/usr/bin/env python3
"""Smoke test for a running proxy (default :8087). Exit non-zero on failure."""

import json
import os
import sys
import urllib.request

BASE = os.environ.get("MTA_PROXY_BASE", "http://127.0.0.1:8087")
FAIL = []


def get(path, timeout=60):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        assert r.status == 200, f"{path} -> {r.status}"
        return json.load(r)


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name, detail)
    if not cond:
        FAIL.append(name)


d = get("/mta/health", timeout=10)
check("health", d.get("ok") is True)

d = get("/mta/board?lat=40.7140&lon=-73.9507&limitStations=1&limitArrivals=4")
s = d["stations"][0]
check("lorimer-id", s["station"]["id"] == "L10", s["station"]["id"])
check(
    "lorimer-L",
    any(a["route"] == "L" for a in s["arrivals"]),
    str([a["route"] + ":" + str(a["mins"]) for a in s["arrivals"]]),
)

d = get("/mta/board?lat=40.7356&lon=-73.9910&limitStations=3&limitArrivals=2")
ids = {s["station"]["id"] for s in d["stations"]}
routes = set(d["stations"][0]["station"]["routes"])
check("union-complex", len(ids) == 1 and {"L", "4", "N"} <= routes, str(sorted(routes)))

d = get("/mta/board?lat=40.0&lon=-74.0&limitStations=99&limitArrivals=99")
check("clamp", len(d["stations"]) <= 5, str(len(d["stations"])))

sys.exit(1 if FAIL else 0)
