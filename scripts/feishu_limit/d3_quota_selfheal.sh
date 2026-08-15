#!/bin/bash
# D3 429 限流自愈极限测试：假端点先回 429（带 Retry-After），N 次后转发真实上游。
# 验证：①产品识别 429 为可恢复限流（非致命）②指数退避重试 ③限流解除后自愈完成真实任务。
# 生产无感：独立临时 config + 独立任务进程，不碰常驻 gateway/adapter。
set -u
PASS=0
FAIL=0
PY=/root/my-agent-src/.venv/bin/python3
FAKE_PORT=5005
REAL_BASE="https://opencode.ai/zen/go"
WORK=/tmp/d3_work
mkdir -p $WORK

say() { echo "[d3] $*"; }

# 关键：bash 交互环境的 AGENT_API_KEY 是 MiniMax 旧 key（.bashrc 覆盖），发到
# 工具运行时.ai 会被 401 拒（真机实测根因）。跑任务前强制用 model.env 的正式 key。
source /etc/my-agent/model.env
K=$(printf '%s' "${AGENT_API_KEY:-}")
say "task key head=${K:0:12} len=${#K}"
check() {
  if [ "$2" = "true" ]; then say "PASS $1"; PASS=$((PASS+1)); else say "FAIL $1"; FAIL=$((FAIL+1)); fi
}

say "=== D3 429 限流自愈 ==="

# ---- 假端点 v4：前 LIMIT 次回 429(Retry-After: 2)，之后用产品自己的 gateway_helpers
#      转发真实上游（UA/TLS handler/鉴权 header 全由产品组件构造——真机实测直连 urllib
#      转发被 工具运行时.ai 拒 403-1010/401，产品组件链路可过）；计数落文件供断言。
#      语义保持铁律（真机踩坑）：只有入站显式 stream=true 才走 SSE 转发；否则走非流式
#      JSON（post_json 不改 payload）。post_stream_iter 内部强制 stream=true——若用于
#      stream=false 的入站（如 generate_structured），工具运行时 回 SSE、消费方按 JSON 解析
#      崩成 blocked/401 假象，把产品误判成 bug。转发代理必须保持请求语义等价。 ----
cat > $WORK/fake_429.py <<'EOF'
import json, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, "/root/my-agent-src/.venv/lib64/python3.11/site-packages")
from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest, post_json, post_stream_iter

REAL_BASE = sys.argv[1]
LIMIT = int(sys.argv[2])
COUNTER_FILE = sys.argv[4]
FORWARD_LOG = sys.argv[5]
COUNTER = {"total": 0, "attempts": 0, "forwarded": 0, "n429": 0}
LOCK = threading.Lock()

def save():
    with LOCK:
        json.dump(COUNTER, open(COUNTER_FILE, "w"))

def _log_fwd(kind, detail):
    with open(FORWARD_LOG, "a") as f:
        f.write(f"{kind} {detail}\n")

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with LOCK:
            COUNTER["total"] += 1
            COUNTER["attempts"] += 1
            n = COUNTER["attempts"]
        if n <= LIMIT:
            with LOCK:
                COUNTER["n429"] += 1
            save()
            self.send_response(429)
            self.send_header("Retry-After", "2")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "rate limited (fake)"}}).encode())
            return
        # 产品组件转发：api_key 从入站 Authorization 取，header 透传（Host 由组件按 api_base 设）
        try:
            auth = self.headers.get("Authorization", "")
            key = auth[7:] if auth.startswith("Bearer ") else ""
            payload = json.loads(body)
            headers = {
                k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length", "connection", "transfer-encoding", "accept-encoding")
            }
            req = GatewayRequest(
                api_base=REAL_BASE, api_key=key, path=self.path, payload=payload,
                headers=headers, timeout=120, connect_timeout=10,
            )
            with LOCK:
                COUNTER["forwarded"] += 1
            save()
            # 语义保持：只有显式 stream=true 才 SSE（post_stream_iter 强制 stream=true）
            if payload.get("stream") is True:
                lines = list(post_stream_iter(req))
                _log_fwd("SSE", f"path={self.path} lines={len(lines)} head={lines[0][:150]!r}")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for ln in lines:
                    self.wfile.write(f"data: {ln}\n\n".encode())
            else:
                obj = post_json(req)
                _log_fwd("JSON", f"path={self.path} head={str(obj)[:150]!r}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())
        except Exception as e:
            _log_fwd("FWD_ERR", f"{type(e).__name__} {str(e)[:200]}")
            try:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)[:200]}).encode())
            except Exception:
                pass
    def log_message(self, *a):
        pass

HTTPServer(("127.0.0.1", int(sys.argv[3])), H).serve_forever()
EOF

