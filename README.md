# AI Agent for Android OS

An autonomous AI terminal agent for Android that writes code, manages files, and runs shell commands automatically directly inside Termux. Built specifically to run on 32-bit (armv7l) phones as well as modern 64-bit devices.

This agent connects directly to the Google Gemini API using only Python's built-in standard libraries. It turns an old or spare Android device into an autonomous programming assistant: describe a task, and the agent writes the code, inspects errors, executes shell commands, and loops automatically until the job is done—all with an interactive (y/n) safety prompt before running any command on your device.

## 🛠️ Prerequisites

Before getting started, make sure you have:
* **An Android Device** — Any phone running Android 7.0 or newer (works on 32-bit ARM and modern 64-bit phones).
* **Termux Installed** — Downloaded directly from this repository (for older devices) or the official release page.
* **A Free Gemini API Key** — From Google AI Studio (no credit card or paid subscription needed).

## 🚀 Quick Start Guide

### Step 1: Install Termux on Your Phone

Choose the right version for your device:
* **For Legacy 32-bit Phones (older devices):** Download and install `Termux_32bit.apk` directly from this repository: https://github.com/termux/termux-app/releases/download/v0.118.1/termux-app_v0.118.1+github-debug_armeabi-v7a.apk
* **For Modern 64-bit Phones (Pixel 7+, Galaxy S24+, Android 14+):** Download the official 64-bit APK directly from [Termux GitHub Releases (arm64-v8a)](https://github.com/termux/termux-app/releases/download/v0.118.1/termux-app_v0.118.1+github-debug_arm64-v8a.apk).

### Step 2: Set Up the Environment

Open the Termux app and run these commands to update package lists and install Git and Python:
```bash
pkg update -y && pkg install git python -y
```

### Step 3: Clone the Repository

Clone this repository and enter the project folder:
```bash
git clone https://github.com/netizen4-bit/agent042.git
cd agent042
```

## 🔑 Getting Your Free API Key

⚠️ **IMPORTANT: Avoid Billing Account Errors**
When creating your key, do not attach a Google Cloud billing account. Projects with depleted prepaid balances will trigger a `402 Payment Required` error. A fresh project with no billing attached gives you access to the permanent free tier (1,500 free requests per day).

1. Visit [Google AI Studio](https://aistudio.google.com/app/apikey).
2. Sign in with your Google Account and click **Create API key**.
3. Select **Create a new project** from the dropdown menu (e.g., name it `termux-agent`).
4. Copy your API key.

## ⚙️ Running the Agent

Export your API key into Termux and start the agent:
```bash
export GEMINI_API_KEY="paste_your_api_key_here"
python3 agent.py
```

Once running, type your request. The agent will respond, generate code, and prompt for confirmation before running any shell commands on your phone.

## 📁 Project Structure

```plaintext
agent042/
├── agent.py            # The core autonomous execution loop (pure Python standard library)
├── Termux_32bit.apk    # Offline bundled 32-bit Termux installer for legacy devices
├── README.md           # Documentation and setup guide
└── LICENSE             # MIT License
```

## 💬 How to Command the Agent

The agent is designed to understand natural language instructions and translate them into shell commands and scripts.

### Common Task Examples

* **File creation and execution:**
  "Create a Python script named monitor.py that checks battery status and print the result."
* **System diagnostics:**
  "Check available storage space, active RAM usage, and tell me if the system is running low."
* **Code debugging:**
  "Look at the error in script.py, explain what is broken, patch it, and test-run it."

### ⚠️ Pro-Tip: Autonomous Execution Safety

The agent wraps all terminal commands in `<cmd>...</cmd>` tags. Before any command executes, you will see:
```plaintext
[Agent wants to run: uname -m] Allow? (y/n)
```
* Press `y` to approve the command and automatically pass the terminal output back to Gemini.
* Press `n` to decline the command. The agent will adapt its strategy or ask for alternative instructions.

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
