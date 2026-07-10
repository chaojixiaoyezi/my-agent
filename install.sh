#!/usr/bin/env bash
# my-agent 一行安装器 —— 默认把 CLI 安装成透明容器运行(默认接 MiniMax-M2.7)。
#
# 用法:
#   curl -fsSL <raw-url>/install.sh | bash            # 默认容器模式
#   curl -fsSL <raw-url>/install.sh | bash -s -- --host # 明确选择宿主 venv 开发模式
#   MYAGENT_BRANCH=enterprise-features bash install.sh # 指定分支
#   MYAGENT_SRC=/path/to/checkout bash install.sh      # 从本地检出/rsync 来的源码装(不走 git)
#
# 容器模式会复用已有 Docker/Podman；干净 Linux 无运行时时尝试安装 rootless Podman。生成的
# `my-agent` 包装器自动挂当前工作区和 ~/.my-agent，用户无需手动进入容器。宿主模式装 editable。
set -euo pipefail

REPO="${MYAGENT_REPO:-https://github.com/chaojixiaoyezi/my-agent.git}"
BRANCH="${MYAGENT_BRANCH:-main}"
HOME_DIR="${MYAGENT_HOME_DIR:-$HOME/.my-agent}"        # 纯数据目录(不含源码/venv,学 my-agent-claw)
SRC_DIR="${MYAGENT_SRC_DIR:-$HOME/my-agent-src}"        # 安装目录:源码 + venv,与数据目录分离
VENV_DIR=""                                             # 在 obtain_source 定下 SRC_DIR 后设为 $SRC_DIR/.venv
EXTRAS="${MYAGENT_EXTRAS:-scale,secrets}"
LOCAL_SRC="${MYAGENT_SRC:-}"
BIN_DIR="${MYAGENT_BIN_DIR:-$HOME/.local/bin}"
INSTALL_MODE="${MYAGENT_INSTALL_MODE:-container}"
IMAGE="${MYAGENT_IMAGE:-my-agent:local}"
CONTAINER_RUNTIME=""
CONTAINER_USE_SUDO=0

log() { printf '\033[1;36m[my-agent 安装]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --container) INSTALL_MODE="container" ;;
      --host) INSTALL_MODE="host" ;;
      *) die "未知参数: $1(支持 --container / --host)" ;;
    esac
    shift
  done
}

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

ensure_source_tools() {
  if [ -z "$LOCAL_SRC" ] && ! command -v git >/dev/null 2>&1; then
    log "未发现 git,尝试安装…"
    pkg_install git
  fi
}

runtime_works() {
  local runtime="$1"
  "$runtime" info >/dev/null 2>&1
}

ensure_container_runtime() {
  if [ -n "${MYAGENT_CONTAINER_RUNTIME:-}" ]; then
    command -v "$MYAGENT_CONTAINER_RUNTIME" >/dev/null 2>&1 \
      || die "找不到指定容器运行时: $MYAGENT_CONTAINER_RUNTIME"
    CONTAINER_RUNTIME="$MYAGENT_CONTAINER_RUNTIME"
  elif command -v docker >/dev/null 2>&1; then
    CONTAINER_RUNTIME="docker"
  elif command -v podman >/dev/null 2>&1; then
    CONTAINER_RUNTIME="podman"
  else
    log "未发现 Docker/Podman,尝试安装 rootless Podman…"
    pkg_install podman || die "Podman 安装失败；请先安装 Docker 或 Podman。"
    CONTAINER_RUNTIME="podman"
  fi

  if runtime_works "$CONTAINER_RUNTIME"; then
    return
  fi
  if [ "$CONTAINER_RUNTIME" = "docker" ] && command -v sudo >/dev/null 2>&1 \
      && sudo docker info >/dev/null 2>&1; then
    CONTAINER_USE_SUDO=1
    return
  fi
  die "$CONTAINER_RUNTIME 已安装但当前不可用；请启动运行时并确认当前用户有执行权限。"
}

container_runtime() {
  if [ "$CONTAINER_USE_SUDO" = "1" ]; then
    sudo "$CONTAINER_RUNTIME" "$@"
  else
    "$CONTAINER_RUNTIME" "$@"
  fi
}

