#!/usr/bin/env python3
"""
agent042-fork — Termux autonomous coding agent.

Fork of netizen4-bit/agent042, rebuilt by Juan1anip.

Features:
  • Real conversation history (the original had amnesia)
  • Auto-model discovery — never breaks when Google retires a model
  • Auto-recovery from 404 model-not-found (mid-session model switch)
  • Retry with backoff on 429 / 5xx
  • Multiple <cmd> blocks per turn
  • Command blocklist + auto-approve for safe reads
  • Subprocess timeout so hung commands don't lock the terminal
  • Session save/load — history survives restarts
  • Slash commands: /help /clear /save /load /list /model /models /undo
                    /context /stream /approve-all /exit
  • Streaming responses with live token output
  • Token usage after each turn
  • Colored, structured output
  • Signal handling — Ctrl-C kills the running command, not just the agent

Pure stdlib. Tested on Python 3.8+.
"""

import os
import re
import sys
import json
import time
import signal
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime

try:
    import readline
except ImportError:
    readline = None


VERSION = "2.1"


# ══════════════════════════════════════════════════════════════════════
# ANSI colors
# ══════════════════════════════════════════════════════════════════════

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"
    RED = "\033[31m"
    GREY = "\033[90m"


BANNER = f"""{C.MAGENTA}{C.BOLD}
   ┌─────────────────────────────────────────┐
   │   agent042-fork · v{VERSION} · termux       │
   └─────────────────────────────────────────┘{C.RESET}
"""


# ══════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════

HOME = Path.home()
AGENT_DIR = HOME / ".agent042"
SESSIONS_DIR = AGENT_DIR / "sessions"
CONFIG_PATH = AGENT_DIR / "config.json"
MODELS_CACHE_PATH = AGENT_DIR / "models_cache.json"
HISTORY_PATH = AGENT_DIR / "history"

AGENT_DIR.mkdir(exist_ok=True)
SESSIONS_DIR.mkdir(exist_ok=True)


DEFAULT_CONFIG = {
    # "model" is left empty — auto-discovery fills it in on first run
    "model": "",
    "api_url_template": (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "{model}:generateContent"
    ),
    "stream_url_template": (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "{model}:streamGenerateContent?alt=sse"
    ),
    "models_url": "https://generativelanguage.googleapis.com/v1beta/models",
    "request_timeout": 60,
    "command_timeout": 60,
    "max_retries": 3,
    "stream": True,
    "temperature": 0.3,
    "max_output_tokens": 2048,
    "auto_discover_models": True,
}


SYSTEM_PROMPT = """You are agent042, an autonomous Termux CLI assistant running on an Android phone.

You help the user by thinking through tasks and running shell commands on their device.

TO RUN A COMMAND, wrap it in <cmd>...</cmd> tags. You may include multiple <cmd> blocks in one reply if the commands are independent (e.g. checking several things at once). Each command runs, its output is fed back to you, and you continue.

Example reply:
    I'll check what's in the current directory and what Python version you have.

    <cmd>ls -la</cmd>
    <cmd>python3 --version</cmd>

Rules:
- ONE command per <cmd> block. Don't chain with && unless it's truly a single task.
- After you see command output, decide the next step. Keep going until the task is done.
- When done, respond with a short plain-text summary (no <cmd> tags).
- If the task needs the user to see something, print it with cat/echo/etc.
- Never invent command output — wait for the real result.
- Be concise. No filler. No emojis.
- Only suggest commands that work on Termux (Android). Don't use sudo, apt, or Linux-only paths.
- Prefer 'pkg' over 'apt' if a package install is needed.

Available tools (all run via <cmd>):
- Shell: ls, cat, echo, pwd, find, grep, head, tail, wc, df, du, ps
- File editing: cat > file << 'EOF' ... EOF, nano, sed, awk
- Python: python3, pip install <pkg> --break-system-packages
- Termux: pkg install <pkg>, termux-battery-status, termux-wifi-connectioninfo
- Network: curl, wget, ping

Safety: the user must approve every command before it runs, unless it's on the safe auto-run list.
"""


