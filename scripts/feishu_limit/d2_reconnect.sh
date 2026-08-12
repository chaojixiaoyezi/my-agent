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
  -d '{"goal": "用一句话回复：1+1 等于几？"}' | python3 -c "import json,sys; print(json.load(sys.stdin).get('request_id',''))")
check "gateway 冒烟请求排队" "$([ -n "$RID" ] && echo true || echo false)"
if [ -n "$RID" ]; then
  waited=0; OUT=""
  while [ "$waited" -lt 150 ]; do
    OUT=$(curl -s http://127.0.0.1:8420/result/$RID 2>/dev/null)
    if echo "$OUT" | grep -q '"status": "done"\|"status": "failed"'; then break; fi
    sleep 3; waited=$((waited+3))
  done
  CONT=$(echo "$OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('channel_delivery',{}).get('content',''))")
  say "冒烟回复=$CONT"
  check "gateway 冒烟完成" "$([ -n "$CONT" ] && echo true || echo false)"
fi

say "=== D2 结果: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" -eq 0 ]
