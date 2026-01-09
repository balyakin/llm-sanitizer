# LLM Sanitizer 🛡️

> **Safely use AI coding agents with your private codebase.**
> A transparent FUSE filesystem for macOS that obfuscates sensitive data (PII, secrets, proprietary names) on-the-fly for LLM CLIs.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Platform macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)]()
[![Platform Linux](https://img.shields.io/badge/platform-Linux-lightgrey.svg)]()

## 🚀 What is this?

When you use CLI tools like **Gemini**, **Claude**, or **OpenAI** to analyze your local code, you typically give them full read access to your files. **LLM Sanitizer** creates a secure "view" of your project where:

1.  **Sensitive files/folders are renamed** (e.g., `SecretProject` -> `ProjectA`).
2.  **File contents are scrubbed** automatically when read.
3.  **Writes are de-obfuscated** seamlessly, so the AI writes valid code while seeing only safe tokens.
4.  **Git history is protected** (optional `git log` obfuscation).

Your real files on disk remain untouched. The AI agent lives in a Matrix-like simulation of your project.

### How it works (high level)
- `obfuscate_runner.py` mounts a FUSE filesystem over a real project directory.
- A dictionary maps `original -> placeholder`.
- When files are read, the wrapper replaces originals with placeholders.
- When files are written, placeholders are restored back to originals.
- UUID4 hex tokens (32 hex chars, `uuid4().hex`) are automatically tokenized and mapped in a `.obfuscation_map.json` file stored next to your dictionary file.

---

## Requirements

### macOS
- macOS 12+ recommended
- macFUSE (from macfuse project / Homebrew)

### Linux
- Kernel with FUSE support
- `libfuse2` (usually installed by default or via `fuse` package)

### Common
- Python 3.9+
- Python package `fusepy`

---

## Installation

1) Install FUSE

**macOS:**
```bash
brew install --cask macfuse
```
(Allow the extension in System Settings -> Privacy & Security)

**Linux (Debian/Ubuntu):**
```bash
sudo apt-get install fuse libfuse2
```

2) Install the project dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3) Prepare your dictionary
Create a `sensitive_data.json` file in the project directory using the example template:
```bash
cp sensitive_data.example.json sensitive_data.json
```
Edit `sensitive_data.json` to include your sensitive tokens and their placeholders.

---

## Usage

Use the universal wrapper script `run_safe.sh` to launch any CLI tool in the sanitized environment.

```bash
./run_safe.sh <command> [args]
```

**Examples:**

```bash
# Run Gemini CLI
./run_safe.sh gemini

# Run Claude CLI
./run_safe.sh claude

# Run custom command (e.g. ls to inspect the mounted view)
./run_safe.sh ls -R
```

The script:
1.  Mounts an obfuscated view to `/tmp/obf_mounts/<project>_<pid>`.
2.  Launches the specified command inside that directory.
3.  Unmounts automatically when the command exits.

Logs are written to `/tmp/obfuscation/<project>_*.log`.

---

## Shell Aliases (Recommended)

To run these tools comfortably from any project directory, add aliases to your shell configuration (`~/.zshrc` or `~/.bashrc`).

Replace `/path/to/llm-sanitizer` with the actual path where you cloned this repository:

```bash
# LLM Sanitizer Aliases
alias gemini-safe='/path/to/llm-sanitizer/run_safe.sh gemini'
alias claude-safe='/path/to/llm-sanitizer/run_safe.sh claude'
alias codex-safe='/path/to/llm-sanitizer/run_safe.sh codex'
alias qwen-safe='/path/to/llm-sanitizer/run_safe.sh qwen'
```

After saving, reload your shell:
```bash
source ~/.zshrc  # or ~/.bashrc
```

Now you can just navigate to any project and run:
```bash
cd ~/my-secret-project
gemini-safe
```

---

## Configuration

You can override defaults with environment variables:

- `OBFUSCATE_DICT` - Path to your dictionary file (default: `./sensitive_data.json`).
- `OBFUSCATE_PYTHON` - Python interpreter path.
- `OBFUSCATE_FUSE_LIB` - Path to `libfuse.dylib` (auto-detected if possible).
- `OBFUSCATE_FUSE_DAEMON` - Path to `mount_macfuse` (auto-detected if possible).
- `OBFUSCATE_MOUNT_BASE` - Base directory for mounts (default: `/tmp/obf_mounts`).
- `OBFUSCATE_LOG_DIR` - Log directory (default: `/tmp/obfuscation`).
- `OBFUSCATE_LOG_LEVEL` - Log level (`INFO`, `DEBUG`, `WARNING`, `ERROR`).
- `OBFUSCATE_EXCLUDE` - Comma-separated names/patterns excluded from obfuscation (default: `.git,.venv,__pycache__,.idea,.DS_Store`).
- `OBFUSCATE_GIT_LOG` - Set to `0` to disable git log obfuscation.
- `OBFUSCATE_STRICT_PATHS` - Set to `0` to allow access using real (non-obfuscated) path segments.

---

## Direct runner usage

You can also call the python runner directly if you need more control:

```bash
./.venv/bin/python obfuscate_runner.py \
  --source-dir /path/to/project \
  --mount-dir /tmp/obf_mounts/project \
  --dictionary ./sensitive_data.json \
  --command "gemini"
```

Options:
- `--text-extensions` - Comma-separated list of text extensions to process.
- `--exclude` - Comma-separated list of excluded names/patterns.
- `--allow-other` - Allow other users to access the mount.
- `--run-as-uid` / `--run-as-gid` - Drop privileges for the command.
- `--strict-paths` - Block access when a path contains non-obfuscated sensitive tokens.

---

## Notes and Security
- Only the mounted view is obfuscated. Your real files are untouched.
- Obfuscation applies to text files only (checked by extension).
- Ensure `sensitive_data.json` is **never committed** to version control.
- Strict path mode blocks access if a path contains non-obfuscated sensitive tokens (disable with `OBFUSCATE_STRICT_PATHS=0`).
- The git log wrapper only intercepts `git log`, not other git commands.

---

## Troubleshooting

### Mount fails or unmount hangs
- **macOS:** Ensure macFUSE is allowed by macOS. Try reloading: `sudo /Library/Filesystems/macfuse.fs/Contents/Resources/load_macfuse`
- **Linux:** Ensure your user is in the `fuse` group if required: `sudo usermod -aG fuse $USER`

### Wrong libfuse path
The script tries to auto-detect `libfuse`. If it fails:

**macOS:**
```bash
export OBFUSCATE_FUSE_LIB=/opt/homebrew/lib/libfuse.dylib
export OBFUSCATE_FUSE_DAEMON=/Library/Filesystems/macfuse.fs/Contents/Resources/mount_macfuse
```

**Linux:**
```bash
export OBFUSCATE_FUSE_LIB=/usr/lib/x86_64-linux-gnu/libfuse.so.2
```

---

## Project files
- `obfuscate_runner.py` - Core FUSE filesystem implementation.
- `run_safe.sh` - Universal wrapper script.
- `sensitive_data.example.json` - Template for your dictionary.