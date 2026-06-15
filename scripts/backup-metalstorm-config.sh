#!/usr/bin/env bash
# Backup or restore MetalStorm keybindings from the Wine registry.
#
# Usage:
#   ./scripts/backup-metalstorm-config.sh backup   # save current keybindings
#   ./scripts/backup-metalstorm-config.sh restore  # restore from backup

set -euo pipefail

REGISTRY="/home/michael/Games/Heroic/Prefixes/Metalstorm/user.reg"
BACKUP="$(dirname "$0")/metalstorm-user.reg.bak"

case "${1:-}" in
  backup)
    cp "$REGISTRY" "$BACKUP"
    echo "Backed up to $BACKUP"
    ;;
  restore)
    if [[ ! -f "$BACKUP" ]]; then
      echo "No backup found at $BACKUP" >&2
      exit 1
    fi
    cp "$BACKUP" "$REGISTRY"
    echo "Restored from $BACKUP"
    echo "Restart MetalStorm for the keybindings to take effect."
    ;;
  *)
    echo "Usage: $0 backup|restore" >&2
    exit 1
    ;;
esac
