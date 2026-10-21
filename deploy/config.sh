#!/usr/bin/env bash
# Shared deployment settings; sourced by the deployment entry points.
config_directory=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [ -f "$config_directory/.env.deployment" ]; then
  source "$config_directory/.env.deployment"
fi
root=${MTA_PROXY_ROOT:-$HOME/mta-proxy}
# Unit files and remote commands require a plain absolute filesystem path.
if [[ ! "$root" =~ ^/[a-zA-Z0-9_./-]+$ ]]; then
  printf 'MTA_PROXY_ROOT must be an absolute path without spaces or shell metacharacters\n' >&2
  exit 2
fi

install_units() {
  local source_release=$1
  local unit
  mkdir -p "$HOME/.config/systemd/user"
  for unit in "$source_release"/deploy/*.service "$source_release"/deploy/*.timer; do
    sed "s|%h/mta-proxy|$root|g" "$unit" > "$HOME/.config/systemd/user/$(basename "$unit")"
  done
}