install_container_wrapper() {
  local wrapper="$BIN_DIR/my-agent"
  local seccomp_dir="$HOME_DIR/container-security"
  local seccomp_profile="$seccomp_dir/seccomp-bwrap.json"
  mkdir -p "$BIN_DIR" "$HOME_DIR" "$seccomp_dir"
  cp "$SRC_DIR/deploy/seccomp-bwrap.json" "$seccomp_profile"
  chmod 0644 "$seccomp_profile"
  cat >"$wrapper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
RUNTIME_BIN="$CONTAINER_RUNTIME"
USE_SUDO="$CONTAINER_USE_SUDO"
IMAGE="$IMAGE"
MYAGENT_DATA_HOME="$HOME_DIR"
SECCOMP_PROFILE="$seccomp_profile"

runtime() {
  if [ "\$USE_SUDO" = "1" ]; then
    sudo "\$RUNTIME_BIN" "\$@"
  else
    "\$RUNTIME_BIN" "\$@"
  fi
}

tty_args=(-i)
if [ -t 0 ] && [ -t 1 ]; then tty_args+=(-t); fi
workspace="\$(pwd -P)"
env_file="\$(mktemp "\$MYAGENT_DATA_HOME/.container-env.XXXXXX")"
chmod 600 "\$env_file"
trap 'rm -f "\$env_file"' EXIT
for name in AGENT_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY MINIMAX_API_KEY \
  FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_ENCRYPT_KEY FEISHU_VERIFICATION_TOKEN DATABASE_URL; do
  value="\${!name:-}"
  if [ -n "\$value" ]; then printf '%s=%s\n' "\$name" "\$value" >>"\$env_file"; fi
done

runtime run --rm "\${tty_args[@]}" \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --cap-drop=ALL --security-opt=no-new-privileges --security-opt "seccomp=\$SECCOMP_PROFILE" \
  --pids-limit=1024 \
  --user "\$(id -u):\$(id -g)" \
  --env HOME=/my-agent-home --env MY_AGENT_HOME=/my-agent-home \
  --env-file "\$env_file" \
  --volume "\$MYAGENT_DATA_HOME:/my-agent-home" \
  --volume "\$workspace:/workspace" --workdir /workspace \
  "\$IMAGE" my-agent "\$@"
EOF
  chmod +x "$wrapper"
  log "已安装透明容器 CLI: $wrapper"
}

container_probe() {
  local runtime_args=(run --rm --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m)
  runtime_args+=(--cap-drop=ALL --security-opt=no-new-privileges)
  runtime_args+=(--security-opt "seccomp=$SRC_DIR/deploy/seccomp-bwrap.json" --pids-limit=128)
  runtime_args+=(--user "$(id -u):$(id -g)" "$IMAGE")
  runtime_args+=(python -m agent_py_agent.agent.tooling.sandbox --quiet)
  container_runtime "${runtime_args[@]}" \
    || die "容器已构建，但 bwrap namespace/mount 自检失败；该节点不会降级为宿主执行。"
}

install_container() {
  ensure_container_runtime
  log "构建容器镜像: $IMAGE"
  container_runtime build -f "$SRC_DIR/deploy/Dockerfile" -t "$IMAGE" "$SRC_DIR"
  log "在最终容器约束下执行 bwrap 隔离自检…"
  container_probe
  install_container_wrapper
  "$BIN_DIR/my-agent" --help >/dev/null 2>&1 \
    && log "✅ my-agent 容器安装成功" \
    || die "容器 CLI 安装后不可用。"
  cat <<EOF

装好了。以后直接使用 my-agent，命令会自动在容器中运行，不需要 docker exec：
  1. 设 key:   export AGENT_API_KEY="你的_minimax_key"
  2. 确认 PATH: export PATH="\$PATH:$BIN_DIR"
  3. 试一把:   my-agent run "创建 hello.py 并写个单测跑通"

持久数据: $HOME_DIR
当前工作目录会作为唯一项目工作区挂入 /workspace；宿主其他目录不会暴露给容器。
更新镜像: 重新执行同一条安装命令。
EOF
}

install_host() {
  ensure_python
  VENV_DIR="$SRC_DIR/.venv"   # venv 放源码目录旁,与数据目录(HOME_DIR)彻底分离——家目录从此纯数据
  log "创建 venv: $VENV_DIR"
  python3 -m venv "$VENV_DIR"
  log "安装 my-agent(editable + 依赖 extras=$EXTRAS,自带依赖一并装)…"
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install -e "$SRC_DIR[$EXTRAS]" || "$VENV_DIR/bin/pip" install -e "$SRC_DIR"
  mkdir -p "$BIN_DIR"
  ln -sf "$VENV_DIR/bin/my-agent" "$BIN_DIR/my-agent"
  log "已暴露 CLI: $BIN_DIR/my-agent"
  "$VENV_DIR/bin/my-agent" --help >/dev/null 2>&1 && log "✅ my-agent 宿主开发模式安装成功" || die "安装后 my-agent 不可用。"
  cat <<EOF

宿主开发模式已安装。注意：owner-scoped run_command 在非 Linux/bwrap 不可用时会安全拒绝，
不会降级执行。生产或多用户使用请重新运行安装器的默认容器模式。
EOF
}

main() {
  parse_args "$@"
  log "目标目录: $HOME_DIR"
  log "安装模式: $INSTALL_MODE"
  ensure_source_tools
  obtain_source
  case "$INSTALL_MODE" in
    container) install_container ;;
    host) install_host ;;
    *) die "MYAGENT_INSTALL_MODE 必须是 container 或 host" ;;
  esac
}

main "$@"
