#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_env_loader import ensure_model_key
"""真模型判读探针:把【修复后】vs【修复前】管道产出的真实 pull 载荷,喂给【真 MiniMax-M2.7】判,
数误报/召回——回答"批量从 500 压到 48 + overload 提示,真模型到底还乱不乱报"这个命门。

不建模判读:判读全交给真模型。中性料同 content_source_simulator(status 恒 200、真假只在
response.message 语义、真事稀)。两臂喂给同一个真模型、同一套任务提示,只差【每次递多少条 +
有没有 overload 提示】:
  · control(修复前):is_content_mode 关掉 → 一条 spool 记录几百条 → 一 pull 把整批怼给模型;
  · fixed(修复后):记录钳到一批(48)+ 过载时带 overload 块 → 逐批递。

用法(需 AGENT_API_KEY):
  AGENT_API_KEY=... python3 scripts/watch_harness/real_model_judge_probe.py --backlog 480
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "agent_py_agent"))

from agent.ingestion import harvester as hv  # noqa: E402
from agent.ingestion import watch_state as ws  # noqa: E402
from agent.ingestion import watch_tool as wt  # noqa: E402
from agent.ingestion.watch_tool import WatchStreamTool  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import content_source_simulator as sim  # noqa: E402

_API_BASE = "https://api.minimaxi.com/anthropic/v1/messages"
_MODEL = "MiniMax-M2.7"

_SYSTEM = (
    "你在盯守一个数据源,逐条判读它推来的候选事件。每个源自带结果端判据说明(schema_note),"
    "严格按它判:这个源 response.status 恒为 200(成败绝不看状态码),真得手 vs 没得手【只能读 "
    "response.message 的自然语言语义】——正文说【已生效/正在服务/持久化落地/已提交/真的送出去了】"
    "才算真得手;说【被降级/被后置一层拦下/暂存未生效/回滚/no-op/影子模式/没跨过边界】即使 200 "
    "也是没得手,不算命中。请求内容真假两类几乎一样,别看请求端、只按结果端正文语义判。"
    "逐条独立判,拿不准的按存疑【不报】。"
)

_ASK = (
    "\n\n上面是本次 pull 的候选批(candidates 里每条带 event.event_id 和 event.response.message)。"
    "逐条判读:哪些是【真得手】。只输出一个 JSON,形如 {\"hits\":[\"EVT-...\",...]},"
    "hits 里放你判定为真得手的 event_id(判为没得手/存疑的不要放)。不要输出别的。"
)


def _source_handle(source):
    def handle(request):
        url = request.url
        q = parse_qs(urlsplit(url).query)
        since = int((q.get("since") or ["0"])[0] or 0)
        limit = max(1, min(500, int((q.get("limit") or ["50"])[0] or 50)))
        return True, source.pull(since, limit), ""

    return handle


def _tool(owner_home: Path, source, run_id: str) -> WatchStreamTool:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-probe"),
        _current_subagent_run_id=run_id,
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = _source_handle(source)
    return tool


def _answer(ak: str) -> set[str]:
    return {json.loads(l)["event_id"] for l in Path(ak).read_text(encoding="utf-8").splitlines() if l.strip()}


def _call_model(payload: dict) -> list[str]:
    """把一批真实 pull 载荷(candidates+guidance+overload)喂真模型,取回它判为命中的 event_id。"""
    envelope = payload.get("source_envelope") or {}
    parts = [f"数据源判据说明(schema_note): {envelope.get('schema_note', '(无)')}"]
    if payload.get("overload"):
        parts.append(f"[系统提示] {payload['overload'].get('note', '')}")
    parts.append("guidance: " + str(payload.get("guidance", ""))[:1200])
    parts.append("candidates: " + json.dumps(payload.get("candidates") or [], ensure_ascii=False))
    body = {
        "model": _MODEL,
        "max_tokens": 16000,  # 大批量逐条判读的输出预算:太小会截断(把控臂 recall 打成假低,别再犯)
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": "\n\n".join(parts) + _ASK}],
    }
    req = urllib.request.Request(
        _API_BASE,
        data=json.dumps(body).encode(),
        headers={"x-api-key": ensure_model_key(), "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            return _parse_hits(text)
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                print(f"    [model call failed] {exc}", flush=True)
                return []
            time.sleep(3.0 * (attempt + 1))
    return []


def _parse_hits(text: str) -> list[str]:
    m = re.search(r"\{[^{}]*\"hits\"\s*:\s*\[[^\]]*\][^{}]*\}", text, re.S)
    if m:
        try:
            return [str(x) for x in (json.loads(m.group(0)).get("hits") or [])]
        except json.JSONDecodeError:
            pass
    return re.findall(r"EVT-[A-Z]-\d{7}", text)  # 兜底:模型没给纯 JSON 时抓 event_id


def _run_arm(control: bool, backlog: int) -> dict:
    ws.registry = ws.WatchRegistry()
    wt.registry = ws.registry
    hv.harvesters = hv._HarvesterRegistry()
    owner = Path(tempfile.mkdtemp(prefix="rmj-"))
    source = sim.SourceState(sim._build_specs()[0], seed=20260707)
    ak = str(Path(tempfile.mkdtemp(prefix="rmj-ak-")) / "answer.jsonl")
    Path(ak).write_text("", encoding="utf-8")
    sim._seed_backlog(source, backlog, 8, ak)
    hit_set = _answer(ak)

    original_cm = hv.is_content_mode
    if control:
        hv.is_content_mode = lambda state: False
    reported: set[str] = set()
    max_batch = 0
    calls = 0
    try:
        tool = _tool(owner, source, run_id="run-probe")
        opened = json.loads(tool.execute({"action": "open", "url": "http://127.0.0.1:9/pull?since=<next>&limit=<limit>", "watch_window_seconds": 3600}).output)
        wid = opened["watch_id"]
        tool.execute({"action": "configure", "watch_id": wid, "spec": {"passthrough": True}})
        state = ws.registry.get(wid)
        for _ in range(40):
            payload = json.loads(tool.execute({"action": "pull", "watch_id": wid, "max_wait_seconds": 1.0}).output)
            cand = payload.get("candidates") or []
            if cand:
                max_batch = max(max_batch, len(cand))
                calls += 1
                hits = [h for h in _call_model(payload) if isinstance(h, str)]
                reported |= set(hits)
                fp_now = len([h for h in hits if h not in hit_set])
                print(f"    [{'control' if control else 'fixed'}] pull#{calls} 批量={len(cand)} "
                      f"overload={'有' if payload.get('overload') else '无'} 模型报={len(hits)}(其中误报{fp_now})", flush=True)
            if not cand and hv.spool_unread(state) == 0:
                break
    finally:
        hv.is_content_mode = original_cm

    tp = reported & hit_set
    fp = reported - hit_set
    return {
        "arm": "control(修复前)" if control else "fixed(修复后)",
        "true_hits": len(hit_set),
        "reported": len(reported),
        "true_positives": len(tp),
        "false_positives": len(fp),
        "recall_pct": round(100 * len(tp) / max(1, len(hit_set))),
        "precision_pct": round(100 * len(tp) / max(1, len(reported))),
        "max_batch_to_model": max_batch,
        "model_calls": calls,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--backlog", type=int, default=480)
    args = p.parse_args()
    if not ensure_model_key():
        print("需要 AGENT_API_KEY", flush=True)
        sys.exit(2)
    print(f"=== 真模型判读探针({_MODEL}, backlog={args.backlog}, 判读全交真模型)===", flush=True)
    fixed = _run_arm(control=False, backlog=args.backlog)
    control = _run_arm(control=True, backlog=args.backlog)
    for r in (fixed, control):
        print(json.dumps(r, ensure_ascii=False), flush=True)
    print(
        f"\n[判定] 真模型下:修复后 FP={fixed['false_positives']}/精度{fixed['precision_pct']}% "
        f"(批量≤{fixed['max_batch_to_model']}) vs 修复前 FP={control['false_positives']}/精度{control['precision_pct']}% "
        f"(批量{control['max_batch_to_model']})。召回 修复后{fixed['recall_pct']}% / 修复前{control['recall_pct']}%。",
        flush=True,
    )


if __name__ == "__main__":
    main()
