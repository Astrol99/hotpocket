#!/bin/sh
# Install claude-reload by linking it into ~/.local/bin (or $CLAUDE_RELOAD_BIN_DIR).
# Linking (not copying) means `claude-reload update` (a git pull) updates it in place.
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)
BIN_DIR="${CLAUDE_RELOAD_BIN_DIR:-$HOME/.local/bin}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "claude-reload needs python3. Install it and run this again." >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then
  echo "claude-reload needs Python 3.8 or newer (found $(python3 --version 2>&1))." >&2
  exit 1
fi
if ! command -v claude >/dev/null 2>&1; then
  echo "Note: Claude Code (claude) isn't on your PATH yet. Install it before using claude-reload." >&2
fi

chmod +x "$DIR/claude-reload"
mkdir -p "$BIN_DIR"
ln -sf "$DIR/claude-reload" "$BIN_DIR/claude-reload"
echo "Installed: $BIN_DIR/claude-reload -> $DIR/claude-reload"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Add $BIN_DIR to your PATH, e.g. in ~/.bashrc or ~/.zshrc:"
     echo "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

echo
echo "Next, add accounts:"
echo "  claude-reload import tokens.txt     # file from your admin (label=token lines)"
echo "  claude-reload add <label>           # or authorize one account yourself"
echo "Then run claude-reload wherever you'd run claude."
