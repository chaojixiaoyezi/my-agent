#!/usr/bin/env bash
# LLM: shared helpers for git-worktree based parallel development scripts.
# 给人看的解释：
# 这里放 workstream 脚本共用的小函数。
# 每个入口脚本只描述自己的动作，路径计算、名字校验和分支命名都统一从这里来。

set -euo pipefail

workstream_repo_root() {
  # LLM: resolve the repository root from the script location.
  # 给人看的解释：
  # 不管你从哪个目录执行脚本，它都会先找到真正的仓库根目录。
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  git -C "${script_dir}/.." rev-parse --show-toplevel
}

workstream_default_root() {
  # LLM: return the default sibling directory that stores all worktrees.
  # 给人看的解释：
  # 默认把并行工作区放到当前仓库旁边，避免塞进主仓库内部。
  local repo_root
  repo_root="$(workstream_repo_root)"
  local parent
  parent="$(cd "${repo_root}/.." && pwd)"
  printf '%s\n' "${MY_AGENT_WORKTREE_ROOT:-${parent}/my-agent-worktrees}"
}

workstream_validate_name() {
  # LLM: reject names that would create unsafe paths or confusing branches.
  # 给人看的解释：
  # workstream 名字会出现在目录和分支里，所以只允许简单、安全、可读的字符。
  local name="$1"
  if [[ ! "${name}" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
    printf '非法 workstream 名称: %s\n' "${name}" >&2
    printf '请使用小写字母、数字、点、下划线或短横线，例如 memory 或 tools-boundary。\n' >&2
    return 2
  fi
}

workstream_branch_name() {
  # LLM: map a workstream name to the default git branch name.
  # 给人看的解释：
  # 默认分支都放到 workstream/ 前缀下面，主线一眼能看出来源。
  printf 'workstream/%s\n' "$1"
}

workstream_target_path() {
  # LLM: map a workstream name to its worktree path.
  # 给人看的解释：
  # 同一个名字永远映射到同一个默认目录，方便记忆和脚本查找。
  local name="$1"
  local root
  root="$(workstream_default_root)"
  printf '%s/%s\n' "${root}" "${name}"
}

workstream_usage_root_note() {
  # LLM: print the current worktree root for help text.
  # 给人看的解释：
  # 帮助信息里显示实际目录，用户不用猜环境变量最后展开成哪里。
  printf '当前 worktree root: %s\n' "$(workstream_default_root)"
}
