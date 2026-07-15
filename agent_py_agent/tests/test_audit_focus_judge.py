"""/audit 无历史聚焦判读回填(治长盯守 clear 惯性沟)的机制单测。

真机根因:判读塞进长命子代理累积会话,连判 ~25 条 clear 后把明显真事也顺手判 clear。修法=每批
候选另起无历史聚焦模型调用判读、把权威判据回填 pull 载荷。本单测用假后端钉死【回填/解析/降级/
只走保证档】的结构契约(真模型召回由 H 全链路台真调 M2.7 验),不碰模型质量维度。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.common.audit_activation import AUDIT_ATTR
from agent.ingestion.audit_judge import focus_judge_candidates
from agent.ingestion.watch_tool import _attach_focus_verdicts


class _FakeBackend:
    """假后端:按 ack_id 回定死的 verdict,记录收到的 system/messages(验证无历史+领域注入)。"""

    def __init__(self, verdicts: dict[str, str]) -> None:
        self.verdicts = verdicts
        self.seen_system = ""
        self.seen_messages: list = []

    def generate(self, prompt, on_chunk=None, tools=None, messages=None):
        self.seen_system = prompt
        self.seen_messages = messages or []
        rows = [{"ack_id": a, "verdict": v, "evidence": f"ev-{a}"} for a, v in self.verdicts.items()]
        return SimpleNamespace(text=json.dumps({"verdicts": rows}, ensure_ascii=False))


def _rows(acks: list[str]) -> list[dict]:
    return [{"ack_id": a, "event": {"request": f"req-{a}", "response": f"resp-{a}"}} for a in acks]


def test_focus_judge_parses_and_keys_by_ack():
    backend = _FakeBackend({"5:0": "hit", "5:1": "clear"})
    agent = SimpleNamespace(backend=backend)
    out = focus_judge_candidates(agent, "安全日志:读请求+响应判", _rows(["5:0", "5:1"]))
    assert out == {"5:0": {"verdict": "hit", "evidence": "ev-5:0"}, "5:1": {"verdict": "clear", "evidence": "ev-5:1"}}


def test_focus_judge_is_history_free_single_batch_call():
    # 关键:聚焦判读必须是【一次无历史】调用——只带本批候选,不带任何往轮对话(治惯性沟的根)。
    backend = _FakeBackend({"9:0": "hit"})
    agent = SimpleNamespace(backend=backend)
    focus_judge_candidates(agent, "note", _rows(["9:0"]))
    assert len(backend.seen_messages) == 1 and backend.seen_messages[0]["role"] == "user"
    assert "9:0" in backend.seen_messages[0]["content"]
    assert "note" in backend.seen_system  # 领域判据从源 note 注入,代码不内置领域词


def test_focus_judge_uses_provider_native_schema_when_available():
    class _StructuredBackend:
        def __init__(self):
            self.schema = None

        def generate_structured(self, prompt, *, response_schema, messages=None):
            del prompt
            self.schema = response_schema
            cand = json.loads(messages[0]["content"].split("candidates: ", 1)[1])
            rows = [
                {"ack_id": row["ack_id"], "verdict": "clear", "evidence": "blocked"}
                for row in cand
            ]
            return SimpleNamespace(text=json.dumps({"verdicts": rows}))

    backend = _StructuredBackend()
    out = focus_judge_candidates(SimpleNamespace(backend=backend), "note", _rows(["1:0"]))

    assert out["1:0"]["verdict"] == "clear"
    assert backend.schema["properties"]["verdicts"]["items"]["additionalProperties"] is False


def test_focus_judge_splits_truncated_batch_and_keeps_resolved_rows():
    class _LengthLimitedBackend:
        def __init__(self):
            self.batch_sizes = []

        def generate(self, prompt, on_chunk=None, tools=None, messages=None):
            del prompt, on_chunk, tools
            cand = json.loads(messages[0]["content"].split("candidates: ", 1)[1])
            self.batch_sizes.append(len(cand))
            if len(cand) > 4:
                return SimpleNamespace(text='{"verdicts":[{"ack_id":"cut')
            rows = [
                {"ack_id": row["ack_id"], "verdict": "hit", "evidence": "done"}
                for row in cand
            ]
            return SimpleNamespace(text=json.dumps({"verdicts": rows}))

    backend = _LengthLimitedBackend()
    out = focus_judge_candidates(
        SimpleNamespace(backend=backend),
        "note",
        _rows([f"1:{index}" for index in range(9)]),
    )

    assert len(out) == 9
    assert backend.batch_sizes == [9, 4, 5, 2, 3]


def test_focus_judge_no_backend_returns_empty():
    # 无模型后端(测试台桩 agent / 未装配)→ 空,回退子代理自判,绝不抛、绝不拦路。
    assert focus_judge_candidates(SimpleNamespace(), "note", _rows(["1:0"])) == {}
    assert focus_judge_candidates(SimpleNamespace(backend=None), "note", _rows(["1:0"])) == {}


def test_focus_judge_backend_error_returns_empty():
    class _Boom:
        def generate(self, prompt, on_chunk=None, tools=None, messages=None):
            raise RuntimeError("network down")

    assert focus_judge_candidates(SimpleNamespace(backend=_Boom()), "note", _rows(["1:0"])) == {}


def test_focus_judge_drops_unknown_ack_and_bad_verdict():
    # 只认合法 ack_id + 合法 verdict:模型串号/给非法结论一律丢弃,不猜不补默认。
    class _Weird:
        def generate(self, prompt, on_chunk=None, tools=None, messages=None):
            rows = [{"ack_id": "1:0", "verdict": "hit"}, {"ack_id": "not-in-batch", "verdict": "hit"},
                    {"ack_id": "1:1", "verdict": "maybe"}]
            return SimpleNamespace(text=json.dumps({"verdicts": rows}))

    out = focus_judge_candidates(SimpleNamespace(backend=_Weird()), "note", _rows(["1:0", "1:1"]))
    assert out == {"1:0": {"verdict": "hit", "evidence": ""}}


def test_focus_judge_unparseable_text_returns_empty():
    class _Chatty:
        def generate(self, prompt, on_chunk=None, tools=None, messages=None):
            return SimpleNamespace(text="我觉得这些都挺可疑的,但我没有输出 JSON。")

    assert focus_judge_candidates(SimpleNamespace(backend=_Chatty()), "note", _rows(["1:0"])) == {}


def test_attach_focus_verdicts_by_ack():
    rows = [{"ack_id": "5:0", "event": {}}, {"ack_id": "5:1", "event": {}}, {"ack_id": "5:2", "event": {}}]
    n = _attach_focus_verdicts(rows, {"5:0": {"verdict": "hit", "evidence": "admin granted"}, "5:2": {"verdict": "clear", "evidence": ""}})
    assert n == 2
    assert rows[0]["focus_verdict"] == "hit" and rows[0]["focus_evidence"] == "admin granted"
    assert "focus_verdict" not in rows[1]  # 没判到的不乱贴
    assert rows[2]["focus_verdict"] == "clear" and "focus_evidence" not in rows[2]  # 空证据不贴


def test_attach_focus_verdicts_empty_is_noop():
    rows = [{"ack_id": "5:0", "event": {}}]
    assert _attach_focus_verdicts(rows, None) == 0
    assert _attach_focus_verdicts(rows, {}) == 0
    assert "focus_verdict" not in rows[0]


# ── 全链路接线(假后端 + 假源):/audit pull 真路必须把聚焦判据回填到候选行 + 挂 note ──


class _AuditFakeSource:
    def __init__(self, specs):
        self.events = [
            {"seq": i + 1, "event_id": f"E-{i+1:04d}", "request": req, "response": resp}
            for i, (req, resp) in enumerate(specs)
        ]

    def handle(self, url):
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(url).query)
        since = int(q.get("since", ["0"])[0])
        items = [dict(e) for e in self.events if e["seq"] >= since][: int(q.get("limit", ["400"])[0])]
        nxt = (items[-1]["seq"] + 1) if items else max(since, len(self.events))
        return True, {"items": items, "next_cursor": nxt}, ""


def _hit_escalated_backend():
    """假后端:response 含 'escalated' 判 hit、其余 clear——按收到的 ack 精确对位(验回填不串号)。"""
    backend = _FakeBackend({})

    def _gen(prompt, on_chunk=None, tools=None, messages=None):
        cand = json.loads(messages[0]["content"].split("candidates: ", 1)[1])
        rows = [{"ack_id": c["ack_id"], "evidence": "role=admin",
                 "verdict": "hit" if "escalated" in (c["event"] or {}).get("response", "") else "clear"} for c in cand]
        return SimpleNamespace(text=json.dumps({"verdicts": rows}))

    backend.generate = _gen
    return backend


def _drain_then_pull(tool, state, wid):
    import time

    from agent.ingestion import harvester as hv

    deadline = time.time() + 8
    while time.time() < deadline and int(state.totals.get("spool_candidates", 0) or 0) < 2:
        time.sleep(0.05)
    got = None
    for _ in range(15):
        payload = json.loads(tool.execute({"action": "pull", "watch_id": wid, "max_wait_seconds": 3}).output)
        if payload.get("candidates"):
            got = payload
            break
    hv.stop_harvester(wid)
    return got


def test_audit_pull_attaches_focus_verdict_end_to_end(tmp_path, monkeypatch):
    """真 /audit pull 路(收割→spool→pull 渲染)必须给候选回填 focus_verdict 并挂 focus_judgment_note。
    用假后端(定死 verdict)钉死接线;真模型召回由 H 全链路台真调 M2.7 验。"""
    from agent.ingestion import harvester as hv
    from agent.ingestion import watch_state as ws
    from agent.ingestion import watch_tool as wt
    from agent.ingestion.watch_tool import WatchStreamTool

    monkeypatch.setattr(ws, "registry", ws.WatchRegistry())
    monkeypatch.setattr(wt, "registry", ws.registry)
    monkeypatch.setattr(hv, "harvesters", hv._HarvesterRegistry())
    source = _AuditFakeSource([("POST /users/1 role=admin", "200 OK {\"role\":\"admin\"} (privilege escalated)"),
                               ("GET /health", "200 OK ok")])
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(tmp_path / "owner"), owner_id="u-t"),
        backend=_hit_escalated_backend(),
        _current_run_params=SimpleNamespace(task_attributes={AUDIT_ATTR: True}),
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = source.handle
    opened = json.loads(
        tool.execute(
            {
                "action": "open",
                "url": "http://127.0.0.1:9/pull",
                "watch_window_seconds": 600,
            }
        ).output
    )
    assert opened.get("audit_guarantee") is True
    wid = opened["watch_id"]
    got = _drain_then_pull(tool, ws.registry.get_or_load(tmp_path / "owner", wid), wid)
    assert got is not None, "应能 pull 到候选"
    assert got.get("focus_judged", 0) >= 1 and "focus_judgment_note" in got
    # 真命中(privilege escalated)聚焦判 hit,普通流 clear——回填按 ack 精确对位,不串号
    hit_rows = [c for c in got["candidates"] if c.get("focus_verdict") == "hit"]
    assert hit_rows and "escalated" in hit_rows[0]["event"]["response"]
