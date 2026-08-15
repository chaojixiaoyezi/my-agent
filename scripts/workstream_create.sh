#!/usr/bin/env bash
# 用它开一条并行开发线。
# 它会创建目录、创建或复用 workstream/<name> 分支，并打印下一步怎么进入这条线。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/workstream_common.sh
source "${SCRIPT_DIR}/workstream_common.sh"

usage() {
  # 参数很少：必填名字，可选 base 分支和自定义分支名。
  cat <<'EOF'
Usage:
  scripts/workstream_create.sh <name> [--from <base>] [--branch <branch>] [--dry-run]

Examples:
  scripts/workstream_create.sh memory
  scripts/workstream_create.sh tools-boundary --from main
  scripts/workstream_create.sh runtime-v2 --branch workstream/runtime-v2
  scripts/workstream_create.sh memory --dry-run
EOF
  workstream_usage_root_note
}

NAME=""
BASE="HEAD"
BRANCH=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --from)
      if [[ $# -lt 2 ]]; then
        printf '--from 需要一个 base 参数。\n' >&2
        exit 2
      fi
      BASE="${2:-}"
      shift 2
      ;;
    --branch)
      if [[ $# -lt 2 ]]; then
        printf '--branch 需要一个 branch 参数。\n' >&2
        exit 2
      fi
      BRANCH="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --*)
      printf '未知参数: %s\n' "$1" >&2
      usage
      exit 2
      ;;
    *)
      if [[ -n "${NAME}" ]]; then
        printf '只能传一个 workstream 名称。\n' >&2
        usage
        exit 2
      fi
      NAME="$1"
      shift
      ;;
  esac
done

if [[ -z "${NAME}" ]]; then
  usage
  exit 2
fi

workstream_validate_name "${NAME}"

REPO_ROOT="$(workstream_repo_root)"
WORKTREE_ROOT="$(workstream_default_root)"
TARGET="$(workstream_target_path "${NAME}")"
BRANCH="${BRANCH:-$(workstream_branch_name "${NAME}")}"

if [[ -e "${TARGET}" ]]; then
  printf '目标目录已存在: %s\n' "${TARGET}" >&2
  printf '如果这是旧 worktree，请先检查 scripts/workstream_status.sh。\n' >&2
  exit 2
fi

if [[ "${DRY_RUN}" == "true" ]]; then
  cat <<EOF
WORKSTREAM_CREATE_DRY_RUN
name: ${NAME}
branch: ${BRANCH}
base: ${BASE}
path: ${TARGET}
repo: ${REPO_ROOT}
EOF
  exit 0
fi

mkdir -p "${WORKTREE_ROOT}"

if git -C "${REPO_ROOT}" show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  git -C "${REPO_ROOT}" worktree add "${TARGET}" "${BRANCH}"
else
  git -C "${REPO_ROOT}" worktree add -b "${BRANCH}" "${TARGET}" "${BASE}"
fi

cat <<EOF
WORKSTREAM_CREATED
name: ${NAME}
branch: ${BRANCH}
base: ${BASE}
path: ${TARGET}

Next:
  cd ${TARGET}
  git status --short
  cp HANDOFF_TEMPLATE.md HANDOFF_${NAME}.md

Open visible terminal:
  ${REPO_ROOT}/scripts/open_workstream.sh ${NAME}
EOF
