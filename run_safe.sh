#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# LLM Sanitizer Launcher
# -----------------------------------------------------------------------------
# Usage: ./run_safe.sh <command> [args...]
# Example: ./run_safe.sh gemini
# -----------------------------------------------------------------------------

# Detect the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(pwd)"
PROJECT_NAME="$(basename "$PROJECT_DIR")"

# --- Configuration (defaults can be overridden by env vars) ---

# Path to the Python interpreter (default: look for .venv in script dir or use system python3)
if [[ -n "${OBFUSCATE_PYTHON:-}" ]]; then
    PYTHON_BIN="${OBFUSCATE_PYTHON}"
elif [[ -f "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
else
    PYTHON_BIN="python3"
fi

RUNNER="${SCRIPT_DIR}/obfuscate_runner.py"

# Path to the sensitive data dictionary
# Default: looks for sensitive_data.json in the script directory
DICT_PATH="${OBFUSCATE_DICT:-${SCRIPT_DIR}/sensitive_data.json}"

if [[ ! -f "$DICT_PATH" ]]; then
    echo "Error: Dictionary file not found at $DICT_PATH"
    echo "Please create a sensitive_data.json file or set OBFUSCATE_DICT."
    exit 1
fi

DICT_DIR="$(dirname "$DICT_PATH")"
export OBFUSCATE_DICT="$DICT_PATH"
export OBFUSCATE_UUID_MAP="${DICT_DIR}/.obfuscation_map.json"

# --- OS Detection & FUSE Configuration ---

OS_NAME="$(uname -s)"
MOUNT_HELPER=""

if [[ "$OS_NAME" == "Darwin" ]]; then
    # macOS Configuration
    if [[ -z "${OBFUSCATE_FUSE_LIB:-}" ]]; then
        if [[ -f "/usr/local/lib/libfuse.dylib" ]]; then
            export FUSE_LIBRARY_PATH="/usr/local/lib/libfuse.dylib"
        elif [[ -f "/opt/homebrew/lib/libfuse.dylib" ]]; then
            export FUSE_LIBRARY_PATH="/opt/homebrew/lib/libfuse.dylib"
        else
            export FUSE_LIBRARY_PATH="libfuse.dylib"
        fi
    else
        export FUSE_LIBRARY_PATH="${OBFUSCATE_FUSE_LIB}"
    fi

    if [[ -z "${OBFUSCATE_FUSE_DAEMON:-}" ]]; then
         if [[ -f "/Library/Filesystems/macfuse.fs/Contents/Resources/mount_macfuse" ]]; then
            MOUNT_HELPER="/Library/Filesystems/macfuse.fs/Contents/Resources/mount_macfuse"
         else
            MOUNT_HELPER="mount_macfuse"
         fi
    else
        MOUNT_HELPER="${OBFUSCATE_FUSE_DAEMON}"
    fi

elif [[ "$OS_NAME" == "Linux" ]]; then
    # Linux Configuration
    if [[ -z "${OBFUSCATE_FUSE_LIB:-}" ]]; then
        # Try common locations for libfuse.so
        # fusepy typically needs libfuse.so.2 for broader compatibility, or just libfuse.so
        FOUND_LIB=""
        for cand in \
            "/usr/lib/libfuse.so.2" \
            "/usr/lib/x86_64-linux-gnu/libfuse.so.2" \
            "/usr/lib/aarch64-linux-gnu/libfuse.so.2" \
            "/lib/x86_64-linux-gnu/libfuse.so.2" \
            "/usr/lib/libfuse.so" \
            "/usr/local/lib/libfuse.so"
        do
            if [[ -f "$cand" ]]; then
                FOUND_LIB="$cand"
                break
            fi
        done
        
        if [[ -n "$FOUND_LIB" ]]; then
            export FUSE_LIBRARY_PATH="$FOUND_LIB"
        else
            # Let ctypes find it by name
            export FUSE_LIBRARY_PATH="libfuse.so.2"
        fi
    else
        export FUSE_LIBRARY_PATH="${OBFUSCATE_FUSE_LIB}"
    fi
    
    # Linux typically doesn't need a specific mount helper env var for fusepy
    # unless using specific implementations, but we leave MOUNT_HELPER empty/default.
    MOUNT_HELPER=""
else
    echo "Warning: Unsupported OS '$OS_NAME'. Attempting to run with defaults."
fi

if [[ -n "$MOUNT_HELPER" ]]; then
    export _FUSE_DAEMON_PATH="${MOUNT_HELPER}"
fi

# --- Git Obfuscation Wrapper ---

GIT_WRAPPER_DIR=""
if [[ "${OBFUSCATE_GIT_LOG:-1}" == "1" ]]; then
  REAL_GIT="$(command -v git || true)"
  if [[ -n "$REAL_GIT" ]]; then
    GIT_WRAPPER_DIR="$(mktemp -d "${TMPDIR:-/tmp}/obf_git_XXXXXX")"
    
    # Create the python wrapper for git
    cat > "${GIT_WRAPPER_DIR}/git" <<'PY'
#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys

def load_replacements(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    replacements = data.get("replacements")
    if not isinstance(replacements, list):
        return {}
    mapping = {}
    for entry in replacements:
        if not isinstance(entry, dict):
            continue
        original = entry.get("original")
        placeholder = entry.get("placeholder")
        if isinstance(original, str) and isinstance(placeholder, str):
            mapping[original] = placeholder
    return mapping

def ordered_pairs(mapping):
    if not mapping:
        return []
    return [(key, mapping[key]) for key in sorted(mapping.keys(), key=len, reverse=True)]

def build_regex(mapping):
    if not mapping:
        return None
    parts = sorted(mapping.keys(), key=len, reverse=True)
    escaped = [re.escape(part) for part in parts]
    try:
        return re.compile("|".join(escaped))
    except re.error:
        return None

def obfuscate_text(text, mapping, pairs, regex):
    if not text:
        return text
    if regex is not None:
        return regex.sub(lambda m: mapping[m.group(0)], text)
    result = text
    for original, placeholder in pairs:
        result = result.replace(original, placeholder)
    return result

def is_git_log(args):
    i = 0
    # Arguments that take a value need to be skipped
    options_with_value = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if arg.startswith("-"):
            if arg in options_with_value:
                i += 2
            else:
                i += 1
            continue
        return arg == "log"
    if i < len(args):
        return args[i] == "log"
    return False

def main():
    args = sys.argv[1:]
    real_git = os.environ.get("OBFUSCATE_REAL_GIT") or "/usr/bin/git"
    
    # Pass through non-log commands directly
    if not is_git_log(args):
        os.execv(real_git, [real_git] + args)

    # Intercept git log
    proc = subprocess.Popen([real_git] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = proc.communicate()

    mapping = load_replacements(os.environ.get("OBFUSCATE_DICT"))
    pairs = ordered_pairs(mapping)
    regex = build_regex(mapping)
    
    stdout = obfuscate_text(stdout, mapping, pairs, regex)
    stderr = obfuscate_text(stderr, mapping, pairs, regex)

    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    sys.exit(proc.returncode)

if __name__ == "__main__":
    main()
PY
    chmod +x "${GIT_WRAPPER_DIR}/git"
    export OBFUSCATE_REAL_GIT="${REAL_GIT}"
    export PATH="${GIT_WRAPPER_DIR}:${PATH}"
    
    # Ensure cleanup of git wrapper on exit
    trap 'rm -rf "${GIT_WRAPPER_DIR}"; if [[ -n "${MOUNT_DIR:-}" ]]; then rmdir "${MOUNT_DIR}" 2>/dev/null || true; fi' EXIT
  fi
fi

# --- Execution Setup ---

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <command_to_run> [args...]"
    exit 1
fi

TARGET_COMMAND="$1"
shift

MOUNT_BASE="${OBFUSCATE_MOUNT_BASE:-/tmp/obf_mounts}"
LOG_DIR="${OBFUSCATE_LOG_DIR:-/tmp/obfuscation}"
LOG_LEVEL="${OBFUSCATE_LOG_LEVEL:-INFO}"
EXCLUDES="${OBFUSCATE_EXCLUDE:-.git,.venv,__pycache__,.idea,.DS_Store}"
STRICT_PATHS="${OBFUSCATE_STRICT_PATHS:-1}"

mkdir -p "$MOUNT_BASE" "$LOG_DIR"
MOUNT_DIR="${MOUNT_BASE}/${PROJECT_NAME}_$$"
mkdir -p "$MOUNT_DIR"

# Determine if we need sudo for mounting
USE_SUDO="${OBFUSCATE_USE_SUDO:-}"
if [[ -z "$USE_SUDO" ]]; then
  if [[ "$OS_NAME" == "Darwin" ]]; then
      # On macOS with macFUSE, checking version is a proxy for check if we have access
      if [[ -x "$MOUNT_HELPER" ]] && "$MOUNT_HELPER" --version >/dev/null 2>&1; then
        USE_SUDO="0"
      else
        USE_SUDO="1"
      fi
  elif [[ "$OS_NAME" == "Linux" ]]; then
      # On Linux, users in 'fuse' group usually don't need sudo.
      # We assume no sudo by default for Linux to avoid prompt spam.
      USE_SUDO="0"
  else
      USE_SUDO="0"
  fi
fi

RUN_ARGS=(
  "$PYTHON_BIN" "$RUNNER"
  --source-dir "$PROJECT_DIR"
  --mount-dir "$MOUNT_DIR"
  --dictionary "$DICT_PATH"
  --command "$TARGET_COMMAND"
  --log-file "${LOG_DIR}/${PROJECT_NAME}_${TARGET_COMMAND}.log"
  --log-level "$LOG_LEVEL"
  --exclude "$EXCLUDES"
)

if [[ "${STRICT_PATHS}" != "0" ]]; then
  RUN_ARGS+=(--strict-paths)
fi

# Append any remaining arguments intended for the runner if necessary
# (Currently we pass arguments to the target command inside the 'command' string, 
#  but here we are just launching the runner which spawns the command. 
#  If the user provided extra args to run_safe.sh, we might want to append them 
#  to the command string or runner args. For now, let's assume $1 is the full command binary
#  and we don't support passing complex args to the inner command easily without quoting.)

if [[ "$USE_SUDO" == "1" ]]; then
  # preserve environment variables when using sudo
  RUN_ARGS=(sudo -E "${RUN_ARGS[@]}" --run-as-uid "$(id -u)" --run-as-gid "$(id -g)")
fi

echo "🛡️  Mounting obfuscated view at: $MOUNT_DIR"
echo "🚀 Launching: $TARGET_COMMAND"

"${RUN_ARGS[@]}"
EXIT_CODE=$?

# Cleanup happens in trap
exit $EXIT_CODE
