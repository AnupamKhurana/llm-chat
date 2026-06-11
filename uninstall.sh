#!/usr/bin/env bash
set -e

INSTALL_DIR="$HOME/.local/share/llm-chat"
BIN_DIR="$HOME/.local/bin"
WRAPPER="$BIN_DIR/llm-chat"

echo "🗑️  Uninstalling LLM Chat Terminal..."
echo ""

# Check if anything is actually installed
if [ ! -d "$INSTALL_DIR" ] && [ ! -f "$WRAPPER" ]; then
    echo "⚠️  Nothing to uninstall — LLM Chat Terminal does not appear to be installed."
    exit 0
fi

# Show what will be removed
echo "The following will be removed:"
[ -d "$INSTALL_DIR" ] && echo "  📂 $INSTALL_DIR  (app files, venv, cache, sessions)"
[ -f "$WRAPPER" ]     && echo "  🔗 $WRAPPER  (executable wrapper)"
echo ""

# Confirm with the user
read -r -p "Proceed with uninstall? [y/N] " confirm
case "$confirm" in
    [yY][eE][sS]|[yY])
        ;;
    *)
        echo "❌ Uninstall cancelled."
        exit 0
        ;;
esac

# Kill any running llama.cpp server managed by the app
PID_FILE="$INSTALL_DIR/cache/llama_server.pid"
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo "🛑 Stopping managed llama.cpp server (PID $PID)..."
        pkill -P "$PID" 2>/dev/null || true
        kill "$PID" 2>/dev/null || true
    fi
fi

# Remove the install directory (venv, app, cache, sessions)
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    echo "  ✅ Removed $INSTALL_DIR"
fi

# Remove the executable wrapper
if [ -f "$WRAPPER" ]; then
    rm -f "$WRAPPER"
    echo "  ✅ Removed $WRAPPER"
fi

echo ""
echo "✅ LLM Chat Terminal has been uninstalled."
echo "------------------------------------------------------"
echo "Note: Your shell PATH was not modified. If you manually"
echo "added ~/.local/bin to your PATH for this app, you may"
echo "remove that entry from your shell config if no longer needed."
echo "------------------------------------------------------"