# ══════════════════════════════════════════════════════════════════════
# Command classification
# ══════════════════════════════════════════════════════════════════════

SAFE_PREFIXES = (
    "ls", "pwd", "cat", "head", "tail", "wc", "grep", "find", "which",
    "whoami", "uname", "date", "df", "du", "free", "ps",
    "echo", "printf", "type", "file", "stat", "readlink", "realpath",
    "python3 --version", "python --version", "pip --version",
    "node --version", "npm --version", "git --version", "curl --version",
    "pkg list-installed", "pkg search", "termux-battery-status",
    "termux-wifi-connectioninfo", "termux-location",
)

BLOCKED_PATTERNS = (
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=/dev/zero",
    ":(){:|:&};:",
    "> /dev/sd",
    "chmod 777 /",
    "mv /* ",
    "> /system/",
    "> /data/",
    "shutdown",
    "reboot",
)


def classify_command(cmd):
    """Return 'safe', 'blocked', or 'unsafe'."""
    c = cmd.strip()

    for pat in BLOCKED_PATTERNS:
        if pat in c:
            return "blocked"

    if any(x in c for x in ("; ", "&&", "||", "|", "`", "$(")):
        first = c.split()[0] if c.split() else ""
        if first in ("ls", "cat", "head", "tail", "grep", "find", "wc"):
            return "safe"
        return "unsafe"

    for prefix in SAFE_PREFIXES:
        if c == prefix or c.startswith(prefix + " "):
            return "safe"

    return "unsafe"


# ══════════════════════════════════════════════════════════════════════
# Config + session management
# ══════════════════════════════════════════════════════════════════════

CONFIG = dict(DEFAULT_CONFIG)
API_KEY = os.getenv("GEMINI_API_KEY", "")


def load_config():
    global CONFIG
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            CONFIG.update(saved)
        except Exception:
            pass


def save_config():
    try:
        CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2))
    except Exception:
        pass


def session_path(sid):
    return SESSIONS_DIR / f"{sid}.json"


def save_session(sid, conversation, meta=None):
    try:
        data = {
            "id": sid,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "model": CONFIG["model"],
            "conversation": conversation,
        }
        if meta:
            data.update(meta)
        session_path(sid).write_text(json.dumps(data))
    except Exception as e:
        print(f"{C.RED}[session save failed: {e}]{C.RESET}")


def load_session(sid):
    p = session_path(sid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def list_sessions():
    out = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            out.append({
                "id": data.get("id", f.stem),
                "updated": data.get("updated", ""),
                "model": data.get("model", "?"),
                "n": len(data.get("conversation", [])),
                "mtime": f.stat().st_mtime,
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def new_session_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ══════════════════════════════════════════════════════════════════════
# Model discovery
# ══════════════════════════════════════════════════════════════════════

def _version_key(name):
    """Extract (major, minor) version tuple from a model id like gemini-3.8-flash."""
    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2) or 0))


def discover_models(api_key=None, use_cache=True, quiet=False):
    """
    Fetch the live list of Gemini models that support generateContent.

    Returns a list of dicts: [{"name": "gemini-3.8-flash", "display": "...", ...}]
    Returns [] on any failure.
    """
    key = api_key or API_KEY
    if not key:
        return []

    # Try cache first
    if use_cache and MODELS_CACHE_PATH.exists():
        try:
            cached = json.loads(MODELS_CACHE_PATH.read_text())
            age = time.time() - cached.get("ts", 0)
            if age < 24 * 3600 and cached.get("models"):
                return cached["models"]
        except Exception:
            pass

    url = f"{CONFIG['models_url']}?key={urllib.parse.quote(key)}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if not quiet:
            msg = extract_error_message(e)
            print(f"{C.YELLOW}  ⚠ model discovery failed (HTTP {e.code}): {msg}{C.RESET}")
        return []
    except Exception as e:
        if not quiet:
            print(f"{C.YELLOW}  ⚠ model discovery failed: {e}{C.RESET}")
        return []

    models = []
    for m in data.get("models", []):
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" not in methods:
            continue
        name = m.get("name", "")
        if name.startswith("models/"):
            name = name[len("models/"):]
        models.append({
            "name": name,
            "display": m.get("displayName", name),
            "description": m.get("description", ""),
        })

    # Filter out obvious non-chat variants
    def is_chat_capable(m):
        n = m["name"].lower()
        bad = ("embedding", "aqa", "imagen", "veo", "tts", "audio")
        return not any(b in n for b in bad)

    models = [m for m in models if is_chat_capable(m)]
    models.sort(key=lambda m: _version_key(m["name"]), reverse=True)

    try:
        MODELS_CACHE_PATH.write_text(json.dumps({
            "ts": time.time(),
            "models": models,
        }, indent=2))
    except Exception:
        pass

    return models


