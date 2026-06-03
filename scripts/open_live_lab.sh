#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

python3 - "$REPO_ROOT" "$@" <<'PY'
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def applescript_string(value: str) -> str:
    """Quote a shell command as an AppleScript string.

    macOS Terminal 需要 AppleScript 字符串。
    这里把反斜杠和双引号转义，避免路径或参数里有特殊字符时打不开。
    """

    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


repo_root = Path(sys.argv[1]).resolve()
args = sys.argv[2:]
command = "cd " + shlex.quote(str(repo_root)) + " && python3 scripts/live_agent_lab.py"
if args:
    command += " " + " ".join(shlex.quote(arg) for arg in args)

if sys.platform == "darwin" and shutil.which("osascript"):
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "Terminal" to activate',
            "-e",
            f'tell application "Terminal" to do script {applescript_string(command)}',
        ],
        check=True,
    )
else:
    os.execvp("bash", ["bash", "-lc", command])
PY
