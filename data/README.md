# MTA entrance snapshot

Retrieved 2026-09-13 from https://data.ny.gov/resource/i9wp-a4ja.json?$limit=10000&$order=:id
Dataset: MTA Subway Entrances and Exits: 2024 (i9wp-a4ja).
Raw response retained for reproducible offline normalization by entrances.py.
SHA-256: 278835e3ed7eece8c3629ad675aecf1cef7b02c339a55345bde731cf913fb526

Only entry_allowed=YES and finite, in-range coordinates are retained. Semicolon-separated GTFS stop IDs match exactly; repeated coordinates are deduplicated per station and merged board. No proximity-based station reassignment. Missing data omits the optional station.entrances field.

Distances are straight-line. Snapshot data does not establish live availability or access to any selected platform.

Alternative sources checked 2026-09-13: OSM supports railway=subway_entrance, stop_area membership and exit-only roles (https://wiki.openstreetmap.org/wiki/Metro_Mapping). Live OSM API sample bbox=-73.993,40.733,-73.987,40.737 links 13 Union Square entrance nodes to relations 3420670,3420671,11167764, versus 12 MTA rows for R20/635 and none for L03. OSM citywide Overpass queries returned HTTP 406/429, so citywide coverage is unverified. OSM was not imported; it requires attribution and ODbL compliance. Citymapper discontinued self-service APIs June 23, 2023 (https://citymapper.com/news/2596/sdks-and-apis-come-to-an-end).
