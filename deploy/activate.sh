#!/usr/bin/env bash
# Run on the server: bash RELEASE/deploy/activate.sh RELEASE
set -euo pipefail
release=$(realpath "$1")
root="$HOME/mta-proxy"
mkdir -p "$root/snapshots" "$HOME/.config/systemd/user"
stamp=$(date -u +%Y%m%dT%H%M%S)-$$
snapshot="$root/snapshots/$stamp.json"
tailscale funnel status --json > "$snapshot"
python3 - "$snapshot" <<'PY'
import json, sys
c=json.load(open(sys.argv[1]))
h=c.get('Web',{}).get('proxy.example.net:443',{}).get('Handlers',{})
assert '/mta' not in h or h['/mta']=={'Proxy':'http://127.0.0.1:8087/mta'}, 'Conflicting /mta route'
PY
previous=$(readlink -f "$root/current" || true)
added=0
rollback() {
  trap - ERR
  if [ "$added" = 1 ]; then sudo -n tailscale funnel --https=443 --set-path=/mta off || true; fi
  if [ -n "$previous" ] && [ -d "$previous" ]; then
    ln -sfn "$previous" "$root/current"
    cp "$previous"/deploy/*.service "$previous"/deploy/*.timer "$HOME/.config/systemd/user/"
    systemctl --user daemon-reload
    systemctl --user restart mta-proxy.service
  else
    systemctl --user disable --now mta-proxy.service mta-proxy-health.timer || true
    rm -f "$root/current"
  fi
}
trap rollback ERR
python3 -m venv "$release/.venv"
"$release/.venv/bin/pip" install --disable-pip-version-check -r "$release/requirements.lock"
(cd "$release" && .venv/bin/python -m unittest -v test_board test_hosting test_audit test_entrances test_alerts test_timetable)
if [ -n "$previous" ] && [ -d "$previous" ]; then ln -sfn "$previous" "$root/previous"; fi
ln -sfn "$release" "$root/current"
cp "$release"/deploy/*.service "$release"/deploy/*.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable mta-proxy.service mta-proxy-health.timer
systemctl --user restart mta-proxy.service
for attempt in {1..15}; do
  if curl -fsS --max-time 2 http://127.0.0.1:8087/mta/health; then break; fi
  sleep 1
done
(cd "$release" && .venv/bin/python healthcheck.py)
if ! python3 - "$snapshot" <<'PY'
import json,sys
c=json.load(open(sys.argv[1]))
sys.exit(0 if '/mta' in c['Web']['proxy.example.net:443']['Handlers'] else 1)
PY
then added=1; fi
sudo -n tailscale funnel --bg --https=443 --set-path=/mta http://127.0.0.1:8087/mta
curl --retry 3 --retry-delay 2 -fsS --max-time 10 https://proxy.example.net/mta/health
tailscale funnel status --json > "$root/snapshots/$stamp-after.json"
python3 - "$snapshot" "$root/snapshots/$stamp-after.json" <<'PY'
import json,sys
before,after=[json.load(open(p)) for p in sys.argv[1:]]
h=after['Web']['proxy.example.net:443']['Handlers']
assert h.pop('/mta')=={'Proxy':'http://127.0.0.1:8087/mta'}
before['Web']['proxy.example.net:443']['Handlers'].pop('/mta',None)
assert before==after, 'Existing Funnel configuration changed'
PY
systemctl --user start mta-proxy-health.timer
printf 'Activated %s; route snapshot %s\n' "$release" "$snapshot"
