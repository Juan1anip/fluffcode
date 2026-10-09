#!/usr/bin/env python3
"""
fluffcode — Termux coding agent with native tool calling.

Fork of netizen4-bit/agent042, rebuilt by Juan1anip.

Version 2.0 — multi-provider:
  • Providers: Gemini, Groq, OpenRouter, OpenAI
  • Mid-chat /provider to switch
  • Per-provider API keys, model cache, and default model
  • Auto-model discovery (skips non-chat and no-tool variants)
  • Setup wizard asks for provider, key, name, personality
  • Native function calling for both API styles
  • Sessions, streaming, backups, robust error handling

Pure stdlib. Termux-friendly.
"""

import os
import re
import sys
import json
import time
import signal
import socket
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


VERSION = "2.0"


# ══════════════════════════════════════════════════════════════════════
# Colors
# ══════════════════════════════════════════════════════════════════════

class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    CYAN = "\033[36m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
    MAGENTA = "\033[35m"; RED = "\033[31m"; GREY = "\033[90m"


def clear_screen():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


BANNER = f"""{C.MAGENTA}{C.BOLD}
   ┌─────────────────────────────────────────┐
   │   fluffcode · v{VERSION} · multi-provider   │
   └─────────────────────────────────────────┘{C.RESET}
"""


# ══════════════════════════════════════════════════════════════════════
# Paths
# ══════════════════════════════════════════════════════════════════════

HOME = Path.home()
AGENT_DIR = HOME / ".agent042"
SESSIONS_DIR = AGENT_DIR / "sessions"
BACKUP_DIR = AGENT_DIR / "backups"
CACHE_DIR = AGENT_DIR / "cache"
CONFIG_PATH = AGENT_DIR / "config.json"
HISTORY_PATH = AGENT_DIR / "history"

for d in (AGENT_DIR, SESSIONS_DIR, BACKUP_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════
# Provider registry
# ══════════════════════════════════════════════════════════════════════

PROVIDERS = {
    "gemini": {
        "label": "Google Gemini",
        "api_style": "gemini",
        "chat_url": "https://generativelanguage.googleapis.com/v1beta/models/{model}",
        "models_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "key_hint": "starts with AIza or AQ.",
        "key_prefixes": ("AIza", "AQ."),
        "signup": "https://aistudio.google.com/apikey",
        "notes": "free tier, 1500 req/day",
    },
    "groq": {
        "label": "Groq",
        "api_style": "openai",
        "chat_url": "https://api.groq.com/openai/v1/chat/completions",
        "models_url": "https://api.groq.com/openai/v1/models",
        "key_hint": "starts with gsk_",
        "key_prefixes": ("gsk_",),
        "signup": "https://console.groq.com/keys",
        "notes": "fastest, generous free tier",
    },
    "openrouter": {
        "label": "OpenRouter",
        "api_style": "openai",
        "chat_url": "https://openrouter.ai/api/v1/chat/completions",
        "models_url": "https://openrouter.ai/api/v1/models",
        "key_hint": "starts with sk-or-",
        "key_prefixes": ("sk-or-",),
        "signup": "https://openrouter.ai/keys",
        "notes": "many models, some free",
    },
    "openai": {
        "label": "OpenAI",
        "api_style": "openai",
        "chat_url": "https://api.openai.com/v1/chat/completions",
        "models_url": "https://api.openai.com/v1/models",
        "key_hint": "starts with sk-",
        "key_prefixes": ("sk-",),
        "signup": "https://platform.openai.com/api-keys",
        "notes": "paid",
    },
}

PROVIDER_ORDER = ["gemini", "groq", "openrouter", "openai"]


def detect_provider_from_key(key):
    key = (key or "").strip()
    if key.startswith("gsk_"):
        return "groq"
    if key.startswith("sk-or-"):
        return "openrouter"
    if key.startswith("AIza") or key.startswith("AQ."):
        return "gemini"
    if key.startswith("sk-"):
        return "openai"
    return None


# ══════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════

def _blank_provider_state():
    return {"api_key": "", "model": ""}


DEFAULT_CONFIG = {
    "user_name": "",
    "custom_prompt": "",
    "active_provider": "gemini",
    "providers": {name: _blank_provider_state() for name in PROVIDERS},
    "workspace": "",
    "request_timeout": 90,
    "command_timeout": 60,
    "max_retries": 3,
    "stream": True,
    "temperature": 0.2,
    "max_output_tokens": 4096,
    "max_steps": 25,
    "read_max_lines": 500,
    "read_max_bytes": 40000,
    "shell_max_chars": 8000,
    "setup_done": False,
}

CONFIG = json.loads(json.dumps(DEFAULT_CONFIG))


def _migrate_old_config(saved):
    """
    Old format had: api_key, model, base_url, models_url at the top level.
    Migrate into providers.gemini (the only provider the old code supported).
    """
    if "providers" in saved and "active_provider" in saved:
        return saved  # already new format

    old_key = saved.get("api_key", "")
    old_model = saved.get("model", "")
    if not old_key and not old_model:
        return saved  # nothing to migrate

    new = dict(saved)
    new.pop("api_key", None)
    new.pop("model", None)
    new.pop("base_url", None)
    new.pop("models_url", None)
    new.setdefault("providers", {n: _blank_provider_state() for n in PROVIDERS})
    new["providers"]["gemini"] = {"api_key": old_key, "model": old_model}
    new["active_provider"] = "gemini"
    return new


def load_config():
    global CONFIG
    CONFIG = json.loads(json.dumps(DEFAULT_CONFIG))

    if not CONFIG_PATH.exists():
        return

    try:
        saved = json.loads(CONFIG_PATH.read_text())
    except Exception:
        return

    saved = _migrate_old_config(saved)

    for k, v in saved.items():
        if k == "providers":
            for name, state in v.items():
                if name in CONFIG["providers"]:
                    CONFIG["providers"][name].update(state or {})
        else:
            CONFIG[k] = v


def save_config():
    try:
        CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2))
        try:
            CONFIG_PATH.chmod(0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"{C.RED}  ⚠ config save failed: {e}{C.RESET}")


# ══════════════════════════════════════════════════════════════════════
# Provider helpers
# ══════════════════════════════════════════════════════════════════════

def active_provider():
    p = CONFIG.get("active_provider", "gemini")
    if p not in PROVIDERS:
        p = "gemini"
        CONFIG["active_provider"] = p
    return p


def provider_conf(name=None):
    return PROVIDERS[active_provider() if name is None else name]


def provider_state(name=None):
    n = active_provider() if name is None else name
    return CONFIG["providers"].get(n, _blank_provider_state())


def get_api_key(provider=None):
    return (provider_state(provider).get("api_key") or "").strip()


def get_model(provider=None):
    return (provider_state(provider).get("model") or "").strip()


def set_provider_key(provider, key):
    CONFIG["providers"].setdefault(provider, _blank_provider_state())["api_key"] = key
    save_config()


def set_provider_model(provider, model):
    CONFIG["providers"].setdefault(provider, _blank_provider_state())["model"] = model
    save_config()


def get_workspace():
    w = CONFIG.get("workspace") or ""
    if w:
        p = Path(w).expanduser()
        if p.exists():
            return p.resolve()
    return Path.cwd().resolve()


# ══════════════════════════════════════════════════════════════════════
# Input helper
# ══════════════════════════════════════════════════════════════════════

