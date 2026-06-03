#!/usr/bin/env bash
# 用它看现在开了哪些并行开发线、在哪个分支、有没有未提交改动。
# 主线集成前先跑一遍，能减少漏看某条线的风险。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/workstream_common.sh
source "${SCRIPT_DIR}/workstream_common.sh"

REPO_ROOT="$(workstream_repo_root)"

printf 'WORKSTREAM_STATUS\n'
printf 'repo: %s\n' "${REPO_ROOT}"
printf 'worktree_root: %s\n\n' "$(workstream_default_root)"

git -C "${REPO_ROOT}" worktree list --porcelain | awk '/^worktree / {print substr($0, 10)}' |
while IFS= read -r path; do
  if [[ ! -d "${path}" ]]; then
    continue
  fi
  branch="$(git -C "${path}" branch --show-current 2>/dev/null || true)"
  head="$(git -C "${path}" rev-parse --short HEAD 2>/dev/null || true)"
  dirty_count="$(git -C "${path}" status --short 2>/dev/null | wc -l | tr -d ' ')"
  printf '%s\n' '---'
  printf 'path: %s\n' "${path}"
  printf 'branch: %s\n' "${branch:-detached}"
  printf 'head: %s\n' "${head:-unknown}"
  printf 'dirty_files: %s\n' "${dirty_count}"
  if [[ "${dirty_count}" != "0" ]]; then
    git -C "${path}" status --short
  fi
done
