#!/usr/bin/env python3
import argparse
import sys
import json
import datetime
import concurrent.futures
import os
import urllib.request
import inspect
import subprocess
import time
import atexit
import signal
from typing import List, Dict, Any, Callable

from ddgs import DDGS
import ollama

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(APP_DIR, "cache")

def cleanup_llama_cpp_server():
    log_dir = CACHE_DIR
    pid_file = os.path.join(log_dir, "llama_server.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r") as f:
                pid = int(f.read().strip())
            
            # Kill children first (like llama-server)
            subprocess.run(["pkill", "-P", str(pid)], capture_output=True)
            # Kill the server script process itself
            os.kill(pid, signal.SIGTERM)
            
            # Clean up the pid file
            os.remove(pid_file)
        except Exception:
            pass

# Register cleanup handler and signal handlers to ensure it runs on normal exit or SIGTERM
atexit.register(cleanup_llama_cpp_server)
try:
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
except ValueError:
    pass
def sanitize_messages(messages: list) -> list:
    """
    Sanitizes message history to ensure compliance with OpenAI / LLM backend specifications:
    1. Every tool call in assistant messages must have 'id' and 'type="function"'.
    2. Every subsequent tool response message must have 'tool_call_id' matching the assistant's tool call id.
    """
    import uuid
    last_tool_calls = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            sanitized_tcs = []
            last_tool_calls = []
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                tc_copy = tc.copy()
                orig_id = tc_copy.get("id")
                new_id = orig_id or f"call_{uuid.uuid4().hex[:8]}"
                tc_copy["id"] = new_id
                tc_copy["type"] = "function"
                sanitized_tcs.append(tc_copy)
                func_name = tc_copy.get("function", {}).get("name")
                last_tool_calls.append({
                    "orig_id": orig_id,
                    "name": func_name,
                    "new_id": new_id
                })
            msg["tool_calls"] = sanitized_tcs
        elif msg.get("role") == "tool":
            tc_name = msg.get("name")
            tc_id = msg.get("tool_call_id")
            matched = False
            for tc_info in last_tool_calls:
                if (tc_id and tc_info["orig_id"] == tc_id) or (not tc_id and tc_info["name"] == tc_name):
                    msg["tool_call_id"] = tc_info["new_id"]
                    matched = True
                    break
            if not matched and not tc_id:
                msg["tool_call_id"] = f"call_{uuid.uuid4().hex[:8]}"
    return messages

def clean_messages_for_api(messages: list) -> list:
    """
    Creates a copy of the messages list and removes <think>...</think> blocks
    from assistant messages before sending them to the LLM backend.
    This prevents reasoning models from repeating thoughts in multi-turn/tool-call loops.
    """
    import re
    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue
        if msg.get("role") == "assistant" and msg.get("content"):
            msg_copy = msg.copy()
            msg_copy["content"] = re.sub(r"<think>.*?(?:</think>|$)", "", msg["content"], flags=re.DOTALL).strip()
            cleaned.append(msg_copy)
        else:
            cleaned.append(msg)
    return cleaned

def get_sessions_dir() -> str:
    sessions_dir = os.path.join(CACHE_DIR, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    return sessions_dir

def save_session(session_id: str, backend: str, endpoint: str, model: str, messages: list):
    try:
        sessions_dir = get_sessions_dir()
        
        # Extract a preview from the messages
        preview = "New Conversation"
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if content:
                    preview = content[:60].replace("\n", " ")
                    if len(content) > 60:
                        preview += "..."
                    break

        session_data = {
            "id": session_id,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "backend": backend,
            "endpoint": endpoint,
            "model": model,
            "messages": messages,
            "preview": preview
        }
        
        session_file = os.path.join(sessions_dir, session_id)
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)
            
        # Keep only the top 10 most recently modified sessions
        all_files = []
        for fname in os.listdir(sessions_dir):
            if fname.startswith("session_") and fname.endswith(".json"):
                fpath = os.path.join(sessions_dir, fname)
                if os.path.isfile(fpath):
                    all_files.append((fpath, os.path.getmtime(fpath)))
                    
        # Sort by modification time ascending (oldest first)
        all_files.sort(key=lambda x: x[1])
        
        if len(all_files) > 10:
            to_delete = all_files[:-10]
            for fpath, _ in to_delete:
                try:
                    os.remove(fpath)
                except Exception:
                    pass
    except Exception:
        pass

def focus_current_terminal():
    unique_title = f"llm_chat_focus_{int(time.time())}"
    # Set unique terminal title
    sys.stdout.write(f"\x1b]2;{unique_title}\x07")
    sys.stdout.flush()
    time.sleep(0.1)  # small delay for window manager to update title
    
    # 1. Try KWin Scripting via qdbus6 (KDE Plasma Wayland/X11)
    kwin_js = f"""
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) {{
    var t = windows[i].caption || windows[i].title || "";
    if (t.indexOf("{unique_title}") !== -1) {{
        workspace.activeWindow = windows[i];
        break;
    }}
}}
"""
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
            f.write(kwin_js)
            temp_name = f.name
        
        res = subprocess.run(
            ["qdbus6", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript", temp_name],
            capture_output=True, text=True
        )
        if res.returncode == 0:
            script_id = res.stdout.strip()
            subprocess.run(
                ["qdbus6", "org.kde.KWin", f"/Scripting/Script{script_id}", "org.kde.kwin.Script.run"],
                capture_output=True
            )
            subprocess.run(
                ["qdbus6", "org.kde.KWin", f"/Scripting/Script{script_id}", "org.kde.kwin.Script.stop"],
                capture_output=True
            )
    except Exception:
        pass
    finally:
        try:
            if 'temp_name' in locals() and os.path.exists(temp_name):
                os.remove(temp_name)
        except Exception:
            pass

    # 2. Try xdotool (standard X11)
    try:
        res = subprocess.run(["xdotool", "search", "--name", unique_title], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            win_id = res.stdout.strip().split()[0]
            subprocess.run(["xdotool", "windowactivate", win_id], capture_output=True)
    except Exception:
        pass

    # 3. Try wmctrl (alternative X11)
    try:
        subprocess.run(["wmctrl", "-a", unique_title], capture_output=True)
    except Exception:
        pass

    # Restore terminal title
    sys.stdout.write("\x1b]2;LLM Chat Terminal\007")
    sys.stdout.flush()

# Rich for beautiful terminal UI
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.live import Live
from rich.console import Group
from rich.text import Text
from rich.prompt import IntPrompt, Prompt

# Prompt Toolkit for an advanced input prompt (history, arrows, auto-completion)
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.completion import Completer, Completion

class SlashCommandCompleter(Completer):
    def __init__(self, commands):
        self.commands = commands

    def get_completions(self, document, complete_event):
        text_before = document.text_before_cursor
        # Trigger completions only if prompt starts with '/' and contains no spaces before the cursor
        if text_before.startswith('/') and ' ' not in text_before:
            lower_before = text_before.lower()
            for cmd in self.commands:
                if cmd.lower().startswith(lower_before):
                    yield Completion(cmd, start_position=-len(text_before))

commands = ['/bye', '/exit', '/clear', '/compact', '/history', '/sessions', '/save']
commands_completer = SlashCommandCompleter(commands)

console = Console(highlight=False)

def web_search(queries: List[str]) -> str:
    """
    Perform a parallel web search using DuckDuckGo across multiple query variations and return the combined results.
    
    Args:
        queries (List[str]): A list of search query variations to look up on the web simultaneously. Generate as many variations as you need for your search.
    """
    console.print(f"[cyan]🔍 Parallel searching {len(queries)} variations: {queries}[/cyan]")
    
    variations = queries
    
    def fetch(q: str):
        try:
            results = DDGS().text(q, max_results=2)
            if not results:
                results = DDGS().news(q, max_results=2)
            return q, results
        except Exception:
            return q, None

    all_results = []
    seen_urls = set()
    
    num_workers = max(1, len(variations))
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(fetch, v): v for v in variations}
        for future in concurrent.futures.as_completed(futures):
            q, res = future.result()
            if res:
                for r in res:
                    url = r.get('href', r.get('url', ''))
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        title = r.get('title', '')
                        snippet = r.get('body', r.get('snippet', ''))
                        all_results.append(f"Title: {title}\nURL: {url}\nSnippet: {snippet}")

    if not all_results:
        return "No results found."
    
    return "\n\n".join(all_results)

# Registry for tools
AVAILABLE_TOOLS = {
    "web_search": web_search,
}

def function_to_openai_tool(func: Callable) -> Dict[str, Any]:
    """
    Convert a Python function with docstrings and type annotations into a standard OpenAI tool schema.
    """
    sig = inspect.signature(func)
    doc = func.__doc__ or ""
    
    # Extract description: take lines up to "Args:" or "Parameters:" or empty lines that separate sections
    doc_lines = [line.strip() for line in doc.split('\n')]
    description_parts = []
    for line in doc_lines:
        if line.startswith("Args:") or line.startswith("Parameters:") or line.startswith("Returns:"):
            break
        description_parts.append(line)
    description = " ".join([p for p in description_parts if p]).strip()

    properties = {}
    required = []

    for name, param in sig.parameters.items():
        annotation = param.annotation
        param_type = "string"  # default fallback
        items_type = None
        
        if annotation == str:
            param_type = "string"
        elif annotation == int:
            param_type = "integer"
        elif annotation == float:
            param_type = "number"
        elif annotation == bool:
            param_type = "boolean"
        elif hasattr(annotation, "__origin__"):
            origin = annotation.__origin__
            if origin in (list, List):
                param_type = "array"
                args = getattr(annotation, "__args__", None)
                if args:
                    if args[0] == str:
                        items_type = "string"
                    elif args[0] == int:
                        items_type = "integer"
                    elif args[0] == float:
                        items_type = "number"
                    elif args[0] == bool:
                        items_type = "boolean"
            elif origin in (dict, Dict):
                param_type = "object"
        elif annotation in (list, List):
            param_type = "array"
            items_type = "string"
        elif annotation in (dict, Dict):
            param_type = "object"

        # Try to extract parameter description from docstring
        param_desc = ""
        for line in doc_lines:
            if line.startswith(f"{name} ") or line.startswith(f"{name}:"):
                parts = line.split(":", 1)
                if len(parts) > 1:
                    param_desc = parts[1].strip()
                else:
                    param_desc = line.replace(f"{name}", "").strip()
                break

        param_schema = {"type": param_type}
        if param_desc:
            param_schema["description"] = param_desc
        if items_type:
            param_schema["items"] = {"type": items_type}
            
        properties[name] = param_schema
        if param.default == inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
    }

