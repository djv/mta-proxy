# Historical hosting verification — 2026-09-10

These are retained results from one deployment, not current live verification.
Private hostnames, account paths and personal device/use records are omitted.
The deployment used Python 3.12, pinned dependencies and 496 station records.

- 12 proxy tests passed, including HTTP 503 preservation, partial feed behavior,
  configurable binding, health failure classification and query-free request logs.
- Public health, nearby lookup, merged station complexes, station-ID lookup,
  N/S route filtering and 400/404 bodies passed, as did five live smoke checks.
- Eight simultaneous requests after restart (empty feed cache) succeeded in
  0.988–2.120 seconds. This was one cold-cache sample, not a latency bound.
- A process-kill recovery check observed a new PID and one restart after the
  configured three-second delay. The listener was loopback-only, and the service
  and five-minute health timer were enabled.
- Before/after Funnel JSON matched after excluding only the added `/mta`
  handler on 443. Other shared-server routes retained their baseline behavior.
- Application request logs contained method/path/status/timing without queries.
- One ten-minute public soak completed 39 requests over 600.6 seconds with no
  failed or partial responses; maximum latency was 8.165 seconds. It completed
  the planned interval. This small sample does not establish an availability SLO.

Reproduction tools: `test_smoke.py`, `verify_hosted.py`, `healthcheck.py` and
`deploy/activate.sh`. See README for configuration and live-check prerequisites.