def pick_best_model(models):
    """
    Choose the best chat model from a list.

    Preference: newest *flash* > newest *pro* > newest anything.
    """
    if not models:
        return None

    def ranked(m):
        n = m["name"].lower()
        v = _version_key(m["name"])
        # score: (tier, version)
        if "flash" in n and "lite" not in n and "thinking" not in n:
            tier = 3
        elif "pro" in n and "vision" not in n:
            tier = 2
        elif "flash" in n:
            tier = 1
        else:
            tier = 0
        return (tier, v[0], v[1])

    ranked_list = sorted(models, key=ranked, reverse=True)
    return ranked_list[0]["name"]


def ensure_model():
    """
    Make sure CONFIG['model'] is set and (probably) valid.

    Rules:
      1. If empty, discover and pick.
      2. If it doesn't appear in the live list, discover and re-pick.
      3. If discovery fails, keep whatever is set (so offline still works).
    """
    if not CONFIG.get("auto_discover_models", True):
        return

    if not API_KEY:
        return

    need_discovery = not CONFIG.get("model")

    # If we have a model, try to validate against the live list
    if not need_discovery and CONFIG.get("model"):
        models = discover_models(quiet=True)
        if models:
            names = [m["name"] for m in models]
            if CONFIG["model"] not in names:
                print(f"{C.YELLOW}  ⚠ {CONFIG['model']} is no longer available{C.RESET}")
                need_discovery = True

    if need_discovery:
        print(f"{C.GREY}  › discovering available models…{C.RESET}")
        models = discover_models(use_cache=False)
        if not models:
            if not CONFIG.get("model"):
                # Nothing to fall back to
                print(f"{C.RED}  ✗ could not discover any model. "
                      f"Set one manually: /model <name>{C.RESET}")
            return
        best = pick_best_model(models)
        if best:
            old = CONFIG.get("model") or "(none)"
            CONFIG["model"] = best
            save_config()
            if old != best:
                print(f"{C.GREEN}  ✓ model set to {best}{C.RESET}")
            return


def handle_model_404():
    """
    Called when we get a 404 model-not-found.
    Refresh the model list, switch to the best one, return True if we changed.
    """
    print(f"{C.YELLOW}  ⚠ model '{CONFIG['model']}' rejected — refreshing{C.RESET}")
    models = discover_models(use_cache=False, quiet=True)
    if not models:
        return False
    best = pick_best_model(models)
    if not best or best == CONFIG["model"]:
        return False
    CONFIG["model"] = best
    save_config()
    print(f"{C.GREEN}  ✓ switched to {best}{C.RESET}")
    return True


# ══════════════════════════════════════════════════════════════════════
# Gemini API client
# ══════════════════════════════════════════════════════════════════════

def build_payload(history):
    contents = []
    for turn in history:
        role = turn.get("role")
        text = turn.get("text", "")
        if role not in ("user", "model"):
            continue
        if not text.strip():
            continue
        contents.append({
            "role": role,
            "parts": [{"text": text}],
        })

    return {
        "contents": contents,
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "generationConfig": {
            "temperature": CONFIG["temperature"],
            "maxOutputTokens": CONFIG["max_output_tokens"],
        },
    }


