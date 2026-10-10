#!/data/data/com.termux/files/usr/bin/bash
# fluffcode installer for Termux on Android.
#
# Usage (one line, from a fresh Termux):
#   curl -fsSL https://raw.githubusercontent.com/Juan1anip/fluffcode/main/install.sh | bash
#
# This script:
#   1. Verifies it's running in Termux
#   2. Installs python and git if missing
#   3. Clones or updates fluffcode to ~/fluffcode
#   4. Launches the agent

set -e

REPO_HTTPS="https://github.com/Juan1anip/fluffcode.git"
DEST="$HOME/fluffcode"

# ─── colors ─────────────────────────────────────────────────────────
if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
    CYAN=$'\033[36m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'
else
    BOLD=""; DIM=""; RESET=""; CYAN=""; GREEN=""; YELLOW=""; RED=""
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s\n' "${GREEN}  ✓${RESET} $*"; }
warn() { printf '%s\n' "${YELLOW}  ⚠${RESET} $*"; }
err()  { printf '%s\n' "${RED}  ✗${RESET} $*"; }

# ─── banner ─────────────────────────────────────────────────────────
cat <<'BANNER'

   ┌─────────────────────────────────────────┐
   │   fluffcode · installer                 │
   └─────────────────────────────────────────┘

BANNER

# ─── 1. verify Termux ───────────────────────────────────────────────
if [ ! -d "/data/data/com.termux" ]; then
    err "this installer is for Termux on Android"
    say ""
    say "   install Termux from F-Droid first:"
    say "   ${CYAN}https://f-droid.org/packages/com.termux/${RESET}"
    say ""
    exit 1
fi
ok "running in Termux"

# ─── 2. install python and git ──────────────────────────────────────
NEED_PKGS=()
command -v python3 >/dev/null 2>&1 || NEED_PKGS+=(python)
command -v git     >/dev/null 2>&1 || NEED_PKGS+=(git)

if [ ${#NEED_PKGS[@]} -gt 0 ]; then
    say "${DIM}  › installing: ${NEED_PKGS[*]}${RESET}"
    pkg update -y >/dev/null 2>&1 || true
    if ! pkg install -y "${NEED_PKGS[@]}" >/dev/null 2>&1; then
        err "pkg install failed"
        say ""
        say "   try running manually:"
        say "     ${CYAN}pkg update${RESET}"
        say "     ${CYAN}pkg install ${NEED_PKGS[*]}${RESET}"
        say ""
        exit 1
    fi
    ok "installed: ${NEED_PKGS[*]}"
else
    ok "python3 and git already present"
fi

# verify python actually runs
if ! python3 --version >/dev/null 2>&1; then
    err "python3 installed but won't run"
    exit 1
fi

# ─── 3. clone or update ─────────────────────────────────────────────
if [ -d "$DEST/.git" ]; then
    say "${DIM}  › updating $DEST${RESET}"
    if git -C "$DEST" pull --ff-only --quiet 2>/dev/null; then
        ok "updated existing install"
    else
        warn "git pull failed — using existing copy"
    fi
elif [ -e "$DEST" ]; then
    err "$DEST exists but isn't a git repo"
    say ""
    say "   move or delete it, then try again:"
    say "     ${CYAN}mv $DEST ${DEST}.old${RESET}"
    say ""
    exit 1
else
    say "${DIM}  › cloning to $DEST${RESET}"
    if ! git clone --quiet "$REPO_HTTPS" "$DEST"; then
        err "clone failed — check your network"
        exit 1
    fi
    ok "cloned"
fi

# ─── 4. first-time hint ─────────────────────────────────────────────
if [ ! -f "$HOME/.agent042/config.json" ]; then
    say ""
    say "${BOLD}  first run will ask for:${RESET}"
    say "  · your name"
    say "  · which provider (Gemini / Groq / OpenRouter / OpenAI)"
    say "  · an API key"
    say "  · an optional custom personality"
    say ""
    say "  free key from Groq: ${CYAN}https://console.groq.com/keys${RESET}"
    say "  free key from Gemini: ${CYAN}https://aistudio.google.com/apikey${RESET}"
    say ""
fi

# ─── 5. launch ──────────────────────────────────────────────────────
say "  ${GREEN}launching fluffcode…${RESET}"
say ""
cd "$DEST"
exec python3 agent.py
