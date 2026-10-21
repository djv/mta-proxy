#!/usr/bin/env bash
# Usage: deploy/deploy.sh [ssh-host]; see .env.deployment.example.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.sh
host=${1:-${MTA_PROXY_SSH_HOST:?Set MTA_PROXY_SSH_HOST or pass an SSH host}}
if [ -z "${MTA_PROXY_ROOT:-}" ]; then
  root=$(ssh "$host" 'printf "%s/mta-proxy" "$HOME"')
fi
export MTA_PROXY_ROOT="$root"
source deploy/config.sh
release=$(date -u +%Y%m%dT%H%M%S)-$(git rev-parse --short HEAD)
remote="$root/releases/$release"
ssh "$host" "mkdir -p '$remote'"
tar -czf - server.py entrances.py alerts.py timetable.py data test_entrances.py test_alerts.py test_timetable.py test_deploy_config.py verify_hosted.py healthcheck.py stations.json requirements.lock test_board.py test_hosting.py test_audit.py deploy | ssh "$host" "tar -xzf - -C '$remote'"
printf -v activate_command '%q ' env "MTA_PROXY_ROOT=$root" "MTA_PROXY_PUBLIC_HOST=${MTA_PROXY_PUBLIC_HOST:-}" bash "$remote/deploy/activate.sh" "$remote"
ssh "$host" "$activate_command"
