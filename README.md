# agent042-fork

A fork of [netizen4-bit/agent042](https://github.com/netizen4-bit/agent042) — a Termux autonomous agent for Android.

**Forked and rebuilt by [@Juan1anip](https://github.com/Juan1anip).**

## What's different from upstream

The original had a real bug: it passed an empty list where the conversation history was supposed to go. Every reply was generated with zero context — the agent had amnesia between messages. This fork fixes that and adds the things that make it actually usable.

### Fixes
- **Conversation memory** — history is passed to Gemini correctly and assistant replies are appended back
- **Working model name** — auto-discovered at runtime instead of hardcoded
- **Rate limit handling** — retries 429/5xx with backoff instead of exiting
- **Multiple commands per turn** — all `<cmd>` blocks run, not just the first
- **Subprocess timeout** — hung commands die after 60s
- **Ctrl-C** kills the running command, not the agent
- **Safety filter** — refuses `rm -rf /`, `mkfs`, fork bombs
- **Auto-approve safe reads** — `ls`, `cat`, `df`, etc. run without prompting

### New features
- **Auto-model discovery** — fetches Google's live model list on startup and picks the newest Flash
- **404 auto-recovery** — if the model gets retired mid-session, catches the error, switches, and retries
- **Sessions** — history persists across restarts (`/save`, `/load`, `/list`)
- **Streaming** — see tokens as they arrive
- **Token usage** — printed after each reply
- **Slash commands** — `/help`, `/models`, `/refresh-models`, `/context`, `/undo`, `/model`, `/stream`, `/approve-all`, `/exit`
- **Colored output** — model text, commands, and results are visually distinct

## Install

```bash
pkg update -y && pkg install git python -y
git clone https://github.com/Juan1anip/agent042.git agent042-fork
cd agent042-fork
export GEMINI_API_KEY="your_key_here"
python3 agent.py