def parse_retry_delay(err, default=8.0):
    try:
        body = err.read().decode("utf-8", "ignore")
    except Exception:
        return default
    m = re.search(r"retry in ([\d.]+)s", body)
    if m:
        try:
            return float(m.group(1)) + 1.0
        except ValueError:
            pass
    m = re.search(r'"retryDelay":\s*"(\d+)s"', body)
    if m:
        try:
            return float(m.group(1)) + 1.0
        except ValueError:
            pass
    try:
        ra = err.headers.get("Retry-After") if err.headers else None
        if ra:
            return float(ra) + 1.0
    except Exception:
        pass
    return default


def extract_error_message(err):
    try:
        body = err.read().decode("utf-8", "ignore")
        data = json.loads(body)
        return data.get("error", {}).get("message", body[:300])
    except Exception:
        return f"HTTP {getattr(err, 'code', '?')}"


def _is_model_404(err):
    """Detect the 'model not found / retired' class of errors."""
    if err.code != 404:
        # Some Gemini errors come back as 400 with a specific message
        if err.code != 400:
            return False
    try:
        body = err.read().decode("utf-8", "ignore")
    except Exception:
        return False
    lowered = body.lower()
    needles = (
        "not found",
        "not available",
        "no longer available",
        "is not supported",
        "does not exist",
        "not supported for",
    )
    return any(n in lowered for n in needles)


