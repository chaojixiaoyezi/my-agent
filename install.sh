#!/usr/bin/env bash
# my-agent 一行安装器 —— 在干净的 Linux 机器上部署 my-agent(默认接 MiniMax-M2.7)。
#
# 用法:
#   curl -fsSL <raw-url>/install.sh | bash            # 从 GitHub 拉默认分支装
#   MYAGENT_BRANCH=enterprise-features bash install.sh # 指定分支
#   MYAGENT_SRC=/path/to/checkout bash install.sh      # 从本地检出/rsync 来的源码装(不走 git)
#
# 自带的 Python 依赖随 pip 一起装(extras=scale,secrets);非自带的系统依赖(git/编译器/PG/浏览器)
# 装完后由 my-agent 自己按需装(你当普通用户用 CLI 跟它说)。装成 editable,故支持 `my-agent update` 自更新。
set -euo pipefail

REPO="${MYAGENT_REPO:-https://github.com/chaojixiaoyezi/my-agent.git}"
BRANCH="${MYAGENT_BRANCH:-main}"
HOME_DIR="${MYAGENT_HOME_DIR:-$HOME/.my-agent}"
SRC_DIR="$HOME_DIR/src"
VENV_DIR="$HOME_DIR/venv"
EXTRAS="${MYAGENT_EXTRAS:-scale,secrets}"
LOCAL_SRC="${MYAGENT_SRC:-}"
BIN_DIR="${MYAGENT_BIN_DIR:-$HOME/.local/bin}"

log() { printf '\033[1;36m[my-agent 安装]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

pkg_install() {
  # 尝试用发行版包管理器装系统依赖(python/git);装不了就让用户自己装。
  local what="$1"
  if command -v dnf >/dev/null 2>&1; then sudo dnf install -y "$what" 2>/dev/null || dnf install -y "$what"
  elif command -v apt-get >/dev/null 2>&1; then sudo apt-get update -qq && sudo apt-get install -y "$what"
  elif command -v yum >/dev/null 2>&1; then sudo yum install -y "$what"
  else die "找不到包管理器,请手动安装 $what 后重试。"; fi
}

ensure_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    log "未发现 python3,尝试安装…"; pkg_install python3 || die "python3 安装失败,请手动安装 ≥3.10。"
  fi
  local ver; ver="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  log "Python 版本: $ver"
  python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' \
    || die "需要 Python ≥3.10(当前 $ver)。请升级后重试。"
  python3 -c 'import venv' 2>/dev/null || pkg_install python3-venv || true
}

obtain_source() {
  if [ -n "$LOCAL_SRC" ]; then
    log "从本地源码安装: $LOCAL_SRC"
    [ -f "$LOCAL_SRC/pyproject.toml" ] || die "$LOCAL_SRC 不是 my-agent 源码目录(缺 pyproject.toml)。"
    mkdir -p "$HOME_DIR"; SRC_DIR="$LOCAL_SRC"
    return
  fi
  command -v git >/dev/null 2>&1 || { log "未发现 git,尝试安装…"; pkg_install git; }
  if [ -d "$SRC_DIR/.git" ]; then
    log "已有检出,git pull 更新: $SRC_DIR"; git -C "$SRC_DIR" pull --ff-only || die "git pull 失败,请检查 $SRC_DIR。"
  else
    log "克隆 $REPO ($BRANCH) → $SRC_DIR"; rm -rf "$SRC_DIR"
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$SRC_DIR" || die "git clone 失败(检查 repo/分支/网络/凭据)。"
  fi
}

main() {
  log "目标目录: $HOME_DIR"
  ensure_python
  obtain_source
  log "创建 venv: $VENV_DIR"
  python3 -m venv "$VENV_DIR"
  log "安装 my-agent(editable + 依赖 extras=$EXTRAS,自带依赖一并装)…"
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install -e "$SRC_DIR[$EXTRAS]" || "$VENV_DIR/bin/pip" install -e "$SRC_DIR"
  mkdir -p "$BIN_DIR"
  ln -sf "$VENV_DIR/bin/my-agent" "$BIN_DIR/my-agent"
  log "已暴露 CLI: $BIN_DIR/my-agent"
  "$VENV_DIR/bin/my-agent" --help >/dev/null 2>&1 && log "✅ my-agent 安装成功" || die "安装后 my-agent 不可用。"
  # 自更新提示按安装方式给:git clone 装的能 my-agent update;本地源码(rsync)装的没有可用 .git/origin,据实说明。
  local update_hint="自更新:   my-agent update          (从 origin 拉最新 + 刷依赖)"
  if [ -n "$LOCAL_SRC" ]; then
    update_hint="自更新:   本地源码安装,更新请重跑本脚本/重新同步源码(my-agent update 仅适用于 git 检出安装)"
  fi
  cat <<EOF

装好了。默认模型已是 MiniMax-M2.7,接下来:
  1. 设 key:   export AGENT_API_KEY="你的_minimax_key"   (建议写进 ~/.bashrc)
  2. 确认 PATH 含 $BIN_DIR(没有就: export PATH="\$PATH:$BIN_DIR")
  3. 试一把:   my-agent run "创建 hello.py 并写个单测跑通"
  4. $update_hint
  5. 接飞书:   my-agent adapter start --channel feishu   (先配好 feishu_* key + 开 8421 端口)
其余系统依赖(浏览器/PG 等)可直接用 CLI 跟 my-agent 说,让它自己装。
EOF
}

main "$@"
