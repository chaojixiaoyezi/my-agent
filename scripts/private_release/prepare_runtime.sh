#!/usr/bin/env bash
# LLM: 仅在隔离 Ubuntu 20.04 构建根中运行；网络只用于构建期解析依赖，交付物安装不联网。
# 模块用途: 从原 production wheel 安装完整 headless 依赖；编译与封装由 build.py 单独执行。
set -euo pipefail
root="${1:?传入构建目录}"
source_dir="${2:?传入冻结源码目录}"
mkdir -p "$root/bundle/runtime" "$root/wheels"
for item in bin lib include share; do cp -a "$root/$item" "$root/bundle/runtime/"; done
# 这是新复制的发行运行时，不再由构建机的 uv 管理；不修改宿主 Python。
rm -f "$root/bundle/runtime/lib/python3.11/EXTERNALLY-MANAGED"
python_bin="$root/bundle/runtime/bin/python3"
export PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
"$python_bin" -m pip wheel --no-deps --no-build-isolation "$source_dir" --wheel-dir "$root/wheels"
wheel="$root/wheels/my_agent-0.3.0-py3-none-any.whl"
"$python_bin" -m pip install --no-compile "$wheel[scale,secrets]"
"$python_bin" -c 'import agent_py_agent, ssl, sqlite3; print("runtime", agent_py_agent.__version__, ssl.OPENSSL_VERSION, sqlite3.sqlite_version)'