def request_stream(payload):
    """POST to Gemini's streaming endpoint. Yields (kind, value) tuples."""
    url = CONFIG["stream_url_template"].format(model=CONFIG["model"])
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": API_KEY,
    }

    resp = None
    for attempt in range(CONFIG["max_retries"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            resp = urllib.request.urlopen(req, timeout=CONFIG["request_timeout"])
            break
        except urllib.error.HTTPError as e:
            # 404 model error — try auto-switching once
            if _is_model_404(e):
                if handle_model_404():
                    url = CONFIG["stream_url_template"].format(model=CONFIG["model"])
                    continue
                print(f"{C.RED}[HTTP {e.code}] {extract_error_message(e)}{C.RESET}")
                return
            if e.code in (429, 500, 502, 503, 504) and attempt < CONFIG["max_retries"] - 1:
                wait = parse_retry_delay(e, default=5.0)
                print(f"{C.YELLOW}  [retry {attempt+1}/{CONFIG['max_retries']} — "
                      f"HTTP {e.code}, waiting {wait:.1f}s]{C.RESET}")
                time.sleep(wait)
                continue
            msg = extract_error_message(e)
            print(f"{C.RED}[HTTP {e.code}] {msg}{C.RESET}")
            return
        except urllib.error.URLError as e:
            if attempt < CONFIG["max_retries"] - 1:
                print(f"{C.YELLOW}  [network error, retrying in 3s…]{C.RESET}")
                time.sleep(3)
                continue
            print(f"{C.RED}[NETWORK] {e.reason}{C.RESET}")
            return
    if resp is None:
        return

    usage = {}
    try:
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                if line.startswith("data: "):
                    line = line[6:]
                elif line.startswith("data:"):
                    line = line[5:]
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue
                for cand in chunk.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if "text" in part:
                            yield ("text", part["text"])
                um = chunk.get("usageMetadata")
                if um:
                    usage = um
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if usage:
        yield ("usage", usage)


def request_once(payload):
    """Non-streaming fallback."""
    url = CONFIG["api_url_template"].format(model=CONFIG["model"])
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": API_KEY,
    }

    for attempt in range(CONFIG["max_retries"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=CONFIG["request_timeout"]) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            text = ""
            for cand in data.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    if "text" in part:
                        text += part["text"]
            return text, data.get("usageMetadata", {})
        except urllib.error.HTTPError as e:
            if _is_model_404(e):
                if handle_model_404():
                    url = CONFIG["api_url_template"].format(model=CONFIG["model"])
                    continue
                print(f"{C.RED}[HTTP {e.code}] {extract_error_message(e)}{C.RESET}")
                return "", {}
            if e.code in (429, 500, 502, 503, 504) and attempt < CONFIG["max_retries"] - 1:
                wait = parse_retry_delay(e, default=5.0)
                print(f"{C.YELLOW}  [retry {attempt+1}/{CONFIG['max_retries']} — "
                      f"HTTP {e.code}, waiting {wait:.1f}s]{C.RESET}")
                time.sleep(wait)
                continue
            print(f"{C.RED}[HTTP {e.code}] {extract_error_message(e)}{C.RESET}")
            return "", {}
        except urllib.error.URLError as e:
            if attempt < CONFIG["max_retries"] - 1:
                time.sleep(3)
                continue
            print(f"{C.RED}[NETWORK] {e.reason}{C.RESET}")
            return "", {}
    return "", {}


def ask_model(history):
    payload = build_payload(history)

    if CONFIG.get("stream", True):
        print(f"{C.GREEN}model › {C.RESET}", end="", flush=True)
        parts = []
        usage = {}
        for kind, value in request_stream(payload):
            if kind == "text":
                print(value, end="", flush=True)
                parts.append(value)
            elif kind == "usage":
                usage = value
        print()
        return "".join(parts), usage
    else:
        text, usage = request_once(payload)
        print(f"{C.GREEN}model › {C.RESET}{text}")
        return text, usage


# ══════════════════════════════════════════════════════════════════════
# Command parsing + execution
# ══════════════════════════════════════════════════════════════════════

CMD_RE = re.compile(r"<cmd>(.*?)</cmd>", re.DOTALL | re.IGNORECASE)


def find_commands(text):
    return [m.group(1).strip() for m in CMD_RE.finditer(text) if m.group(1).strip()]


CURRENT_PROC = {"proc": None}


def run_command(cmd, timeout=None):
    if timeout is None:
        timeout = CONFIG["command_timeout"]
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        CURRENT_PROC["proc"] = proc
        try:
            out, err = proc.communicate(timeout=timeout)
            return out or "", err or "", proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
            time.sleep(0.5)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            return "", f"command timed out after {timeout}s", 124
    except Exception as e:
        return "", f"ERROR: {e}", 1
    finally:
        CURRENT_PROC["proc"] = None


def ask_permission(cmd, classification):
    if classification == "blocked":
        print(f"\n{C.RED}  ✗ blocked by safety filter:{C.RESET} {cmd}")
        return "no"

    if classification == "safe":
        print(f"\n{C.GREY}  ▸ auto-run (safe):{C.RESET} {cmd}")
        return "yes"

    print(f"\n{C.YELLOW}  ▸ run:{C.RESET} {cmd}")
    try:
        ans = input(f"  {C.CYAN}allow? [y/N/a=always] {C.RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "no"
    if ans == "a":
        return "always"
    if ans == "y":
        return "yes"
    return "no"


# ══════════════════════════════════════════════════════════════════════
# Slash commands
# ══════════════════════════════════════════════════════════════════════

HELP = f"""{C.BOLD}agent042 commands{C.RESET}
  {C.CYAN}/help{C.RESET}              this
  {C.CYAN}/clear{C.RESET}             reset the conversation
  {C.CYAN}/save [name]{C.RESET}       save the session
  {C.CYAN}/load <name>{C.RESET}       load a session
  {C.CYAN}/list{C.RESET}              list saved sessions
  {C.CYAN}/model [name]{C.RESET}      show or set the active model
  {C.CYAN}/models{C.RESET}            list every available model
  {C.CYAN}/refresh-models{C.RESET}    re-fetch the model list from Google
  {C.CYAN}/stream on|off{C.RESET}     toggle streaming
  {C.CYAN}/undo{C.RESET}              remove the last user + model turn
  {C.CYAN}/context{C.RESET}           show history size + settings
  {C.CYAN}/approve-all{C.RESET}       auto-approve every command this session
  {C.CYAN}/exit{C.RESET}              quit
"""


ALLOW_ALL = {"on": False}


def cmd_clear(history):
    history.clear()
    print(f"{C.GREEN}✓ conversation cleared{C.RESET}")


def cmd_list():
    sessions = list_sessions()
    if not sessions:
        print(f"{C.GREY}(no saved sessions){C.RESET}")
        return
    print(f"{C.BOLD}saved sessions{C.RESET}")
    for s in sessions:
        print(f"  {C.CYAN}{s['id']}{C.RESET}  "
              f"{C.GREY}{s['updated'][:16]}  {s['n']} turns  {s['model']}{C.RESET}")


def cmd_models():
    print(f"{C.GREY}fetching from Google…{C.RESET}")
    models = discover_models(use_cache=False)
    if not models:
        print(f"{C.RED}no models returned{C.RESET}")
        return
    current = CONFIG["model"]
    best = pick_best_model(models)
    print(f"{C.BOLD}available models{C.RESET} ({len(models)} total)")
    for m in models:
        marker = ""
        if m["name"] == current:
            marker = f" {C.GREEN}← current{C.RESET}"
        elif m["name"] == best and m["name"] != current:
            marker = f" {C.GREY}(recommended){C.RESET}"
        print(f"  {C.CYAN}{m['name']}{C.RESET}{marker}")


def cmd_context(history):
    total_chars = sum(len(t.get("text", "")) for t in history)
    print(f"{C.GREY}turns: {len(history)}  ·  approx chars: {total_chars}  "
          f"(~{total_chars // 4} tokens){C.RESET}")
    print(f"{C.GREY}model: {CONFIG['model']}  ·  streaming: {CONFIG['stream']}{C.RESET}")
    print(f"{C.GREY}auto-approve-all: {ALLOW_ALL['on']}{C.RESET}")
    print(f"{C.GREY}auto-discovery: {CONFIG.get('auto_discover_models')}{C.RESET}")


def cmd_undo(history):
    while history and history[-1].get("role") == "model":
        history.pop()
    while history and history[-1].get("role") == "user":
        history.pop()
    print(f"{C.GREEN}✓ removed last turn{C.RESET}")


# ══════════════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════════════════════

def print_usage(usage):
    if not usage:
        return
    p = usage.get("promptTokenCount", 0)
    c = usage.get("candidatesTokenCount", 0)
    t = usage.get("totalTokenCount", p + c)
    print(f"{C.GREY}  · tokens: prompt={p}  reply={c}  total={t}{C.RESET}")


def run_turn(user_msg, history):
    history.append({"role": "user", "text": user_msg})
    MAX_LOOPS = 15

    for _ in range(MAX_LOOPS):
        reply, usage = ask_model(history)
        if not reply.strip():
            return

        history.append({"role": "model", "text": reply})
        cmds = find_commands(reply)
        if not cmds:
            print_usage(usage)
            return

        for cmd in cmds:
            classification = classify_command(cmd)
            if ALLOW_ALL["on"] and classification != "blocked":
                classification = "safe"

            decision = ask_permission(cmd, classification)
            if decision == "always":
                ALLOW_ALL["on"] = True
                decision = "yes"
            if decision != "yes":
                history.append({
                    "role": "user",
                    "text": f"<result for: {cmd}>\n(user declined)\n</result>",
                })
                continue

            stdout, stderr, rc = run_command(cmd)
            body = stdout
            if stderr:
                body += ("\n[stderr]\n" if body else "[stderr]\n") + stderr
            if len(body) > 4000:
                body = body[:4000] + f"\n... (truncated, {len(body)} chars)"
            print(f"{C.GREY}  ▸ exit={rc}{C.RESET}")
            if body.strip():
                lines = body.splitlines()
                for line in lines[:12]:
                    print(f"{C.GREY}    {line}{C.RESET}")
                if len(lines) > 12:
                    print(f"{C.GREY}    ... ({len(lines) - 12} more lines){C.RESET}")

            history.append({
                "role": "user",
                "text": f"<result for: {cmd}>\nexit={rc}\n{body}\n</result>",
            })

    print(f"{C.YELLOW}[hit max loops, stopping]{C.RESET}")


def main():
    global API_KEY

    load_config()

    if not API_KEY:
        print(f"{C.RED}[ERROR] GEMINI_API_KEY not set.{C.RESET}")
        print(f'{C.GREY}export GEMINI_API_KEY="..."{C.RESET}')
        sys.exit(1)

    # Auto-discover / validate the model
    ensure_model()

    if not CONFIG.get("model"):
        print(f"{C.RED}[ERROR] no model available. Try /models later, "
              f"or run with an internet connection once.{C.RESET}")
        sys.exit(1)

    if readline is not None:
        try:
            if HISTORY_PATH.exists():
                readline.read_history_file(str(HISTORY_PATH))
        except Exception:
            pass
        readline.set_history_length(1000)

    def on_sigint(sig, frame):
        proc = CURRENT_PROC.get("proc")
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
            print(f"\n{C.YELLOW}[command interrupted]{C.RESET}")
            return
        print(f"\n{C.GREY}bye 🐾{C.RESET}")
        try:
            if readline is not None:
                readline.write_history_file(str(HISTORY_PATH))
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, on_sigint)

    print(BANNER)
    print(f"{C.GREEN}✓{C.RESET} model: {C.BOLD}{CONFIG['model']}{C.RESET}")
    print(f"{C.GREEN}✓{C.RESET} streaming: {CONFIG['stream']}")
    print(f"{C.GREY}/help for commands · /exit to quit{C.RESET}\n")

    history = []
    session_id = new_session_id()

    while True:
        try:
            line = input(f"{C.CYAN}you › {C.RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue

        if line.startswith("/"):
            parts = line.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd in ("/exit", "/quit"):
                break
            if cmd == "/help":
                print(HELP); print(); continue
            if cmd == "/clear":
                cmd_clear(history); print(); continue
            if cmd == "/list":
                cmd_list(); print(); continue
            if cmd == "/models":
                cmd_models(); print(); continue
            if cmd == "/refresh-models":
                print(f"{C.GREY}refreshing model cache…{C.RESET}")
                discover_models(use_cache=False)
                ensure_model()
                print(f"{C.GREEN}✓ current model: {CONFIG['model']}{C.RESET}\n")
                continue
            if cmd == "/context":
                cmd_context(history); print(); continue
            if cmd == "/undo":
                cmd_undo(history); print(); continue
            if cmd == "/save":
                sid = arg or session_id
                save_session(sid, history)
                session_id = sid
                print(f"{C.GREEN}✓ saved: {session_path(sid)}{C.RESET}\n")
                continue
            if cmd == "/load":
                if not arg:
                    print(f"{C.RED}usage: /load <session-id>{C.RESET}\n"); continue
                data = load_session(arg)
                if not data:
                    print(f"{C.RED}no such session: {arg}{C.RESET}\n"); continue
                history = data.get("conversation", [])
                session_id = arg
                print(f"{C.GREEN}✓ loaded {arg} ({len(history)} turns){C.RESET}\n")
                continue
            if cmd == "/model":
                if arg:
                    CONFIG["model"] = arg
                    save_config()
                    print(f"{C.GREEN}✓ model = {arg}{C.RESET}\n")
                else:
                    print(f"{C.CYAN}current: {CONFIG['model']}{C.RESET}\n")
                continue
            if cmd == "/stream":
                if arg in ("on", "off"):
                    CONFIG["stream"] = (arg == "on")
                    save_config()
                    print(f"{C.GREEN}✓ streaming {arg}{C.RESET}\n")
                else:
                    print(f"{C.CYAN}streaming is {CONFIG['stream']}{C.RESET}\n")
                continue
            if cmd == "/approve-all":
                ALLOW_ALL["on"] = not ALLOW_ALL["on"]
                state = "ON" if ALLOW_ALL["on"] else "off"
                print(f"{C.GREEN}✓ auto-approve-all is {state}{C.RESET}\n")
                continue
            print(f"{C.RED}unknown command: {cmd}{C.RESET}\n")
            continue

        try:
            run_turn(line, history)
        except KeyboardInterrupt:
            print(f"\n{C.YELLOW}(interrupted){C.RESET}\n")

        save_session(session_id, history)

    if history:
        save_session(session_id, history)
    try:
        if readline is not None:
            readline.write_history_file(str(HISTORY_PATH))
    except Exception:
        pass
    print(f"{C.GREY}bye 🐾{C.RESET}")


if __name__ == "__main__":
    main()
