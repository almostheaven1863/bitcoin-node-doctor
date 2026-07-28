#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${HOME}/.local/share/node-doctor"
BIN_PATH="${HOME}/.local/bin/node-doctor"

echo "Removing Bitcoin Node Doctor..."

if [ -L "$BIN_PATH" ] || [ -f "$BIN_PATH" ]; then
    rm -f "$BIN_PATH"
fi

if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
fi

echo "Bitcoin Node Doctor removed."
echo "Reports and backups were not deleted."
