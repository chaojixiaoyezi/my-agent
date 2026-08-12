#!/bin/bash
# D4 大内容极限测试（可自动化部分）：
#   ① 灌消息法：2MB 大文本 goal → gateway/adapter 链路承受力 + 任务完成
#   ② 大回复输出：任务产出大文本 → channel_delivery 投递不崩
# 真实飞书附件（用户在飞书客户端发大文件/图片给机器人）需人工配合，脚本尾部提示。
set -u
GW="http://127.0.0.1:8420"
USER="limittest-d4-$(date +%s)"
PASS=0
FAIL=0

say() { echo "[d4] $*"; }
check() {
  if [ "$2" = "true" ]; then say "PASS $1"; PASS=$((PASS+1)); else say "FAIL $1"; FAIL=$((FAIL+1)); fi
}

# ---- 构造两档大文本 goal（含锚点标记便于验证完整性） ----
# 档① 100KB：真正上下文内的大负载（中文≈3-4万token，模型可完整读入，应 done + 锚点完整）
# 档② 518KB+：超上下文（真机实测 gateway 快速失败 CONVERSATION_PERSISTENCE_UNAVAILABLE，
#      错误明确不挂死——产品边界保护行为，验证"优雅终止"）
python3 - <<'EOF'
import json
anchor = "大文本锚点-"
def build(n):
    big = (anchor + "这是一段用于极限测试的长文本内容。" * 20 + "\n") * n
    return big
for name, n in (("100k", 100), ("over", 500)):
    big = build(n)
    goal = "请把下面大文本的前50个字符复制到回复里（不要总结）：\n\n" + big
    open(f"/tmp/d4_{name}_goal.json", "w").write(json.dumps({"goal": goal}, ensure_ascii=False))
    print(f"{name}_goal_bytes=", len(goal.encode("utf-8")))
EOF

say "=== D4 大内容 ==="
say "user=$USER"

# 档① 100KB 上下文内：任务应完成 + 锚点完整
RID=$(curl -s -X POST "$GW/ask" -H 'Content-Type: application/json' \
  -H "X-User-Id: $USER" -H 'X-Channel: feishu' \
  --data-binary @/tmp/d4_100k_goal.json | python3 -c "import json,sys; print(json.load(sys.stdin).get('request_id',''))")
check "100KB 大 goal 请求排队" "$([ -n "$RID" ] && echo true || echo false)"
say "rid=$RID"

if [ -n "$RID" ]; then
  waited=0; OUT=""
  while [ "$waited" -lt 300 ]; do
    OUT=$(curl -s "$GW/result/$RID" 2>/dev/null)
    if echo "$OUT" | grep -q '"status": "done"\|"status": "failed"'; then break; fi
    sleep 5; waited=$((waited+5))
  done
  ST=$(echo "$OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null)
  say "100KB 大任务 status=$ST waited=${waited}s"
  check "100KB 大 goal 任务完成" "$([ "$ST" = "done" ] && echo true || echo false)"
  CONT=$(echo "$OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('channel_delivery',{}).get('content',''))" 2>/dev/null)
  say "回复前120字: ${CONT:0:120}"
  check "回复含大文本锚点(未截断丢失)" "$(echo "$CONT" | grep -q '大文本锚点-' && echo true || echo false)"
fi

# 档② 超上下文：行为观察（终止态即可，验证不挂死 + 失败原因可读）
RID3=$(curl -s -X POST "$GW/ask" -H 'Content-Type: application/json' \
  -H "X-User-Id: $USER" -H 'X-Channel: feishu' \
  --data-binary @/tmp/d4_over_goal.json | python3 -c "import json,sys; print(json.load(sys.stdin).get('request_id',''))")
check "超上下文 goal 请求排队" "$([ -n "$RID3" ] && echo true || echo false)"
if [ -n "$RID3" ]; then
  waited=0; OUT3=""
  while [ "$waited" -lt 300 ]; do
    OUT3=$(curl -s "$GW/result/$RID3" 2>/dev/null)
    if echo "$OUT3" | grep -q '"status": "done"\|"status": "failed"'; then break; fi
    sleep 5; waited=$((waited+5))
  done
  ST3=$(echo "$OUT3" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null)
  ERR3=$(echo "$OUT3" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('error','')[:100])" 2>/dev/null)
  say "超上下文 status=$ST3 waited=${waited}s error=$ERR3"
  check "超上下文优雅终止(不挂死)" "$([ "$ST3" = "done" ] || [ "$ST3" = "failed" ] && echo true || echo false)"
  check "失败原因可读(非挂死非空白)" "$([ -n "$ERR3" ] && echo true || echo false)"
fi

# ---- 大回复输出：让 agent 产出大文本（真实飞书投递侧承载） ----
RID2=$(curl -s -X POST "$GW/ask" -H 'Content-Type: application/json' \
  -H "X-User-Id: $USER" -H 'X-Channel: feishu' \
  -d '{"goal": "请输出一段约 500 字的中文说明文，主题：飞书机器人使用说明。不要用任何工具。"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('request_id',''))")
check "大回复请求排队" "$([ -n "$RID2" ] && echo true || echo false)"
if [ -n "$RID2" ]; then
  waited=0; OUT2=""
  while [ "$waited" -lt 240 ]; do
    OUT2=$(curl -s "$GW/result/$RID2" 2>/dev/null)
    if echo "$OUT2" | grep -q '"status": "done"\|"status": "failed"'; then break; fi
    sleep 5; waited=$((waited+5))
  done
  ST2=$(echo "$OUT2" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null)
  CONT2=$(echo "$OUT2" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('channel_delivery',{}).get('content',''))" 2>/dev/null)
  say "大回复 status=$ST2 长度=${#CONT2}"
  check "大回复任务完成" "$([ "$ST2" = "done" ] && echo true || echo false)"
  check "回复长度 >= 300 字(未被截断)" "$([ "${#CONT2}" -ge 300 ] && echo true || echo false)"
fi

say "=== D4 结果: PASS=$PASS FAIL=$FAIL ==="
say "注: 真实飞书附件(用户在飞书客户端发大文件/图片给机器人)需人工配合,未覆盖。"
[ "$FAIL" -eq 0 ]
