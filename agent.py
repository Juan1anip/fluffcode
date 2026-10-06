#!/usr/bin/env python3
"""Termux autonomous execution agent for legacy 32‑bit Android (armv7l).

Requirements (strictly stdlib):
- urllib.request, json, os, subprocess, sys, re, time
- No external dependencies (no requests, no google‑generativeai)

Usage:
    export GEMINI_API_KEY="YOUR_API_KEY"
    python3 agent.py

The script reads the API key from the environment variable ``GEMINI_API_KEY``
and authenticates to the Gemini REST endpoint using the ``x-goog-api-key``
header.  It maintains a conversation history, sends user prompts to the model,
parses any ``<cmd>...</cmd>`` tags in the response, asks for explicit human
approval, executes the command, feeds the command output back to the model and
repeats.

Model: gemini‑3.5‑flash (free tier). System prompt instructs Gemini to behave
as a Termux CLI assistant and to wrap any shell commands in <cmd> tags.
"""

import os
import sys
import json
import urllib.request
import urllib.error
import subprocess
import re
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.5-flash:generateContent"
)
# The API key is read from GEMINI_API_KEY (as requested by the user).
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    sys.stderr.write("[ERROR] GEMINI_API_KEY environment variable not set.\n")
    sys.exit(1)

# System instruction that the model will receive on every request.
SYSTEM_PROMPT = (
    "You are a Termux CLI assistant running on a legacy 32‑bit Android phone "
    "(armv7l). Respond to the user's query and, if you need to run a shell "
    "command or edit a file, wrap the exact command in <cmd>...</cmd> tags. "
    "Only include one <cmd> block per response. Do not execute commands on your "
    "own – the host will parse the tags, ask the user for confirmation, and "
    "run the command via a subprocess."
)

# HTTP request timeout (seconds)
REQUEST_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def build_payload(conversation, user_input):
    """Build the JSON payload for the Gemini request.

    ``conversation`` is a list of dicts already formatted for the API.
    ``user_input`` is the newest user message (string).
    """
    # Append the latest user turn to the history for this request.
    contents = conversation + [{"role": "user", "parts": [{"text": user_input}]}]
    payload = {
        "contents": contents,
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
    }
    return payload

def send_request(payload):
    """POST ``payload`` to the Gemini endpoint and return the response JSON.
    Uses the ``x-goog-api-key`` header for authentication.
    """
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": API_KEY,
    }
    req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"[HTTP {e.code}] {e.reason}\n")
        try:
            body = e.read().decode("utf-8", errors="replace")
            sys.stderr.write(body + "\n")
        except Exception:
            pass
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"[NETWORK ERROR] {e.reason}\n")
        sys.exit(1)

def extract_reply(response_json):
    """Navigate the Gemini response structure and return the text reply."""
    try:
        return response_json["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return "[ERROR] Unexpected response format: " + json.dumps(response_json)

def find_command(text):
    """Search for the first <cmd>...</cmd> block and return the stripped command.
    Returns ``None`` if no block is found.
    """
    m = re.search(r"<cmd>(.*?)</cmd>", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None

def run_command(command):
    """Execute *command* via ``subprocess.run`` and return combined stdout+stderr.
    ``shell=True`` is required because Termux commands are often shell built‑ins.
    """
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True
    )
    out = result.stdout or ""
    err = result.stderr or ""
    return out, err, result.returncode

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    conversation = []  # list of dicts suitable for the API
    print("""============================================================
   Termux Gemini Autonomous Agent (32‑bit ARM)
   Type 'exit' or 'quit' to leave the session.
============================================================""")
    while True:
        try:
            user_msg = input("Agent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[INFO] Session terminated.")
            break
        if user_msg.lower() in {"exit", "quit"}:
            print("[INFO] Goodbye!")
            break
        # Append the user's message to the conversation history.
        conversation.append({"role": "user", "parts": [{"text": user_msg}]})

        payload = build_payload([], user_msg)  # we send full history inside payload
        response_json = send_request(payload)
        reply = extract_reply(response_json)
        print("Model:", reply)

        # Check for a <cmd> block in the model's reply.
        cmd = find_command(reply)
        if cmd:
            # Ask for explicit user consent before execution.
            while True:
                resp = input(f"[Agent wants to run: {cmd}] Allow? (y/n) ").strip().lower()
                if resp in {"y", "yes"}:
                    allowed = True
                    break
                elif resp in {"n", "no"}:
                    allowed = False
                    break
                else:
                    print("Please answer 'y' or 'n'.")
            if allowed:
                stdout, stderr, rc = run_command(cmd)
                # Prepare the feedback message for the model.
                feedback = (
                    f"Command executed: {cmd}\n"
                    f"Return code: {rc}\n"
                    f"--- STDOUT ---\n{stdout}\n"
                    f"--- STDERR ---\n{stderr}\n"
                )
                print("[EXECUTION RESULT]\n", feedback)
                # Feed the output back as a new user message.
                conversation.append({"role": "user", "parts": [{"text": feedback}]})
            else:
                print("[INFO] Command execution aborted by the user.")
                conversation.append({"role": "user", "parts": [{"text": "User declined to run the command."}]})
        # Small pause to avoid hammering the API in case of rapid loops.
        time.sleep(0.2)

if __name__ == "__main__":
    main()
