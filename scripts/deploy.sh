#!/usr/bin/env bash
# Copy the integration to Home Assistant over SSH and restart it, to try a change before releasing it.
#
# Settings come from .deploy.env (copy .deploy.env.example). Usage:
#   scripts/deploy.sh               copy and restart Home Assistant
#   scripts/deploy.sh --no-restart  copy only
#   scripts/deploy.sh --dry-run     show what would happen
#
# It replaces the HACS-installed copy; the next HACS update puts the released version back.
set -euo pipefail
cd "$(dirname "$0")/.."

DRY_RUN=false
RESTART=true
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --no-restart) RESTART=false ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

[[ -f .deploy.env ]] && source .deploy.env
: "${HA_HOST:?Set HA_HOST in .deploy.env (see .deploy.env.example)}"
HA_USER=${HA_USER:-root}
HA_PORT=${HA_PORT:-22}
HA_CONFIG=${HA_CONFIG:-/config}
TARGET="$HA_CONFIG/custom_components"

SSH=(ssh -p "$HA_PORT" "$HA_USER@$HA_HOST")

echo "Copying custom_components/zappistant to $HA_HOST:$TARGET/zappistant"
# Stream it as a tar so the old copy is replaced cleanly, without needing rsync on Home Assistant.
if $DRY_RUN; then
  echo "+ tar (without __pycache__) | ${SSH[*]} \"rm -rf '$TARGET/zappistant' && mkdir -p '$TARGET' && tar -xzf - -C '$TARGET'\""
else
  COPYFILE_DISABLE=1 tar -C custom_components --exclude __pycache__ -czf - zappistant \
    | "${SSH[@]}" "rm -rf '$TARGET/zappistant' && mkdir -p '$TARGET' && tar -xzf - -C '$TARGET'"
fi

if $RESTART; then
  echo "Restarting Home Assistant"
  # Checks the configuration first and refuses to restart if it's broken.
  if $DRY_RUN; then
    echo "+ ${SSH[*]} \"ha core check && ha core restart\""
  else
    "${SSH[@]}" "ha core check && ha core restart"
  fi
else
  echo "Not restarting; restart Home Assistant to load the change."
fi
