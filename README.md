# fluffcode

A terminal coding agent for Termux on Android. Pure Python stdlib.

Fork of [netizen4-bit/agent042](https://github.com/netizen4-bit/agent042), rebuilt by [@Juan1anip](https://github.com/Juan1anip).

## What it is

`fluffcode` is a coding agent that runs in your Termux shell. You describe what you want in plain English. It reads files, writes code, runs commands, and iterates — using native function calling through Gemini, Groq, OpenRouter, or OpenAI.

## What's different from upstream

The original project passed an empty list where the conversation history was supposed to go. Every reply was generated with zero context — the agent had amnesia. That, plus a hardcoded model name that Google retired, made it barely usable.

This fork fixes that and rebuilds the agent around modern LLM APIs.

### Fixes vs upstream

- **Conversation memory** — history is passed correctly, assistant replies are appended back
- **Working model names** — discovered live from each provider's API
- **Rate limit handling** — retries 429/5xx with exponential backoff
- **Native function calling** — replaced `<cmd>` tag parsing with real tool calls
- **Safety filter** — refuses `rm -rf /`, `mkfs`, fork bombs, and similar
- **Auto-approve safe reads** — `ls`, `cat`, `df`, etc. run without prompting
- **Subprocess timeouts** — hung commands die after 60s
- **Ctrl-C** kills the running command, not the agent

### New features

- **Multi-provider** — Gemini, Groq, OpenRouter, OpenAI. Switch with `/provider` mid-chat.
- **7 tools** — read_file, write_file, edit_file, list_dir, glob, grep, run_shell
- **Auto-model discovery** — fetches each provider's live model list, picks the best, handles retirements automatically
- **Sessions** — history persists across restarts. `/save`, `/load`, `/list`.
- **Startup menu** — chat / load session / list / settings / about / exit
- **Setup wizard** — first launch asks for your name, provider, key, and optional personality
- **Streaming** — see tokens as they arrive
- **Automatic backups** — every write and edit snapshots to `~/.agent042/backups/`
- **Colored output** — model text, tool calls, results visually distinct

## Install

```bash
pkg update -y && pkg install git python -y
git clone https://github.com/Juan1anip/fluffcode.git
cd fluffcode
python3 agent.py
