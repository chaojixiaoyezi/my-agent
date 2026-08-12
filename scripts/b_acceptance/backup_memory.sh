#!/bin/bash
# B项验收:备份指定 owner 的 memory 关键文件(验收后恢复,不污染真机数据)
set -euo pipefail
OWNER="$1"   # 形如 owners/local/main 或 owners/providers/feishu/users/ou_xxx
STAMP="${2:-$(date +%Y%m%d-%H%M%S)}"
BASE="/root/.my-agent/${OWNER}"
DEST="/root/.my-agent/backups/b_acceptance/${STAMP}"
mkdir -p "${DEST}"
for rel in memory/long_term/memory.jsonl memory/candidates.jsonl memory/curator/state.json memory/daily memory/curator/runs; do
  if [ -e "${BASE}/${rel}" ]; then
    mkdir -p "${DEST}/$(dirname "${rel}")"
    cp -a "${BASE}/${rel}" "${DEST}/${rel}"
  fi
done
echo "backed up ${OWNER} -> ${DEST}"
