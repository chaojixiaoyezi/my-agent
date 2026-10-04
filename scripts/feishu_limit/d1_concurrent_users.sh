#!/bin/bash
# D1 双用户并发极限测试：gateway 灌消息法（X-User-Id）验证 per-user 隔离。
# 场景：两个用户并发提交任务 + 交叉记忆隔离验证。
# 用法：ssh 测试机 上跑（gateway 在 测试机 8420）。本机运行：bash scripts/feishu_limit/d1_concurrent_users.sh
set -u
GW="http://127.0.0.1:8420"
USER_A="limittest-a-$(date +%s)"
USER_B="limittest-b-$(date +%s)"
PASS=0
FAIL=0

say() { echo "[d1] $*"; }
check() { # check <name> <cond>
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

submit() { # submit <user> <goal> -> request_id (stdout)
  curl -s -X POST "$GW/ask" -H "Content-Type: application/json" \
    -H "X-User-Id: $1" -H "X-Channel: feishu" \
    ${GW_AUTH_HEADER[@]+"${GW_AUTH_HEADER[@]}"} \
    -d "{\"goal\": $(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$2")}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('request_id',''))"
}

wait_result() { # wait_result <request_id> <timeout_seconds> -> result json (stdout, empty if timeout)
  local rid="$1" t="$2" waited=0
  while [ "$waited" -lt "$t" ]; do
    local out
    out=$(curl -s "$GW/result/$rid" ${GW_AUTH_HEADER[@]+"${GW_AUTH_HEADER[@]}"} 2>/dev/null)
    if echo "$out" | grep -q '"status": "done"\|"status": "failed"'; then
      echo "$out"; return 0
    fi
    sleep 3; waited=$((waited+3))
  done
  say "TIMEOUT waiting $rid after ${t}s"
  echo ""
}

say "=== D1 双用户并发 ==="
say "user_a=$USER_A user_b=$USER_B"

# 并发提交：a 自我介绍、b 算术题（同时发出，互不等待）
submit "$USER_A" "请用一句话回答：你叫什么名字？" > /tmp/d1_rid_a.txt &
submit "$USER_B" "请用一句话回答：7 乘以 8 等于几？" > /tmp/d1_rid_b.txt &
wait
RID_A=$(cat /tmp/d1_rid_a.txt)
RID_B=$(cat /tmp/d1_rid_b.txt)
check "双请求都排队" "$([ -n "$RID_A" ] && [ -n "$RID_B" ] && echo true || echo false)"

RES_A=$(wait_result "$RID_A" 180)
RES_B=$(wait_result "$RID_B" 180)
check "a 请求完成" "$(echo "$RES_A" | grep -q '"status": "done"' && echo true || echo false)"
check "b 请求完成" "$(echo "$RES_B" | grep -q '"status": "done"' && echo true || echo false)"

CONT_A=$(echo "$RES_A" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('channel_delivery',{}).get('content',''))")
CONT_B=$(echo "$RES_B" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('channel_delivery',{}).get('content',''))")
say "a_reply=$CONT_A"
say "b_reply=$CONT_B"

check "a 回复是自己的名字" "$(echo "$CONT_A" | grep -qi 'my-agent\|myagent' && echo true || echo false)"
check "b 回复是 56" "$(echo "$CONT_B" | grep -q '56' && echo true || echo false)"
check "回复不串台(a 不含 b 的内容)" "$(echo "$CONT_A" | grep -q '56' && echo false || echo true)"

# 交叉记忆隔离：a 记住暗号，b 问暗号应答不上来
submit "$USER_A" "请记住一条事实：柠檬A的暗号是苹果。" > /tmp/d1_rid_m.txt &
wait
RID_M=$(cat /tmp/d1_rid_m.txt)
wait_result "$RID_M" 120 >/dev/null

submit "$USER_B" "用一句话回答：柠檬A的暗号是什么？如果不知道就说不知道。" > /tmp/d1_rid_q.txt &
wait
RID_Q=$(cat /tmp/d1_rid_q.txt)
RES_Q=$(wait_result "$RID_Q" 150)
CONT_Q=$(echo "$RES_Q" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('channel_delivery',{}).get('content',''))")
say "b 问暗号回复=$CONT_Q"
check "b 不知道 a 的暗号(记忆隔离)" "$(echo "$CONT_Q" | grep -q '苹果' && echo false || echo true)"

submit "$USER_A" "用一句话回答：柠檬A的暗号是什么？" > /tmp/d1_rid_q2.txt &
wait
RID_Q2=$(cat /tmp/d1_rid_q2.txt)
RES_Q2=$(wait_result "$RID_Q2" 150)
CONT_Q2=$(echo "$RES_Q2" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('channel_delivery',{}).get('content',''))")
say "a 问暗号回复=$CONT_Q2"
check "a 记得自己的暗号" "$(echo "$CONT_Q2" | grep -q '苹果' && echo true || echo false)"

# owner home 隔离（文件侧验证；真实路径由 测试机 实测得出：
# /root/.my-agent/owners/providers/feishu/users/<user-id>）
HOME_A="/root/.my-agent/owners/providers/feishu/users/$USER_A"
HOME_B="/root/.my-agent/owners/providers/feishu/users/$USER_B"
check "a 的 owner home 存在" "$([ -d "$HOME_A" ] && echo true || echo false)"
check "b 的 owner home 存在" "$([ -d "$HOME_B" ] && echo true || echo false)"

say "=== D1 结果: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" -eq 0 ]