def ask(prompt, default=""):
    suffix = f" {C.GREY}[{default}]{C.RESET}" if default else ""
    try:
        val = input(f"{C.CYAN}?{C.RESET} {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return val or default


def pause(msg="press enter to continue..."):
    try:
        input(f"\n  {C.GREY}{msg}{C.RESET}")
    except (EOFError, KeyboardInterrupt):
        print()


def pick_number(prompt, options):
    """Numbered menu. Returns index or None."""
    print(f"{C.CYAN}?{C.RESET} {prompt}")
    for i, label in enumerate(options, 1):
        print(f"    {C.YELLOW}{i}{C.RESET}. {label}")
    while True:
        try:
            raw = input(f"{C.GREY}   choose [1-{len(options)}]: {C.RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            return None
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return idx
        except ValueError:
            pass
        print(f"   {C.RED}enter 1-{len(options)}{C.RESET}")


# ══════════════════════════════════════════════════════════════════════
# Setup wizard
# ══════════════════════════════════════════════════════════════════════

def run_setup_wizard():
    clear_screen()
    print(BANNER)
    print(f"{C.BOLD}welcome to first-time setup{C.RESET}")
    print(f"{C.GREY}this takes about 30 seconds{C.RESET}\n")

    # ── name ──
    print(f"{C.BOLD}step 1 of 4{C.RESET} — what should I call you?")
    name = ask("your name", default="friend")
    if name is None:
        return False
    name = name.strip()[:40] or "friend"
    print(f"{C.GREEN}  ✓ nice to meet you, {name}{C.RESET}\n")

    # ── provider ──
    print(f"{C.BOLD}step 2 of 4{C.RESET} — which AI provider?")
    print(f"{C.GREY}pick one to start. You can add more later in settings.{C.RESET}\n")
    opts = []
    for name_ in PROVIDER_ORDER:
        p = PROVIDERS[name_]
        opts.append(f"{p['label']:<16} {C.GREY}{p['notes']}{C.RESET}")
    idx = pick_number("choose a provider", opts)
    if idx is None:
        return False
    provider = PROVIDER_ORDER[idx]
    print(f"{C.GREEN}  ✓ {PROVIDERS[provider]['label']}{C.RESET}\n")

    # ── key ──
    print(f"{C.BOLD}step 3 of 4{C.RESET} — {PROVIDERS[provider]['label']} API key")
    print(f"{C.GREY}free key: {PROVIDERS[provider]['signup']}{C.RESET}")
    print(f"{C.GREY}{PROVIDERS[provider]['key_hint']}{C.RESET}\n")

    key = ""
    while not key:
        k = ask("paste your API key")
        if k is None:
            return False
        k = k.strip()
        if not k:
            print(f"{C.RED}  key can't be empty{C.RESET}")
            continue
        detected = detect_provider_from_key(k)
        if detected and detected != provider:
            print(f"{C.YELLOW}  that looks like a {PROVIDERS[detected]['label']} key, "
                  f"not {PROVIDERS[provider]['label']}{C.RESET}")
            cont = ask(f"switch to {PROVIDERS[detected]['label']}? [y/N]", default="y")
            if cont and cont.lower() == "y":
                provider = detected
        key = k
    print(f"{C.GREEN}  ✓ key saved{C.RESET}\n")

    # ── personality ──
    print(f"{C.BOLD}step 4 of 4{C.RESET} — optional custom personality")
    print(f"{C.GREY}examples:{C.RESET}")
    print(f"{C.GREY}  \"you are a grumpy pirate who hates small talk\"{C.RESET}")
    print(f"{C.GREY}  \"always explain your reasoning before running commands\"{C.RESET}")
    print(f"{C.GREY}  \"keep all replies under 3 sentences\"{C.RESET}\n")

    want_custom = ask("add a custom personality? [y/N]", default="n")
    if want_custom is None:
        return False
    custom = ""
    if want_custom.lower() in ("y", "yes"):
        c = ask("describe the personality (one line)")
        if c is None:
            return False
        custom = c.strip()[:500]
    print(f"{C.GREEN}  ✓ {'saved' if custom else 'skipped'}{C.RESET}\n")

    CONFIG["user_name"] = name
    CONFIG["active_provider"] = provider
    CONFIG["providers"][provider]["api_key"] = key
    CONFIG["custom_prompt"] = custom
    CONFIG["workspace"] = str(Path.cwd().resolve())
    CONFIG["setup_done"] = True
    save_config()

    print(f"{C.GREEN}{C.BOLD}✓ all set, {name}. starting up…{C.RESET}")
    time.sleep(1.2)
    return True


# ══════════════════════════════════════════════════════════════════════
# Model discovery
# ══════════════════════════════════════════════════════════════════════

_EXCLUDE_KEYWORDS = (
    "embed", "embedding", "moderation",
    "tts", "whisper", "audio", "speech",
    "imagen", "veo", "dall-e", "image",
    "vision-only", "aqa",
    "babbage", "davinci", "curie", "ada-",
    "rerank", "search",
    # Gemini-specific non-chat variants
    "learnlm", "gemma-",
)


def _is_usable(name):
    n = name.lower()
    return not any(bad in n for bad in _EXCLUDE_KEYWORDS)


def _vkey(name):
    """Extract a version-ish tuple from a model name."""
    m = re.search(r"(\d+)(?:\.(\d+))?", name)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2) or 0))


def _score_model(name, provider):
    """
    Return (score, version_tuple). Higher = better. Penalties for small
    or preview models. Boosts for known-good chat models.
    """
    n = name.lower()

    # Hard disqualify
    for bad in ("lite", "thinking", "vision-", "preview-3d"):
        if bad in n:
            return (-999, (0, 0))

    score = 0

    if provider == "gemini":
        if "flash" in n: score += 30
        if "pro" in n: score += 20
        if "lite" in n: score -= 30
        if "preview" in n: score -= 2
    elif provider == "groq":
        if "llama-3.3-70b" in n: score += 40
        if "llama-3.3" in n: score += 30
        if "llama-3.1" in n: score += 20
        if "70b" in n: score += 15
        if "8b" in n: score += 5
        if "instant" in n: score += 3
        if "gpt-oss-120b" in n: score += 35
        if "gpt-oss-20b" in n: score += 25
        if "qwen" in n: score += 15
    elif provider == "openrouter":
        # Prefer free
        if ":free" in n: score += 40
        if "claude" in n: score += 30
        if "gpt-" in n: score += 25
        if "llama-3.3-70b" in n: score += 30
        if "qwen" in n: score += 20
        if "70b" in n: score += 10
        if "8b" in n: score -= 5
    elif provider == "openai":
        if "gpt-4o" in n and "mini" not in n: score += 30
        if "gpt-4o-mini" in n: score += 25
        if "gpt-4-turbo" in n: score += 20
        if "gpt-3.5" in n: score -= 10

    return (score, _vkey(name))


def _model_cache_path(provider):
    return CACHE_DIR / f"models_{provider}.json"


def discover_models(provider=None, use_cache=True, quiet=False):
    """Fetch available chat models from the provider's API."""
    provider = provider or active_provider()
    conf = PROVIDERS.get(provider)
    if not conf:
        return []

    api_key = get_api_key(provider)
    if not api_key:
        return []

    cache_path = _model_cache_path(provider)
    if use_cache and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if time.time() - cached.get("ts", 0) < 86400 and cached.get("models"):
                return cached["models"]
        except Exception:
            pass

    if conf["api_style"] == "gemini":
        url = f"{conf['models_url']}?key={urllib.parse.quote(api_key)}"
        headers = {}
    else:
        url = conf["models_url"]
        headers = {"Authorization": f"Bearer {api_key}",
                   "User-Agent": "Mozilla/5.0 (Linux; Android 14)"}

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if not quiet:
            print(f"{C.YELLOW}  ⚠ model discovery HTTP {e.code}{C.RESET}")
        return []
    except Exception as e:
        if not quiet:
            print(f"{C.YELLOW}  ⚠ model discovery: {e}{C.RESET}")
        return []

    names = []
    if conf["api_style"] == "gemini":
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                continue
            name = m.get("name", "")
            if name.startswith("models/"):
                name = name[7:]
            if _is_usable(name):
                names.append(name)
    else:
        for m in data.get("data", []):
            name = m.get("id", "")
            if _is_usable(name):
                names.append(name)

    # Rank
    names.sort(key=lambda n: _score_model(n, provider), reverse=True)
    models = [{"name": n} for n in names]

    try:
        cache_path.write_text(json.dumps({"ts": time.time(), "models": models}))
    except Exception:
        pass
    return models