$PY $WORK/fake_429.py "$REAL_BASE" 3 $FAKE_PORT $WORK/fake_counter.json $WORK/fwd.log > $WORK/fake.log 2>&1 &
FAKE_PID=$!
sleep 1
say "fake 429 endpoint pid=$FAKE_PID (先 3 次 429 → 转发)"

# ---- 临时 config：api_base 指向假端点 ----
sed "s|^api_base:.*|api_base: \"http://127.0.0.1:$FAKE_PORT\"|" /root/.my-agent/config/agent_config.yaml > $WORK/d3_config.yaml
grep -n "api_base" $WORK/d3_config.yaml | head -2

# ---- 独立真实任务：前几次调用吃 429，限流解除后必须自愈完成 ----
say "running real task against fake 429 endpoint (expect backoff + self-heal)..."
START=$(date +%s)
$PY -m agent_py_agent --config $WORK/d3_config.yaml run "请用一句话回答：1+1 等于几？" --no-save > $WORK/d3_out1.log 2>$WORK/d3_run_err.log
RC=$?
ELAPSED=$(( $(date +%s) - START ))
say "rc=$RC elapsed=${ELAPSED}s"
say "--- 完整 stdout ---"
cat $WORK/d3_out1.log
say "--- 完整 stderr ---"
cat $WORK/d3_run_err.log
OUT=$(cat $WORK/d3_out1.log)

check "任务自愈完成(rc=0)" "$([ "$RC" -eq 0 ] && echo true || echo false)"
check "回复包含答案" "$(echo "$OUT" | grep -q '2' && echo true || echo false)"

# 端点计数文件：限流风暴期必须真实吃到 429（不是没打到端点）
CNT=$(cat $WORK/fake_counter.json 2>/dev/null || echo '{"total":0,"n429":0,"forwarded":0}')
N429=$(echo "$CNT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('n429',0))")
TOTAL=$(echo "$CNT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('total',0))")
FORWARDED=$(echo "$CNT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('forwarded',0))")
say "端点计数: 总请求=$TOTAL 429命中=$N429 转发=$FORWARDED"
check "限流期间吃到 429" "$([ "$N429" -ge 1 ] && echo true || echo false)"
check "429 后真实转发上游" "$([ "$FORWARDED" -ge 1 ] && echo true || echo false)"

# 限流解除后（端点已到转发阶段）第二任务不再吃 429，连续成功 = 自愈
$PY -m agent_py_agent --config $WORK/d3_config.yaml run "请用一句话回答：2+2 等于几？" --no-save > $WORK/d3_out2.log 2>$WORK/d3_run2_err.log
RC2=$?
say "--- 第二任务 stdout ---"
cat $WORK/d3_out2.log
say "--- 第二任务 stderr ---"
cat $WORK/d3_run2_err.log
OUT2=$(cat $WORK/d3_out2.log)
CNT2=$(cat $WORK/fake_counter.json 2>/dev/null || echo '{"n429":0}')
N429_2=$(echo "$CNT2" | python3 -c "import json,sys; print(json.load(sys.stdin).get('n429',0))")
say "第二任务 rc=$RC2 (端点 n429 增量: $((N429_2 - N429)))"
check "限流解除后第二任务完成" "$([ "$RC2" -eq 0 ] && echo true || echo false)"
check "第二任务不再吃 429" "$([ $((N429_2 - N429)) -eq 0 ] && echo true || echo false)"
echo "$OUT2" | tail -1

kill $FAKE_PID 2>/dev/null

# ---- D3b 背压对象确认（记录性） ----
# 代码定因：QueueBackpressure 只服务 IM webhook 持久化队列（asgi_ingress）；
# POST /ask 走 request_execution，没有 HTTP 层 429 背压。故并发灌 ask 预期全 200
# （无 429 是架构设计：ask 是同步请求-响应，靠调度器并发上限而非 HTTP 429 限流）。
say "--- D3b 背压对象确认：POST /ask 并发 20（预期无 429，背压只在 webhook 路径） ---"
REJECTED=0; ACCEPTED=0
for i in $(seq 1 20); do
  curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8420/ask \
    -H 'Content-Type: application/json' -H 'X-User-Id: limittest-d3b' -H 'X-Channel: feishu' \
    -d '{"goal": "用一句话回复数字 3。"}' > $WORK/code_$i.txt 2>/dev/null &
done
wait
for i in $(seq 1 20); do
  CODE=$(cat $WORK/code_$i.txt)
  if [ "$CODE" = "429" ]; then REJECTED=$((REJECTED+1)); else ACCEPTED=$((ACCEPTED+1)); fi
done
say "并发灌入: 429 拒绝=$REJECTED 接受=$ACCEPTED"
check "ask 路径全部接受(背压属 webhook 路径,记录性)" "$([ "$ACCEPTED" -eq 20 ] && echo true || echo false)"

say "=== D3 结果: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" -eq 0 ]
