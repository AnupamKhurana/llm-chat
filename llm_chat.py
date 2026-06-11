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

# Prompt Toolkit for an advanced input prompt (history, arrows)
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML

console = Console()

def web_search(queries: List[str]) -> str:
    """
    Perform a parallel web search using DuckDuckGo across multiple query variations and return the combined results.
    
    Args:
        queries (List[str]): A list of up to 5 search query variations to look up on the web simultaneously. You should generate these variations based on what you need to find.
    """
    console.print(f"[cyan]🔍 Parallel searching {len(queries)} variations: {queries}[/cyan]")
    
    variations = queries[:5] # Limit to max 5 variations for safety
    
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
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
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
    
    return "\n\n".join(all_results[:10])

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
            messages=messages,
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

    # We use direct console printing instead of Live to avoid screen flickering on long outputs
    for chunk in response_stream:
        msg = chunk.message
        
        # Collect tool calls
        if msg.tool_calls:
            collected_tool_calls.extend(msg.tool_calls)

        # Handle native thinking field from newer models
        if getattr(msg, "thinking", None):
            collected_thinking += msg.thinking
            console.print(msg.thinking, style="dim", end="")
            sys.stdout.flush()
            
        if msg.content:
            content = msg.content
            buffer += content
            
            while True:
                if not in_think and "<think>" in buffer:
                    idx = buffer.find("<think>")
                    collected_content += buffer[:idx]
                    console.print(buffer[:idx], end="")
                    console.print("<think>", style="dim", end="")
                    in_think = True
                    buffer = buffer[idx + 7:]
                elif in_think and "</think>" in buffer:
                    idx = buffer.find("</think>")
                    collected_thinking += buffer[:idx]
                    console.print(buffer[:idx], style="dim", end="")
                    console.print("</think>", style="dim", end="")
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
                        console.print(text_to_print, style="dim", end="")
                    else:
                        collected_content += text_to_print
                        console.print(text_to_print, end="")
                    buffer = buffer[safe_len:]
            sys.stdout.flush()

    # Flush remaining buffer
    if buffer:
        if in_think:
            collected_thinking += buffer
            console.print(buffer, style="dim", end="")
        else:
            collected_content += buffer
            console.print(buffer, end="")
        sys.stdout.flush()
        
    print() # newline after response
    
    # Convert tool calls to dictionaries to avoid JSON serialization issues
    serializable_tool_calls = []
    if collected_tool_calls:
        for tc in collected_tool_calls:
            # Pydantic v2 has model_dump, v1 has dict, fallback to standard properties
            if hasattr(tc, 'model_dump'):
                serializable_tool_calls.append(tc.model_dump())
            elif hasattr(tc, 'dict'):
                serializable_tool_calls.append(tc.dict())
            else:
                serializable_tool_calls.append({
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                })
    
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

