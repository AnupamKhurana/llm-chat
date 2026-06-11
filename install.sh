#!/usr/bin/env bash
set -e

echo "🚀 Installing LLM Chat Terminal..."

INSTALL_DIR="$HOME/.local/share/llm-chat"
BIN_DIR="$HOME/.local/bin"

echo "📂 Creating directories..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$BIN_DIR"

echo "🐍 Setting up Python virtual environment..."
if command -v uv >/dev/null 2>&1; then
    uv venv "$INSTALL_DIR/venv"
    uv pip install --python "$INSTALL_DIR/venv" -r requirements.txt
else
    python3 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install -q -r requirements.txt
fi

echo "📦 Copying files..."
cp llm_chat.py "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/llm_chat.py"

echo "🔗 Creating executable wrapper..."
cat << EOF > "$BIN_DIR/llm-chat"
#!/usr/bin/env bash
exec "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/llm_chat.py" "\$@"
EOF
chmod +x "$BIN_DIR/llm-chat"

echo "✅ Installation complete!"
echo "------------------------------------------------------"
echo "You can now run the app from anywhere using the command:"
echo ""
echo "    llm-chat"
echo ""
echo "Note: Make sure $BIN_DIR is in your system PATH."
echo "------------------------------------------------------"