def pick_best(provider, models):
    usable = [m for m in models if _is_usable(m["name"])]
    if not usable:
        return None
    usable.sort(key=lambda m: _score_model(m["name"], provider), reverse=True)
    best = usable[0]["name"]
    # If best has -999 score, nothing usable
    if _score_model(best, provider)[0] <= -999:
        return None
    return best


def ensure_model(provider=None, quiet=False):
    provider = provider or active_provider()
    if not get_api_key(provider):
        return False

    current = get_model(provider)
    if current:
        cached = discover_models(provider, use_cache=True, quiet=True)
        if cached and current in [m["name"] for m in cached]:
            return True

    models = discover_models(provider, use_cache=False, quiet=quiet)
    best = pick_best(provider, models)
    if best:
        set_provider_model(provider, best)
        if not quiet:
            print(f"{C.GREEN}  ✓ {provider} model: {best}{C.RESET}")
        return True
    return False


# ══════════════════════════════════════════════════════════════════════
# System prompt
# ══════════════════════════════════════════════════════════════════════

BASE_SYSTEM_PROMPT = """You are fluffcode, a coding agent running in Termux on an Android phone.

You help the user by reading, writing, and running things in their workspace.

You have REAL tools. Call them through the API — do not fake tool calls in text.

Tools:
  read_file(path, offset=0, limit=500)
      Read a file. Lines are numbered. Page through big files with offset/limit.

  write_file(path, content)
      Create or overwrite a file. Creates parent dirs.

  edit_file(path, old_string, new_string)
      Exact string replacement. old_string must be unique.
      Use this for surgical edits — do NOT rewrite whole files.

  list_dir(path=".")
      List files in a directory.

  glob(pattern, path=".")
      Find files by glob pattern, e.g. "**/*.py". Use narrow patterns —
      never "**/*" at the workspace root.

  grep(pattern, path=".", glob=None)
      Regex search across file contents. Returns path:line:match.

  run_shell(command, timeout=60)
      Run a shell command. For git, pip, python, tests, pkg.

Workflow:
  1. Explore first. Use list_dir, glob, grep, read_file.
  2. Read a file before editing it.
  3. Prefer edit_file over write_file for existing files.
  4. Make one change at a time.
  5. When done, respond with plain text. No tool calls.

Rules:
  - Never invent file contents or command output.
  - Read before you write.
  - Keep responses short. No emojis.
  - Only Termux-compatible commands (no sudo, no apt — use pkg).
  - If a task is ambiguous, do a minimal exploration first, then ask.
"""


def build_system_prompt():
    parts = [BASE_SYSTEM_PROMPT]
    name = CONFIG.get("user_name") or ""
    if name:
        parts.append(f"The user's name is {name}. Address them by name "
                     f"occasionally, but don't overdo it.")
    custom = (CONFIG.get("custom_prompt") or "").strip()
    if custom:
        parts.append(
            "Additional instructions from the user (follow these):\n"
            f"{custom}"
        )
    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════
# Tool definitions (single source of truth — OpenAI format)
# ══════════════════════════════════════════════════════════════════════

