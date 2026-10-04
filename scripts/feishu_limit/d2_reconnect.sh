#!/bin/bash
# D2 adapter 断线重连极限测试：kill -9 真实飞书 adapter → systemd 自愈拉起 → 通道恢复。
# 验证：①进程级自愈（Restart=always）②adapter 重新建立长连接 ③gateway 链路不受影响。
set -u
PY=/root/my-agent-src/.venv/bin/python3
PASS=0
FAIL=0

say() { echo "[d2] $*"; }
check() {
  if [ "$2" = "true" ]; then say "PASS $1"; PASS=$((PASS+1)); else say "FAIL $1"; FAIL=$((FAIL+1)); fi
}

# ---- G3 本机凭据（G2b 前置）：复用产品入口 gateway_script_headers()；token 只进变量，不回显、不落盘 ----
# 取不到时按 G2b 开关分流：开关关（默认）照常发（产品入口会打印降级 warning），开关开则停下说明。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY_BIN="${MY_AGENT_PY:-python3}"
GW_TOKEN=""
GW_CRED_RC=0
GW_TOKEN=$(PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PY_BIN" - <<'PY'
import sys

try:
    from agent_py_agent.cli.gateway_client_headers import gateway_script_headers
    from agent_py_agent.agent.gateway_parts.local_client_token import LocalClientCredentialError
except Exception as exc:  # 环境不满足：无法加载产品凭据入口。
    sys.stderr.write("credential_env_error:%s\n" % type(exc).__name__)
    sys.exit(3)

try:
    sys.stdout.write(gateway_script_headers().get("X-Gateway-Token", ""))
except LocalClientCredentialError as exc:  # 开关开且凭据不可用：产品入口拒绝。
    sys.stderr.write("credential_unavailable:%s\n" % exc.reason_code)
    sys.exit(2)
except Exception as exc:
    sys.stderr.write("credential_env_error:%s\n" % type(exc).__name__)
    sys.exit(3)
PY
) || GW_CRED_RC=$?
if [ "$GW_CRED_RC" -eq 2 ]; then
  say "FATAL 本机客户端凭据不可用且 G2b 开关已开启：停止测试；请先启动一次 Gateway 生成凭据后重试。"
  exit 1
elif [ "$GW_CRED_RC" -ne 0 ]; then
  say "FATAL 无法获取本机客户端凭据（rc=$GW_CRED_RC，环境或配置问题）：停止测试；可用 MY_AGENT_PY 指定能 import agent_py_agent 的 python 后重试。"
  exit 1
fi
# 降级时数组为空；空数组展开用 ${GW_AUTH_HEADER[@]+"${GW_AUTH_HEADER[@]}"}（bash 3.2/4.x 兼容，set -u 下直接展开空数组会报 unbound）。
GW_AUTH_HEADER=()
if [ -n "$GW_TOKEN" ]; then
  GW_AUTH_HEADER=(-H "X-Gateway-Token: $GW_TOKEN")
else
  say "提示 本机客户端凭据不可用（G2b 开关关闭的降级口径）：本次不带凭据继续发送。"
fi

SVC=my-agent-feishu
OLD_PID=$(systemctl show $SVC -p ExecMainPID --value)
ACTIVE_BEFORE=$(systemctl is-active $SVC)
say "=== D2 断线重连 ==="
say "old_pid=$OLD_PID active=$ACTIVE_BEFORE"

check "服务原先是 active" "$([ "$ACTIVE_BEFORE" = "active" ] && echo true || echo false)"
[ -n "$OLD_PID" ] && [ "$OLD_PID" != "0" ] || { say "FATAL 无旧 pid"; exit 1; }

# 硬杀：模拟进程崩溃（kill -9 无 SIGTERM 善后）
kill -9 "$OLD_PID"
say "killed -9 $OLD_PID, waiting for systemd restart (RestartSec=10)..."
sleep 20

NEW_PID=$(systemctl show $SVC -p ExecMainPID --value)
ACTIVE=$(systemctl is-active $SVC)
say "new_pid=$NEW_PID active=$ACTIVE"

check "systemd 自动拉起新进程" "$([ -n "$NEW_PID" ] && [ "$NEW_PID" != "0" ] && [ "$NEW_PID" != "$OLD_PID" ] && echo true || echo false)"
check "服务重新 active" "$([ "$ACTIVE" = "active" ] && echo true || echo false)"

# adapter 重新建立飞书长连接（启动日志证据）
WS_LOG=$(journalctl -u $SVC --since "30 seconds ago" --no-pager 2>/dev/null | grep -c "长连接 WS 模式\|已启动")
check "adapter 重建长连接(启动日志)" "$([ "$WS_LOG" -ge 1 ] && echo true || echo false)"

# 通道进程证据（systemd 长连接模式不写 daemon pid 文件，adapter status 不适用；
# 用进程命令行 + 启动日志两个独立证据验证通道真正在跑）
RUNNING_LINE=$(ps aux | grep "[a]dapter start" | grep -c "feishu")
check "adapter 进程在跑(feishu)" "$([ "$RUNNING_LINE" -ge 1 ] && echo true || echo false)"

# gateway 链路不受影响：冒烟 ask
RID=$(curl -s -X POST http://127.0.0.1:8420/ask -H 'Content-Type: application/json' \
  -H 'X-User-Id: limittest-d2' -H 'X-Channel: feishu' \
  ${GW_AUTH_HEADER[@]+"${GW_AUTH_HEADER[@]}"} \
  -d '{"goal": "用一句话回复：1+1 等于几？"}' | python3 -c "import json,sys; print(json.load(sys.stdin).get('request_id',''))")
check "gateway 冒烟请求排队" "$([ -n "$RID" ] && echo true || echo false)"
if [ -n "$RID" ]; then
  waited=0; OUT=""
  while [ "$waited" -lt 150 ]; do
    OUT=$(curl -s http://127.0.0.1:8420/result/$RID ${GW_AUTH_HEADER[@]+"${GW_AUTH_HEADER[@]}"} 2>/dev/null)
    if echo "$OUT" | grep -q '"status": "done"\|"status": "failed"'; then break; fi
    sleep 3; waited=$((waited+3))
  done
  CONT=$(echo "$OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('channel_delivery',{}).get('content',''))")
  say "冒烟回复=$CONT"
  check "gateway 冒烟完成" "$([ -n "$CONT" ] && echo true || echo false)"
fi

say "=== D2 结果: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" -eq 0 ]
