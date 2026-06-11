# LLM Chat Terminal 🚀

An advanced, feature-rich command-line interface for interacting with LLM backends (Ollama and llama.cpp).

This application bridges the gap between raw LLM APIs and the power of agentic tools, allowing models to perform parallel web searches, process deep reasoning, and maintain long-context conversations right from your terminal.

## ✨ Features

- **Multi-Backend Support**: Connect seamlessly to **Ollama** or a local **llama.cpp** HTTP server.
- **Auto-Detection**: Dynamically checks which backend is currently running on startup so you don't have to specify any options.
- **Beautiful Terminal UI**: Powered by `Rich` and `prompt_toolkit`. Features full Markdown rendering, syntax highlighting for code blocks, and an interactive prompt with history (Up/Down arrows).
- **Parallel Web Search Tool**: Models can execute live internet searches. If a model needs context, it generates multiple queries which are executed concurrently across 5 worker threads via DuckDuckGo, automatically deduping and formatting the results. Works out-of-the-box on both Ollama and llama.cpp.
- **Native Reasoning Support**: Flawlessly supports reasoning models (like DeepSeek R1). The model's "thinking" process is beautifully streamed into a dim, bounded panel in real-time, separating internal logic from the final answer.
- **Context Management**: 
  - `/compact`: Instructs the model to read the entire conversation history, generate a highly concise summary, and replace the bulky history with the summary to save memory.
  - `/clear`: Instantly wipes the conversation memory to start fresh.
  - **Auto-Warning**: The UI warns you if the conversation context is growing too large (30+ messages).

## 🛠️ Installation (Linux)

You can install this app globally on any Linux machine so you can run it from anywhere.

1. Clone or download this repository.
2. Run the provided installation script:

```bash
./install.sh
```

### What `install.sh` does:
- Creates an isolated Python virtual environment in `~/.local/share/llm-chat/venv`.
- Installs all dependencies automatically (it utilizes `uv` if installed for ultra-fast setup, otherwise falls back to standard `pip`).
- Copies the script and creates a globally accessible executable wrapper at `~/.local/bin/llm-chat`.

*(Note: Ensure `~/.local/bin` is in your system's `$PATH`)*

## 🗑️ Uninstall

To completely remove the app and all its data:

```bash
./uninstall.sh
```

This removes the virtual environment, cached data, saved sessions, and the `llm-chat` executable wrapper. You'll be asked to confirm before anything is deleted.

## 🚀 Usage

Once installed, simply invoke the app:

```bash
llm-chat
```

By default, the app will automatically detect which backend (Ollama or llama.cpp) is running:
- If only Ollama is running, it lists your models and lets you select one interactively.
- If only llama.cpp is running, it connects automatically to your local llama.cpp server and starts the chat.

### Custom Backend Arguments

You can explicitly force a backend or override the endpoints:

```bash
# Force Ollama backend
llm-chat -b ollama

# Force llama.cpp backend with a custom port/host
llm-chat -b llamacpp -e http://localhost:8080

# Specify model directly
llm-chat -b llamacpp -m my-custom-model
```

### Chat Commands
Once inside the chat interface, you can use the following commands:
- **`>>> /clear`** - Wipes all conversation history.
- **`>>> /compact`** - Replaces your long conversation history with an intelligent summary to save context space.
- **`>>> /bye`** or **`/exit`** - Exits the application.

## 📦 Adding Custom Tools

The app is designed to be easily extensible. Open `llm_chat.py` and you'll find an `AVAILABLE_TOOLS` registry:

```python
# Registry for tools
AVAILABLE_TOOLS = {
    "web_search": web_search,
    # "my_custom_tool": my_custom_tool,
}
```

To add a new tool:
1. Define a standard Python function.
2. Ensure you add robust **type hints** and a clear **docstring** (both backends parse these schemas to understand how to use the tool).
3. Add your function to the `AVAILABLE_TOOLS` dictionary.

The terminal UI will automatically handle generating the appropriate JSON schemas, executing the tools, and formatting their results!