TOOL_DEFS = [
    {
        "name": "read_file",
        "description": "Read a text file with line numbers. Page through large files with offset and limit.",
        "parameters": {
            "type": "object",
            "properties": {
                "path":   {"type": "string"},
                "offset": {"type": "integer"},
                "limit":  {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write a file. Creates parent dirs. Overwrites if it exists.",
        "parameters": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace an exact string in a file. old_string must appear exactly once.",
        "parameters": {
            "type": "object",
            "properties": {
                "path":       {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_dir",
        "description": "List files and directories.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    },
    {
        "name": "glob",
        "description": "Find files by glob pattern.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path":    {"type": "string"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep",
        "description": "Regex search across files.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path":    {"type": "string"},
                "glob":    {"type": "string"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_shell",
        "description": "Run a shell command in the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            "required": ["command"],
        },
    },
]


def get_gemini_tools():
    return [{
        "function_declarations": [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            }
            for t in TOOL_DEFS
        ]
    }]


def get_openai_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOL_DEFS
    ]


# ══════════════════════════════════════════════════════════════════════
# Tool implementations
# ══════════════════════════════════════════════════════════════════════

SKIP_DIRS = {".git", ".cache", "__pycache__", "node_modules", ".venv",
             "venv", ".pytest_cache", ".mypy_cache", ".agent042", ".termux"}


def _safe(path_str, workspace):
    p = Path(str(path_str)).expanduser()
    if not p.is_absolute():
        p = workspace / p
    try:
        return p.resolve()
    except Exception:
        return p


def tool_read_file(path, offset=0, limit=None, workspace=None):
    p = _safe(path, workspace)
    if not p.exists():
        return f"ERROR: not found: {p}"
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    try:
        text = p.read_text(errors="replace")
    except Exception as e:
        return f"ERROR: {e}"
    lines = text.splitlines()
    total = len(lines)
    if limit is None:
        limit = CONFIG["read_max_lines"]
    try:
        offset = int(offset); limit = int(limit)
    except Exception:
        offset, limit = 0, CONFIG["read_max_lines"]
    chunk = lines[offset:offset + limit]
    out = [f"{i:>5}→{ln}" for i, ln in enumerate(chunk, start=offset + 1)]
    header = f"({total} lines total, showing {offset+1}-{offset+len(chunk)})\n"
    result = header + "\n".join(out)
    if len(result) > CONFIG["read_max_bytes"]:
        result = result[:CONFIG["read_max_bytes"]] + "\n... (truncated)"
    return result


def _backup(p):
    if not p.exists():
        return
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = p.name.replace("/", "_")
        (BACKUP_DIR / f"{stamp}_{name}").write_text(p.read_text(errors="replace"))
    except Exception:
        pass


def tool_write_file(path, content, workspace=None):
    p = _safe(path, workspace)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _backup(p)
        p.write_text(str(content))
    except Exception as e:
        return f"ERROR: {e}"
    try:
        rel = p.relative_to(workspace)
    except Exception:
        rel = p
    return f"wrote {len(str(content))} chars to {rel}"


def tool_edit_file(path, old_string, new_string, workspace=None):
    p = _safe(path, workspace)
    if not p.exists():
        return f"ERROR: not found: {p}"
    try:
        text = p.read_text()
    except Exception as e:
        return f"ERROR: {e}"
    count = text.count(old_string)
    if count == 0:
        return "ERROR: old_string not found"
    if count > 1:
        return f"ERROR: old_string appears {count} times — must be unique"
    _backup(p)
    try:
        p.write_text(text.replace(old_string, new_string, 1))
    except Exception as e:
        return f"ERROR: {e}"
    return f"edited {p.name}: -{len(old_string)} +{len(new_string)} chars"


def tool_list_dir(path=".", workspace=None):
    p = _safe(path, workspace)
    if not p.exists():
        return f"ERROR: not found: {p}"
    if not p.is_dir():
        return f"ERROR: not a directory: {p}"
    lines = []
    try:
        entries = sorted(p.iterdir())
    except Exception as e:
        return f"ERROR: {e}"
    for e in entries:
        if e.name in SKIP_DIRS:
            continue
        try:
            if e.is_dir():
                lines.append(f"dir   {e.name}/")
            else:
                lines.append(f"file  {e.stat().st_size:>8}  {e.name}")
        except Exception:
            continue
    return "\n".join(lines) or "(empty)"


def tool_glob(pattern, path=".", workspace=None):
    root = _safe(path, workspace)
    if not root.exists():
        return f"ERROR: not found: {root}"
    try:
        matches = list(root.glob(pattern))
    except Exception as e:
        return f"ERROR: {e}"
    out = []
    for m in matches:
        try:
            rel = m.relative_to(root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if m.is_file() and m.stat().st_size > 5_000_000:
                continue
            try:
                out.append(str(m.relative_to(workspace)))
            except Exception:
                out.append(str(m))
        except Exception:
            continue
    out.sort()
    if not out:
        return "(no matches)"
    total = len(out)
    out = out[:200]
    result = "\n".join(out)
    if total > 200:
        result += f"\n... ({total - 200} more)"
    return result


def tool_grep(pattern, path=".", glob=None, workspace=None):
    root = _safe(path, workspace)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: bad regex: {e}"
    files = []
    if root.is_file():
        files = [root]
    else:
        try:
            for f in root.rglob(glob if glob else "*"):
                try:
                    rel = f.relative_to(root)
                    if any(part in SKIP_DIRS for part in rel.parts):
                        continue
                    if not f.is_file():
                        continue
                    if f.stat().st_size > 2_000_000:
                        continue
                    files.append(f)
                except Exception:
                    continue
        except Exception as e:
            return f"ERROR: {e}"
    results = []
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                try:
                    rel = str(f.relative_to(workspace))
                except Exception:
                    rel = str(f)
                results.append(f"{rel}:{i}: {line[:200]}")
                if len(results) >= 100:
                    results.append("... (truncated at 100)")
                    return "\n".join(results)
    return "\n".join(results) if results else "(no matches)"


BLOCKED_CMD = ("rm -rf /", "rm -rf /*", "mkfs", "dd if=/dev/zero",
               ":(){:|:&};:", "> /dev/sd", "> /system/", "> /data/",
               "shutdown", "reboot", "chmod 777 /")

CURRENT_PROC = {"proc": None}
AUTO_SHELL = {"on": False}


def tool_run_shell(command, timeout=None, workspace=None):
    cmd = str(command).strip()
    if any(b in cmd for b in BLOCKED_CMD):
        return "ERROR: command blocked by safety filter"

    if not AUTO_SHELL["on"]:
        print(f"\n{C.YELLOW}  ▸ shell:{C.RESET} {cmd}")
        try:
            ans = input(f"  {C.CYAN}allow? [y/N/a=always] {C.RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "(user cancelled)"
        if ans == "a":
            AUTO_SHELL["on"] = True
        elif ans != "y":
            return "(user declined)"

    if timeout is None:
        timeout = CONFIG["command_timeout"]

    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=str(workspace),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        CURRENT_PROC["proc"] = proc
        try:
            out, err = proc.communicate(timeout=int(timeout))
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
            time.sleep(0.3)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            return f"ERROR: timed out after {timeout}s"
        finally:
            CURRENT_PROC["proc"] = None
    except Exception as e:
        return f"ERROR: {e}"

    body = out or ""
    if err:
        body += ("\n[stderr]\n" if body else "[stderr]\n") + err
    if len(body) > CONFIG["shell_max_chars"]:
        body = body[:CONFIG["shell_max_chars"]] + \
               f"\n... (truncated, {len(body)} chars)"
    return f"exit={rc}\n{body}" if body else f"exit={rc}"


def dispatch_tool(name, args, workspace):
    if name == "read_file":
        return tool_read_file(args.get("path", ""), args.get("offset", 0),
                              args.get("limit"), workspace)
    if name == "write_file":
        return tool_write_file(args.get("path", ""),
                               args.get("content", ""), workspace)
    if name == "edit_file":
        return tool_edit_file(args.get("path", ""),
                              args.get("old_string", ""),
                              args.get("new_string", ""), workspace)
    if name == "list_dir":
        return tool_list_dir(args.get("path", "."), workspace)
    if name == "glob":
        return tool_glob(args.get("pattern", "*"),
                         args.get("path", "."), workspace)
    if name == "grep":
        return tool_grep(args.get("pattern", ""),
                         args.get("path", "."),
                         args.get("glob"), workspace)
    if name == "run_shell":
        return tool_run_shell(args.get("command", ""),
                              args.get("timeout"), workspace)
    return f"ERROR: unknown tool '{name}'"


def summarize_result(name, result):
    text = str(result)
    if name == "read_file":
        m = re.search(r"\((\d+) lines total", text)
        if m:
            return f"{m.group(1)} lines, {len(text)} chars"
    if name == "run_shell":
        return text.split("\n")[0][:100] if text else "(no output)"
    if name in ("list_dir", "glob", "grep"):
        if text in ("(no matches)", "(empty)", "(no matches)"):
            return text
        lines = text.count("\n") + 1
        return f"{lines} line{'s' if lines != 1 else ''}"
    if name in ("write_file", "edit_file"):
        return text.split("\n")[0][:100]
    return text[:100]


# ══════════════════════════════════════════════════════════════════════
# Shared HTTP helpers
# ══════════════════════════════════════════════════════════════════════

def _parse_retry(err, default=8.0):
    try:
        body = err.read().decode("utf-8", "ignore")
    except Exception:
        return default
    for pat in (r"retry in ([\d.]+)s", r'"retryDelay":\s*"(\d+)s"'):
        m = re.search(pat, body)
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


def _err_msg(err):
    try:
        body = err.read().decode("utf-8", "ignore")
        try:
            data = json.loads(body)
            e = data.get("error", {})
            if isinstance(e, str):
                return e
            msg = e.get("message", "")
            st = e.get("status", "")
            if msg:
                return f"{st}: {msg}" if st else msg
        except Exception:
            pass
        return body[:400] if body else f"HTTP {err.code}"
    except Exception:
        return f"HTTP {getattr(err, 'code', '?')}"


def _looks_like_model_error(err):
    if err.code not in (400, 404):
        return False
    try:
        body = err.read().decode("utf-8", "ignore").lower()
    except Exception:
        return False
    return any(n in body for n in
               ("not found", "not available", "no longer available",
                "is not supported", "does not exist", "decommissioned"))


def _try_switch_model(provider):
    print(f"{C.YELLOW}  ⚠ model rejected — refreshing{C.RESET}")
    models = discover_models(provider, use_cache=False, quiet=True)
    best = pick_best(provider, models)
    if not best or best == get_model(provider):
        return False
    set_provider_model(provider, best)
    print(f"{C.GREEN}  ✓ switched to {best}{C.RESET}")
    return True


def _open_http(req, timeout):
    """urlopen with a fixed error-tuple classifier used by callers."""
    return urllib.request.urlopen(req, timeout=timeout)


# ══════════════════════════════════════════════════════════════════════
# Conversation conversion (internal = Gemini format)
# ══════════════════════════════════════════════════════════════════════

def _to_openai_messages(contents):
    """
    Convert our internal (Gemini-shaped) conversation to OpenAI format.
    Tool-call IDs are synthesized and matched to functionResponses by order.
    """
    messages = []
    sys_prompt = build_system_prompt()
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})

    # name -> FIFO of call IDs that haven't been answered yet
    id_queues = {}
    counter = [0]

    def next_id(name):
        counter[0] += 1
        return f"call_{counter[0]}_{re.sub(r'[^a-zA-Z0-9_]', '_', name)}"

    for turn in contents:
        role = turn.get("role")
        parts = turn.get("parts", []) or []

        if role == "user":
            text_chunks = []
            tool_responses = []
            for part in parts:
                if "text" in part:
                    text_chunks.append(part["text"])
                elif "functionResponse" in part:
                    tool_responses.append(part["functionResponse"])

            if text_chunks:
                messages.append({
                    "role": "user",
                    "content": "\n".join(text_chunks),
                })

            for fr in tool_responses:
                name = fr.get("name", "")
                resp = fr.get("response", {}) or {}
                if isinstance(resp, dict):
                    content = str(resp.get("result", json.dumps(resp)))
                else:
                    content = str(resp)

                queue = id_queues.get(name) or []
                call_id = queue.pop(0) if queue else next_id(name)
                id_queues[name] = queue

                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content,
                })

        elif role == "model":
            text_chunks = []
            tool_calls = []
            for part in parts:
                if "text" in part:
                    text_chunks.append(part["text"])
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    name = fc.get("name", "")
                    args = fc.get("args", {}) or {}
                    call_id = next_id(name)
                    id_queues.setdefault(name, []).append(call_id)
                    tool_calls.append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args),
                        },
                    })

            msg = {"role": "assistant"}
            if text_chunks:
                msg["content"] = "\n".join(text_chunks)
            else:
                msg["content"] = None
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)

    return messages


