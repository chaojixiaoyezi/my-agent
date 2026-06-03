#!/usr/bin/env bash
# 用它把某条开发线打开成你能看到的终端窗口。
# 不传命令时会停在 shell；传命令时会先执行命令，再保持窗口打开。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/workstream_common.sh
source "${SCRIPT_DIR}/workstream_common.sh"

usage() {
  # 第一个参数是 workstream 名字；后面可以跟一整条要在终端里执行的命令。
  cat <<'EOF'
Usage:
  scripts/open_workstream.sh <name> [command]

Examples:
  scripts/open_workstream.sh memory
  scripts/open_workstream.sh tools-boundary "git status --short"
EOF
  workstream_usage_root_note
}

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

NAME="$1"
shift
workstream_validate_name "${NAME}"

TARGET="$(workstream_target_path "${NAME}")"
if [[ ! -d "${TARGET}" ]]; then
  printf '未找到 workstream 目录: %s\n' "${TARGET}" >&2
  printf '请先运行: scripts/workstream_create.sh %s\n' "${NAME}" >&2
  exit 2
fi

USER_COMMAND="${*:-}"

python3 - "${TARGET}" "${NAME}" "${USER_COMMAND}" <<'PY'
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


target = Path(sys.argv[1]).resolve()
name = sys.argv[2]
user_command = sys.argv[3]

intro = (
    "echo WORKSTREAM="
    + shlex.quote(name)
    + " && echo PATH="
    + shlex.quote(str(target))
    + " && git status --short"
)
if user_command:
    command = "cd " + shlex.quote(str(target)) + " && " + intro + " && " + user_command + " ; exec ${SHELL:-bash} -l"
else:
    command = "cd " + shlex.quote(str(target)) + " && " + intro + " ; exec ${SHELL:-bash} -l"

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