def stream_and_process_llamacpp(endpoint: str, model_name: str, messages: List[Dict[str, Any]], tools: List[Callable], options: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Stream the response from llama.cpp server, process <think> tags for color, and collect tool calls.
    Returns the complete message object to append to history.
    """
    # 1. Format tools to OpenAI JSON format
    formatted_tools = [function_to_openai_tool(t) for t in tools] if tools else None
    
    # 2. Prepare payload
    payload = {
        "model": model_name,
        "messages": messages,
        "stream": True,
    }
    if options:
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
    if formatted_tools:
        payload["tools"] = formatted_tools

    url = f"{endpoint.rstrip('/')}/v1/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    
    try:
        response = urllib.request.urlopen(req)
    except Exception as e:
        console.print(f"[bold red]Error: llama.cpp request failed: {e}[/bold red]")
        return {}

    collected_content = ""
    collected_thinking = ""
    accumulated_tool_calls = {}
    
    in_think = False
    buffer = ""

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
                
                # Handle reasoning content stream (e.g. from llama.cpp server for reasoning models)
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    if not in_think:
                        console.print("<think>", style="dim", end="")
                        in_think = True
                    collected_thinking += reasoning
                    console.print(reasoning, style="dim", end="")
                    sys.stdout.flush()

                # Handle standard content stream
                content = delta.get("content")
                if content:
                    if in_think:
                        # We were in reasoning mode and now transition to standard content
                        console.print("</think>", style="dim", end="")
                        in_think = False
                    buffer += content
                    
                    while True:
                        if not in_think and "<think>" in buffer:
                            idx = buffer.find("<think>")
                            collected_content += buffer[:idx]
                            console.print(buffer[:idx], end="")
                            console.print("<think>", style="dim", end="")
                            in_think = True
                            buffer = buffer[idx + 7:]
                        elif in_think and "</think>" in buffer:
                            idx = buffer.find("</think>")
                            collected_thinking += buffer[:idx]
                            console.print(buffer[:idx], style="dim", end="")
                            console.print("</think>", style="dim", end="")
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
                                console.print(text_to_print, style="dim", end="")
                            else:
                                collected_content += text_to_print
                                console.print(text_to_print, end="")
                            buffer = buffer[safe_len:]
                    sys.stdout.flush()
    except Exception as e:
        console.print(f"\n[bold red]Error parsing stream: {e}[/bold red]")
    finally:
        response.close()

    # Flush remaining buffer
    if buffer:
        if in_think:
            collected_thinking += buffer
            console.print(buffer, style="dim", end="")
        else:
            collected_content += buffer
            console.print(buffer, end="")
        sys.stdout.flush()
        
    if in_think:
        console.print("</think>", style="dim", end="")
        in_think = False
        
    print() # newline after response
    
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

def main():
    parser = argparse.ArgumentParser(description="LLM Chat Terminal with Tools (supports Ollama and llama.cpp)")
    parser.add_argument("-b", "--backend", type=str, choices=["ollama", "llamacpp"], help="Backend to use (ollama or llamacpp)")
    parser.add_argument("-e", "--endpoint", type=str, help="Custom endpoint URL (defaults: ollama system client, llamacpp: http://localhost:8080)")
    parser.add_argument("-m", "--model", type=str, help="Model name to use")
    parser.add_argument("-s", "--system", type=str, help="Custom system prompt override (or path to a system prompt text file)")
    parser.add_argument("-t", "--temperature", type=float, default=0.7, help="Generation temperature (default: 0.7)")
    parser.add_argument("-l", "--list", action="store_true", help="List locally available models and exit")
    parser.add_argument("--no-tools", action="store_true", help="Disable web search tool")
    args = parser.parse_args()

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
            
        if ollama_models:
            console.print(Panel("[bold cyan]Available Ollama Models:[/bold cyan]"))
            for i, m in enumerate(ollama_models, 1):
                console.print(f"  [bold green]{i}.[/bold green] [cyan]{m}[/cyan]")
        if llamacpp_models:
            console.print(Panel(f"[bold cyan]Available models on llama.cpp server ({detect_endpoint}):[/bold cyan]"))
            for i, m in enumerate(llamacpp_models, 1):
                console.print(f"  [bold green]{i}.[/bold green] [cyan]{m}[/cyan]")
        if not ollama_models and not llamacpp_models:
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
        
        server_script = os.path.expanduser("~/run_llama_server.sh")
        server_script_exists = os.path.exists(server_script)
        
        options = []
        menu_lines = []
        opt_idx = 1
        
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
            menu_lines.append("")
            
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
                
        if not options:
            console.print(Panel(
                "[bold red]No LLM backends or saved sessions detected.[/bold red]\n\n"
                "Please make sure either:\n"
                "  - [bold]Ollama[/bold] is running (start Ollama service)\n"
                "  - A [bold]llama.cpp server[/bold] is running on http://localhost:8080\n"
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
        messages = loaded_messages
    else:
        messages = [
            {"role": "system", "content": get_agent_harness()}
        ]
    session = PromptSession()
    
    style = Style.from_dict({
        'prompt': 'bold #00aa00',
    })
    
    console.print(Panel(
        f"[bold blue]Starting chat with model:[/bold blue] [cyan]{selected_model}[/cyan] ({'Ollama' if backend == 'ollama' else 'llama.cpp'})\n"
        f"[bold blue]Endpoint:[/bold blue] [cyan]{endpoint if endpoint else 'Default Ollama'}[/cyan]\n"
        f"[bold blue]Available tools:[/bold blue] [cyan]{', '.join(AVAILABLE_TOOLS.keys()) if tools_list else 'None'}[/cyan]\n\n"
        "[dim]Type '/bye' or '/exit' to exit.\n"
        "Type '/clear' to wipe context, '/compact' to summarize history.\n"
        "Type '/save <filename>' to save chat transcript.[/dim]",
        title="LLM Chat Advanced Terminal",
        expand=False
    ))
    
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
                if backend == "ollama":
                    summary_resp = ollama.chat(
                        model=selected_model,
                        messages=messages + [summary_prompt],
                        options=generation_options
                    )
                    summary_text = summary_resp.get("message", {}).get("content", "")
                else:
                    url = f"{endpoint.rstrip('/')}/v1/chat/completions"
                    payload = {
                        "model": selected_model,
                        "messages": messages + [summary_prompt],
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
