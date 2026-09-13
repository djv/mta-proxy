#!/usr/bin/env bash
# Usage: deploy/deploy.sh [ssh-host]
set -euo pipefail
cd "$(dirname "$0")/.."
host=${1:-server}
release=$(date -u +%Y%m%dT%H%M%S)-$(git rev-parse --short HEAD)
remote="~/mta-proxy/releases/$release"
ssh "$host" "mkdir -p '$remote'"
tar -czf - server.py entrances.py data test_entrances.py healthcheck.py stations.json requirements.lock test_board.py test_hosting.py test_audit.py deploy | ssh "$host" "tar -xzf - -C '$remote'"
ssh "$host" "bash '$remote/deploy/activate.sh' '$remote'"
