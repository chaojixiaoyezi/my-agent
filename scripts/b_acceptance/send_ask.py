"""B项验收辅助:POST /ask 灌真实消息到网关并轮询完成。

用法: python3 send_ask.py <prompt文件> [X-User-Id] [X-Channel] [--wait]
无 X-User-Id → admin/chat(主 owner local/main);带 X-User-Id → 该渠道用户 scoped owner。
"""
import json
import sys
import time
import urllib.error
import urllib.request

args = [a for a in sys.argv[1:] if a != "--wait"]
wait = "--wait" in sys.argv
prompt_file = args[0]
user_id = args[1] if len(args) > 1 else ""
channel = args[2] if len(args) > 2 else "chat"

with open(prompt_file, encoding="utf-8") as prompt_handle:
    prompt = prompt_handle.read()
payload = {"kind": "ask", "prompt": prompt, "metadata": {"channel": channel, "user_id": user_id or "admin"}}
headers = {"Content-Type": "application/json"}
if user_id:
    headers["X-User-Id"] = user_id
    headers["X-Channel"] = channel

req = urllib.request.Request(
    "http://127.0.0.1:8420/ask",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers=headers,
)
with urllib.request.urlopen(req, timeout=30) as resp:
    result = json.loads(resp.read().decode("utf-8", "replace"))
request_id = result.get("request_id", "")
print(f"submitted request_id={request_id} status={result.get('status')} user={user_id or 'admin'}", flush=True)
if not wait or not request_id:
    sys.exit(0)

for attempt in range(90):
    time.sleep(10)
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:8420/result/{request_id}", timeout=10)
        body = json.loads(r.read().decode("utf-8", "replace"))
        status = body.get("status") or body.get("final_status") or "?"
        out = (body.get("output") or body.get("response") or body.get("text") or "")
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)[:3000]
        print(f"[{attempt}] status={status}", flush=True)
        if status in ("processing", "queued", "pending", "running"):
            continue
        print(f"---response---\n{text}\n---end---", flush=True)
        sys.exit(0)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"[{attempt}] processing...", flush=True)
            continue
        print(f"[{attempt}] HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}", flush=True)
    except Exception as exc:
        print(f"[{attempt}] {type(exc).__name__}: {exc}", flush=True)
        time.sleep(5)
print("TIMEOUT waiting for result", flush=True)
