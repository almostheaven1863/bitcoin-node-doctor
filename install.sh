#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${HOME}/.local/share/node-doctor"
BIN_DIR="${HOME}/.local/bin"

echo "Installing Bitcoin Node Doctor..."

mkdir -p "$INSTALL_DIR"
mkdir -p "$BIN_DIR"

cp -a "$PROJECT_DIR/node_doctor" "$INSTALL_DIR/"
cp "$PROJECT_DIR/node-doctor" "$INSTALL_DIR/node-doctor"
cp "$PROJECT_DIR/VERSION" "$INSTALL_DIR/VERSION"

chmod +x "$INSTALL_DIR/node-doctor"

ln -sfn "$INSTALL_DIR/node-doctor" "$BIN_DIR/node-doctor"

echo
echo "Installed:"
echo "  $INSTALL_DIR"
echo
echo "Command:"
echo "  $BIN_DIR/node-doctor"
echo
echo "Run:"
echo "  node-doctor updates"
