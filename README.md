# 🚀 LLM Chat Terminal — Agentic Local AI CLI

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Ollama](https://img.shields.io/badge/Ollama-Supported-000000?style=for-the-badge&logo=ollama&logoColor=white)](https://ollama.ai)
[![llama.cpp](https://img.shields.io/badge/llama.cpp-Server-FF6B6B?style=for-the-badge)](https://github.com/ggerganov/llama.cpp)
[![Rich](https://img.shields.io/badge/UI-Rich_%26_Prompt_Toolkit-306998?style=for-the-badge)](https://github.com/Textualize/rich)
[![DeepSeek-R1](https://img.shields.io/badge/Reasoning-DeepSeek_R1_Ready-blue?style=for-the-badge)](https://deepseek.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)

An advanced, feature-rich command-line interface for interacting with local LLM backends (**Ollama**, **llama.cpp**, and **Unsloth Studio**). Bridges raw local model APIs with agentic capabilities—including real-time multi-threaded web search, live reasoning stream visualization (DeepSeek R1), and automatic context compaction.

---

## ✨ Key Features

- 🔄 **Multi-Backend Auto-Detection**: Automatically detects active Ollama, llama.cpp, or Unsloth Studio servers on launch with zero manual configuration.
- 🌐 **Parallel Web Search Agent**: Enables local models to autonomously generate search queries and execute concurrent DuckDuckGo searches across worker threads, deduplicating and ingesting live web sources.
- 🧠 **Native Reasoning Stream (CoT)**: Beautifully renders Chain-of-Thought thinking traces (such as DeepSeek-R1) in a dimmed, bounded panel in real-time before streaming the final response.
- 🎨 **Rich Terminal Experience**: Syntax-highlighted code blocks, Markdown tables, multi-line prompts with history navigation (↑/↓), and customizable color palettes.
- 🧠 **Intelligent Context Management**:
  - `/compact`: Condenses full conversation history into a semantic summary to preserve model context.
  - `/clear`: Resets active session memory immediately.
  - **Context Warning**: Proactively monitors token memory and notifies you when conversations exceed optimal limits.
- 🧩 **Extensible Tool Registry**: Add custom Python functions as model-callable tools via standard Python docstrings and type hints.

---

## 🛠️ Architecture & Supported Backends

```
  ┌─────────────────────────────────────────────────────────┐
  │                 LLM Chat Terminal (CLI)                 │
  │     (prompt_toolkit interactive prompt + Rich UI)       │
  └───────────┬─────────────────────────────────┬───────────┘
              │                                 │
     [ Agentic Tool Loop ]              [ Inference Layer ]
              │                                 │
   • Parallel DuckDuckGo Search      • Ollama API (:11434)
   • Context Auto-Compaction         • llama.cpp Server (:8080)
   • Custom Python Tool Registry     • Unsloth Studio (:8888)
```

---

## 🚀 Quick Start Guide

### Prerequisites
- Python 3.10+
- An active local LLM runtime ([Ollama](https://ollama.ai) or [llama.cpp server](https://github.com/ggerganov/llama.cpp))

### 1. Clone & Set Up
```bash
git clone https://github.com/AnupamKhurana/llm-chat.git
cd llm-chat
```

### 2. Install Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Launch
```bash
python llm_chat.py
```
The CLI automatically scans your local network ports:
- If **Ollama** is running, it prompts with an interactive model selector.
- If **llama.cpp server** is running, it connects and initializes streaming.
- If **Unsloth Studio** is running, it connects seamlessly.

---

## ⚙️ Command-Line Options

```bash
# Force a specific backend
python llm_chat.py -b ollama
python llm_chat.py -b llamacpp -e http://localhost:8080
python llm_chat.py -b unsloth -e http://localhost:8888

# Directly specify a model
python llm_chat.py -b ollama -m deepseek-r1:8b
```

### Interactive In-Chat Commands

| Command | Action |
| :--- | :--- |
| `>>> /compact` | Summarizes older turns and frees token context |
| `>>> /clear` | Wipes active context memory |
| `>>> /bye` or `/exit` | Gracefully closes the session |

---

## 🧩 Adding Custom Agentic Tools

Adding new tools is straightforward. Register any typed Python function into `AVAILABLE_TOOLS`:

```python
def fetch_weather(location: str) -> str:
    """Fetch real-time weather information for a specified city or location."""
    # Custom API or logic here
    return f"Weather in {location}: 24°C, Sunny"

AVAILABLE_TOOLS = {
    "web_search": web_search,
    "fetch_weather": fetch_weather,
}
```
The CLI auto-generates JSON function schemas and injects execution results back into the model prompt.

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for details.
