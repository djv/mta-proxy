#!/usr/bin/env bash
# Run on server. Optional --remove-route removes ONLY /mta on 443.
set -euo pipefail
root="$HOME/mta-proxy"
previous=$(readlink -f "$root/previous" || true)
if [ -n "$previous" ] && [ -d "$previous" ]; then
  current=$(readlink -f "$root/current")
  ln -sfn "$previous" "$root/current"
  ln -sfn "$current" "$root/previous"
  cp "$previous"/deploy/*.service "$previous"/deploy/*.timer "$HOME/.config/systemd/user/"
  systemctl --user daemon-reload
  systemctl --user restart mta-proxy.service
else
  systemctl --user disable --now mta-proxy.service mta-proxy-health.timer
fi
if [ "${1:-}" = --remove-route ]; then
  sudo -n tailscale funnel --https=443 --set-path=/mta off
fi