def stream_and_process(model: str, messages: List[Dict[str, Any]], tools: List[Callable], options: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Stream the response from Ollama, process <think> tags for color, and collect tool calls.
    Returns the complete message object to append to history.
    """
    try:
        response_stream = ollama.chat(
            model=model,
            messages=clean_messages_for_api(messages),
            tools=tools,
            stream=True,
            options=options
        )
    except ollama.ResponseError as e:
        console.print(f"[bold red]Error: {e.error}[/bold red]")
        return {}

    collected_content = ""
    collected_tool_calls = []
    collected_thinking = ""
    
    # State for <think> tags embedded in content
    in_think = False
    buffer = ""

    with Live(auto_refresh=False, console=console) as live:
        for chunk in response_stream:
            msg = chunk.message
            
            # Collect tool calls
            if msg.tool_calls:
                collected_tool_calls.extend(msg.tool_calls)

            # Handle native thinking field from newer models
            if getattr(msg, "thinking", None):
                collected_thinking += msg.thinking
                
            if msg.content:
                content = msg.content
                buffer += content
                
                while True:
                    if not in_think and "<think>" in buffer:
                        idx = buffer.find("<think>")
                        collected_content += buffer[:idx]
                        in_think = True
                        buffer = buffer[idx + 7:]
                    elif in_think and "</think>" in buffer:
                        idx = buffer.find("</think>")
                        collected_thinking += buffer[:idx]
                        in_think = False
                        buffer = buffer[idx + 8:]
                    else:
                        break
                        
                if len(buffer) > 0:
                    safe_len = len(buffer)
                    for i in range(1, 8):
                        # If buffer ends with a partial tag, hold it back
                        if (not in_think and "<think>".startswith(buffer[-i:])) or (in_think and "</think>".startswith(buffer[-i:])):
                            safe_len = len(buffer) - i
                            break
                    
                    if safe_len > 0:
                        text_to_print = buffer[:safe_len]
                        if in_think:
                            collected_thinking += text_to_print
                        else:
                            collected_content += text_to_print
                        buffer = buffer[safe_len:]
            
            renderables = []
            if collected_thinking:
                renderables.append(Panel(collected_thinking, title="Thinking Process", style="dim", border_style="dim"))
            if collected_content:
                renderables.append(Markdown(collected_content))
            
            if renderables:
                live.update(Group(*renderables), refresh=True)

        # Flush remaining buffer
        if buffer:
            if in_think:
                collected_thinking += buffer
            else:
                collected_content += buffer
            buffer = ""
            
        renderables = []
        if collected_thinking:
            renderables.append(Panel(collected_thinking, title="Thinking Process", style="dim", border_style="dim"))
        if collected_content:
            renderables.append(Markdown(collected_content))
        if renderables:
            live.update(Group(*renderables), refresh=True)
    
    # Convert tool calls to dictionaries to avoid JSON serialization issues, ensuring OpenAI format
    serializable_tool_calls = []
    if collected_tool_calls:
        import uuid
        for tc in collected_tool_calls:
            tc_dict = {}
            if isinstance(tc, dict):
                tc_dict = tc.copy()
            elif hasattr(tc, 'model_dump'):
                tc_dict = tc.model_dump()
            elif hasattr(tc, 'dict'):
                tc_dict = tc.dict()
            else:
                tc_dict = {
                    "id": getattr(tc, "id", None),
                    "type": "function",
                    "function": {
                        "name": getattr(getattr(tc, "function", None), "name", None) or getattr(tc, "name", "unknown"),
                        "arguments": getattr(getattr(tc, "function", None), "arguments", None) or getattr(tc, "arguments", {})
                    }
                }
            if not tc_dict.get("id"):
                tc_dict["id"] = f"call_{uuid.uuid4().hex[:8]}"
            if not tc_dict.get("type"):
                tc_dict["type"] = "function"
            serializable_tool_calls.append(tc_dict)
    
    # Reconstruct raw content to save in history so next turn understands <think> if needed
    raw_content = ""
    if collected_thinking:
        raw_content += f"<think>{collected_thinking}</think>"
    raw_content += collected_content

    return {
        "role": "assistant",
        "content": raw_content,
        "tool_calls": serializable_tool_calls if serializable_tool_calls else None
    }

def stream_and_process_openai_compatible(endpoint: str, model_name: str, messages: List[Dict[str, Any]], tools: List[Callable], options: Dict[str, Any] = None, headers: Dict[str, str] = None) -> Dict[str, Any]:
    """
    Generic function to stream the response from an OpenAI-compatible server (like llama.cpp or Unsloth Studio),
    process <think> tags for color, and collect tool calls.
    """
    # 1. Format tools to OpenAI JSON format
    formatted_tools = [function_to_openai_tool(t) for t in tools] if tools else None
    
    # 2. Prepare payload
    payload = {
        "model": model_name,
        "messages": clean_messages_for_api(messages),
        "stream": True,
    }
    if options:
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
    if formatted_tools:
        payload["tools"] = formatted_tools

    url = f"{endpoint.rstrip('/')}/v1/chat/completions"
    req_headers = {'Content-Type': 'application/json'}
    if headers:
        req_headers.update(headers)
        
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers=req_headers
    )
    
    try:
        response = urllib.request.urlopen(req)
    except Exception as e:
        console.print(f"[bold red]Error: Request failed: {e}[/bold red]")
        return {}

    collected_content = ""
    collected_thinking = ""
    accumulated_tool_calls = {}
    
    in_think = False
    buffer = ""

    with Live(auto_refresh=False, console=console) as live:
        try:
            for line in response:
                line = line.decode('utf-8').strip()
                if not line:
                    continue
                if line.startswith('data: '):
                    data_str = line[6:]
                    if data_str == '[DONE]':
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    
                    # Collect tool calls
                    if "tool_calls" in delta:
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {
                                    "id": tc.get("id"),
                                    "type": "function",
                                    "function": {
                                        "name": tc.get("function", {}).get("name", ""),
                                        "arguments": ""
                                    }
                                }
                            if tc.get("id"):
                                accumulated_tool_calls[idx]["id"] = tc.get("id")
                            if tc.get("function", {}).get("name"):
                                accumulated_tool_calls[idx]["function"]["name"] = tc.get("function", {}).get("name")
                            arg_chunk = tc.get("function", {}).get("arguments", "")
                            accumulated_tool_calls[idx]["function"]["arguments"] += arg_chunk
                    
                    # Handle reasoning content stream (e.g. from server for reasoning models)
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        if not in_think:
                            in_think = True
                        collected_thinking += reasoning
    
                    # Handle standard content stream
                    content = delta.get("content")
                    if content:
                        if in_think:
                            in_think = False
                        buffer += content
                        
                        while True:
                            if not in_think and "<think>" in buffer:
                                idx = buffer.find("<think>")
                                collected_content += buffer[:idx]
                                in_think = True
                                buffer = buffer[idx + 7:]
                            elif in_think and "</think>" in buffer:
                                idx = buffer.find("</think>")
                                collected_thinking += buffer[:idx]
                                in_think = False
                                buffer = buffer[idx + 8:]
                            else:
                                break
                                
                        if len(buffer) > 0:
                            safe_len = len(buffer)
                            for i in range(1, 8):
                                # Hold back partial tags
                                if (not in_think and "<think>".startswith(buffer[-i:])) or (in_think and "</think>".startswith(buffer[-i:])):
                                    safe_len = len(buffer) - i
                                    break
                            
                            if safe_len > 0:
                                text_to_print = buffer[:safe_len]
                                if in_think:
                                    collected_thinking += text_to_print
                                else:
                                    collected_content += text_to_print
                                buffer = buffer[safe_len:]
                    
                    renderables = []
                    if collected_thinking:
                        renderables.append(Panel(collected_thinking, title="Thinking Process", style="dim", border_style="dim"))
                    if collected_content:
                        renderables.append(Markdown(collected_content))
                    
                    if renderables:
                        live.update(Group(*renderables), refresh=True)
        except Exception as e:
            console.print(f"\n[bold red]Error parsing stream: {e}[/bold red]")
        finally:
            response.close()
    
        # Flush remaining buffer
        if buffer:
            if in_think:
                collected_thinking += buffer
            else:
                collected_content += buffer
            buffer = ""
            
        renderables = []
        if collected_thinking:
            renderables.append(Panel(collected_thinking, title="Thinking Process", style="dim", border_style="dim"))
        if collected_content:
            renderables.append(Markdown(collected_content))
        if renderables:
            live.update(Group(*renderables), refresh=True)
    
    # Convert tool calls to dictionaries to avoid JSON serialization issues
    serializable_tool_calls = []
    if accumulated_tool_calls:
        for idx in sorted(accumulated_tool_calls.keys()):
            tc = accumulated_tool_calls[idx]
            try:
                args_dict = json.loads(tc["function"]["arguments"])
                tc["function"]["arguments"] = args_dict
            except json.JSONDecodeError:
                pass
            serializable_tool_calls.append(tc)

    # Reconstruct raw content to save in history
    raw_content = ""
    if collected_thinking:
        raw_content += f"<think>{collected_thinking}</think>"
    raw_content += collected_content

    return {
        "role": "assistant",
        "content": raw_content,
        "tool_calls": serializable_tool_calls if serializable_tool_calls else None
    }

def stream_and_process_llamacpp(endpoint: str, model_name: str, messages: List[Dict[str, Any]], tools: List[Callable], options: Dict[str, Any] = None) -> Dict[str, Any]:
    return stream_and_process_openai_compatible(endpoint, model_name, messages, tools, options)

def stream_and_process_unsloth(endpoint: str, model_name: str, messages: List[Dict[str, Any]], tools: List[Callable], options: Dict[str, Any] = None) -> Dict[str, Any]:
    headers = {}
    token = os.getenv("UNSLOTH_STUDIO_AUTH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return stream_and_process_openai_compatible(endpoint, model_name, messages, tools, options, headers)

def start_llama_cpp_server(endpoint: str, model_name: str = None):
    """
    Launches ~/run_llama_server.sh in a new terminal window or directly if needed,
    and waits for "srv  update_slots: all slots are idle" to confirm successful start.
    """
    server_script = os.path.expanduser("~/run_llama_server.sh")
    if not os.path.exists(server_script):
        console.print(f"[bold red]Startup script '{server_script}' not found.[/bold red]")
        sys.exit(1)

    log_dir = CACHE_DIR
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "llama_server.log")
    pid_file = os.path.join(log_dir, "llama_server.pid")

    # Clear previous log file and pid file if they exist
    for fpath in (log_file, pid_file):
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass

    # Parse host and port from endpoint
    host = None
    port = None
    if endpoint:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(endpoint)
            if parsed.hostname:
                host = parsed.hostname
            if parsed.port:
                port = str(parsed.port)
        except Exception:
            pass

    # Build arguments for the server script
    args_list = ["--no-browser"]
    if host:
        args_list.extend(["-H", host])
    if port:
        args_list.extend(["-p", port])
    if model_name:
        args_list.extend(["-m", model_name])

    args_str = " ".join(args_list)

    # Propagate LLAMA_BUILD environment variable if set
    env_str = ""
    if "LLAMA_BUILD" in os.environ:
        env_str = f"LLAMA_BUILD={os.environ['LLAMA_BUILD']} "

    # Find an available terminal emulator
    emulator = None
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        emulators = ["gnome-terminal", "konsole", "xfce4-terminal", "xterm", "kitty", "alacritty", "uxterm"]
        for em in emulators:
            if os.system(f"which {em} > /dev/null 2>&1") == 0:
                emulator = em
                break

    # Command to run in the terminal: writes PID to pid_file, runs the script with exec, pipes output to tee, and saves to log_file
    cmd_str = f"bash -c 'echo $$ > {pid_file}; exec {env_str}{server_script} {args_str} 2>&1 | tee {log_file}'"

    console.print(Panel(
        f"[bold yellow]llama.cpp server is not running on {endpoint}[/bold yellow]\n\n"
        f"Launching [cyan]{server_script} {args_str}[/cyan] in a new terminal window to start the server.",
        title="Starting llama.cpp Server",
        expand=False
    ))

    launched = False
    if emulator:
        try:
            if emulator == "gnome-terminal":
                subprocess.Popen(["gnome-terminal", "--", "bash", "-c", cmd_str])
            elif emulator == "konsole":
                subprocess.Popen(["konsole", "-e", "bash", "-c", cmd_str])
            elif emulator in ["xfce4-terminal", "xterm", "uxterm", "alacritty"]:
                subprocess.Popen([emulator, "-e", f"bash -c \"{cmd_str}\""])
            elif emulator == "kitty":
                subprocess.Popen(["kitty", "bash", "-c", cmd_str])
            launched = True
            console.print(f"[green]Opened new terminal window using {emulator} for server startup.[/green]")
        except Exception as e:
            console.print(f"[yellow]Failed to launch terminal emulator '{emulator}': {e}[/yellow]")
            launched = False

    if not launched:
        console.print("[bold yellow]Please start the server manually in a new terminal window using this command:[/bold yellow]")
        console.print(f"    {server_script}")
        console.print("\nPress [bold green]Enter[/bold green] when the server has started and you see \"srv  update_slots: all slots are idle\" in the server terminal.")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            sys.exit(1)
        return

    # If launched, monitor log file for startup confirmation
    console.print("[cyan]Waiting for server to initialize...[/cyan]")
    console.print("[dim]Monitoring logs for: \"srv  update_slots: all slots are idle\"[/dim]")

    start_time = time.time()
    success = False

    try:
        with Live(Text("Initializing...", style="yellow"), console=console, refresh_per_second=2) as live:
            while True:
                if os.path.exists(log_file):
                    try:
                        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                            log_content = f.read()
                            if "srv  update_slots: all slots are idle" in log_content:
                                success = True
                                break
                    except Exception:
                        pass

                # Check for timeout (5 minutes)
                elapsed = time.time() - start_time
                if elapsed > 300:
                    break

                live.update(Text(f"Waiting for server... ({int(elapsed)}s elapsed)", style="yellow"))
                time.sleep(1.0)
    except (KeyboardInterrupt, SystemExit):
        console.print("\n[yellow]Startup monitoring interrupted.[/yellow]")
        sys.exit(1)

    if success:
        console.print("[bold green]Success! llama.cpp server has successfully started and is idle.[/bold green]\n")
        focus_current_terminal()
        time.sleep(1.0)
    else:
        console.print("[bold red]Timeout: Could not detect successful server startup in logs.[/bold red]")
        console.print("Please make sure you have successfully completed the script's interactive inputs and the server is running.")
        sys.exit(1)

def start_unsloth_server(endpoint: str):
    """
    Launches ~/run_unsloth_server.sh in a new terminal window or directly if needed,
    and waits for the server to respond.
    """
    server_script = os.path.expanduser("~/run_unsloth_server.sh")
    if not os.path.exists(server_script):
        console.print(f"[bold red]Startup script '{server_script}' not found.[/bold red]")
        sys.exit(1)

    log_dir = CACHE_DIR
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "unsloth_server.log")
    pid_file = os.path.join(log_dir, "unsloth_server.pid")

    # Clear previous log file and pid file if they exist
    for fpath in (log_file, pid_file):
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass

    # Parse host and port from endpoint
    host = None
    port = None
    if endpoint:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(endpoint)
            if parsed.hostname:
                host = parsed.hostname
            if parsed.port:
                port = str(parsed.port)
        except Exception:
            pass

    # Build arguments for the server script
    args_list = []
    if host:
        args_list.extend(["-H", host])
    if port:
        args_list.extend(["-p", port])

    args_str = " ".join(args_list)

    # Find an available terminal emulator
    emulator = None
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        emulators = ["gnome-terminal", "konsole", "xfce4-terminal", "xterm", "kitty", "alacritty", "uxterm"]
        for em in emulators:
            if os.system(f"which {em} > /dev/null 2>&1") == 0:
                emulator = em
                break

    cmd_str = f"bash -c 'echo $$ > {pid_file}; exec {server_script} {args_str} 2>&1 | tee {log_file}'"

    console.print(Panel(
        f"[bold yellow]Unsloth Studio is not running on {endpoint}[/bold yellow]\n\n"
        f"Launching [cyan]{server_script} {args_str}[/cyan] in a new terminal window to start the server.",
        title="Starting Unsloth Studio",
        expand=False
    ))

    launched = False
    if emulator:
        try:
            if emulator == "gnome-terminal":
                subprocess.Popen(["gnome-terminal", "--", "bash", "-c", cmd_str])
            elif emulator == "konsole":
                subprocess.Popen(["konsole", "-e", "bash", "-c", cmd_str])
            elif emulator in ["xfce4-terminal", "xterm", "uxterm", "alacritty"]:
                subprocess.Popen([emulator, "-e", f"bash -c \"{cmd_str}\""])
            elif emulator == "kitty":
                subprocess.Popen(["kitty", "bash", "-c", cmd_str])
            launched = True
            console.print(f"[green]Opened new terminal window using {emulator} for server startup.[/green]")
        except Exception as e:
            console.print(f"[yellow]Failed to launch terminal emulator '{emulator}': {e}[/yellow]")
            launched = False

    if not launched:
        console.print("[bold yellow]Please start the server manually in a new terminal window using this command:[/bold yellow]")
        console.print(f"    {server_script} {args_str}")
        console.print("\nPress [bold green]Enter[/bold green] when the server has started.")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            sys.exit(1)
        return

    # If launched, monitor endpoint for startup confirmation
    console.print("[cyan]Waiting for Unsloth Studio to initialize...[/cyan]")
    
    detect_host = host or "127.0.0.1"
    detect_port = port or "8888"
    start_time = time.time()
    success_status = False
    import urllib.error

    try:
        with Live(Text("Initializing...", style="yellow"), console=console, refresh_per_second=2) as live:
            while True:
                try:
                    url = f"http://{detect_host}:{detect_port}/v1/models"
                    headers = {}
                    token = os.getenv("UNSLOTH_STUDIO_AUTH_TOKEN")
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=1.0) as resp:
                        if resp.status == 200:
                            success_status = True
                            break
                except urllib.error.HTTPError:
                    success_status = True
                    break
                except Exception:
                    pass

                # Check for timeout (5 minutes)
                elapsed = time.time() - start_time
                if elapsed > 300:
                    break

                live.update(Text(f"Waiting for Unsloth Studio... ({int(elapsed)}s elapsed)", style="yellow"))
                time.sleep(1.5)
    except (KeyboardInterrupt, SystemExit):
        console.print("\n[yellow]Startup monitoring interrupted.[/yellow]")
        sys.exit(1)

    if success_status:
        console.print("[bold green]Success! Unsloth Studio has successfully started.[/bold green]\n")
        focus_current_terminal()
        time.sleep(1.0)
    else:
        console.print("[bold red]Timeout: Could not detect successful server startup.[/bold red]")
        sys.exit(1)

def print_history_in_markdown(messages: List[Dict[str, Any]]):
    console.print("\n[bold cyan]=== Conversation History ===[/bold cyan]")
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            if content.startswith("Summary of earlier conversation:"):
                console.print(f"\n[bold yellow]System Summary:[/bold yellow] [dim]{content[32:].strip()}[/dim]")
            continue
        
        if role == "user":
            console.print(f"\n[bold green]User:[/bold green] {content}")
        elif role == "assistant":
            console.print(f"\n[bold blue]Assistant:[/bold blue]")
            import re
            thinking_match = re.match(r"^<think>(.*?)</think>(.*)", content, flags=re.DOTALL)
            if thinking_match:
                thinking, main_content = thinking_match.groups()
                console.print(Panel(thinking.strip(), title="Thinking Process", style="dim", border_style="dim"))
                console.print(Markdown(main_content.strip()))
            else:
                console.print(Markdown(content))
        elif role == "tool":
            console.print(f"\n[bold yellow]Tool Result ({msg.get('name')}):[/bold yellow]")
            console.print(msg.get("content", ""))
    console.print("\n[bold cyan]============================[/bold cyan]\n")

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LLM Chat Terminal - Web Edition</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/11.1.1/marked.min.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
    <style>
        :root {
            --bg-primary: #0a0915;
            --bg-secondary: #121026;
            --bg-glass: rgba(18, 16, 38, 0.75);
            --border-glass: rgba(255, 255, 255, 0.06);
            --border-vibrant: rgba(139, 92, 246, 0.15);
            --text-primary: #f8fafc;
            --text-secondary: #cbd5e1;
            --text-muted: #64748b;
            --accent-purple: #8b5cf6;
            --accent-pink: #ec4899;
            --accent-cyan: #06b6d4;
            --accent-green: #10b981;
            --accent-red: #ef4444;
            --accent-gradient: linear-gradient(135deg, #6366f1, #a855f7, #ec4899);
            --accent-glow: rgba(139, 92, 246, 0.35);
            
            --bg-user-msg: linear-gradient(135deg, #4f46e5, #7c3aed);
            --bg-assistant-msg: rgba(255, 255, 255, 0.02);
            --bubble-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);
            --sidebar-width: 320px;
        }

        body.light-mode {
            --bg-primary: #f8fafc;
            --bg-secondary: #ffffff;
            --bg-glass: rgba(255, 255, 255, 0.85);
            --border-glass: rgba(15, 23, 42, 0.08);
            --border-vibrant: rgba(99, 102, 241, 0.12);
            --text-primary: #0f172a;
            --text-secondary: #334155;
            --text-muted: #94a3b8;
            --accent-purple: #6366f1;
            --accent-pink: #db2777;
            --accent-cyan: #0891b2;
            --accent-green: #16a34a;
            --accent-red: #dc2626;
            --accent-gradient: linear-gradient(135deg, #4f46e5, #7c3aed, #db2777);
            --accent-glow: rgba(99, 102, 241, 0.15);
            
            --bg-user-msg: linear-gradient(135deg, #4f46e5, #7c3aed);
            --bg-assistant-msg: #ffffff;
            --bubble-shadow: 0 4px 20px rgba(15, 23, 42, 0.05);
        }

        * {
            box-sizing: border-box;
            scrollbar-width: thin;
            scrollbar-color: rgba(255, 255, 255, 0.1) transparent;
        }

        .light-mode * {
            scrollbar-color: rgba(0, 0, 0, 0.12) transparent;
        }

        body {
            margin: 0;
            padding: 0;
            font-family: 'Plus Jakarta Sans', 'Inter', sans-serif;
            background: var(--bg-primary);
            background-image: 
                radial-gradient(circle at 10% 20%, rgba(99, 102, 241, 0.08) 0%, transparent 40%),
                radial-gradient(circle at 90% 80%, rgba(236, 72, 153, 0.05) 0%, transparent 45%);
            color: var(--text-primary);
            height: 100vh;
            display: flex;
            overflow: hidden;
            transition: background 0.3s, color 0.3s;
        }

        .light-mode body {
            background-image: 
                radial-gradient(circle at 10% 20%, rgba(99, 102, 241, 0.05) 0%, transparent 40%),
                radial-gradient(circle at 90% 80%, rgba(236, 72, 153, 0.03) 0%, transparent 45%);
        }

        /* Sidebar Styling */
        .sidebar {
            width: var(--sidebar-width);
            background: var(--bg-glass);
            border-right: 1px solid var(--border-glass);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            height: 100%;
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1), margin-left 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            z-index: 10;
        }

        body.sidebar-collapsed .sidebar {
            margin-left: calc(-1 * var(--sidebar-width));
            transform: translateX(calc(-1 * var(--sidebar-width)));
        }

        .sidebar-header {
            padding: 24px 20px;
            border-bottom: 1px solid var(--border-glass);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .logo {
            font-family: 'Outfit', sans-serif;
            font-size: 1.35rem;
            font-weight: 800;
            background: var(--accent-gradient);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: flex;
            align-items: center;
            gap: 10px;
            letter-spacing: -0.02em;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background-color: var(--accent-green);
            box-shadow: 0 0 10px var(--accent-green);
            animation: pulse-green 2s infinite;
        }

        @keyframes pulse-green {
            0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
            100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        .sidebar-content {
            flex-grow: 1;
            overflow-y: auto;
            padding: 24px 20px;
            display: flex;
            flex-direction: column;
            gap: 24px;
        }

        .section-title {
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: var(--text-muted);
            margin-bottom: 12px;
            font-weight: 700;
        }

        .dropdown-control {
            width: 100%;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border-glass);
            border-radius: 12px;
            padding: 12px 16px;
            color: var(--text-primary);
            font-size: 0.9rem;
            outline: none;
            cursor: pointer;
            transition: all 0.3s;
            appearance: none;
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 24 24' stroke='%23cbd5e1'%3E%3Cpath stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M19 9l-7 7-7-7'/%3E%3C/svg%3E");
            background-repeat: no-repeat;
            background-position: right 16px center;
            background-size: 16px;
            padding-right: 40px;
        }

        .light-mode .dropdown-control {
            background: rgba(0, 0, 0, 0.02);
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 24 24' stroke='%23334155'%3E%3Cpath stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M19 9l-7 7-7-7'/%3E%3C/svg%3E");
            background-repeat: no-repeat;
            background-position: right 16px center;
            background-size: 16px;
        }

        .dropdown-control option {
            background: #121026;
            color: #f3f4f6;
        }

        .light-mode .dropdown-control option {
            background: #ffffff;
            color: #0f172a;
        }

        .dropdown-control:focus {
            border-color: var(--accent-purple);
            box-shadow: 0 0 0 3px var(--accent-glow);
        }

        /* Toggle switches */
        .toggle-switch-container {
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 0.85rem;
            color: var(--text-secondary);
            padding: 2px 0;
        }
        .switch {
            position: relative;
            display: inline-block;
            width: 42px;
            height: 22px;
        }
        .switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }
        .slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: rgba(255, 255, 255, 0.08);
            transition: .3s cubic-bezier(0.4, 0, 0.2, 1);
            border-radius: 22px;
            border: 1px solid var(--border-glass);
        }
        .light-mode .slider {
            background-color: rgba(0, 0, 0, 0.06);
        }
        .slider:before {
            position: absolute;
            content: "";
            height: 14px;
            width: 14px;
            left: 3px;
            bottom: 3px;
            background-color: var(--text-secondary);
            transition: .3s cubic-bezier(0.4, 0, 0.2, 1);
            border-radius: 50%;
        }
        .switch input:checked + .slider {
            background-color: var(--accent-purple);
            border-color: transparent;
        }
        .switch input:checked + .slider:before {
            transform: translateX(20px);
            background-color: white;
        }

        #theme-toggle-btn {
            background: none;
            border: none;
            cursor: pointer;
            font-size: 1.15rem;
            padding: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--text-secondary);
            border-radius: 50%;
            transition: all 0.2s ease;
        }

        #theme-toggle-btn:hover {
            color: var(--text-primary);
            background: rgba(255, 255, 255, 0.06);
            transform: rotate(15deg);
        }

        .light-mode #theme-toggle-btn:hover {
            background: rgba(0, 0, 0, 0.04);
        }

        .sessions-list {
            display: flex;
            flex-direction: column;
            gap: 8px;
            max-height: 250px;
            overflow-y: auto;
            padding-right: 4px;
        }

        .session-card {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border-glass);
            border-radius: 12px;
            padding: 14px;
            cursor: pointer;
            transition: all 0.2s ease;
            position: relative;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .light-mode .session-card {
            background: rgba(0, 0, 0, 0.01);
        }

        .session-card:hover {
            background: rgba(255, 255, 255, 0.05);
            border-color: var(--border-vibrant);
            transform: translateY(-1px);
        }

        .light-mode .session-card:hover {
            background: rgba(0, 0, 0, 0.03);
        }

        .session-card.active {
            background: rgba(139, 92, 246, 0.08);
            border-color: var(--accent-purple);
            box-shadow: 0 4px 15px rgba(139, 92, 246, 0.15);
        }

        .light-mode .session-card.active {
            background: rgba(99, 102, 241, 0.08);
            border-color: var(--accent-purple);
        }

        .session-meta {
            display: flex;
            justify-content: space-between;
            font-size: 0.72rem;
            color: var(--text-muted);
            font-weight: 500;
        }

        .session-preview {
            font-size: 0.85rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            color: var(--text-secondary);
        }

        .session-delete-btn {
            position: absolute;
            top: 12px;
            right: 12px;
            background: transparent;
            border: none;
            color: var(--text-muted);
            cursor: pointer;
            font-size: 0.85rem;
            display: flex;
            align-items: center;
            justify-content: center;
            opacity: 0.5;
            transition: all 0.2s ease;
            padding: 4px;
            border-radius: 6px;
        }

        .session-card:hover .session-delete-btn {
            opacity: 0.8;
        }

        .session-delete-btn:hover {
            color: var(--accent-red) !important;
            opacity: 1 !important;
            background: rgba(239, 68, 68, 0.15);
            transform: scale(1.1);
        }

        .btn {
            width: 100%;
            background: var(--accent-gradient);
            border: none;
            border-radius: 12px;
            padding: 14px;
            color: white;
            font-weight: 600;
            font-size: 0.95rem;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            box-shadow: 0 4px 12px rgba(139, 92, 246, 0.25);
        }

        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(139, 92, 246, 0.4);
        }

        .btn:active {
            transform: translateY(0);
        }

        .btn-secondary {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border-glass);
            color: var(--text-primary);
            box-shadow: none;
        }

        .light-mode .btn-secondary {
            background: rgba(0, 0, 0, 0.02);
            border-color: var(--border-glass);
        }

        .btn-secondary:hover {
            background: rgba(255, 255, 255, 0.08);
            border-color: var(--text-muted);
            box-shadow: none;
        }

        .light-mode .btn-secondary:hover {
            background: rgba(0, 0, 0, 0.04);
        }

        /* Chat Window Styling */
        .chat-container {
            flex-grow: 1;
            display: flex;
            flex-direction: column;
            height: 100%;
            min-width: 0;
            background: transparent;
        }

        .chat-header {
            padding: 18px 24px;
            border-bottom: 1px solid var(--border-glass);
            background: var(--bg-glass);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            display: flex;
            align-items: center;
            justify-content: space-between;
            z-index: 5;
        }

        .chat-title-container {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }

        .chat-title {
            font-family: 'Outfit', sans-serif;
            font-size: 1.15rem;
            font-weight: 700;
            letter-spacing: -0.01em;
        }

        .chat-subtitle {
            font-size: 0.78rem;
            color: var(--text-muted);
        }

        .icon-btn {
            background: none;
            border: none;
            cursor: pointer;
            color: var(--text-secondary);
            font-size: 1.2rem;
            padding: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            transition: all 0.2s ease;
        }
        .icon-btn:hover {
            background: rgba(255, 255, 255, 0.06);
            color: var(--text-primary);
        }
        .light-mode .icon-btn:hover {
            background: rgba(0, 0, 0, 0.04);
        }

        .messages-list {
            flex-grow: 1;
            overflow-y: auto;
            padding: 30px 24px;
            display: flex;
            flex-direction: column;
            gap: 24px;
        }

        .message-row {
            display: flex;
            width: 100%;
            animation: fadeInMsg 0.3s ease-out;
        }

        @keyframes fadeInMsg {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .message-row.user {
            justify-content: flex-end;
        }

        .message-row.assistant {
            justify-content: flex-start;
        }

        .message-bubble {
            max-width: 80%;
            padding: 16px 20px;
            border-radius: 20px;
            font-size: 0.96rem;
            line-height: 1.6;
            box-shadow: var(--bubble-shadow);
        }

        .message-row.user .message-bubble {
            background: var(--bg-user-msg);
            color: white;
            border-bottom-right-radius: 4px;
        }

        .message-row.assistant .message-bubble {
            background: var(--bg-assistant-msg);
            border: 1px solid var(--border-glass);
            border-bottom-left-radius: 4px;
            width: 100%;
            max-width: 85%;
        }

        /* Thinking Panel Accordion */
        .thinking-panel {
            background: rgba(255, 255, 255, 0.015);
            border: 1px solid var(--border-glass);
            border-left: 3px solid var(--accent-purple);
            border-radius: 12px;
            margin-bottom: 16px;
            overflow: hidden;
            transition: all 0.25s ease;
        }

        .light-mode .thinking-panel {
            background: rgba(0, 0, 0, 0.005);
        }

        .thinking-header {
            padding: 12px 16px;
            background: rgba(255, 255, 255, 0.01);
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.82rem;
            color: var(--text-muted);
            font-weight: 600;
            letter-spacing: 0.02em;
        }

        .thinking-header::after {
            content: '▼';
            font-size: 0.7rem;
            transition: transform 0.25s ease;
            transform: rotate(0deg);
        }

        .thinking-panel.collapsed .thinking-header::after {
            transform: rotate(-90deg);
        }

        .thinking-content {
            padding: 16px;
            font-size: 0.88rem;
            color: var(--text-muted);
            font-style: italic;
            border-top: 1px solid var(--border-glass);
            white-space: pre-wrap;
            background: rgba(0, 0, 0, 0.1);
        }

        .light-mode .thinking-content {
            background: rgba(0, 0, 0, 0.01);
        }

        .thinking-panel.collapsed .thinking-content {
            display: none;
        }

        /* Tool Status Cards */
        .tool-panel {
            background: rgba(6, 182, 212, 0.03);
            border: 1px solid rgba(6, 182, 212, 0.15);
            border-left: 3px solid var(--accent-cyan);
            border-radius: 12px;
            margin-bottom: 16px;
            max-width: 85%;
            width: 100%;
            overflow: hidden;
            box-shadow: var(--bubble-shadow);
            align-self: flex-start;
        }

        .tool-header {
            padding: 12px 16px;
            background: rgba(6, 182, 212, 0.06);
            font-size: 0.82rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            color: var(--accent-cyan);
            font-weight: 700;
            cursor: pointer;
            letter-spacing: 0.02em;
        }

        .tool-header::after {
            content: '▼';
            font-size: 0.7rem;
            transition: transform 0.25s ease;
        }

        .tool-panel.collapsed .tool-header::after {
            transform: rotate(-90deg);
        }

        .tool-content {
            padding: 16px;
            font-size: 0.85rem;
            background: rgba(0, 0, 0, 0.25);
            color: var(--text-secondary);
            border-top: 1px solid rgba(6, 182, 212, 0.08);
            white-space: pre-wrap;
            font-family: monospace;
            word-break: break-all;
        }

        .light-mode .tool-content {
            background: rgba(0, 0, 0, 0.02);
        }

        .light-mode .tool-panel {
            background: rgba(6, 182, 212, 0.06);
            border-color: rgba(6, 182, 212, 0.2);
        }

        .light-mode .tool-header {
            background: rgba(6, 182, 212, 0.12);
            color: #0369a1;
        }

        .tool-panel.collapsed .tool-content {
            display: none;
        }

        body.hide-thinking .thinking-panel {
            display: none !important;
        }

        /* Hide empty assistant bubble rows if they contain only a hidden thinking panel or empty text container */
        body.hide-thinking .message-row.assistant:not(:has(.text-container:not(:empty))) {
            display: none !important;
        }

        body.hide-tools .tool-panel {
            display: none !important;
        }

        /* Input Panel Floating Search style */
        .input-outer {
            padding: 16px 24px 30px;
            background: transparent;
            position: relative;
            display: flex;
            flex-direction: column;
            align-items: center;
            width: 100%;
        }

        .input-container {
            display: flex;
            align-items: center;
            gap: 15px;
            background: var(--bg-glass);
            border: 1px solid var(--border-vibrant);
            border-radius: 20px;
            padding: 12px 18px;
            position: relative;
            width: 100%;
            max-width: 850px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            transition: all 0.3s ease;
        }

        .light-mode .input-container {
            box-shadow: 0 10px 30px rgba(15, 23, 42, 0.06);
            border-color: var(--border-glass);
        }

        .input-container:focus-within {
            border-color: var(--accent-purple);
            box-shadow: 0 0 0 3px var(--accent-glow), 0 10px 30px rgba(0, 0, 0, 0.25);
        }

        .chat-input {
            flex-grow: 1;
            background: transparent;
            border: none;
            color: var(--text-primary);
            font-size: 0.96rem;
            outline: none;
            resize: none;
            max-height: 140px;
            height: 24px;
            font-family: inherit;
        }

        .send-btn {
            background: var(--accent-purple);
            border: none;
            border-radius: 12px;
            width: 38px;
            height: 38px;
            color: white;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s ease;
            box-shadow: 0 4px 10px rgba(139, 92, 246, 0.2);
        }

        .send-btn:hover {
            transform: scale(1.06);
            background: #7c3aed;
            box-shadow: 0 4px 15px rgba(139, 92, 246, 0.35);
        }

        .send-btn:active {
            transform: scale(0.96);
        }

        /* Autocomplete Box */
        .autocomplete-box {
            position: absolute;
            bottom: calc(100% + 8px);
            left: 50%;
            transform: translateX(-50%);
            background: var(--bg-glass);
            border: 1px solid var(--border-glass);
            border-radius: 12px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            max-height: 200px;
            overflow-y: auto;
            width: 100%;
            max-width: 850px;
            z-index: 100;
            display: none;
        }

        .autocomplete-item {
            padding: 12px 18px;
            cursor: pointer;
            font-size: 0.9rem;
            transition: background 0.2s ease;
            color: var(--text-secondary);
        }

        .autocomplete-item:hover, .autocomplete-item.selected {
            background: rgba(139, 92, 246, 0.15);
            color: var(--text-primary);
        }

        /* Loading typing indicator */
        .typing-indicator {
            display: flex;
            align-items: center;
            gap: 5px;
            padding: 6px 10px;
        }

        .typing-dot {
            width: 6px;
            height: 6px;
            background-color: var(--text-muted);
            border-radius: 50%;
            animation: bounce 1.4s infinite ease-in-out both;
        }

        .typing-dot:nth-child(1) { animation-delay: -0.32s; }
        .typing-dot:nth-child(2) { animation-delay: -0.16s; }

        @keyframes bounce {
            0%, 80%, 100% { transform: scale(0.2); opacity: 0.4; }
            40% { transform: scale(1.0); opacity: 1; }
        }

        /* Markdown styling additions */
        .message-bubble pre {
            background: rgba(0, 0, 0, 0.2);
            padding: 14px;
            border-radius: 12px;
            overflow-x: auto;
            border: 1px solid var(--border-glass);
            margin: 12px 0;
        }

        .light-mode .message-bubble pre {
            background: #f1f5f9;
        }

        .message-bubble code {
            font-family: 'Fira Code', 'Courier New', monospace;
            background: rgba(255, 255, 255, 0.08);
            padding: 3px 6px;
            border-radius: 6px;
            font-size: 0.88em;
            color: var(--accent-pink);
        }

        .light-mode .message-bubble code {
            background: rgba(0, 0, 0, 0.05);
        }

        .message-bubble pre code {
            background: transparent;
            padding: 0;
            border-radius: 0;
            color: inherit;
        }

        .message-bubble p {
            margin: 0 0 12px 0;
        }

        .message-bubble p:last-child {
            margin: 0;
        }

        .message-bubble table {
            border-collapse: collapse;
            width: 100%;
            margin: 12px 0;
        }

        .message-bubble th, .message-bubble td {
            border: 1px solid var(--border-glass);
            padding: 10px 14px;
            text-align: left;
        }

        .message-bubble th {
            background: rgba(255, 255, 255, 0.03);
            font-weight: 600;
        }

        /* Welcome Page Redesign */
        .welcome-container {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100%;
            text-align: center;
            max-width: 800px;
            margin: auto;
            padding: 40px 20px;
        }

        .welcome-logo {
            font-size: 4rem;
            margin-bottom: 24px;
            background: var(--accent-gradient);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            filter: drop-shadow(0 0 20px rgba(139, 92, 246, 0.2));
            animation: float 6s ease-in-out infinite;
        }

        @keyframes float {
            0% { transform: translateY(0px); }
            50% { transform: translateY(-10px); }
            100% { transform: translateY(0px); }
        }

        .welcome-title {
            font-family: 'Outfit', sans-serif;
            font-size: 2.2rem;
            font-weight: 800;
            margin: 0 0 12px 0;
            letter-spacing: -0.02em;
            background: linear-gradient(to right, var(--text-primary), var(--text-secondary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .welcome-subtitle {
            font-size: 1rem;
            line-height: 1.6;
            margin: 0 0 32px 0;
            color: var(--text-secondary);
            max-width: 550px;
        }

        .suggestions-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            width: 100%;
            max-width: 700px;
        }

        @media (max-width: 600px) {
            .suggestions-grid {
                grid-template-columns: 1fr;
            }
        }

        .suggestion-card {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border-glass);
            border-radius: 16px;
            padding: 18px;
            text-align: left;
            cursor: pointer;
            transition: all 0.25s ease;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .light-mode .suggestion-card {
            background: rgba(0, 0, 0, 0.01);
        }

        .suggestion-card:hover {
            background: rgba(139, 92, 246, 0.06);
            border-color: var(--accent-purple);
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(0, 0, 0, 0.15);
        }

        .light-mode .suggestion-card:hover {
            background: rgba(99, 102, 241, 0.06);
            box-shadow: 0 8px 25px rgba(99, 102, 241, 0.06);
        }

        .suggestion-icon {
            font-size: 1.5rem;
        }

        .suggestion-text {
            font-size: 0.9rem;
            font-weight: 500;
            color: var(--text-secondary);
            line-height: 1.4;
        }

        .suggestion-card:hover .suggestion-text {
            color: var(--text-primary);
        }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="sidebar-header">
            <div class="logo">
                <span>💬</span> LLM Chat Web
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
                <button id="theme-toggle-btn" title="Toggle Light/Dark Theme">☀️</button>
                <div class="status-dot"></div>
            </div>
        </div>
        <div class="sidebar-content">
            <div>
                <div class="section-title">Backend</div>
                <select id="backend-select" class="dropdown-control">
                    <option value="ollama">Ollama (Local)</option>
                    <option value="llamacpp">llama.cpp Server</option>
                    <option value="unsloth">Unsloth Studio</option>
                </select>
            </div>
            <div>
                <div class="section-title">Model</div>
                <select id="model-select" class="dropdown-control">
                    <option value="">Loading models...</option>
                </select>
            </div>
            <div style="margin: 8px 0; display: flex; flex-direction: column; gap: 12px;">
                <div class="toggle-switch-container">
                    <span>Show Thinking</span>
                    <label class="switch">
                        <input type="checkbox" id="toggle-thinking" checked>
                        <span class="slider"></span>
                    </label>
                </div>
                <div class="toggle-switch-container">
                    <span>Show Tool Runs</span>
                    <label class="switch">
                        <input type="checkbox" id="toggle-tools" checked>
                        <span class="slider"></span>
                    </label>
                </div>
            </div>
            <button id="new-chat-btn" class="btn">
                <span>➕</span> New Session
            </button>
            <div>
                <div class="section-title">Session Controls</div>
                <div style="display: flex; flex-direction: column; gap: 8px;">
                    <button id="sidebar-clear-btn" class="btn btn-secondary" style="justify-content: flex-start; padding: 10px 14px; font-size: 0.85rem;">
                        <span>🧹</span> Clear Active Chat
                    </button>
                    <button id="sidebar-compact-btn" class="btn btn-secondary" style="justify-content: flex-start; padding: 10px 14px; font-size: 0.85rem;">
                        <span>📦</span> Compact Context
                    </button>
                    <button id="sidebar-save-btn" class="btn btn-secondary" style="justify-content: flex-start; padding: 10px 14px; font-size: 0.85rem;">
                        <span>💾</span> Save Last Response
                    </button>
                    <button id="sidebar-history-btn" class="btn btn-secondary" style="justify-content: flex-start; padding: 10px 14px; font-size: 0.85rem;">
                        <span>📜</span> View Raw History
                    </button>
                    <button id="sidebar-sessions-btn" class="btn btn-secondary" style="justify-content: flex-start; padding: 10px 14px; font-size: 0.85rem;">
                        <span>🗂️</span> Focus Recent Chats
                    </button>
                    <button id="sidebar-exit-btn" class="btn btn-secondary" style="justify-content: flex-start; padding: 10px 14px; font-size: 0.85rem;">
                        <span>🚪</span> Close Window
                    </button>
                </div>
            </div>
            <div style="flex-grow: 1; display: flex; flex-direction: column; min-height: 150px;">
                <div class="section-title">Recent Chats</div>
                <div id="sessions-list" class="sessions-list">
                    <!-- Session cards dynamically loaded -->
                </div>
            </div>
            <button id="clear-all-btn" class="btn btn-secondary">
                🗑️ Clear All Sessions
            </button>
        </div>
    </div>

    <div class="chat-container">
        <div class="chat-header">
            <div style="display: flex; align-items: center; gap: 15px;">
                <button id="sidebar-toggle-btn" class="icon-btn" title="Toggle Sidebar">☰</button>
                <div class="chat-title-container">
                    <div id="active-model-title" class="chat-title">New Conversation</div>
                    <div id="active-backend-subtitle" class="chat-subtitle">No session active</div>
                </div>
            </div>
        </div>

        <div id="messages-list" class="messages-list">
            <!-- Messages render here -->
        </div>

        <div class="input-outer">
            <div id="autocomplete-box" class="autocomplete-box">
                <!-- Autocomplete items -->
            </div>
            <div class="input-container">
                <textarea id="chat-input" class="chat-input" placeholder="Type a message or '/' for commands..." rows="1"></textarea>
                <button id="send-btn" class="send-btn">
                    <span>➔</span>
                </button>
            </div>
        </div>
    </div>

    <script>
        const AVAILABLE_COMMANDS = ['/bye', '/exit', '/clear', '/compact', '/history', '/sessions', '/save'];
        let activeSessionId = null;
        let activeMessages = [];
        let modelsList = { ollama: [], llamacpp: [], unsloth: [] };
        
        // DOM Elements
        const backendSelect = document.getElementById('backend-select');
        const modelSelect = document.getElementById('model-select');
        const newChatBtn = document.getElementById('new-chat-btn');
        const clearAllBtn = document.getElementById('clear-all-btn');
        const sessionsListEl = document.getElementById('sessions-list');
        const activeModelTitle = document.getElementById('active-model-title');
        const activeBackendSubtitle = document.getElementById('active-backend-subtitle');
        const messagesListEl = document.getElementById('messages-list');
        const chatInput = document.getElementById('chat-input');
        const sendBtn = document.getElementById('send-btn');
        const autocompleteBox = document.getElementById('autocomplete-box');

        // Init marked (handling newer versions safely)
        try {
            if (typeof marked.use === 'function') {
                marked.use({
                    gfm: true,
                    breaks: true
                });
            } else if (typeof marked.setOptions === 'function') {
                marked.setOptions({
                    gfm: true,
                    breaks: true
                });
            }
        } catch (e) {
            console.warn("Failed to set marked options", e);
        }

        // Auto-expand input
        chatInput.addEventListener('input', () => {
            chatInput.style.height = 'auto';
            chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
            handleAutocomplete();
        });

        // Fetch Sessions
        async function fetchSessions() {
            try {
                const res = await fetch('/api/sessions');
                const sessions = await res.json();
                renderSessions(sessions);
            } catch (err) {
                console.error("Error loading sessions", err);
            }
        }

        // Render Sessions list
        function renderSessions(sessions) {
            sessionsListEl.innerHTML = '';
            if (sessions.length === 0) {
                sessionsListEl.innerHTML = '<div style="font-size: 0.85rem; color: var(--text-muted); text-align: center; padding: 20px;">No recent sessions</div>';
                return;
            }

            sessions.forEach(s => {
                const card = document.createElement('div');
                card.className = `session-card ${s.id === activeSessionId ? 'active' : ''}`;
                card.onclick = () => loadSession(s);
                
                card.innerHTML = `
                    <div class="session-meta">
                        <span>${s.backend}</span>
                        <span>${s.timestamp.split(' ')[0]}</span>
                    </div>
                    <div class="session-preview">${s.preview || 'Empty session'}</div>
                    <button class="session-delete-btn" title="Delete Session" onclick="event.stopPropagation(); deleteSession('${s.id}')">🗑️</button>
                `;
                sessionsListEl.appendChild(card);
            });
        }

        // Load specific Session
        function loadSession(session) {
            activeSessionId = session.id;
            activeMessages = session.messages;
            activeModelTitle.innerText = session.model;
            activeBackendSubtitle.innerText = `${session.backend} session • ${session.timestamp}`;
            
            // Set selections
            backendSelect.value = session.backend;
            populateModels(session.backend, session.model);

            renderMessages();
            fetchSessions();
        }

        // Render Active messages
        function renderMessages() {
            messagesListEl.innerHTML = '';
            
            // Filter out system messages
            const visibleMessages = activeMessages.filter(m => m.role !== 'system');
            
            if (visibleMessages.length === 0) {
                messagesListEl.innerHTML = `
                    <div class="welcome-container">
                        <div class="welcome-logo">✨</div>
                        <h1 class="welcome-title">How can I assist you today?</h1>
                        <p class="welcome-subtitle">Select your backend and model on the left, then start chatting. Here are some quick ideas:</p>
                        
                        <div class="suggestions-grid">
                            <div class="suggestion-card" onclick="useSuggestion('Explain quantum computing in simple terms')">
                                <div class="suggestion-icon">⚛️</div>
                                <div class="suggestion-text">Explain quantum computing in simple terms</div>
                            </div>
                            <div class="suggestion-card" onclick="useSuggestion('Write a clean Python implementation of Quicksort')">
                                <div class="suggestion-icon">🐍</div>
                                <div class="suggestion-text">Write a Python implementation of Quicksort</div>
                            </div>
                            <div class="suggestion-card" onclick="useSuggestion('Draft an email proposing a weekend team-building activity')">
                                <div class="suggestion-icon">✉️</div>
                                <div class="suggestion-text">Draft a team-building email proposal</div>
                            </div>
                            <div class="suggestion-card" onclick="useSuggestion('Brainstorm 5 unique startup ideas in the renewable energy sector')">
                                <div class="suggestion-icon">💡</div>
                                <div class="suggestion-text">Brainstorm startup ideas in green energy</div>
                            </div>
                        </div>
                    </div>
                `;
                return;
            }

            console.log("renderMessages processing. visibleMessages count:", visibleMessages.length);
            visibleMessages.forEach(msg => {
                console.log("Processing message:", { role: msg.role, hasToolCalls: !!msg.tool_calls, toolCalls: msg.tool_calls });
                // Skip tool result messages since they are rendered inline in the assistant tool_calls panel
                if (msg.role === 'tool') {
                    return;
                }

                // Render tool panels if present on assistant message
                try {
                    if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
                        msg.tool_calls.forEach(tc => {
                            if (!tc || !tc.function) return;
                            const funcName = tc.function.name || 'unknown';
                            const funcArgs = typeof tc.function.arguments === 'string'
                                ? tc.function.arguments
                                : JSON.stringify(tc.function.arguments || {});
                            
                            // Find matching tool result in history
                            let resultVal = "No output returned.";
                            const currentMsgIdx = visibleMessages.indexOf(msg);
                            if (currentMsgIdx !== -1) {
                                for (let i = currentMsgIdx + 1; i < visibleMessages.length; i++) {
                                    const nextMsg = visibleMessages[i];
                                    if (nextMsg.role === 'tool' && (nextMsg.name === funcName || nextMsg.tool_call_id === tc.id)) {
                                        resultVal = nextMsg.content;
                                        break;
                                    }
                                }
                            }

                            const row = document.createElement('div');
                            row.className = 'message-row assistant';
                            
                            const panel = document.createElement('div');
                            panel.className = 'tool-panel';
                            panel.onclick = () => panel.classList.toggle('collapsed');
                            panel.innerHTML = `
                                <div class="tool-header">🛠️ Tool Run: ${funcName}</div>
                                <div class="tool-content">Arguments: ${funcArgs}\\nResult: ${resultVal}</div>
                            `;
                            row.appendChild(panel);
                            messagesListEl.appendChild(row);
                        });
                    }
                } catch (err) {
                    console.error("Error rendering tool calls", err);
                }

                let content = msg.content || '';
                if (msg.role === 'assistant' && !content.trim()) {
                    return; // Skip empty assistant bubbles
                }

                const row = document.createElement('div');
                row.className = `message-row ${msg.role}`;
                
                const bubble = document.createElement('div');
                bubble.className = 'message-bubble';
                
                // Extract thinking process if present
                if (msg.role === 'assistant' && content.startsWith('<think>')) {
                    const thinkEnd = content.indexOf('</think>');
                    if (thinkEnd !== -1) {
                        const thinkingText = content.substring(7, thinkEnd);
                        const mainText = content.substring(thinkEnd + 8);
                        
                        const thinkPanel = document.createElement('div');
                        thinkPanel.className = 'thinking-panel';
                        thinkPanel.onclick = (e) => {
                            e.stopPropagation();
                            thinkPanel.classList.toggle('collapsed');
                        };
                        thinkPanel.innerHTML = `
                            <div class="thinking-header">Thinking Process</div>
                            <div class="thinking-content">${thinkingText.trim()}</div>
                        `;
                        bubble.appendChild(thinkPanel);
                        content = mainText.trim();
                    }
                }
                
                if (content.trim()) {
                    const textContainer = document.createElement('div');
                    textContainer.className = 'text-container';
                    textContainer.innerHTML = marked.parse(content);
                    bubble.appendChild(textContainer);
                }
                
                if (bubble.children.length > 0) {
                    row.appendChild(bubble);
                    messagesListEl.appendChild(row);
                }
            });
            
            // Highlight code blocks
            document.querySelectorAll('pre code').forEach((block) => {
                hljs.highlightElement(block);
            });

            messagesListEl.scrollTop = messagesListEl.scrollHeight;
        }

        // Delete Session
        async function deleteSession(id) {
            if (confirm("Delete this session?")) {
                await fetch(`/api/sessions/${id}`, { method: 'DELETE' });
                if (activeSessionId === id) {
                    resetSession();
                }
                fetchSessions();
            }
        }

        // Reset Session
        function resetSession() {
            activeSessionId = null;
            activeMessages = [];
            activeModelTitle.innerText = "New Conversation";
            activeBackendSubtitle.innerText = "No session active";
            renderMessages();
        }

        // Clear All
        clearAllBtn.onclick = async () => {
            if (confirm("Are you sure you want to delete ALL saved sessions?")) {
                await fetch('/api/sessions/clear', { method: 'POST' });
                resetSession();
                fetchSessions();
            }
        };

        // Load models list
        async function fetchModels() {
            try {
                const res = await fetch('/api/models');
                const data = await res.json();
                modelsList = data;
                populateModels(backendSelect.value);
            } catch (err) {
                console.error("Error loading models", err);
            }
        }

        function populateModels(backend, selectedModel = null) {
            modelSelect.innerHTML = '';
            const models = modelsList[backend] || [];
            
            if (models.length === 0) {
                modelSelect.innerHTML = '<option value="">No models available</option>';
                return;
            }

            models.forEach(m => {
                const opt = document.createElement('option');
                opt.value = m;
                opt.innerText = m;
                if (m === selectedModel) opt.selected = true;
                modelSelect.appendChild(opt);
            });
        }

        backendSelect.onchange = () => {
            populateModels(backendSelect.value);
        };

        newChatBtn.onclick = () => {
            resetSession();
            fetchSessions();
        };

        // Autocomplete Logic
        function handleAutocomplete() {
            const val = chatInput.value;
            if (val.startsWith('/') && !val.includes(' ')) {
                const query = val.toLowerCase();
                const matches = AVAILABLE_COMMANDS.filter(c => c.startsWith(query));
                if (matches.length > 0) {
                    renderAutocomplete(matches);
                } else {
                    autocompleteBox.style.display = 'none';
                }
            } else {
                autocompleteBox.style.display = 'none';
            }
        }

        function renderAutocomplete(matches) {
            autocompleteBox.innerHTML = '';
            matches.forEach((m, idx) => {
                const item = document.createElement('div');
                item.className = 'autocomplete-item';
                item.innerText = m;
                item.onclick = () => {
                    chatInput.value = m + ' ';
                    chatInput.focus();
                    autocompleteBox.style.display = 'none';
                };
                autocompleteBox.appendChild(item);
            });
            autocompleteBox.style.display = 'block';
        }

        async function runClearCommand() {
            if (!activeSessionId) {
                resetSession();
                return;
            }
            try {
                const res = await fetch(`/api/sessions/${activeSessionId}/clear`, { method: 'POST' });
                const data = await res.json();
                if (data.status === 'success') {
                    activeMessages = data.messages;
                    renderMessages();
                    fetchSessions();
                } else {
                    alert("Error clearing session: " + data.error);
                }
            } catch (err) {
                alert("Failed to clear session: " + err.message);
            }
        }

        async function runCompactCommand() {
            if (!activeSessionId) {
                alert("No active session to compact.");
                return;
            }
            const compactIndicator = document.createElement('div');
            compactIndicator.className = 'message-row assistant';
            compactIndicator.id = 'compact-indicator-row';
            compactIndicator.innerHTML = `
                <div class="message-bubble">
                    <div style="font-size: 0.9rem; color: var(--text-secondary); display: flex; align-items: center; gap: 8px;">
                        <div class="status-dot" style="animation: pulse-green 1s infinite;"></div>
                        Compacting context (generating summary)...
                    </div>
                </div>
            `;
            messagesListEl.appendChild(compactIndicator);
            messagesListEl.scrollTop = messagesListEl.scrollHeight;

            try {
                const res = await fetch(`/api/sessions/${activeSessionId}/compact`, { method: 'POST' });
                const data = await res.json();
                
                const indicator = document.getElementById('compact-indicator-row');
                if (indicator) indicator.remove();

                if (data.status === 'success') {
                    activeMessages = data.messages;
                    renderMessages();
                    fetchSessions();
                } else {
                    alert("Error compacting: " + data.error);
                }
            } catch (err) {
                const indicator = document.getElementById('compact-indicator-row');
                if (indicator) indicator.remove();
                alert("Failed to compact session: " + err.message);
            }
        }

        async function runSaveCommand(filepath) {
            if (!activeSessionId) {
                alert("No active session to save.");
                return;
            }
            if (!filepath) {
                alert("Please specify a filename or path.");
                return;
            }
            try {
                const res = await fetch(`/api/sessions/${activeSessionId}/save`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filepath: filepath })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    alert("Last response successfully saved to: " + data.filepath);
                } else {
                    alert("Error saving: " + data.error);
                }
            } catch (err) {
                alert("Failed to save response: " + err.message);
            }
        }

        // Send Message
        async function sendMessage() {
            const val = chatInput.value.trim();
            if (!val) return;

            // Intercept slash commands
            if (val.startsWith('/')) {
                const parts = val.split(' ');
                const cmd = parts[0];
                chatInput.value = '';
                chatInput.style.height = 'auto';

                if (cmd === '/clear') {
                    runClearCommand();
                    return;
                } else if (cmd === '/compact') {
                    runCompactCommand();
                    return;
                } else if (cmd === '/save') {
                    const filepath = parts.slice(1).join(' ').trim();
                    if (!filepath) {
                        alert("Usage: /save <filename>");
                    } else {
                        runSaveCommand(filepath);
                    }
                    return;
                } else if (cmd === '/bye' || cmd === '/exit') {
                    runExitCommand();
                    return;
                } else if (cmd === '/history') {
                    runHistoryCommand();
                    return;
                } else if (cmd === '/sessions') {
                    runSessionsCommand();
                    return;
                }
            }

            chatInput.value = '';
            chatInput.style.height = 'auto';

            // Push User Message
            activeMessages.push({ role: 'user', content: val });
            renderMessages();

            // Typing indicator
            const indicatorRow = document.createElement('div');
            indicatorRow.className = 'message-row assistant';
            indicatorRow.id = 'typing-indicator-row';
            indicatorRow.innerHTML = `
                <div class="message-bubble">
                    <div class="typing-indicator">
                        <div class="typing-dot"></div>
                        <div class="typing-dot"></div>
                        <div class="typing-dot"></div>
                    </div>
                </div>
            `;
            messagesListEl.appendChild(indicatorRow);
            messagesListEl.scrollTop = messagesListEl.scrollHeight;

            const selectedModel = modelSelect.value;
            const selectedBackend = backendSelect.value;

            if (!selectedModel) {
                alert("Please select a model first. If no models are available, ensure Ollama/llama.cpp is running.");
                return;
            }

            try {
                const response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        session_id: activeSessionId,
                        backend: selectedBackend,
                        model: selectedModel,
                        messages: activeMessages
                    })
                });

                // Remove typing indicator
                const tr = document.getElementById('typing-indicator-row');
                if (tr) tr.remove();

                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                
                let currentBubble = null;
                let currentTextContainer = null;
                let currentThinkPanel = null;
                let currentThinkContent = null;
                let currentToolPanel = null;
                let curContent = '';
                let curThinking = '';

                function ensureBubble() {
                    if (!currentBubble) {
                        const assistantBubbleRow = document.createElement('div');
                        assistantBubbleRow.className = 'message-row assistant';
                        currentBubble = document.createElement('div');
                        currentBubble.className = 'message-bubble';
                        assistantBubbleRow.appendChild(currentBubble);
                        messagesListEl.appendChild(assistantBubbleRow);
                    }
                    return currentBubble;
                }

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    
                    const chunk = decoder.decode(value);
                    const lines = chunk.split('\\n');
                    
                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            const dataStr = line.substring(6).trim();
                            if (!dataStr) continue;
                            
                            try {
                                const data = JSON.parse(dataStr);
                                if (data.type === 'meta') {
                                    activeSessionId = data.session_id;
                                } else if (data.type === 'thinking') {
                                    ensureBubble();
                                    if (!currentThinkPanel) {
                                        currentThinkPanel = document.createElement('div');
                                        currentThinkPanel.className = 'thinking-panel';
                                        currentThinkPanel.onclick = () => currentThinkPanel.classList.toggle('collapsed');
                                        currentThinkPanel.innerHTML = `
                                            <div class="thinking-header">Thinking Process</div>
                                            <div class="thinking-content"></div>
                                        `;
                                        if (currentTextContainer) {
                                            currentBubble.insertBefore(currentThinkPanel, currentTextContainer);
                                        } else {
                                            currentBubble.appendChild(currentThinkPanel);
                                        }
                                        currentThinkContent = currentThinkPanel.querySelector('.thinking-content');
                                    }
                                    curThinking += data.content;
                                    currentThinkContent.innerText = curThinking;
                                    messagesListEl.scrollTop = messagesListEl.scrollHeight;
                                } else if (data.type === 'content') {
                                    curContent += data.content;
                                    if (curContent.trim()) {
                                        ensureBubble();
                                        if (!currentTextContainer) {
                                            currentTextContainer = document.createElement('div');
                                            currentTextContainer.className = 'text-container';
                                            currentBubble.appendChild(currentTextContainer);
                                        }
                                        currentTextContainer.innerHTML = marked.parse(curContent);
                                        
                                        // Highlight code blocks dynamically
                                        currentTextContainer.querySelectorAll('pre code').forEach((block) => {
                                            hljs.highlightElement(block);
                                        });
                                    }
                                    messagesListEl.scrollTop = messagesListEl.scrollHeight;
                                } else if (data.type === 'tool_start') {
                                    // Reset assistant containers for any future response parts
                                    currentBubble = null;
                                    currentTextContainer = null;
                                    currentThinkPanel = null;
                                    currentThinkContent = null;
                                    curContent = '';
                                    curThinking = '';

                                    const row = document.createElement('div');
                                    row.className = 'message-row assistant';

                                    currentToolPanel = document.createElement('div');
                                    currentToolPanel.className = 'tool-panel';
                                    currentToolPanel.onclick = () => currentToolPanel.classList.toggle('collapsed');
                                    currentToolPanel.innerHTML = `
                                        <div class="tool-header">⚙️ Executing: ${data.name}...</div>
                                        <div class="tool-content">Arguments: ${JSON.stringify(data.arguments)}</div>
                                    `;
                                    row.appendChild(currentToolPanel);
                                    messagesListEl.appendChild(row);
                                    messagesListEl.scrollTop = messagesListEl.scrollHeight;
                                } else if (data.type === 'tool_result') {
                                    if (currentToolPanel) {
                                        currentToolPanel.querySelector('.tool-header').innerText = `✅ Executed: ${data.name}`;
                                        currentToolPanel.querySelector('.tool-content').innerText += `\nResult: ${data.result}`;
                                    }
                                    messagesListEl.scrollTop = messagesListEl.scrollHeight;
                                } else if (data.type === 'error') {
                                    ensureBubble();
                                    if (!currentTextContainer) {
                                        currentTextContainer = document.createElement('div');
                                        currentTextContainer.className = 'text-container';
                                        currentBubble.appendChild(currentTextContainer);
                                    }
                                    curContent += `\n\n<span style="color: var(--accent-red); font-weight: bold;">⚠️ Error: ${data.content}</span>`;
                                    currentTextContainer.innerHTML = marked.parse(curContent);
                                    messagesListEl.scrollTop = messagesListEl.scrollHeight;
                                } else if (data.type === 'done') {
                                    activeMessages = data.messages;
                                    fetchSessions();
                                }
                            } catch (e) {
                                console.error("Error parsing event data", e);
                            }
                        }
                    }
                }
            } catch (err) {
                console.error("Error sending chat", err);
                const tr = document.getElementById('typing-indicator-row');
                if (tr) tr.remove();
                alert("Failed to communicate with LLM: " + err.message);
            }
        }

        sendBtn.onclick = sendMessage;
        chatInput.onkeydown = (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        };

        async function runHistoryCommand() {
            let rawHistory = activeMessages.map(m => `### ${m.role.toUpperCase()}\\n\\n${m.content}`).join('\\n\\n---\\n\\n');
            if (!rawHistory.trim()) {
                alert("History is empty.");
                return;
            }
            const historyWindow = window.open("", "_blank");
            historyWindow.document.write(`
                <html>
                <head>
                    <title>Conversation History</title>
                    <style>
                        body { font-family: monospace; background: #0a0915; color: #cbd5e1; padding: 20px; white-space: pre-wrap; line-height: 1.5; }
                        h3 { color: #8b5cf6; margin-top: 0; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 5px; }
                        hr { border: 0; border-top: 1px dashed rgba(255,255,255,0.1); margin: 20px 0; }
                    </style>
                </head>
                <body>
                    ${rawHistory.replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\\n/g, "<br>")}
                </body>
                </html>
            `);
            historyWindow.document.close();
        }

        async function runExitCommand() {
            if (confirm("Shut down the chat server and exit?")) {
                try {
                    const overlay = document.createElement('div');
                    overlay.style.position = 'fixed';
                    overlay.style.top = '0';
                    overlay.style.left = '0';
                    overlay.style.width = '100vw';
                    overlay.style.height = '100vh';
                    overlay.style.background = 'rgba(10, 9, 21, 0.95)';
                    overlay.style.display = 'flex';
                    overlay.style.flexDirection = 'column';
                    overlay.style.alignItems = 'center';
                    overlay.style.justifyContent = 'center';
                    overlay.style.color = '#f8fafc';
                    overlay.style.fontFamily = 'system-ui, sans-serif';
                    overlay.style.zIndex = '9999';
                    overlay.innerHTML = `
                        <div style="font-size: 3rem; margin-bottom: 20px;">🚪</div>
                        <div style="font-size: 1.5rem; font-weight: bold; margin-bottom: 10px;">Chat Server Shut Down</div>
                        <div style="color: #64748b; font-size: 0.95rem;">The session has ended. You can safely close this browser window or tab.</div>
                    `;
                    document.body.appendChild(overlay);

                    await fetch('/api/exit', { method: 'POST' });
                } catch (e) {
                    console.error("Failed to shutdown server cleanly", e);
                }
                window.close();
            }
        }

        function runSessionsCommand() {
            const listEl = document.getElementById('sessions-list');
            if (listEl) {
                listEl.scrollIntoView({ behavior: 'smooth' });
                listEl.style.boxShadow = "0 0 15px var(--accent-glow)";
                setTimeout(() => listEl.style.boxShadow = "none", 1500);
            }
        }

        // Bind Sidebar Control Buttons
        document.getElementById('sidebar-clear-btn').onclick = runClearCommand;
        document.getElementById('sidebar-compact-btn').onclick = runCompactCommand;
        document.getElementById('sidebar-save-btn').onclick = () => {
            const filepath = prompt("Enter the filename or path to save the last response to (e.g. response.txt or ~/output.md):");
            if (filepath) {
                runSaveCommand(filepath);
            }
        };
        document.getElementById('sidebar-history-btn').onclick = runHistoryCommand;
        document.getElementById('sidebar-sessions-btn').onclick = runSessionsCommand;
        document.getElementById('sidebar-exit-btn').onclick = runExitCommand;

        // Sidebar Toggle Logic
        const sidebarToggleBtn = document.getElementById('sidebar-toggle-btn');
        sidebarToggleBtn.onclick = () => {
            document.body.classList.toggle('sidebar-collapsed');
            const collapsed = document.body.classList.contains('sidebar-collapsed');
            localStorage.setItem('sidebarCollapsed', collapsed ? 'true' : 'false');
        };
        if (localStorage.getItem('sidebarCollapsed') === 'true') {
            document.body.classList.add('sidebar-collapsed');
        }

        // Suggestions logic
        window.useSuggestion = function(text) {
            chatInput.value = text;
            chatInput.style.height = 'auto';
            chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
            chatInput.focus();
        };

        // Theme Toggle Logic
        const themeToggleBtn = document.getElementById('theme-toggle-btn');
        if (localStorage.getItem('theme') === 'light') {
            document.documentElement.classList.add('light-mode');
            themeToggleBtn.innerText = '🌙';
        } else {
            document.documentElement.classList.remove('light-mode');
            themeToggleBtn.innerText = '☀️';
        }
        themeToggleBtn.onclick = () => {
            document.documentElement.classList.toggle('light-mode');
            const isLight = document.documentElement.classList.contains('light-mode');
            themeToggleBtn.innerText = isLight ? '🌙' : '☀️';
            localStorage.setItem('theme', isLight ? 'light' : 'dark');
        };

        // Show/Hide Thinking and Tools Toggle
        const toggleThinking = document.getElementById('toggle-thinking');
        const toggleTools = document.getElementById('toggle-tools');

        // Restore settings from localStorage
        const hideThinkingPref = localStorage.getItem('hideThinking') === 'true';
        const hideToolsPref = localStorage.getItem('hideTools') === 'true';

        if (hideThinkingPref) {
            toggleThinking.checked = false;
            document.body.classList.add('hide-thinking');
        }
        if (hideToolsPref) {
            toggleTools.checked = false;
            document.body.classList.add('hide-tools');
        }

        toggleThinking.onchange = () => {
            if (toggleThinking.checked) {
                document.body.classList.remove('hide-thinking');
                localStorage.setItem('hideThinking', 'false');
            } else {
                document.body.classList.add('hide-thinking');
                localStorage.setItem('hideThinking', 'true');
            }
        };

        toggleTools.onchange = () => {
            if (toggleTools.checked) {
                document.body.classList.remove('hide-tools');
                localStorage.setItem('hideTools', 'false');
            } else {
                document.body.classList.add('hide-tools');
                localStorage.setItem('hideTools', 'true');
            }
        };

        // On Load
        fetchSessions();
        fetchModels();
    </script>
</body>
</html>
"""

def run_web_server(endpoint_arg: str = None, temperature: float = 0.7, no_tools: bool = False, backend_arg: str = None):
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
        from fastapi.middleware.cors import CORSMiddleware
        import uvicorn
        import webbrowser
        import asyncio
    except ImportError:
        console.print("[bold red]Error: fastapi and uvicorn are required for web mode.[/bold red]")
        console.print("[yellow]Please install them in your Python virtual environment (e.g. pip install fastapi uvicorn).[/yellow]")
        sys.exit(1)

    app = FastAPI(title="LLM Chat Terminal Web App")
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    nonlocal_endpoint = endpoint_arg if (backend_arg == 'llamacpp' or not backend_arg) else "http://localhost:8080"
    if not nonlocal_endpoint:
        nonlocal_endpoint = "http://localhost:8080"

    nonlocal_unsloth_endpoint = endpoint_arg if (backend_arg == 'unsloth') else "http://localhost:8888"
    if not nonlocal_unsloth_endpoint:
        nonlocal_unsloth_endpoint = "http://localhost:8888"

    tools_list = [] if no_tools else list(AVAILABLE_TOOLS.values())
    generation_options = {"temperature": temperature}

    def get_agent_harness_web():
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"""You are an advanced, autonomous AI agent. Your primary goal is to complete the user's objectives safely, accurately, and efficiently.
Current System Date and Time: {current_time}

Follow these core principles at all times:
1. FOCUS: Always stay aligned with the user's primary objective. Break down complex tasks step-by-step.
2. SAFETY: Do not perform irreversible or destructive actions. If an action seems risky, inform the user.
3. VERIFICATION: Do not hallucinate or guess facts. Always use your available tools (like web_search) to gather accurate, up-to-date information before concluding.
4. CONCISENESS: Provide clear, structured, and direct answers. Avoid unnecessary conversational filler."""

    @app.get("/", response_class=HTMLResponse)
    async def get_index():
        return HTML_CONTENT

    @app.get("/api/sessions")
    async def get_sessions():
        sessions = []
        try:
            sessions_dir = get_sessions_dir()
            session_files = []
            for fname in os.listdir(sessions_dir):
                if fname.startswith("session_") and fname.endswith(".json"):
                    fpath = os.path.join(sessions_dir, fname)
                    if os.path.isfile(fpath):
                        session_files.append((fpath, os.path.getmtime(fpath)))
            session_files.sort(key=lambda x: x[1], reverse=True)
            
            for fpath, _ in session_files:
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        sdata = json.load(f)
                        sessions.append({
                            "id": sdata.get("id"),
                            "timestamp": sdata.get("timestamp"),
                            "backend": sdata.get("backend"),
                            "endpoint": sdata.get("endpoint"),
                            "model": sdata.get("model"),
                            "preview": sdata.get("preview", ""),
                            "messages": sdata.get("messages", [])
                        })
                except Exception:
                    pass
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return sessions

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        try:
            sessions_dir = get_sessions_dir()
            s_file = os.path.join(sessions_dir, session_id)
            if os.path.exists(s_file):
                os.remove(s_file)
                return {"status": "success"}
            return JSONResponse({"error": "Session not found"}, status_code=404)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/sessions/clear")
    async def clear_all_sessions():
        try:
            sessions_dir = get_sessions_dir()
            deleted_count = 0
            for fname in os.listdir(sessions_dir):
                if fname.startswith("session_") and fname.endswith(".json"):
                    os.remove(os.path.join(sessions_dir, fname))
                    deleted_count += 1
            return {"status": "success", "deleted_count": deleted_count}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/models")
    async def get_models():
        ollama_models = []
        try:
            models_response = ollama.list()
            ollama_models = [m.model for m in getattr(models_response, 'models', [])]
        except Exception:
            pass
            
        llamacpp_models = []
        try:
            url = f"{nonlocal_endpoint.rstrip('/')}/v1/models"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                llamacpp_models = [m.get("id") for m in data.get("data", [])]
        except Exception:
            pass

        unsloth_models = []
        try:
            url = f"{nonlocal_unsloth_endpoint.rstrip('/')}/v1/models"
            headers = {}
            token = os.getenv("UNSLOTH_STUDIO_AUTH_TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                unsloth_models = [m.get("id") for m in data.get("data", [])]
        except Exception:
            pass
            
        return {
            "ollama": ollama_models,
            "llamacpp": llamacpp_models,
            "llamacpp_endpoint": nonlocal_endpoint,
            "unsloth": unsloth_models,
            "unsloth_endpoint": nonlocal_unsloth_endpoint
        }

    @app.post("/api/sessions/{session_id}/clear")
    async def clear_session(session_id: str):
        try:
            sessions_dir = get_sessions_dir()
            session_file = os.path.join(sessions_dir, session_id)
            if not os.path.exists(session_file):
                return JSONResponse({"error": "Session not found"}, status_code=404)
            with open(session_file, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            
            messages = [{"role": "system", "content": get_agent_harness_web()}]
            save_session(session_id, sdata.get("backend"), sdata.get("endpoint"), sdata.get("model"), messages)
            return {"status": "success", "messages": messages}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/sessions/{session_id}/compact")
    async def compact_session(session_id: str):
        try:
            sessions_dir = get_sessions_dir()
            session_file = os.path.join(sessions_dir, session_id)
            if not os.path.exists(session_file):
                return JSONResponse({"error": "Session not found"}, status_code=404)
            with open(session_file, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            
            messages = sdata.get("messages", [])
            chat_messages = [m for m in messages if m.get("role") in ("user", "assistant")]
            if not chat_messages:
                return JSONResponse({"error": "No conversation history to compact"}, status_code=400)
            
            backend = sdata.get("backend", "ollama")
            selected_model = sdata.get("model")
            endpoint = sdata.get("endpoint")
            if not endpoint:
                if backend == "unsloth":
                    endpoint = nonlocal_unsloth_endpoint
                else:
                    endpoint = nonlocal_endpoint
            
            summary_prompt = {
                "role": "user", 
                "content": "Please provide a highly concise summary of our conversation so far. Include all important facts, code snippets, decisions, and context. I will use this summary to replace our previous messages to save context space."
            }
            
            summary_text = ""
            api_messages = clean_messages_for_api(messages)
            if backend == "ollama":
                loop = asyncio.get_event_loop()
                def call_ollama():
                    return ollama.chat(
                        model=selected_model,
                        messages=api_messages + [summary_prompt],
                        options=generation_options
                    )
                summary_resp = await loop.run_in_executor(None, call_ollama)
                summary_text = summary_resp.get("message", {}).get("content", "")
            else:
                url = f"{endpoint.rstrip('/')}/v1/chat/completions"
                payload = {
                    "model": selected_model,
                    "messages": api_messages + [summary_prompt],
                    "stream": False,
                }
                if generation_options and "temperature" in generation_options:
                    payload["temperature"] = generation_options["temperature"]
                
                loop = asyncio.get_event_loop()
                def call_llamacpp():
                    headers = {'Content-Type': 'application/json'}
                    if backend == "unsloth":
                        token = os.getenv("UNSLOTH_STUDIO_AUTH_TOKEN")
                        if token:
                            headers["Authorization"] = f"Bearer {token}"
                    req = urllib.request.Request(
                        url,
                        data=json.dumps(payload).encode('utf-8'),
                        headers=headers
                    )
                    with urllib.request.urlopen(req) as resp:
                        res_data = json.loads(resp.read().decode('utf-8'))
                        return res_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                summary_text = await loop.run_in_executor(None, call_llamacpp)
            
            if summary_text:
                new_messages = [
                    {"role": "system", "content": get_agent_harness_web()},
                    {"role": "system", "content": f"Summary of earlier conversation:\n{summary_text}"}
                ]
                save_session(session_id, backend, endpoint, selected_model, new_messages)
                return {"status": "success", "messages": new_messages}
            else:
                return JSONResponse({"error": "Failed to generate summary"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/sessions/{session_id}/save")
    async def save_session_file(session_id: str, request: Request):
        try:
            data = await request.json()
            filepath = data.get("filepath")
            if not filepath:
                return JSONResponse({"error": "Filename/filepath is required"}, status_code=400)
                
            sessions_dir = get_sessions_dir()
            session_file = os.path.join(sessions_dir, session_id)
            if not os.path.exists(session_file):
                return JSONResponse({"error": "Session not found"}, status_code=404)
            with open(session_file, "r", encoding="utf-8") as f:
                sdata = json.load(f)
                
            messages = sdata.get("messages", [])
            last_assistant_msg = None
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    last_assistant_msg = msg
                    break
            
            if not last_assistant_msg:
                return JSONResponse({"error": "No assistant response found to save"}, status_code=400)
                
            content = last_assistant_msg.get("content", "")
            import re
            clean_content = re.sub(r"^<think>.*?</think>\s*", "", content, flags=re.DOTALL)
            
            resolved_path = os.path.abspath(os.path.expanduser(filepath))
            with open(resolved_path, "w", encoding="utf-8") as f:
                f.write(clean_content)
                
            return {"status": "success", "filepath": resolved_path}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/exit")
    async def exit_endpoint():
        import threading
        import time
        import os
        import signal
        def shutdown():
            time.sleep(0.5)
            os.kill(os.getpid(), signal.SIGINT)
        threading.Thread(target=shutdown).start()
        return {"status": "exiting"}

    @app.post("/api/chat")
    async def chat_endpoint(request: Request):
        data = await request.json()
        session_id = data.get("session_id")
        if not session_id:
            session_id = f"session_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        backend = data.get("backend", "ollama")
        endpoint = data.get("endpoint")
        if not endpoint:
            if backend == "unsloth":
                endpoint = nonlocal_unsloth_endpoint
            else:
                endpoint = nonlocal_endpoint
        model = data.get("model")
        messages = sanitize_messages(data.get("messages", []))

        # Ensure system prompt is present
        has_system = any(m.get("role") == "system" for m in messages)
        if not has_system:
            messages.insert(0, {"role": "system", "content": get_agent_harness_web()})
            
        async def event_generator():
            nonlocal messages, session_id
            
            while True:
                yield f"data: {json.dumps({'type': 'meta', 'session_id': session_id})}\n\n"
                await asyncio.sleep(0.01)
                
                collected_content = ""
                collected_thinking = ""
                collected_tool_calls = []
                accumulated_tool_calls = {}
                
                in_think = False
                buffer = ""
                
                if backend == "ollama":
                    try:
                        def call_ollama():
                            return ollama.chat(
                                model=model,
                                messages=clean_messages_for_api(messages),
                                tools=tools_list,
                                stream=True,
                                options=generation_options
                            )
                        
                        loop = asyncio.get_event_loop()
                        response_stream = await loop.run_in_executor(None, call_ollama)
                        
                        for chunk in response_stream:
                            msg = chunk.message
                            if msg.tool_calls:
                                collected_tool_calls.extend(msg.tool_calls)
                            if getattr(msg, "thinking", None):
                                collected_thinking += msg.thinking
                                yield f"data: {json.dumps({'type': 'thinking', 'content': msg.thinking})}\n\n"
                                await asyncio.sleep(0.01)
                            if msg.content:
                                content = msg.content
                                buffer += content
                                
                                while True:
                                    if not in_think and "<think>" in buffer:
                                        idx = buffer.find("<think>")
                                        text_chunk = buffer[:idx]
                                        if text_chunk:
                                            collected_content += text_chunk
                                            yield f"data: {json.dumps({'type': 'content', 'content': text_chunk})}\n\n"
                                        in_think = True
                                        buffer = buffer[idx + 7:]
                                    elif in_think and "</think>" in buffer:
                                        idx = buffer.find("</think>")
                                        think_chunk = buffer[:idx]
                                        if think_chunk:
                                            collected_thinking += think_chunk
                                            yield f"data: {json.dumps({'type': 'thinking', 'content': think_chunk})}\n\n"
                                        in_think = False
                                        buffer = buffer[idx + 8:]
                                    else:
                                        break
                                
                                if len(buffer) > 0:
                                    safe_len = len(buffer)
                                    for i in range(1, 8):
                                        if (not in_think and "<think>".startswith(buffer[-i:])) or (in_think and "</think>".startswith(buffer[-i:])):
                                            safe_len = len(buffer) - i
                                            break
                                    if safe_len > 0:
                                        text_to_print = buffer[:safe_len]
                                        if in_think:
                                            collected_thinking += text_to_print
                                            yield f"data: {json.dumps({'type': 'thinking', 'content': text_to_print})}\n\n"
                                        else:
                                            collected_content += text_to_print
                                            yield f"data: {json.dumps({'type': 'content', 'content': text_to_print})}\n\n"
                                        buffer = buffer[safe_len:]
                                await asyncio.sleep(0.001)
                                
                        if buffer:
                            if in_think:
                                collected_thinking += buffer
                                yield f"data: {json.dumps({'type': 'thinking', 'content': buffer})}\n\n"
                            else:
                                collected_content += buffer
                                yield f"data: {json.dumps({'type': 'content', 'content': buffer})}\n\n"
                            buffer = ""
                            
                    except Exception as e:
                        yield f"data: {json.dumps({'type': 'error', 'content': f'Ollama error: {str(e)}'})}\n\n"
                        break
                        
                else:
                    try:
                        formatted_tools = [function_to_openai_tool(t) for t in tools_list] if tools_list else None
                        payload = {
                            "model": model,
                            "messages": clean_messages_for_api(messages),
                            "stream": True,
                        }
                        if generation_options and "temperature" in generation_options:
                            payload["temperature"] = generation_options["temperature"]
                        if formatted_tools:
                            payload["tools"] = formatted_tools
                            
                        def fetch_llamacpp():
                            url = f"{endpoint.rstrip('/')}/v1/chat/completions"
                            headers = {'Content-Type': 'application/json'}
                            if backend == "unsloth":
                                token = os.getenv("UNSLOTH_STUDIO_AUTH_TOKEN")
                                if token:
                                    headers["Authorization"] = f"Bearer {token}"
                            req = urllib.request.Request(
                                url,
                                data=json.dumps(payload).encode('utf-8'),
                                headers=headers
                            )
                            return urllib.request.urlopen(req)
                            
                        loop = asyncio.get_event_loop()
                        response = await loop.run_in_executor(None, fetch_llamacpp)
                        
                        try:
                            while True:
                                line = await loop.run_in_executor(None, lambda: response.readline())
                                if not line:
                                    break
                                line_str = line.decode('utf-8').strip()
                                if not line_str:
                                    continue
                                if line_str.startswith('data: '):
                                    data_str = line_str[6:]
                                    if data_str == '[DONE]':
                                        break
                                    try:
                                        chunk = json.loads(data_str)
                                    except:
                                        continue
                                    
                                    choices = chunk.get("choices", [])
                                    if not choices:
                                        continue
                                    choice = choices[0]
                                    delta = choice.get("delta", {})
                                    
                                    if "tool_calls" in delta:
                                        for tc in delta["tool_calls"]:
                                            idx = tc.get("index", 0)
                                            if idx not in accumulated_tool_calls:
                                                accumulated_tool_calls[idx] = {
                                                    "id": tc.get("id"),
                                                    "type": "function",
                                                    "function": {
                                                        "name": tc.get("function", {}).get("name", ""),
                                                        "arguments": ""
                                                    }
                                                }
                                            if tc.get("id"):
                                                accumulated_tool_calls[idx]["id"] = tc.get("id")
                                            if tc.get("function", {}).get("name"):
                                                accumulated_tool_calls[idx]["function"]["name"] = tc.get("function", {}).get("name")
                                            arg_chunk = tc.get("function", {}).get("arguments", "")
                                            accumulated_tool_calls[idx]["function"]["arguments"] += arg_chunk
                                            
                                    reasoning = delta.get("reasoning_content")
                                    if reasoning:
                                        if not in_think:
                                            in_think = True
                                        collected_thinking += reasoning
                                        yield f"data: {json.dumps({'type': 'thinking', 'content': reasoning})}\n\n"
                                        await asyncio.sleep(0.01)
                                        
                                    content = delta.get("content")
                                    if content:
                                        if in_think:
                                            in_think = False
                                        buffer += content
                                        
                                        while True:
                                            if not in_think and "<think>" in buffer:
                                                idx = buffer.find("<think>")
                                                text_chunk = buffer[:idx]
                                                if text_chunk:
                                                    collected_content += text_chunk
                                                    yield f"data: {json.dumps({'type': 'content', 'content': text_chunk})}\n\n"
                                                in_think = True
                                                buffer = buffer[idx + 7:]
                                            elif in_think and "</think>" in buffer:
                                                idx = buffer.find("</think>")
                                                think_chunk = buffer[:idx]
                                                if think_chunk:
                                                    collected_thinking += think_chunk
                                                    yield f"data: {json.dumps({'type': 'thinking', 'content': think_chunk})}\n\n"
                                                in_think = False
                                                buffer = buffer[idx + 8:]
                                            else:
                                                break
                                                
                                        if len(buffer) > 0:
                                            safe_len = len(buffer)
                                            for i in range(1, 8):
                                                if (not in_think and "<think>".startswith(buffer[-i:])) or (in_think and "</think>".startswith(buffer[-i:])):
                                                    safe_len = len(buffer) - i
                                                    break
                                            if safe_len > 0:
                                                text_to_print = buffer[:safe_len]
                                                if in_think:
                                                    collected_thinking += text_to_print
                                                    yield f"data: {json.dumps({'type': 'thinking', 'content': text_to_print})}\n\n"
                                                else:
                                                    collected_content += text_to_print
                                                    yield f"data: {json.dumps({'type': 'content', 'content': text_to_print})}\n\n"
                                                buffer = buffer[safe_len:]
                                        await asyncio.sleep(0.001)
                                        
                            if buffer:
                                if in_think:
                                    collected_thinking += buffer
                                    yield f"data: {json.dumps({'type': 'thinking', 'content': buffer})}\n\n"
                                else:
                                    collected_content += buffer
                                    yield f"data: {json.dumps({'type': 'content', 'content': buffer})}\n\n"
                            buffer = ""
                        finally:
                            response.close()
                    except Exception as e:
                        yield f"data: {json.dumps({'type': 'error', 'content': f'llama.cpp error: {str(e)}'})}\n\n"
                        break
                
                serializable_tool_calls = []
                import uuid
                if backend == "ollama":
                    if collected_tool_calls:
                        for tc in collected_tool_calls:
                            tc_dict = {}
                            if isinstance(tc, dict):
                                tc_dict = tc.copy()
                            elif hasattr(tc, 'model_dump'):
                                tc_dict = tc.model_dump()
                            elif hasattr(tc, 'dict'):
                                tc_dict = tc.dict()
                            else:
                                tc_dict = {
                                    "id": getattr(tc, "id", None),
                                    "type": "function",
                                    "function": {
                                        "name": getattr(getattr(tc, "function", None), "name", None) or getattr(tc, "name", "unknown"),
                                        "arguments": getattr(getattr(tc, "function", None), "arguments", None) or getattr(tc, "arguments", {})
                                    }
                                }
                            if not tc_dict.get("id"):
                                tc_dict["id"] = f"call_{uuid.uuid4().hex[:8]}"
                            if not tc_dict.get("type"):
                                tc_dict["type"] = "function"
                            serializable_tool_calls.append(tc_dict)
                else:
                    if accumulated_tool_calls:
                        for idx in sorted(accumulated_tool_calls.keys()):
                            tc = accumulated_tool_calls[idx]
                            try:
                                args_dict = json.loads(tc["function"]["arguments"])
                                tc["function"]["arguments"] = args_dict
                            except:
                                pass
                            if not tc.get("id"):
                                tc["id"] = f"call_{uuid.uuid4().hex[:8]}"
                            if not tc.get("type"):
                                tc["type"] = "function"
                            serializable_tool_calls.append(tc)
                
                raw_content = ""
                if collected_thinking:
                    raw_content += f"<think>{collected_thinking}</think>"
                raw_content += collected_content
                
                assistant_msg = {
                    "role": "assistant",
                    "content": raw_content,
                    "tool_calls": serializable_tool_calls if serializable_tool_calls else None
                }
                
                messages.append(assistant_msg)
                save_session(session_id, backend, endpoint, model, messages)
                
                if not serializable_tool_calls:
                    yield f"data: {json.dumps({'type': 'done', 'messages': messages})}\n\n"
                    break
                
                for tool_call in serializable_tool_calls:
                    func_name = tool_call.get("function", {}).get("name")
                    func_args = tool_call.get("function", {}).get("arguments", {})
                    
                    yield f"data: {json.dumps({'type': 'tool_start', 'name': func_name, 'arguments': func_args})}\n\n"
                    await asyncio.sleep(0.01)
                    
                    tool_func = AVAILABLE_TOOLS.get(func_name)
                    if tool_func:
                        try:
                            def run_tool():
                                return tool_func(**func_args)
                            result = await loop.run_in_executor(None, run_tool)
                        except Exception as e:
                            result = f"Error executing tool: {e}"
                    else:
                        result = f"Error: Tool '{func_name}' not found."
                        
                    yield f"data: {json.dumps({'type': 'tool_result', 'name': func_name, 'result': str(result)})}\n\n"
                    await asyncio.sleep(0.01)
                    
                    messages.append({
                        "role": "tool",
                        "name": func_name,
                        "content": str(result)
                    })
                    save_session(session_id, backend, endpoint, model, messages)
                
        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # Find a free port starting at 8000
    import socket
    port = 8000
    for p in range(8000, 8100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', p))
            port = p
            break
        except OSError:
            continue

    # Start the server and launch the browser
    console.print(Panel(
        f"[bold green]Starting LLM Chat Terminal Web App...[/bold green]\n\n"
        f"Opening [cyan]http://127.0.0.1:{port}[/cyan] in your browser.",
        title="Web Server Started",
        expand=False
    ))
    
    # Run uvicorn server
    def start_browser():
        time.sleep(1.5)
        webbrowser.open(f"http://127.0.0.1:{port}")
        
    import threading
    threading.Thread(target=start_browser, daemon=True).start()
    
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

def main():
    parser = argparse.ArgumentParser(description="LLM Chat Terminal with Tools (supports Ollama, llama.cpp, and Unsloth Studio)")
    parser.add_argument("-b", "--backend", type=str, choices=["ollama", "llamacpp", "unsloth"], help="Backend to use (ollama, llamacpp, or unsloth)")
    parser.add_argument("-e", "--endpoint", type=str, help="Custom endpoint URL (defaults: ollama system client, llamacpp: http://localhost:8080, unsloth: http://localhost:8888)")
    parser.add_argument("-m", "--model", type=str, help="Model name to use")
    parser.add_argument("-s", "--system", type=str, help="Custom system prompt override (or path to a system prompt text file)")
    parser.add_argument("-t", "--temperature", type=float, default=0.7, help="Generation temperature (default: 0.7)")
    parser.add_argument("-l", "--list", action="store_true", help="List locally available models and exit")
    parser.add_argument("--no-tools", action="store_true", help="Disable web search tool")
    parser.add_argument("-w", "--web", action="store_true", help="Launch the modern web app interface")
    args = parser.parse_args()

    if args.web:
        run_web_server(endpoint_arg=args.endpoint, temperature=args.temperature, no_tools=args.no_tools, backend_arg=args.backend)
        return

    # Determine backend
    backend = args.backend
    endpoint = args.endpoint
    selected_model = None
    session_id = f"session_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    loaded_messages = None

    if args.list:
        # Check running backends
        ollama_models = []
        try:
            models_response = ollama.list()
            ollama_models = [m.model for m in getattr(models_response, 'models', [])]
        except Exception:
            pass
            
        llamacpp_models = []
        detect_endpoint = endpoint or "http://localhost:8080"
        try:
            url = f"{detect_endpoint.rstrip('/')}/v1/models"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                llamacpp_models = [m.get("id") for m in data.get("data", [])]
        except Exception:
            pass

        unsloth_models = []
        detect_unsloth_endpoint = endpoint or "http://localhost:8888"
        try:
            url = f"{detect_unsloth_endpoint.rstrip('/')}/v1/models"
            headers = {}
            token = os.getenv("UNSLOTH_STUDIO_AUTH_TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                unsloth_models = [m.get("id") for m in data.get("data", [])]
        except Exception:
            pass
            
        if ollama_models:
            console.print(Panel("[bold cyan]Available Ollama Models:[/bold cyan]"))
            for i, m in enumerate(ollama_models, 1):
                console.print(f"  [bold green]{i}.[/bold green] [cyan]{m}[/cyan]")
        if llamacpp_models:
            console.print(Panel(f"[bold cyan]Available models on llama.cpp server ({detect_endpoint}):[/bold cyan]"))
            for i, m in enumerate(llamacpp_models, 1):
                console.print(f"  [bold green]{i}.[/bold green] [cyan]{m}[/cyan]")
        if unsloth_models:
            console.print(Panel(f"[bold cyan]Available models on Unsloth Studio ({detect_unsloth_endpoint}):[/bold cyan]"))
            for i, m in enumerate(unsloth_models, 1):
                console.print(f"  [bold green]{i}.[/bold green] [cyan]{m}[/cyan]")
        if not ollama_models and not llamacpp_models and not unsloth_models:
            console.print("[bold red]No running LLM backends detected to list models.[/bold red]")
        sys.exit(0)

    # CLI Overrides
    if args.backend or args.model:
        backend = args.backend
        selected_model = args.model
        
        # Try to resolve backend if not specified but model is
        if not backend and selected_model:
            ollama_models = []
            try:
                models_response = ollama.list()
                ollama_models = [m.model for m in getattr(models_response, 'models', [])]
            except Exception:
                pass
            if selected_model in ollama_models:
                backend = "ollama"
            else:
                backend = "llamacpp"
        
        if not backend:
            backend = "ollama"
            
        if backend == "ollama":
            if selected_model:
                ollama_models = []
                try:
                    models_response = ollama.list()
                    ollama_models = [m.model for m in getattr(models_response, 'models', [])]
                except Exception:
                    pass
                if selected_model not in ollama_models:
                    matched = False
                    for m in ollama_models:
                        if m.split(':')[0] == selected_model.split(':')[0]:
                            selected_model = m
                            matched = True
                            break
                    if not matched:
                        console.print(f"[bold yellow]Model '{selected_model}' is not available locally in Ollama.[/bold yellow]")
                        pull_choice = Prompt.ask("Would you like to pull/download it from Ollama library?", choices=["y", "n"], default="y")
                        if pull_choice == "y":
                            console.print(f"[cyan]Pulling model '{selected_model}'...[/cyan]")
                            try:
                                with Live(Text("Starting download..."), refresh_per_second=4, console=console) as live:
                                    for progress in ollama.pull(selected_model, stream=True):
                                        status = progress.get('status', '')
                                        total = progress.get('total', 0)
                                        completed = progress.get('completed', 0)
                                        if total > 0:
                                            percent = (completed / total) * 100
                                            total_mb = total / (1024 * 1024)
                                            completed_mb = completed / (1024 * 1024)
                                            live.update(Text(f"{status}: {percent:.1f}% ({completed_mb:.1f}/{total_mb:.1f} MB)"))
                                        else:
                                            live.update(Text(status))
                                console.print(f"[bold green]Successfully pulled model '{selected_model}'![/bold green]")
                            except Exception as e:
                                console.print(f"[bold red]Failed to pull model: {e}[/bold red]")
                                sys.exit(1)
            else:
                ollama_models = []
                try:
                    models_response = ollama.list()
                    ollama_models = [m.model for m in getattr(models_response, 'models', [])]
                except Exception:
                    pass
                if not ollama_models:
                    console.print("[bold red]No Ollama models found. Please download a model first.[/bold red]")
                    sys.exit(1)
                console.print(Panel("[bold cyan]Available Ollama Models:[/bold cyan]"))
                for i, m in enumerate(ollama_models, 1):
                    console.print(f"  [bold green]{i}.[/bold green] [cyan]{m}[/cyan]")
                choice = IntPrompt.ask("\nSelect a model by number", choices=[str(i) for i in range(1, len(ollama_models) + 1)])
                selected_model = ollama_models[choice - 1]
        elif backend == "unsloth":
            if not endpoint:
                endpoint = "http://localhost:8888"
            server_running = False
            try:
                url = f"{endpoint.rstrip('/')}/v1/models"
                headers = {}
                token = os.getenv("UNSLOTH_STUDIO_AUTH_TOKEN")
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        server_running = True
            except urllib.error.HTTPError:
                server_running = True
            except Exception:
                pass
            if not server_running:
                start_unsloth_server(endpoint)
            if not selected_model:
                try:
                    url = f"{endpoint.rstrip('/')}/v1/models"
                    headers = {}
                    token = os.getenv("UNSLOTH_STUDIO_AUTH_TOKEN")
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req) as resp:
                        data = json.loads(resp.read().decode('utf-8'))
                        available_models = [m.get("id") for m in data.get("data", [])]
                        selected_model = available_models[0] if available_models else "unsloth-model"
                except Exception:
                    selected_model = "unsloth-model"
        else:
            if not endpoint:
                endpoint = "http://localhost:8080"
            server_running = False
            try:
                url = f"{endpoint.rstrip('/')}/v1/models"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        server_running = True
            except Exception:
                pass
            if not server_running:
                start_llama_cpp_server(endpoint, selected_model)
            if not selected_model:
                try:
                    url = f"{endpoint.rstrip('/')}/v1/models"
                    req = urllib.request.Request(url)
                    with urllib.request.urlopen(req) as resp:
                        data = json.loads(resp.read().decode('utf-8'))
                        available_models = [m.get("id") for m in data.get("data", [])]
                        selected_model = available_models[0] if available_models else "llama.cpp-model"
                except Exception:
                    selected_model = "llama.cpp-model"
    else:
        # Load unified menu
        while True:
            sessions = []
            try:
                sessions_dir = get_sessions_dir()
                session_files = []
                for fname in os.listdir(sessions_dir):
                    if fname.startswith("session_") and fname.endswith(".json"):
                        fpath = os.path.join(sessions_dir, fname)
                        if os.path.isfile(fpath):
                            session_files.append((fpath, os.path.getmtime(fpath)))
                session_files.sort(key=lambda x: x[1], reverse=True)
                
                for fpath, _ in session_files[:10]:
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            sdata = json.load(f)
                            sessions.append(sdata)
                    except Exception:
                        pass
            except Exception:
                pass

            # Check backends
            ollama_running = False
            ollama_models = []
            try:
                models_response = ollama.list()
                ollama_models = [m.model for m in getattr(models_response, 'models', [])]
                ollama_running = True
            except Exception:
                pass
                
            llamacpp_running = False
            detect_endpoint = endpoint or "http://localhost:8080"
            try:
                url = f"{detect_endpoint.rstrip('/')}/v1/models"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        llamacpp_running = True
            except Exception:
                pass
            
            unsloth_running = False
            detect_unsloth_endpoint = endpoint or "http://localhost:8888"
            unsloth_models = []
            import urllib.error
            try:
                url = f"{detect_unsloth_endpoint.rstrip('/')}/v1/models"
                headers = {}
                token = os.getenv("UNSLOTH_STUDIO_AUTH_TOKEN")
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        unsloth_running = True
                        data = json.loads(resp.read().decode('utf-8'))
                        unsloth_models = [m.get("id") for m in data.get("data", [])]
            except urllib.error.HTTPError:
                unsloth_running = True
            except Exception:
                pass
            
            server_script = os.path.expanduser("~/run_llama_server.sh")
            server_script_exists = os.path.exists(server_script)
            
            options = []
            menu_lines = []
            opt_idx = 1
            
            if ollama_running:
                menu_lines.append("[bold green]Start New Ollama Session:[/bold green]")
                for m in ollama_models:
                    menu_lines.append(f"  [bold green]{opt_idx}.[/bold green] [cyan]{m}[/cyan]")
                    options.append({"type": "ollama", "data": m})
                    opt_idx += 1
                menu_lines.append(f"  [bold green]{opt_idx}.[/bold green] [yellow]Run custom Ollama model (by name/pull)[/yellow]")
                options.append({"type": "ollama_custom"})
                opt_idx += 1
                menu_lines.append("")
                
            if llamacpp_running or server_script_exists:
                menu_lines.append("[bold cyan]Start New llama.cpp Session:[/bold cyan]")
                if llamacpp_running:
                    menu_lines.append(f"  [bold green]{opt_idx}.[/bold green] [cyan]Connect to running server[/cyan] ({detect_endpoint})")
                    options.append({"type": "llamacpp_running", "data": detect_endpoint})
                    opt_idx += 1
                else:
                    menu_lines.append(f"  [bold green]{opt_idx}.[/bold green] [cyan]Start local server & connect[/cyan] (using run_llama_server.sh)")
                    options.append({"type": "llamacpp_start", "data": detect_endpoint})
                    opt_idx += 1
                menu_lines.append("")

            menu_lines.append("[bold cyan]Start New Unsloth Studio Session:[/bold cyan]")
            if unsloth_running and unsloth_models:
                for m in unsloth_models:
                    menu_lines.append(f"  [bold green]{opt_idx}.[/bold green] [cyan]{m}[/cyan] ({detect_unsloth_endpoint})")
                    options.append({"type": "unsloth", "data": {"model": m, "endpoint": detect_unsloth_endpoint}})
                    opt_idx += 1
            else:
                label = f"Connect to running Unsloth Studio ({detect_unsloth_endpoint})" if unsloth_running else f"Connect to Unsloth Studio ({detect_unsloth_endpoint})"
                menu_lines.append(f"  [bold green]{opt_idx}.[/bold green] [cyan]{label}[/cyan]")
                options.append({"type": "unsloth", "data": {"model": "unsloth-model", "endpoint": detect_unsloth_endpoint}})
                opt_idx += 1
            menu_lines.append("")
                
            if sessions:
                menu_lines.append("[bold yellow]Recent Chat Sessions:[/bold yellow]")
                for s in sessions:
                    timestamp = s.get("timestamp", "Unknown time")
                    model_name = s.get("model", "Unknown model")
                    preview = s.get("preview", "")
                    chat_count = len([m for m in s.get("messages", []) if m.get("role") in ("user", "assistant")])
                    menu_lines.append(f"  [bold green]{opt_idx}.[/bold green] [cyan]{model_name}[/cyan] ({chat_count} msgs) - [dim]{timestamp}[/dim]")
                    menu_lines.append(f"     [italic white]\"{preview}\"[/italic white]")
                    options.append({"type": "session", "data": s})
                    opt_idx += 1
                menu_lines.append(f"  [bold green]{opt_idx}.[/bold green] [bold red]Manage / Delete Saved Sessions[/bold red]")
                options.append({"type": "manage_sessions"})
                opt_idx += 1
                menu_lines.append("")
                    
            if not options:
                console.print(Panel(
                    "[bold red]No LLM backends or saved sessions detected.[/bold red]\n\n"
                    "Please make sure either:\n"
                    "  - [bold]Ollama[/bold] is running (start Ollama service)\n"
                    "  - A [bold]llama.cpp server[/bold] is running on http://localhost:8080\n"
                    "  - An [bold]Unsloth Studio[/bold] endpoint is running on http://localhost:8888\n"
                    "  - The script [bold]~/run_llama_server.sh[/bold] is available to start the server",
                    title="Startup Error",
                    border_style="red"
                ))
                sys.exit(1)
                
            console.print(Panel(
                "\n".join(menu_lines).strip(),
                title="[bold cyan]LLM Chat Terminal Startup[/bold cyan]",
                expand=False
            ))
            
            try:
                choice = IntPrompt.ask("\nSelect an option by number", choices=[str(i) for i in range(1, len(options) + 1)], default=1)
                selected = options[choice - 1]
                
                if selected["type"] == "manage_sessions":
                    console.print(Panel(
                        "  [bold green]1.[/bold green] Delete a specific saved session\n"
                        "  [bold green]2.[/bold green] Clear/Delete ALL saved sessions\n"
                        "  [bold green]3.[/bold green] Cancel and return to main menu",
                        title="[bold red]Session Management Options[/bold red]",
                        border_style="red",
                        expand=False
                    ))
                    try:
                        sub_choice = IntPrompt.ask("\nSelect an option", choices=["1", "2", "3"], default=3)
                    except (KeyboardInterrupt, EOFError):
                        continue
                    
                    if sub_choice == 1:
                        console.print("\n[bold yellow]Saved Sessions:[/bold yellow]")
                        for idx, s in enumerate(sessions, 1):
                            timestamp = s.get("timestamp", "Unknown time")
                            model_name = s.get("model", "Unknown model")
                            preview = s.get("preview", "")
                            chat_count = len([m for m in s.get("messages", []) if m.get("role") in ("user", "assistant")])
                            console.print(f"  [bold green]{idx}.[/bold green] [cyan]{model_name}[/cyan] ({chat_count} msgs) - [dim]{timestamp}[/dim] (\"{preview[:40]}...\")")
                        try:
                            del_choice = IntPrompt.ask("\nEnter the number of the session to delete (or 0 to cancel)", choices=[str(i) for i in range(0, len(sessions) + 1)], default=0)
                        except (KeyboardInterrupt, EOFError):
                            continue
                        if del_choice > 0:
                            target_s = sessions[del_choice - 1]
                            try:
                                s_file = os.path.join(get_sessions_dir(), target_s["id"])
                                if os.path.exists(s_file):
                                    os.remove(s_file)
                                    console.print(f"[bold green]Deleted session: {target_s['id']}[/bold green]")
                                else:
                                    console.print(f"[bold red]Session file not found: {s_file}[/bold red]")
                            except Exception as e:
                                console.print(f"[bold red]Error deleting session: {e}[/bold red]")
                    elif sub_choice == 2:
                        try:
                            confirm = Prompt.ask("[bold red]Are you sure you want to delete ALL saved sessions?[/bold red] (yes/no)", choices=["yes", "no"], default="no")
                        except (KeyboardInterrupt, EOFError):
                            continue
                        if confirm == "yes":
                            try:
                                sessions_dir = get_sessions_dir()
                                deleted_count = 0
                                for fname in os.listdir(sessions_dir):
                                    if fname.startswith("session_") and fname.endswith(".json"):
                                        os.remove(os.path.join(sessions_dir, fname))
                                        deleted_count += 1
                                console.print(f"[bold green]Successfully deleted all {deleted_count} saved sessions![/bold green]")
                            except Exception as e:
                                console.print(f"[bold red]Error deleting sessions: {e}[/bold red]")
                    continue

                if selected["type"] == "session":
                    sdata = selected["data"]
                    session_id = sdata["id"]
                    backend = sdata["backend"]
                    endpoint = sdata["endpoint"]
                    selected_model = sdata["model"]
                    loaded_messages = sdata["messages"]
                    console.print(f"[bold green]Resuming session from {sdata.get('timestamp')}...[/bold green]")
                    if backend == "llamacpp":
                        if not endpoint:
                            endpoint = "http://localhost:8080"
                        server_running = False
                        try:
                            url = f"{endpoint.rstrip('/')}/v1/models"
                            req = urllib.request.Request(url)
                            with urllib.request.urlopen(req, timeout=1.0) as resp:
                                if resp.status == 200:
                                    server_running = True
                        except Exception:
                            pass
                        if not server_running:
                            start_llama_cpp_server(endpoint, selected_model)
                            
                elif selected["type"] == "ollama":
                    backend = "ollama"
                    selected_model = selected["data"]
                    
                elif selected["type"] == "ollama_custom":
                    backend = "ollama"
                    selected_model = Prompt.ask("Enter Ollama model name").strip()
                    if selected_model:
                        ollama_models = []
                        try:
                            models_response = ollama.list()
                            ollama_models = [m.model for m in getattr(models_response, 'models', [])]
                        except Exception:
                            pass
                        if selected_model not in ollama_models:
                            matched = False
                            for m in ollama_models:
                                if m.split(':')[0] == selected_model.split(':')[0]:
                                    selected_model = m
                                    matched = True
                                    break
                            if not matched:
                                console.print(f"[bold yellow]Model '{selected_model}' is not available locally in Ollama.[/bold yellow]")
                                pull_choice = Prompt.ask("Would you like to pull/download it from Ollama library?", choices=["y", "n"], default="y")
                                if pull_choice == "y":
                                    console.print(f"[cyan]Pulling model '{selected_model}'...[/cyan]")
                                    try:
                                        with Live(Text("Starting download..."), refresh_per_second=4, console=console) as live:
                                            for progress in ollama.pull(selected_model, stream=True):
                                                status = progress.get('status', '')
                                                total = progress.get('total', 0)
                                                completed = progress.get('completed', 0)
                                                if total > 0:
                                                    percent = (completed / total) * 100
                                                    total_mb = total / (1024 * 1024)
                                                    completed_mb = completed / (1024 * 1024)
                                                    live.update(Text(f"{status}: {percent:.1f}% ({completed_mb:.1f}/{total_mb:.1f} MB)"))
                                                else:
                                                    live.update(Text(status))
                                        console.print(f"[bold green]Successfully pulled model '{selected_model}'![/bold green]")
                                    except Exception as e:
                                        console.print(f"[bold red]Failed to pull model: {e}[/bold red]")
                                        sys.exit(1)
                                else:
                                    console.print("[yellow]Exiting. Please pull the model or select a local model.[/yellow]")
                                    sys.exit(1)
                    else:
                        console.print("[yellow]No model entered. Exiting.[/yellow]")
                        sys.exit(1)
                        
                elif selected["type"] == "llamacpp_running":
                    backend = "llamacpp"
                    endpoint = selected["data"]
                    try:
                        url = f"{endpoint.rstrip('/')}/v1/models"
                        req = urllib.request.Request(url)
                        with urllib.request.urlopen(req) as resp:
                            data = json.loads(resp.read().decode('utf-8'))
                            available_models = [m.get("id") for m in data.get("data", [])]
                            selected_model = available_models[0] if available_models else "llama.cpp-model"
                    except Exception:
                        selected_model = "llama.cpp-model"
                        
                elif selected["type"] == "llamacpp_start":
                    backend = "llamacpp"
                    endpoint = selected["data"]
                    start_llama_cpp_server(endpoint)
                    try:
                        url = f"{endpoint.rstrip('/')}/v1/models"
                        req = urllib.request.Request(url)
                        with urllib.request.urlopen(req) as resp:
                            data = json.loads(resp.read().decode('utf-8'))
                            available_models = [m.get("id") for m in data.get("data", [])]
                            selected_model = available_models[0] if available_models else "llama.cpp-model"
                    except Exception:
                        selected_model = "llama.cpp-model"
                        
                elif selected["type"] == "unsloth":
                    backend = "unsloth"
                    endpoint = selected["data"]["endpoint"]
                    selected_model = selected["data"]["model"]
                    server_running = False
                    try:
                        url = f"{endpoint.rstrip('/')}/v1/models"
                        headers = {}
                        token = os.getenv("UNSLOTH_STUDIO_AUTH_TOKEN")
                        if token:
                            headers["Authorization"] = f"Bearer {token}"
                        req = urllib.request.Request(url, headers=headers)
                        with urllib.request.urlopen(req, timeout=1.0) as resp:
                            if resp.status == 200:
                                server_running = True
                    except urllib.error.HTTPError:
                        server_running = True
                    except Exception:
                        pass
                    if not server_running:
                        start_unsloth_server(endpoint)
                        
                break # normal selection made, exit the startup menu loop
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Startup cancelled. Exiting.[/yellow]")
                sys.exit(1)

    # Pass the actual function references to the client
    tools_list = [] if args.no_tools else list(AVAILABLE_TOOLS.values())
    
    # Load system prompt override if specified
    system_prompt_content = None
    if args.system:
        if os.path.isfile(args.system):
            try:
                with open(args.system, 'r', encoding='utf-8') as f:
                    system_prompt_content = f.read().strip()
            except Exception as e:
                console.print(f"[bold red]Error reading system prompt file: {e}[/bold red]")
                sys.exit(1)
        else:
            system_prompt_content = args.system

    # Agent Harness - System Prompt to guide the model's behavior safely and efficiently
    def get_agent_harness():
        if system_prompt_content:
            return system_prompt_content
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"""You are an advanced, autonomous AI agent. Your primary goal is to complete the user's objectives safely, accurately, and efficiently.
Current System Date and Time: {current_time}

Follow these core principles at all times:
1. FOCUS: Always stay aligned with the user's primary objective. Break down complex tasks step-by-step.
2. SAFETY: Do not perform irreversible or destructive actions. If an action seems risky, inform the user.
3. VERIFICATION: Do not hallucinate or guess facts. Always use your available tools (like web_search) to gather accurate, up-to-date information before concluding.
4. CONCISENESS: Provide clear, structured, and direct answers. Avoid unnecessary conversational filler."""

    if loaded_messages is not None:
        messages = sanitize_messages(loaded_messages)
    else:
        messages = [
            {"role": "system", "content": get_agent_harness()}
        ]
    session = PromptSession(completer=commands_completer)
    
    style = Style.from_dict({
        'prompt': 'bold #00aa00',
    })
    
    console.print(Panel(
        f"[bold blue]Starting chat with model:[/bold blue] [cyan]{selected_model}[/cyan] ({'Ollama' if backend == 'ollama' else 'llama.cpp'})\n"
        f"[bold blue]Endpoint:[/bold blue] [cyan]{endpoint if endpoint else 'Default Ollama'}[/cyan]\n"
        f"[bold blue]Available tools:[/bold blue] [cyan]{', '.join(AVAILABLE_TOOLS.keys()) if tools_list else 'None'}[/cyan]\n\n"
        "[dim]Type '/bye' or '/exit' to exit.\n"
        "Type '/clear' to wipe context, '/compact' to summarize history.\n"
        "Type '/history' to print conversation history in markdown.\n"
        "Type '/sessions' to list, resume, or delete saved sessions.\n"
        "Type '/save <filename>' to save chat transcript.[/dim]",
        title="LLM Chat Advanced Terminal",
        expand=False
    ))
    
    if loaded_messages is not None:
        print_history_in_markdown(messages)
        
    generation_options = {"temperature": args.temperature}
    
    while True:
        # Dynamically update system prompt's timestamp to the current time
        if messages and messages[0].get("role") == "system":
            import re
            content = messages[0].get("content", "")
            if "Current System Date and Time:" in content:
                current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                messages[0]["content"] = re.sub(
                    r"Current System Date and Time: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                    f"Current System Date and Time: {current_time}",
                    content
                )

        if len(messages) > 30:
            console.print(f"[bold yellow]Warning: Conversation context is getting long ({len(messages)} messages). Consider using '/compact' or '/clear'.[/bold yellow]")
            
        try:
            chat_count = len([m for m in messages if m.get("role") in ("user", "assistant")])
            user_input = session.prompt(HTML(f'<prompt><b>({chat_count} msgs) >>> </b></prompt>'), style=style)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Exiting...[/dim]")
            break
            
        if user_input.strip() in ["/bye", "/exit"]:
            console.print("[dim]Exiting...[/dim]")
            break
            
        if user_input.strip() == "/sessions":
            console.print(Panel(
                "  [bold green]1.[/bold green] List / Resume another saved session\n"
                "  [bold green]2.[/bold green] Delete a specific saved session\n"
                "  [bold green]3.[/bold green] Clear/Delete ALL saved sessions\n"
                "  [bold green]4.[/bold green] Cancel",
                title="[bold cyan]Session Management[/bold cyan]",
                expand=False
            ))
            try:
                action = IntPrompt.ask("\nSelect an action", choices=["1", "2", "3", "4"], default=4)
            except (KeyboardInterrupt, EOFError):
                continue
            
            if action == 1 or action == 2:
                sessions_dir = get_sessions_dir()
                session_files = []
                for fname in os.listdir(sessions_dir):
                    if fname.startswith("session_") and fname.endswith(".json"):
                        fpath = os.path.join(sessions_dir, fname)
                        if os.path.isfile(fpath):
                            session_files.append((fpath, os.path.getmtime(fpath)))
                session_files.sort(key=lambda x: x[1], reverse=True)
                
                active_sessions = []
                for fpath, _ in session_files:
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            sdata = json.load(f)
                            active_sessions.append(sdata)
                    except Exception:
                        pass
                
                if not active_sessions:
                    console.print("[yellow]No saved sessions found.[/yellow]")
                    continue
                
                console.print(f"\n[bold yellow]Saved Sessions:[/bold yellow]")
                for idx, s in enumerate(active_sessions, 1):
                    timestamp = s.get("timestamp", "Unknown time")
                    model_name = s.get("model", "Unknown model")
                    preview = s.get("preview", "")
                    chat_count = len([m for m in s.get("messages", []) if m.get("role") in ("user", "assistant")])
                    console.print(f"  [bold green]{idx}.[/bold green] [cyan]{model_name}[/cyan] ({chat_count} msgs) - [dim]{timestamp}[/dim] (\"{preview[:40]}...\")")
                
                try:
                    choice = IntPrompt.ask("\nEnter session number (or 0 to cancel)", choices=[str(i) for i in range(0, len(active_sessions) + 1)], default=0)
                except (KeyboardInterrupt, EOFError):
                    continue
                
                if choice > 0:
                    target_s = active_sessions[choice - 1]
                    if action == 1:
                        session_id = target_s["id"]
                        backend = target_s["backend"]
                        endpoint = target_s["endpoint"]
                        selected_model = target_s["model"]
                        messages = sanitize_messages(target_s["messages"])
                        console.print(f"[bold green]Switched to session from {target_s.get('timestamp')} using {selected_model}![/bold green]")
                        print_history_in_markdown(messages)
                    elif action == 2:
                        try:
                            s_file = os.path.join(sessions_dir, target_s["id"])
                            if os.path.exists(s_file):
                                os.remove(s_file)
                                console.print(f"[bold green]Deleted session: {target_s['id']}[/bold green]")
                                if target_s["id"] == session_id:
                                    session_id = f"session_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                            else:
                                console.print(f"[bold red]Session file not found: {s_file}[/bold red]")
                        except Exception as e:
                            console.print(f"[bold red]Error deleting session: {e}[/bold red]")
            
            elif action == 3:
                try:
                    confirm = Prompt.ask("[bold red]Are you sure you want to delete ALL saved sessions?[/bold red] (yes/no)", choices=["yes", "no"], default="no")
                except (KeyboardInterrupt, EOFError):
                    continue
                if confirm == "yes":
                    try:
                        sessions_dir = get_sessions_dir()
                        deleted_count = 0
                        for fname in os.listdir(sessions_dir):
                            if fname.startswith("session_") and fname.endswith(".json"):
                                os.remove(os.path.join(sessions_dir, fname))
                                deleted_count += 1
                        console.print(f"[bold green]Successfully deleted all {deleted_count} saved sessions![/bold green]")
                        session_id = f"session_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                        messages = [
                            {"role": "system", "content": get_agent_harness()}
                        ]
                    except Exception as e:
                        console.print(f"[bold red]Error deleting sessions: {e}[/bold red]")
            continue

        if user_input.strip() == "/clear":
            messages = [{"role": "system", "content": get_agent_harness()}]
            save_session(session_id, backend, endpoint, selected_model, messages)
            console.print("[bold yellow]Conversation context cleared (Harness retained).[/bold yellow]")
            continue
            
        if user_input.strip() == "/compact":
            chat_messages = [m for m in messages if m.get("role") in ("user", "assistant")]
            if not chat_messages:
                console.print("[bold yellow]No conversation history to compact.[/bold yellow]")
                continue
            console.print("[bold yellow]Compacting context (generating summary)...[/bold yellow]")
            summary_prompt = {
                "role": "user", 
                "content": "Please provide a highly concise summary of our conversation so far. Include all important facts, code snippets, decisions, and context. I will use this summary to replace our previous messages to save context space."
            }
            try:
                api_messages = clean_messages_for_api(messages)
                if backend == "ollama":
                    summary_resp = ollama.chat(
                        model=selected_model,
                        messages=api_messages + [summary_prompt],
                        options=generation_options
                    )
                    summary_text = summary_resp.get("message", {}).get("content", "")
                else:
                    url = f"{endpoint.rstrip('/')}/v1/chat/completions"
                    payload = {
                        "model": selected_model,
                        "messages": api_messages + [summary_prompt],
                        "stream": False,
                    }
                    if generation_options and "temperature" in generation_options:
                        payload["temperature"] = generation_options["temperature"]
                    req = urllib.request.Request(
                        url,
                        data=json.dumps(payload).encode('utf-8'),
                        headers={'Content-Type': 'application/json'}
                    )
                    with urllib.request.urlopen(req) as resp:
                        res_data = json.loads(resp.read().decode('utf-8'))
                        summary_text = res_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                if summary_text:
                    messages = [
                        {"role": "system", "content": get_agent_harness()},
                        {"role": "system", "content": f"Summary of earlier conversation:\n{summary_text}"}
                    ]
                    save_session(session_id, backend, endpoint, selected_model, messages)
                    console.print("[bold green]Context successfully compacted into a summary![/bold green]")
                else:
                    console.print("[bold yellow]Failed to generate summary.[/bold yellow]")
            except Exception as e:
                console.print(f"[bold red]Error compacting context: {e}[/bold red]")
            continue
            
        if user_input.strip() == "/history":
            print_history_in_markdown(messages)
            continue

        if user_input.strip().startswith("/save"):
            parts = user_input.strip().split(maxsplit=1)
            if len(parts) < 2:
                console.print("[bold yellow]Usage: /save <filename>[/bold yellow]")
                continue
            filepath = parts[1]
            try:
                # Find the last assistant response
                last_assistant_msg = None
                for msg in reversed(messages):
                    if msg.get("role") == "assistant":
                        last_assistant_msg = msg
                        break
                
                if not last_assistant_msg:
                    console.print("[bold yellow]No assistant response found to save.[/bold yellow]")
                    continue
                
                content = last_assistant_msg.get("content", "")
                import re
                clean_content = re.sub(r"^<think>.*?</think>\s*", "", content, flags=re.DOTALL)
                
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(clean_content)
                console.print(f"[bold green]Last response successfully saved to '{filepath}'![/bold green]")
            except Exception as e:
                console.print(f"[bold red]Failed to save response: {e}[/bold red]")
            continue

        if not user_input.strip():
            continue
            
        messages.append({"role": "user", "content": user_input})
        save_session(session_id, backend, endpoint, selected_model, messages)
        
        while True:
            # Stream the response
            if backend == "ollama":
                assistant_msg = stream_and_process(selected_model, messages, tools_list, options=generation_options)
            elif backend == "unsloth":
                assistant_msg = stream_and_process_unsloth(endpoint, selected_model, messages, tools_list, options=generation_options)
            else:
                assistant_msg = stream_and_process_llamacpp(endpoint, selected_model, messages, tools_list, options=generation_options)
            
            if not assistant_msg:
                break
                
            messages.append(assistant_msg)
            save_session(session_id, backend, endpoint, selected_model, messages)
            
            # Check for tool calls
            tool_calls = assistant_msg.get("tool_calls")
            if not tool_calls:
                break
                
            # Process each tool call
            for tool_call in tool_calls:
                func_name = tool_call.get("function", {}).get("name")
                func_args = tool_call.get("function", {}).get("arguments", {})
                
                console.print(f"[bold cyan]⚙️  Executing tool:[/bold cyan] [cyan]{func_name}[/cyan]\n[dim]Arguments: {func_args}[/dim]")
                
                tool_func = AVAILABLE_TOOLS.get(func_name)
                if tool_func:
                    try:
                        result = tool_func(**func_args)
                    except Exception as e:
                        result = f"Error executing tool: {e}"
                else:
                    result = f"Error: Tool '{func_name}' not found."
                    
                # Format result for display
                display_result = str(result)
                if len(display_result) > 300:
                    display_result = display_result[:300] + "...\n(truncated)"
                
                console.print(Panel(display_result, title="[yellow]Tool Result", border_style="yellow"))
                
                # Append tool result to messages
                messages.append({
                    "role": "tool",
                    "name": func_name,
                    "content": str(result)
                })
                save_session(session_id, backend, endpoint, selected_model, messages)

if __name__ == "__main__":
    main()