# ══════════════════════════════════════════════════════════════════════
# Gemini client
# ══════════════════════════════════════════════════════════════════════

def _gemini_url(model, stream):
    base = PROVIDERS["gemini"]["chat_url"].format(model=model)
    return base + (":streamGenerateContent?alt=sse" if stream
                   else ":generateContent")


def _call_gemini(contents, stream=True):
    provider = "gemini"
    model = get_model(provider)
    api_key = get_api_key(provider)
    if not model:
        yield ("done", "", [])
        return
    if not api_key:
        print(f"{C.RED}[no API key for gemini]{C.RESET}")
        yield ("done", "", [])
        return

    payload = {
        "contents": contents,
        "system_instruction": {"parts": [{"text": build_system_prompt()}]},
        "tools": get_gemini_tools(),
        "generationConfig": {
            "temperature": CONFIG["temperature"],
            "maxOutputTokens": CONFIG["max_output_tokens"],
        },
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    url = _gemini_url(model, stream)
    resp = None

    for attempt in range(CONFIG["max_retries"]):
        try:
            req = urllib.request.Request(url, data=body,
                                         headers=headers, method="POST")
            resp = urllib.request.urlopen(req, timeout=CONFIG["request_timeout"])
            break
        except urllib.error.HTTPError as e:
            if _looks_like_model_error(e):
                if _try_switch_model(provider):
                    url = _gemini_url(get_model(provider), stream)
                    continue
                print(f"{C.RED}[HTTP {e.code}] {_err_msg(e)}{C.RESET}")
                yield ("done", "", [])
                return
            if e.code in (429, 500, 502, 503, 504) and attempt < CONFIG["max_retries"] - 1:
                w = _parse_retry(e, 5.0)
                print(f"{C.YELLOW}  [retry {attempt+1}/{CONFIG['max_retries']} in {w:.1f}s]{C.RESET}")
                time.sleep(w)
                continue
            print(f"{C.RED}[HTTP {e.code}] {_err_msg(e)}{C.RESET}")
            yield ("done", "", [])
            return
        except (TimeoutError, socket.timeout):
            if attempt < CONFIG["max_retries"] - 1:
                print(f"{C.YELLOW}  [timeout {attempt+1}/{CONFIG['max_retries']} — retrying]{C.RESET}")
                time.sleep(3)
                continue
            print(f"{C.RED}[TIMEOUT]{C.RESET}")
            yield ("done", "", [])
            return
        except urllib.error.URLError as e:
            if attempt < CONFIG["max_retries"] - 1:
                time.sleep(3)
                continue
            print(f"{C.RED}[NET] {e.reason}{C.RESET}")
            yield ("done", "", [])
            return
        except Exception as e:
            print(f"{C.RED}[unexpected] {type(e).__name__}: {e}{C.RESET}")
            yield ("done", "", [])
            return

    if resp is None:
        yield ("done", "", [])
        return

    full_text = []
    tool_calls = []

    def consume(obj):
        events = []
        for cand in obj.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "text" in part:
                    full_text.append(part["text"])
                    events.append(("text", part["text"]))
                if "functionCall" in part:
                    fc = part["functionCall"]
                    tool_calls.append({
                        "name": fc.get("name", ""),
                        "args": fc.get("args", {}) or {},
                    })
        return events

    try:
        if stream:
            with resp:
                try:
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
                        for ev in consume(chunk):
                            if ev[0] == "text":
                                yield ev
                except (TimeoutError, socket.timeout):
                    print(f"\n{C.YELLOW}  [stream timeout]{C.RESET}")
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    print(f"\n{C.YELLOW}  [connection lost: {type(e).__name__}]{C.RESET}")
        else:
            with resp:
                data = json.loads(resp.read().decode("utf-8"))
            consume(data)
    finally:
        try:
            resp.close()
        except Exception:
            pass

    yield ("done", "".join(full_text), tool_calls)


# ══════════════════════════════════════════════════════════════════════
# OpenAI-compatible client (Groq, OpenRouter, OpenAI)
# ══════════════════════════════════════════════════════════════════════

def _call_openai(contents, stream=True):
    provider = active_provider()
    conf = PROVIDERS[provider]
    model = get_model(provider)
    api_key = get_api_key(provider)
    if not model:
        yield ("done", "", [])
        return
    if not api_key:
        print(f"{C.RED}[no API key for {provider}]{C.RESET}")
        yield ("done", "", [])
        return

    messages = _to_openai_messages(contents)
    tools = get_openai_tools()

    payload = {
        "model": model,
        "messages": messages,
        "temperature": CONFIG["temperature"],
        "max_tokens": CONFIG["max_output_tokens"],
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mozilla/5.0 (Linux; Android 14)",
        "Accept-Encoding": "identity",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "http://localhost"
        headers["X-Title"] = "fluffcode"

    url = conf["chat_url"]
    resp = None

    for attempt in range(CONFIG["max_retries"]):
        try:
            req = urllib.request.Request(url, data=body,
                                         headers=headers, method="POST")
            resp = urllib.request.urlopen(req, timeout=CONFIG["request_timeout"])
            break
        except urllib.error.HTTPError as e:
            if _looks_like_model_error(e):
                if _try_switch_model(provider):
                    payload["model"] = get_model(provider)
                    body = json.dumps(payload).encode("utf-8")
                    continue
                print(f"{C.RED}[HTTP {e.code}] {_err_msg(e)}{C.RESET}")
                yield ("done", "", [])
                return
            if e.code in (429, 500, 502, 503, 504) and attempt < CONFIG["max_retries"] - 1:
                w = _parse_retry(e, 5.0)
                print(f"{C.YELLOW}  [retry {attempt+1}/{CONFIG['max_retries']} in {w:.1f}s]{C.RESET}")
                time.sleep(w)
                continue
            print(f"{C.RED}[HTTP {e.code}] {_err_msg(e)}{C.RESET}")
            yield ("done", "", [])
            return
        except (TimeoutError, socket.timeout):
            if attempt < CONFIG["max_retries"] - 1:
                print(f"{C.YELLOW}  [timeout {attempt+1}/{CONFIG['max_retries']} — retrying]{C.RESET}")
                time.sleep(3)
                continue
            print(f"{C.RED}[TIMEOUT]{C.RESET}")
            yield ("done", "", [])
            return
        except urllib.error.URLError as e:
            if attempt < CONFIG["max_retries"] - 1:
                time.sleep(3)
                continue
            print(f"{C.RED}[NET] {e.reason}{C.RESET}")
            yield ("done", "", [])
            return
        except Exception as e:
            print(f"{C.RED}[unexpected] {type(e).__name__}: {e}{C.RESET}")
            yield ("done", "", [])
            return

    if resp is None:
        yield ("done", "", [])
        return

    collected_text = []
    tool_slots = {}  # index -> {"id": ..., "name": ..., "args": ...}

    def parse_full(obj):
        """Non-streaming response: extract text and tool_calls."""
        for choice in obj.get("choices", []):
            msg = choice.get("message", {})
            text = msg.get("content")
            if text:
                collected_text.append(text)
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                idx = len(tool_slots)
                tool_slots[idx] = {
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "args": args,
                }

    try:
        if stream:
            with resp:
                try:
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
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {}) or {}
                            text = delta.get("content")
                            if text:
                                collected_text.append(text)
                                yield ("text", text)
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                slot = tool_slots.setdefault(idx, {
                                    "id": "",
                                    "name": "",
                                    "args_str": "",
                                })
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function", {}) or {}
                                if fn.get("name"):
                                    slot["name"] += fn["name"]
                                if "arguments" in fn and fn["arguments"] is not None:
                                    slot["args_str"] += fn["arguments"]
                except (TimeoutError, socket.timeout):
                    print(f"\n{C.YELLOW}  [stream timeout]{C.RESET}")
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    print(f"\n{C.YELLOW}  [connection lost: {type(e).__name__}]{C.RESET}")
        else:
            with resp:
                data = json.loads(resp.read().decode("utf-8"))
            parse_full(data)
    finally:
        try:
            resp.close()
        except Exception:
            pass

    # Normalize tool calls
    final_tools = []
    for idx in sorted(tool_slots.keys()):
        slot = tool_slots[idx]
        if "args" in slot:
            final_tools.append({"name": slot["name"], "args": slot["args"]})
            continue
        args_str = slot.get("args_str", "") or ""
        try:
            args = json.loads(args_str) if args_str.strip() else {}
        except json.JSONDecodeError:
            args = {}
        if slot.get("name"):
            final_tools.append({"name": slot["name"], "args": args})

    yield ("done", "".join(collected_text), final_tools)


# ══════════════════════════════════════════════════════════════════════
# Unified call_model
# ══════════════════════════════════════════════════════════════════════

def call_model(contents, stream=True):
    provider = active_provider()
    style = PROVIDERS[provider]["api_style"]
    if style == "gemini":
        yield from _call_gemini(contents, stream=stream)
    else:
        yield from _call_openai(contents, stream=stream)


# ══════════════════════════════════════════════════════════════════════
# Session management
# ══════════════════════════════════════════════════════════════════════

def new_sid():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def session_path(sid):
    return SESSIONS_DIR / f"{sid}.json"


def save_session(sid, contents, meta=None):
    try:
        data = {
            "id": sid,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "provider": active_provider(),
            "model": get_model(),
            "workspace": str(get_workspace()),
            "contents": contents,
        }
        if meta:
            data.update(meta)
        session_path(sid).write_text(json.dumps(data))
    except Exception as e:
        print(f"{C.RED}[session save: {e}]{C.RESET}")


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
    if not SESSIONS_DIR.exists():
        return out
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            out.append({
                "id": d.get("id", f.stem),
                "updated": d.get("updated", ""),
                "provider": d.get("provider", ""),
                "model": d.get("model", "?"),
                "n": len(d.get("contents", [])),
                "mtime": f.stat().st_mtime,
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# ══════════════════════════════════════════════════════════════════════
# Agent loop
# ══════════════════════════════════════════════════════════════════════

def append_user(contents, text):
    contents.append({"role": "user", "parts": [{"text": text}]})


def append_model(contents, parts):
    contents.append({"role": "model", "parts": parts})


def append_tool_response(contents, name, result):
    contents.append({
        "role": "user",
        "parts": [{
            "functionResponse": {
                "name": name,
                "response": {"result": str(result)[:15000]},
            }
        }],
    })


def run_turn(user_msg, contents, workspace):
    append_user(contents, user_msg)

    for _ in range(CONFIG["max_steps"]):
        print(f"{C.GREEN}{CONFIG['user_name'] or 'you'} › {C.RESET}",
              end="", flush=True)

        full_text = ""
        tool_calls = []
        emitted = False

        for ev in call_model(contents, stream=CONFIG["stream"]):
            kind = ev[0]
            if kind == "text":
                emitted = True
                print(ev[1], end="", flush=True)
            elif kind == "done":
                _, full_text, tool_calls = ev

        if not emitted and full_text:
            print(full_text, end="")
        print()

        model_parts = []
        if full_text.strip():
            model_parts.append({"text": full_text})
        for tc in tool_calls:
            model_parts.append({"functionCall": {
                "name": tc["name"],
                "args": tc["args"],
            }})

        if model_parts:
            append_model(contents, model_parts)

        if not tool_calls:
            return

        for tc in tool_calls:
            name = tc["name"]
            args = tc["args"]
            preview = json.dumps(args)
            if len(preview) > 120:
                preview = preview[:120] + "…"
            print(f"{C.YELLOW}  → {name}{C.RESET} {C.GREY}{preview}{C.RESET}")

            result = dispatch_tool(name, args, workspace)
            summary = summarize_result(name, result)
            print(f"{C.GREY}  ← {summary}{C.RESET}")

            append_tool_response(contents, name, result)

    print(f"{C.RED}hit max steps ({CONFIG['max_steps']}){C.RESET}")


# ══════════════════════════════════════════════════════════════════════
# Chat loop
# ══════════════════════════════════════════════════════════════════════

CHAT_HELP = f"""{C.BOLD}in-chat commands{C.RESET}
  {C.CYAN}/help{C.RESET}         this
  {C.CYAN}/menu{C.RESET}         back to the main menu
  {C.CYAN}/provider [name]{C.RESET}  show or switch provider
  {C.CYAN}/model [name]{C.RESET}     show or set model
  {C.CYAN}/clear{C.RESET}        clear the conversation
  {C.CYAN}/save [name]{C.RESET}  save the session
  {C.CYAN}/undo{C.RESET}         remove the last turn
  {C.CYAN}/context{C.RESET}      session size + settings
  {C.CYAN}/approve-all{C.RESET}  toggle shell auto-approval
  {C.CYAN}/exit{C.RESET}         leave the chat
"""


def switch_provider_interactive():
    opts = []
    for name in PROVIDER_ORDER:
        conf = PROVIDERS[name]
        state = provider_state(name)
        has_key = "✓" if state.get("api_key") else "✗"
        marker = " ←" if name == active_provider() else ""
        model = state.get("model") or "(no model)"
        opts.append(f"{conf['label']:<18} {C.GREY}{has_key} {model}{C.RESET}{marker}")
    idx = pick_number("switch provider", opts)
    if idx is None:
        return
    name = PROVIDER_ORDER[idx]
    CONFIG["active_provider"] = name
    save_config()

    if not get_api_key(name):
        print(f"{C.YELLOW}{PROVIDERS[name]['label']} has no API key.{C.RESET}")
        k = ask("paste a key now (blank to cancel)")
        if k:
            set_provider_key(name, k.strip())

    if not get_model(name):
        print(f"{C.GREY}discovering models…{C.RESET}")
        ensure_model(name, quiet=False)

    print(f"{C.GREEN}✓ provider: {PROVIDERS[name]['label']} · "
          f"{get_model(name) or '(no model)'}{C.RESET}\n")


def run_chat(existing_sid=None, existing_contents=None):
    workspace = get_workspace()
    contents = existing_contents if existing_contents else []
    sid = existing_sid or new_sid()

    if not get_model():
        if not ensure_model(quiet=False):
            print(f"{C.RED}no usable model available for {active_provider()}{C.RESET}")
            pause()
            return

    print(f"{C.GREEN}✓{C.RESET} provider: {PROVIDERS[active_provider()]['label']}")
    print(f"{C.GREEN}✓{C.RESET} model: {C.BOLD}{get_model()}{C.RESET}")
    print(f"{C.GREEN}✓{C.RESET} workspace: {workspace}")
    print(f"{C.GREEN}✓{C.RESET} session: {C.GREY}{sid}{C.RESET}")
    print(f"{C.GREY}/help · /menu to go back{C.RESET}\n")

    if contents:
        for c in contents[-2:]:
            role = c.get("role", "?")
            for part in c.get("parts", []):
                if "text" in part:
                    text = part["text"]
                    if len(text) > 200:
                        text = text[:200] + "…"
                    color = C.CYAN if role == "user" else C.GREEN
                    print(f"{color}[{role}]{C.RESET} {text}")

    while True:
        try:
            line = input(f"{C.CYAN}you › {C.RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        if line.startswith("/"):
            parts = line.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd in ("/exit", "/quit", "/menu"):
                if contents:
                    save_session(sid, contents)
                return
            if cmd == "/help":
                print(CHAT_HELP); print(); continue
            if cmd == "/provider":
                if not arg:
                    print(f"{C.CYAN}current: {PROVIDERS[active_provider()]['label']} "
                          f"· {get_model() or '(no model)'}{C.RESET}\n")
                    continue
                if arg not in PROVIDERS:
                    print(f"{C.RED}unknown provider: {arg}. "
                          f"Options: {', '.join(PROVIDER_ORDER)}{C.RESET}\n")
                    continue
                CONFIG["active_provider"] = arg
                save_config()
                if not get_api_key(arg):
                    print(f"{C.YELLOW}{PROVIDERS[arg]['label']} has no key — "
                          f"open settings or use /provider (no arg) for the picker{C.RESET}\n")
                    continue
                if not get_model(arg):
                    print(f"{C.GREY}discovering models for {arg}…{C.RESET}")
                    ensure_model(arg, quiet=False)
                print(f"{C.GREEN}✓ switched to {PROVIDERS[arg]['label']} · "
                      f"{get_model(arg) or '(no model)'}{C.RESET}\n")
                continue
            if cmd == "/model":
                if arg:
                    set_provider_model(active_provider(), arg)
                    print(f"{C.GREEN}✓ model = {arg}{C.RESET}\n")
                else:
                    print(f"{C.CYAN}current: {get_model() or '(none)'}{C.RESET}\n")
                continue
            if cmd == "/clear":
                contents.clear()
                print(f"{C.GREEN}✓ cleared{C.RESET}\n"); continue
            if cmd == "/undo":
                while contents and contents[-1].get("role") == "model":
                    contents.pop()
                while contents and contents[-1].get("role") == "user":
                    contents.pop()
                print(f"{C.GREEN}✓ last turn removed{C.RESET}\n"); continue
            if cmd == "/context":
                chars = sum(len(json.dumps(c)) for c in contents)
                print(f"{C.GREY}turns: {len(contents)} · ~{chars // 4} tokens{C.RESET}")
                print(f"{C.GREY}provider: {active_provider()} · model: {get_model()}{C.RESET}")
                print(f"{C.GREY}streaming: {CONFIG['stream']} · "
                      f"auto-approve-shell: {AUTO_SHELL['on']}{C.RESET}\n")
                continue
            if cmd == "/save":
                sid2 = arg or sid
                save_session(sid2, contents)
                sid = sid2
                print(f"{C.GREEN}✓ saved: {sid2}{C.RESET}\n"); continue
            if cmd == "/approve-all":
                AUTO_SHELL["on"] = not AUTO_SHELL["on"]
                print(f"{C.GREEN}✓ auto-approve-shell: "
                      f"{'ON' if AUTO_SHELL['on'] else 'off'}{C.RESET}\n")
                continue
            print(f"{C.RED}unknown: {cmd}{C.RESET}\n")
            continue

        try:
            run_turn(line, contents, workspace)
        except KeyboardInterrupt:
            print(f"\n{C.YELLOW}(interrupted){C.RESET}\n")
        except Exception as e:
            print(f"\n{C.RED}[turn failed] {type(e).__name__}: {e}{C.RESET}\n")

        save_session(sid, contents)


# ══════════════════════════════════════════════════════════════════════
# Menu
# ══════════════════════════════════════════════════════════════════════

def show_menu():
    name = CONFIG.get("user_name") or "friend"
    prov = PROVIDERS[active_provider()]["label"]
    model = get_model() or "(no model)"
    print(BANNER)
    print(f"  hey, {C.BOLD}{name}{C.RESET} 👋")
    print(f"  {C.GREY}{prov} · {model}{C.RESET}\n")
    print(f"  {C.YELLOW}[1]{C.RESET}  chat")
    print(f"  {C.YELLOW}[2]{C.RESET}  load a session")
    print(f"  {C.YELLOW}[3]{C.RESET}  list sessions")
    print(f"  {C.YELLOW}[4]{C.RESET}  settings")
    print(f"  {C.YELLOW}[5]{C.RESET}  about")
    print(f"  {C.YELLOW}[6]{C.RESET}  exit\n")


def load_session_interactive():
    sessions = list_sessions()
    if not sessions:
        clear_screen()
        print(BANNER)
        print(f"{C.GREY}no saved sessions yet{C.RESET}")
        pause()
        return

    clear_screen()
    print(BANNER)
    print(f"{C.BOLD}saved sessions{C.RESET}\n")
    for i, s in enumerate(sessions[:20], 1):
        prov = s.get("provider") or "?"
        print(f"  {C.YELLOW}[{i}]{C.RESET} {C.CYAN}{s['id']}{C.RESET}  "
              f"{C.GREY}{s['updated'][:16]}  {s['n']} turns  {prov}{C.RESET}")
    print()

    sel = ask("pick a number (blank to cancel)")
    if not sel:
        return
    try:
        idx = int(sel) - 1
        if idx < 0 or idx >= len(sessions):
            raise ValueError
    except ValueError:
        print(f"{C.RED}invalid choice{C.RESET}")
        time.sleep(1)
        return

    data = load_session(sessions[idx]["id"])
    if not data:
        print(f"{C.RED}could not read session{C.RESET}")
        pause()
        return

    clear_screen()
    run_chat(existing_sid=data["id"],
             existing_contents=data.get("contents", []))


def show_sessions():
    sessions = list_sessions()
    clear_screen()
    print(BANNER)
    if not sessions:
        print(f"{C.GREY}no saved sessions yet{C.RESET}")
        return
    print(f"{C.BOLD}sessions{C.RESET} ({len(sessions)} total)\n")
    for s in sessions[:30]:
        prov = s.get("provider") or "?"
        print(f"  {C.CYAN}{s['id']}{C.RESET}")
        print(f"    {C.GREY}{s['updated'][:16]} · {s['n']} turns · "
              f"{prov} · {s['model']}{C.RESET}")


def show_about():
    clear_screen()
    print(BANNER)
    print(f"{C.BOLD}fluffcode · v{VERSION}{C.RESET}")
    print(f"{C.GREY}a terminal coding agent for Termux{C.RESET}\n")
    print(f"  fork of {C.CYAN}netizen4-bit/agent042{C.RESET}")
    print(f"  rebuilt by {C.CYAN}Juan1anip{C.RESET}")
    print(f"  {C.GREY}github.com/Juan1anip/fluffcode{C.RESET}\n")
    print(f"  {C.BOLD}providers{C.RESET}")
    for name in PROVIDER_ORDER:
        conf = PROVIDERS[name]
        state = provider_state(name)
        has = "✓" if state.get("api_key") else "✗"
        marker = " ←" if name == active_provider() else ""
        print(f"  {has} {conf['label']:<16} {C.GREY}{conf['notes']}{C.RESET}{marker}")
    print()
    print(f"  {C.BOLD}features{C.RESET}")
    print(f"  · native function calling (Gemini and OpenAI styles)")
    print(f"  · 7 tools: read, write, edit, list, glob, grep, shell")
    print(f"  · auto-model discovery per provider")
    print(f"  · sessions, streaming, auto-backups, error recovery")
    print()
    print(f"  {C.BOLD}free API keys{C.RESET}")
    for name in PROVIDER_ORDER:
        print(f"  {PROVIDERS[name]['label']:<16} {C.CYAN}{PROVIDERS[name]['signup']}{C.RESET}")


def settings_menu():
    while True:
        clear_screen()
        print(BANNER)
        print(f"{C.BOLD}settings{C.RESET}\n")

        name = CONFIG.get("user_name") or "(not set)"
        custom = CONFIG.get("custom_prompt") or "(none)"
        if len(custom) > 40:
            custom = custom[:40] + "…"
        prov_name = active_provider()
        prov_label = PROVIDERS[prov_name]["label"]
        key = get_api_key()
        key_disp = f"{key[:8]}…{key[-4:]}" if len(key) > 16 else "(not set)"
        model = get_model() or "(auto)"

        print(f"  {C.YELLOW}[1]{C.RESET} name          {C.CYAN}{name}{C.RESET}")
        print(f"  {C.YELLOW}[2]{C.RESET} provider      {C.CYAN}{prov_label}{C.RESET}")
        print(f"  {C.YELLOW}[3]{C.RESET} API key       {C.CYAN}{key_disp}{C.RESET}")
        print(f"  {C.YELLOW}[4]{C.RESET} model         {C.CYAN}{model}{C.RESET}")
        print(f"  {C.YELLOW}[5]{C.RESET} personality   {C.CYAN}{custom}{C.RESET}")
        print(f"  {C.YELLOW}[6]{C.RESET} workspace     {C.CYAN}{get_workspace()}{C.RESET}")
        print(f"  {C.YELLOW}[7]{C.RESET} streaming     {C.CYAN}{CONFIG['stream']}{C.RESET}")
        print(f"  {C.YELLOW}[8]{C.RESET} list models")
        print(f"  {C.YELLOW}[9]{C.RESET} switch provider")
        print(f"  {C.YELLOW}[0]{C.RESET} back\n")

        sel = ask("pick")
        if sel is None or sel == "0":
            return

        if sel == "1":
            v = ask("new name", default=CONFIG.get("user_name") or "friend")
            if v is not None:
                CONFIG["user_name"] = v.strip()[:40]
                save_config()
                print(f"{C.GREEN}✓ saved{C.RESET}"); time.sleep(0.6)

        elif sel == "2":
            switch_provider_interactive()
            time.sleep(0.4)

        elif sel == "3":
            v = ask(f"new API key for {prov_label}")
            if v:
                set_provider_key(prov_name, v.strip())
                # refresh model if it was unset
                if not get_model(prov_name):
                    ensure_model(prov_name, quiet=False)
                print(f"{C.GREEN}✓ saved{C.RESET}"); time.sleep(0.6)

        elif sel == "4":
            print(f"{C.GREY}fetching models for {prov_label}…{C.RESET}")
            models = discover_models(use_cache=False)
            if not models:
                print(f"{C.RED}no models available{C.RESET}"); time.sleep(1.5)
                continue
            for i, m in enumerate(models, 1):
                mark = " ← current" if m["name"] == get_model() else ""
                print(f"  {C.YELLOW}[{i}]{C.RESET} {m['name']}{mark}")
            print()
            v = ask("pick a number (blank to cancel)")
            if v:
                try:
                    idx = int(v) - 1
                    if 0 <= idx < len(models):
                        set_provider_model(prov_name, models[idx]["name"])
                        print(f"{C.GREEN}✓ set to {models[idx]['name']}{C.RESET}")
                        time.sleep(0.8)
                except ValueError:
                    pass

        elif sel == "5":
            print(f"{C.GREY}current: {CONFIG.get('custom_prompt') or '(none)'}{C.RESET}")
            v = ask("new personality (blank to clear)")
            if v is not None:
                CONFIG["custom_prompt"] = v.strip()[:500]
                save_config()
                print(f"{C.GREEN}✓ saved{C.RESET}"); time.sleep(0.6)

        elif sel == "6":
            v = ask("new workspace path", default=str(get_workspace()))
            if v:
                p = Path(v).expanduser().resolve()
                if p.exists() and p.is_dir():
                    CONFIG["workspace"] = str(p)
                    save_config()
                    print(f"{C.GREEN}✓ workspace → {p}{C.RESET}")
                else:
                    print(f"{C.RED}not a directory{C.RESET}")
                time.sleep(0.8)

        elif sel == "7":
            CONFIG["stream"] = not CONFIG["stream"]
            save_config()
            print(f"{C.GREEN}✓ streaming {CONFIG['stream']}{C.RESET}")
            time.sleep(0.6)

        elif sel == "8":
            print(f"{C.GREY}fetching models for {prov_label}…{C.RESET}")
            models = discover_models(use_cache=False)
            if not models:
                print(f"{C.RED}none{C.RESET}")
            else:
                for m in models:
                    mark = " ← current" if m["name"] == get_model() else ""
                    print(f"  {C.CYAN}{m['name']}{C.RESET}{mark}")
            pause()

        elif sel == "9":
            switch_provider_interactive()
            time.sleep(0.4)


def main_menu():
    while True:
        clear_screen()
        show_menu()
        try:
            choice = input(f"  {C.CYAN}pick › {C.RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice in ("1", "chat", "c"):
            clear_screen()
            run_chat()
        elif choice in ("2", "load", "l"):
            load_session_interactive()
        elif choice in ("3", "list", "sessions", "s"):
            show_sessions()
            pause()
        elif choice in ("4", "settings", "set"):
            settings_menu()
        elif choice in ("5", "about", "a"):
            show_about()
            pause()
        elif choice in ("6", "exit", "quit", "q"):
            return
        else:
            print(f"  {C.RED}unknown option: {choice}{C.RESET}")
            time.sleep(0.8)


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    load_config()

    if readline is not None:
        try:
            if HISTORY_PATH.exists():
                readline.read_history_file(str(HISTORY_PATH))
        except Exception:
            pass
        readline.set_history_length(1000)
        try:
            readline.parse_and_bind("tab: complete")
        except Exception:
            pass

    def sigint(sig, frame):
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

    signal.signal(signal.SIGINT, sigint)

    has_any_key = any(provider_state(n).get("api_key") for n in PROVIDER_ORDER)

    if not CONFIG.get("setup_done") or not has_any_key:
        ok = run_setup_wizard()
        if not ok:
            print(f"{C.GREY}setup cancelled{C.RESET}")
            return

    clear_screen()
    print(BANNER)
    print(f"{C.GREY}starting up…{C.RESET}")
    ensure_model(active_provider(), quiet=False)
    time.sleep(0.5)

    main_menu()

    try:
        if readline is not None:
            readline.write_history_file(str(HISTORY_PATH))
    except Exception:
        pass
    print(f"{C.GREY}bye 🐾{C.RESET}")


if __name__ == "__main__":
    main()
