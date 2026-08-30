"""/audit 保证档(逐条保证判读)的机制层契约。

钉死的契约(每条对应一个上一轮真机崩掉的口子):
1. ack-on-judge:游标/ACK 只在"逐条结论交齐(submit_verdicts)"后推进——空转判读工
   反复 pull 抽不干游标(上一轮 4% 召回的直接根因:pull 本身当 ack,空壳工没判就签收);
2. 交付即立欠账:候选行带 ack_id、在途批带 pending_acks;欠账未清,同人换人一律重投同批;
3. 引擎零丢弃:normal 内容规则命中在保证档也逐条入队(规则只记账不减负),无压组无溢出;
4. 覆盖回执:入队/已判/待判/丢弃 随时可查,丢弃恒 0(>0 = bug 亮红);
5. 判完归档:轮转切走的已判记录进 archive 留痕,不销毁;
6. 抬取不按判读积压背压(只按磁盘水位):判读慢=队列涨,不许把流留在会淘汰的源端;
7. 非保证档零回归:ack-on-next-pull 旧路逐字节不变(既有 takeover 测试盯着)。

铁律:全部结构化信号(计数/状态/令牌),不判内容——候选真假永远归模型。
"""

from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.agent_core.runner.context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent.agent_core.runtime.record_finding_tool import execute_record_finding
from agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_OBJECTIVE_ATTR,
    AUDIT_RUN_EPOCH_ATTR,
    AUDIT_SOURCE_BINDING_PENDING_ATTR,
    AUDIT_SOURCE_BINDING_TOOLS,
    AUDIT_SOURCE_CONFIG_VERSION_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_worker_key,
)
from agent.conversation import ConversationStore
from agent.conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
)
from agent.ingestion import harvester as hv
from agent.ingestion import watch_state as ws
from agent.ingestion import watch_tool as wt
from agent.ingestion.watch_state import new_state as _runtime_new_state
from agent.ingestion.watch_state import persist_state
from agent.ingestion.watch_tool import WatchStreamTool
from agent.settings import AgentConfig
from agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolProtocolSnapshot,
)


def new_state(*args, **kwargs):
    state = _runtime_new_state(*args, **kwargs)
    if str(state.source_url).startswith(("http://", "https://")):
        state.source_envelope = {
            "mode": "cursor",
            "record_boundary": "array_item",
            "record_list_key": "items",
            "cursor_field": "next_cursor",
            "cursor_semantics": "next_position",
            "request": {
                "method": "GET",
                "cursor_binding": {
                    "location": "query",
                    "name": "since",
                    "initial": 0,
                },
                "page_size_binding": {"location": "query", "name": "limit"},
            },
            "valid": True,
        }
    return state


class _FakeSource:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def feed(self, count: int, make=None) -> None:
        base = len(self.events)
        for index in range(count):
            seq = base + index
            self.events.append(
                make(seq) if make else {"seq": seq, "kind": "beat", "note": f"n{seq}"}
            )

    def handle(self, request) -> tuple[bool, object, str]:
        from urllib.parse import parse_qs, urlsplit

        url = request.url
        query = parse_qs(urlsplit(url).query)
        since = int(query.get("since", ["0"])[0])
        limit = int(query.get("limit", ["50"])[0])
        items = [dict(event) for event in self.events if event["seq"] >= since][:limit]
        next_cursor = (items[-1]["seq"] + 1) if items else max(since, len(self.events))
        return True, {"items": items, "next_cursor": next_cursor}, ""


@pytest.fixture()
def owner_home(tmp_path, monkeypatch):
    fresh = ws.WatchRegistry()
    monkeypatch.setattr(ws, "registry", fresh)
    monkeypatch.setattr(wt, "registry", fresh)
    monkeypatch.setattr(hv, "harvesters", hv._HarvesterRegistry())
    yield tmp_path / "owner"
    # open 的 watch 会起收割线程;teardown 停掉防泄漏进后续测试
    # (monkeypatch 撤销前 hv.harvesters 仍指向本测试的 fresh 注册表)。
    for watch_id in fresh.ids():
        hv.stop_harvester(watch_id)


_TOOL_URL = "http://127.0.0.1:9/pull?since=<next>&limit=<limit>"


def _tool(
    owner_home: Path,
    source: _FakeSource,
    user_prompt: str = "",
    task_attributes: dict | None = None,
) -> WatchStreamTool:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-test"),
        _current_user_prompt=user_prompt,
        _current_run_params=SimpleNamespace(
            task_attributes=task_attributes,
            tool_protocol_snapshot=ToolProtocolSnapshot(
                run_id="watch-test-run",
                source_protocol="text",
                capability=ProviderToolCapability(
                    provider="test",
                    endpoint="local://watch-test",
                    model="watch-test",
                    stream=False,
                    native_supported=False,
                    evidence="explicit-watch-test-contract",
                ),
            ),
        ),
        config=AgentConfig(enable_subagents=False),
        subagents=None,
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool.__dict__["_fetch_json"] = source.handle
    return tool


def _audit_source_tool(
    owner_home: Path,
    source: _FakeSource,
    *,
    user_prompt: str = "",
    task_attributes: dict | None = None,
    source_goal: str = "按现场确认的来源资料打开并持续研判这一条来源",
    run_id: str = "source-binding-1",
    attempt_id: str = "source-binding-attempt-1",
) -> WatchStreamTool:
    """Build the pre-binding leaf that a real Audit coordinator creates.

    The helper deliberately does not let the Audit root call ``open``.  The
    first successful open mutates this same typed task into the bound source
    worker, matching the production adoption path.
    """

    attrs = dict(task_attributes or {})
    audit_id = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "audit-test").strip()
    attrs[AUDIT_ATTR] = True
    attrs[AUDIT_SOURCE_BINDING_PENDING_ATTR] = True
    attrs[CONVERSATION_REQUEST_ID_ATTR] = audit_id
    tool = _tool(
        owner_home,
        source,
        user_prompt=user_prompt,
        task_attributes=attrs,
    )
    task = SimpleNamespace(
        id=run_id,
        goal=source_goal,
        status="RUNNING",
        root_id=audit_id,
        parent_id=audit_id,
        child_ids=[],
        runner_active_attempt_id=attempt_id,
        attributes=attrs,
        allowed_tools=list(AUDIT_SOURCE_BINDING_TOOLS),
        allowed_skills=[],
        allowed_write_roots=[],
        role="worker",
        agent_name=f"audit-source-binding-{run_id}",
        acceptance_checks=[],
        context_packs=[],
        context_manifest=SimpleNamespace(
            required_read_paths=[],
            hint_read_paths=[],
        ),
        updated_at=1.0,
        created_at=1.0,
    )
    tool.agent.subagents = _FakeSubagents(task)
    tool.agent._current_subagent_run_id = run_id
    tool.agent._current_subagent_attempt_id = attempt_id
    tool.agent._current_task_attributes = attrs
    tool.agent._current_run_params.run_id = run_id
    tool.agent._current_run_params.request_id = audit_id
    return tool


class _FakeSubagents:
    def __init__(self, task) -> None:
        self.task = task

    def load(self, run_id: str):
        if run_id != self.task.id:
            raise FileNotFoundError(run_id)
        return self.task

    def list_runs(self):
        return [self.task]

    def save(self, task) -> None:
        self.task = task


@contextmanager
def _source_worker_context(
    tool: WatchStreamTool,
    state,
    *,
    run_id: str = "source-worker-1",
    attempt_id: str = "attempt-1",
):
    """Enter the same typed runner context used by a real source worker."""
    audit_id = str(state.audit_root_task_id or "").strip() or "audit-test"
    live_state = ws.registry.get_or_load(state.owner_home, state.watch_id) or state
    for selected in {id(state): state, id(live_state): live_state}.values():
        selected.audit_root_task_id = audit_id
        persist_state(selected)
    worker_key = audit_source_worker_key(audit_id, live_state.watch_id)
    attrs = {
        AUDIT_ATTR: True,
        AUDIT_RUN_EPOCH_ATTR: max(0, int(live_state.audit_run_epoch or 0)),
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: live_state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: live_state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: worker_key,
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(live_state.owner_home),
        AUDIT_SOURCE_CONFIG_VERSION_ATTR: live_state.source_config_version,
    }
    task = SimpleNamespace(
        id=run_id,
        goal=str(state.source_task_goal or "持续研判这一条来源"),
        status="RUNNING",
        runner_active_attempt_id=attempt_id,
        attributes=attrs,
        updated_at=1.0,
        created_at=1.0,
        agent_run_findings_jsonl=str(
            live_state.owner_home
            / "tasks"
            / audit_id
            / "work"
            / "agents"
            / run_id
            / "findings.jsonl"
        ),
    )
    previous_manager = getattr(tool.agent, "subagents", None)
    tool.agent.subagents = _FakeSubagents(task)
    previous = set_current_subagent_context(
        tool.agent,
        run_id=run_id,
        attempt_id=attempt_id,
        task_attributes=attrs,
    )
    try:
        yield task
    finally:
        restore_current_subagent_context(tool.agent, previous)
        tool.agent.subagents = previous_manager


def _audit_state(owner_home: Path, source: _FakeSource, count: int, chunk: int = 4):
    """保证档状态:喂 count 条进真收割管线(引擎全量直通,每条一候选落 spool)。"""
    state = new_state(
        owner_home,
        "http://src.example/pull",
        {"full_read_per_pull": chunk},
    )
    state.audit_guarantee = True
    persist_state(state)
    source.feed(count)
    assert hv._harvest_cycle(state, source.handle)
    return state


def _payload(result) -> dict:
    assert result.ok, result.output
    return json.loads(result.output)


def _ack_ids(records: list[dict]) -> list[str]:
    return [row["ack_id"] for record in records for row in (record.get("candidates") or [])]


def _verdict(
    ack_id: str,
    kind: str = "clear",
    score: int | float = 1,
    note: str = "测试判断理由",
    **extras,
) -> dict:
    if kind == "hit" and "finding" not in extras:
        extras["finding"] = {
            "claim": note,
            "kind": "hit",
            "requires_llm_report": True,
        }
    return {
        "ack_id": ack_id,
        "verdict": kind,
        "score": score,
        "note": note,
        **extras,
    }


def test_audit_pull_hides_internal_candidate_triage_from_model(owner_home) -> None:
    """保证档逐条交付原始事实，不把系统内部筛选理由暗示给研判模型。"""
    source = _FakeSource()
    state = _audit_state(owner_home, source, 1, chunk=1)
    state.audit_root_task_id = "audit-no-triage-hint"
    persist_state(state)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-no-triage-hint"):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )

    assert len(pulled["candidates"]) == 1
    assert "triage" not in pulled["candidates"][0]
    assert pulled["candidates"][0]["event"]["seq"] == 0


def test_hit_requires_typed_reporting_finding(owner_home) -> None:
    source = _FakeSource()
    state = _audit_state(owner_home, source, 1, chunk=1)
    state.audit_root_task_id = "audit-hit-contract"
    persist_state(state)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-hit-contract"):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )
        candidate = pulled["candidates"][0]
        missing = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": candidate["verdict_token"],
                        "verdict": "hit",
                        "score": 95,
                        "note": "模型确认命中但遗漏投递声明",
                    }
                ],
            }
        )
        contradictory = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": candidate["verdict_token"],
                        "verdict": "hit",
                        "score": 95,
                        "note": "模型同时声明命中与不汇报",
                        "finding": {
                            "claim": "该记录满足当前 Audit 的命中条件",
                            "requires_llm_report": False,
                        },
                    }
                ],
            }
        )
        clear_with_finding = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": candidate["verdict_token"],
                        "verdict": "clear",
                        "score": 100,
                        "note": "顶层说无事，但又附带需要升级的结论",
                        "finding": {
                            "claim": "该记录满足当前 Audit 的命中条件",
                            "kind": "hit",
                            "requires_llm_report": True,
                        },
                    }
                ],
            }
        )
        accepted = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": candidate["verdict_token"],
                        "verdict": "hit",
                        "score": 95,
                        "note": "模型确认命中并进入投递账",
                        "finding": {
                            "claim": "该记录满足当前 Audit 的命中条件",
                            "kind": "hit",
                        },
                    }
                ],
            }
        )

    assert missing.ok is False
    assert missing.reported_error_code == "AUDIT_VERDICT_SHAPE_INVALID"
    assert "hit 必须携带 finding" in missing.output
    assert contradictory.ok is False
    assert contradictory.reported_error_code == "AUDIT_VERDICT_SHAPE_INVALID"
    assert "互相矛盾" in contradictory.output
    assert clear_with_finding.ok is False
    assert clear_with_finding.reported_error_code == "AUDIT_VERDICT_SHAPE_INVALID"
    assert "clear 与 finding 互相矛盾" in clear_with_finding.output
    assert accepted.ok is True
    ledger = hv._verdict_ledger_path(state)
    row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert row["finding"]["requires_llm_report"] is True


def test_verdict_tool_commits_valid_rows_when_one_current_row_is_malformed(
    owner_home,
) -> None:
    source = _FakeSource()
    state = _audit_state(owner_home, source, 3, chunk=3)
    state.audit_root_task_id = "audit-partial-shape-contract"
    persist_state(state)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-partial-shape"):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )
        candidates = pulled["candidates"]
        partial = _payload(
            tool.execute(
                {
                    "action": "verdict",
                    "watch_id": state.watch_id,
                    "delivery_ref": pulled["delivery_ref"],
                    "verdicts": [
                        {
                            "verdict_token": candidates[0]["verdict_token"],
                            "verdict": "clear",
                            "score": 2,
                        },
                        {
                            "verdict_token": candidates[1]["verdict_token"],
                            "verdict": "hit",
                            "score": 95,
                            "note": "模型漏了 finding",
                        },
                        {
                            "verdict_token": candidates[2]["verdict_token"],
                            "verdict": "unsure",
                            "score": 50,
                            "note": "证据不足",
                        },
                    ],
                }
            )
        )
        replay = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )

    assert partial["partial"] is True
    assert partial["acked_now"] == 2
    assert partial["malformed"] == 1
    assert partial["pending_remaining"] == 1
    assert [row["ack_id"] for row in replay["candidates"]] == [
        candidates[1]["ack_id"]
    ]


def test_verdict_output_observation_preserves_escaped_provider_shape() -> None:
    rows = [
        {
            "verdict_token": "vt-example",
            "verdict": "clear",
            "score": 2,
            "note": "逐条判断",
        }
    ]
    raw = json.dumps(rows, ensure_ascii=False)

    observed = wt._verdict_output_observation(
        rows,
        raw_verdicts=raw,
        delivery_ref="ad-example",
        watch_id="ws-example",
    )
    expected = {
        "action": "verdict",
        "watch_id": "ws-example",
        "verdicts": raw,
        "delivery_ref": "ad-example",
    }

    assert observed["model_output_records"] == 1
    assert observed["model_output_max_row_bytes"] > 0
    assert observed["model_output_bytes"] == len(
        json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def test_audit_source_tool_schema_defers_cross_field_rules_to_row_handler() -> None:
    from agent.contracts.tool_input_schema import validate_tool_input
    from agent.ingestion.watch_tool_spec import (
        _watch_stream_examples,
        build_watch_stream_model_spec,
    )

    schema = build_watch_stream_model_spec(surface="audit_source").input_schema
    base = {
        "action": "verdict",
        "watch_id": "ws-0123456789",
        "delivery_ref": "ad-111111111111111111111111",
    }
    clear_without_note = {
        **base,
        "verdicts": [
            {
                "verdict_token": "vt-111111111111111111111111",
                "verdict": "clear",
                "score": 1,
            }
        ],
    }
    unsure_without_note = {
        **base,
        "verdicts": [
            {
                "verdict_token": "vt-111111111111111111111111",
                "verdict": "unsure",
                "score": 50,
            }
        ],
    }
    missing_hit_finding = {
        **base,
        "verdicts": [
            {
                "verdict_token": "vt-222222222222222222222222",
                "verdict": "hit",
                "score": 95,
                "note": "命中但没有进入投递账",
            }
        ],
    }
    reporting_hit = {
        **base,
        "verdicts": [
            {
                **missing_hit_finding["verdicts"][0],
                "finding": {
                    "claim": "该记录满足当前 Audit 的命中条件",
                },
            }
        ],
    }
    assert validate_tool_input(clear_without_note, schema).ok is True
    # These rows must reach the per-row handler so one malformed conclusion
    # cannot make the provider reject every other row in the array.
    assert validate_tool_input(unsure_without_note, schema).ok is True
    assert validate_tool_input(missing_hit_finding, schema).ok is True
    assert validate_tool_input(reporting_hit, schema).ok is True

    # Model-facing examples must never teach a shape that the row handler
    # rejects. Conditional row rules remain in the partial-commit handler.
    example_hits = []
    for raw in _watch_stream_examples():
        payload = json.loads(raw)
        example_hits.extend(
            row
            for row in payload.get("verdicts", [])
            if isinstance(row, dict) and row.get("verdict") == "hit"
        )
    assert example_hits
    assert all(isinstance(row.get("finding"), dict) for row in example_hits)


def test_explicit_verdict_rows_persist_independently_and_train_budget(
    owner_home,
    monkeypatch,
) -> None:
    source = _FakeSource()
    state = _audit_state(owner_home, source, 12, chunk=12)
    state.owner_id = "u-test"
    state.audit_root_task_id = "audit-explicit-verdict"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-explicit-verdict"):
        pulled = _payload(
            tool.execute(
                {"action": "pull", "watch_id": state.watch_id, "max_wait_seconds": 0}
            )
        )
        exceptional = pulled["candidates"][7]
        verdicts = [
            (
                {
                    "verdict_token": candidate["verdict_token"],
                    "verdict": "hit",
                    "score": 94,
                    "note": "该条存在单独的结果端证据" + ("证" * 180),
                    "finding": {
                        "claim": "该条满足当前 Audit 的命中条件",
                        "kind": "hit",
                        "requires_llm_report": True,
                    },
                }
                if candidate["ack_id"] == exceptional["ack_id"]
                else {
                    "verdict_token": candidate["verdict_token"],
                    "verdict": "clear",
                    "score": 2,
                }
            )
            for candidate in pulled["candidates"]
        ]
        tool_result = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": verdicts,
            }
        )
        result = _payload(tool_result)

    assert result["acked_now"] == 12
    assert result["verdicts_hit"] == 1
    assert result["verdicts_clear"] == 11
    assert tool_result.result_envelope["runtime_transition"] == {
        "kind": "context_refresh",
        "reason": "durable_slice_committed",
        "resume": "next_durable_slice",
    }
    rows = [
        json.loads(line)
        for line in (
            owner_home / "watch_state" / f"{state.watch_id}.verdicts.ndjson"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 12
    assert len({row["ack_id"] for row in rows}) == 12
    assert all(row["source_ref"].startswith(f"audit://{state.watch_id}/") for row in rows)
    hit = next(row for row in rows if row["verdict"] == "hit")
    assert hit["ack_id"] == exceptional["ack_id"]
    assert sum(not str(row.get("note") or "") for row in rows) == 11

    cursor = hv.read_spool_cursor(state)
    profile = cursor["verdict_output_profile"]
    assert profile["observed_records"] == 12
    expected_envelope = {
        "action": "verdict",
        "watch_id": state.watch_id,
        "verdicts": verdicts,
        "delivery_ref": pulled["delivery_ref"],
    }
    assert profile["serialized_bytes"] == len(
        json.dumps(
            expected_envelope,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert profile["max_row_bytes"] > 500

    source.feed(12)
    assert hv._harvest_cycle(state, source.handle)
    budget = wt._audit_batch_context_estimate(state, tool.agent)
    assert budget["verdict_output_tokens_per_record"] == math.ceil(
        (profile["serialized_bytes"] / profile["observed_records"]) * 1.5 / 3.0
    )
    assert budget["verdict_output_tokens_per_record"] < 96


def test_partial_verdict_refreshes_context_only_after_delivery_is_settled(
    owner_home,
    monkeypatch,
) -> None:
    source = _FakeSource()
    state = _audit_state(owner_home, source, 4, chunk=4)
    state.owner_id = "u-test"
    state.audit_root_task_id = "audit-partial-slice"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-partial-slice"):
        pulled = _payload(
            tool.execute(
                {"action": "pull", "watch_id": state.watch_id, "max_wait_seconds": 0}
            )
        )

        def explicit(candidate):
            return {
                "ack_id": candidate["ack_id"],
                "source_ref": candidate["source_ref"],
                "event_sha256": candidate["event_sha256"],
                "verdict": "clear",
                "score": 1,
                "note": "当前证据未显示异常",
            }

        first = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "verdicts": [explicit(row) for row in pulled["candidates"][:2]],
            }
        )
        second = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "verdicts": [explicit(row) for row in pulled["candidates"][2:]],
            }
        )

    assert _payload(first)["pending_remaining"] == 2
    assert "runtime_transition" not in first.result_envelope
    assert _payload(second)["pending_remaining"] == 0
    assert second.result_envelope["runtime_transition"] == {
        "kind": "context_refresh",
        "reason": "durable_slice_committed",
        "resume": "next_durable_slice",
    }


def test_complete_redelivery_view_refreshes_with_older_pending_rows(
    owner_home,
    monkeypatch,
) -> None:
    source = _FakeSource()
    state = _audit_state(owner_home, source, 8, chunk=8)
    state.owner_id = "u-test"
    state.audit_root_task_id = "audit-redelivery-slice"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-redelivery-slice"):
        initial = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "target_records": 8,
                    "max_wait_seconds": 0,
                }
            )
        )

        def explicit(candidate):
            return {
                "ack_id": candidate["ack_id"],
                "source_ref": candidate["source_ref"],
                "event_sha256": candidate["event_sha256"],
                "verdict": "clear",
                "score": 1,
                "note": "当前证据未显示异常",
            }

        partial = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "verdicts": [explicit(row) for row in initial["candidates"][:4]],
            }
        )
        replay = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "target_records": 2,
                    "max_wait_seconds": 0,
                }
            )
        )
        settled_view = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": replay["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": row["verdict_token"],
                        "verdict": "clear",
                        "score": 1,
                    }
                    for row in replay["candidates"]
                ],
            }
        )

    assert _payload(partial)["pending_remaining"] == 4
    assert "runtime_transition" not in partial.result_envelope
    assert len(replay["candidates"]) == 2
    assert _payload(settled_view)["pending_remaining"] == 2
    assert "runtime_transition" not in settled_view.result_envelope


def test_explicit_verdict_rejects_unknown_duplicate_and_stale_tokens_without_writes(
    owner_home,
    monkeypatch,
) -> None:
    source = _FakeSource()
    state = _audit_state(owner_home, source, 3, chunk=3)
    state.audit_root_task_id = "audit-explicit-invalid"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-explicit-invalid"):
        pulled = _payload(
            tool.execute(
                {"action": "pull", "watch_id": state.watch_id, "max_wait_seconds": 0}
            )
        )
        unknown = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": "vt-ffffffffffffffffffffffff",
                        "verdict": "clear",
                        "score": 1,
                    }
                ],
            }
        )
        token = pulled["candidates"][0]["verdict_token"]
        duplicate = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": token,
                        "verdict": "clear",
                        "score": 1,
                    },
                    {
                        "verdict_token": token,
                        "verdict": "unsure",
                        "score": 50,
                        "note": "同一令牌重复",
                    },
                ],
            }
        )
        stale = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": "ad-000000000000000000000000",
                "verdicts": [
                    {
                        "verdict_token": row["verdict_token"],
                        "verdict": "clear",
                        "score": 1,
                    }
                    for row in pulled["candidates"]
                ],
            }
        )

    assert unknown.ok is False
    assert unknown.reported_error_code == "AUDIT_VERDICT_TOKEN_COVERAGE_INVALID"
    assert "未知" in unknown.output
    assert len(unknown.output.encode("utf-8")) < 1_500
    assert duplicate.ok is False
    assert duplicate.reported_error_code == "AUDIT_VERDICT_TOKEN_COVERAGE_INVALID"
    assert "重复" in duplicate.output
    assert stale.ok is False
    assert stale.reported_error_code == "AUDIT_DELIVERY_REF_INVALID"
    assert "过期" in stale.output
    assert hv.audit_receipt_facts(state)["judged"] == 0
    assert "verdict_output_profile" not in hv.read_spool_cursor(state)


def test_verdict_token_repair_feedback_bounds_large_invalid_sets(
    owner_home,
    monkeypatch,
) -> None:
    source = _FakeSource()
    state = _audit_state(owner_home, source, 155, chunk=155)
    state.audit_root_task_id = "audit-bounded-token-repair"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-bounded-token-repair"):
        pulled = _payload(
            tool.execute(
                {"action": "pull", "watch_id": state.watch_id, "max_wait_seconds": 0}
            )
        )
        delivered_count = len(pulled["candidates"])
        rejected = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": "vt-ffffffffffffffffffffffff",
                        "verdict": "clear",
                        "score": 1,
                    }
                ],
            }
        )

    assert rejected.ok is False
    assert rejected.reported_error_code == "AUDIT_VERDICT_TOKEN_COVERAGE_INVALID"
    assert f"当前交付 {delivered_count} 条" in rejected.output
    assert "未知 1 个" in rejected.output
    assert "vt-ffffffffffffffffffffffff" in rejected.output
    assert len(rejected.output.encode("utf-8")) < 1_500
    assert hv.audit_receipt_facts(state)["judged"] == 0


def test_expiring_runner_slice_replays_only_unsettled_records(
    owner_home,
    monkeypatch,
) -> None:
    source = _FakeSource()
    state = _audit_state(owner_home, source, 155, chunk=155)
    state.audit_root_task_id = "audit-runner-slice-replay"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-runner-slice-replay") as task:
        pulled = _payload(
            tool.execute(
                {"action": "pull", "watch_id": state.watch_id, "max_wait_seconds": 0}
            )
        )
        delivered = len(pulled["candidates"])
        assert delivered > 1
        partial = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": row["verdict_token"],
                        "verdict": "clear",
                        "score": 1,
                    }
                    for row in pulled["candidates"][:-1]
                ],
            }
        )
        assert partial.ok is True
        assert _payload(partial)["pending_remaining"] == 1

        cursor = hv.read_spool_cursor(state)
        cursor["processing_throughput"] = {
            "settled_records": 100,
            "active_seconds": 300.0,
            "records_per_second": 1 / 3,
        }
        hv._write_spool_cursor(state, cursor)
        task.attributes["dynamic_timeout_seconds"] = 300
        task.attributes["runner_session"] = {"started_at": time.time() - 299.9}

        replay = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                    "target_records": delivered,
                }
            )
        )

    assert len(replay["candidates"]) == 1
    assert replay["candidates"][0]["ack_id"] == pulled["candidates"][-1]["ack_id"]
    assert replay["pending_verdicts"] == 1
    assert hv.audit_receipt_facts(state)["judged"] == delivered - 1


def test_delivery_ref_binds_exact_per_record_identity_by_opaque_token(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 3, chunk=3)
    state.owner_id = "u-test"
    state.audit_root_task_id = "audit-verbose-delivery-ref"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-verbose-delivery-ref"):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )
        result = _payload(
            tool.execute(
                {
                    "action": "verdict",
                    "watch_id": state.watch_id,
                    "delivery_ref": pulled["delivery_ref"],
                        "verdicts": [
                            {
                                "verdict_token": candidate["verdict_token"],
                                "verdict": "hit" if index == 1 else "clear",
                                "score": 90 if index == 1 else 3,
                                "note": f"逐条判断 {candidate['ack_id']}",
                                **(
                                    {
                                        "finding": {
                                            "claim": "该条满足当前 Audit 的命中条件",
                                            "kind": "hit",
                                            "requires_llm_report": True,
                                        }
                                    }
                                    if index == 1
                                    else {}
                                ),
                            }
                        for index, candidate in reversed(
                            list(enumerate(pulled["candidates"]))
                        )
                    ],
                }
            )
        )

    assert result["acked_now"] == 3
    rows = [
        json.loads(line)
        for line in (
            owner_home / "watch_state" / f"{state.watch_id}.verdicts.ndjson"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    candidates_by_ack = {
        candidate["ack_id"]: candidate for candidate in pulled["candidates"]
    }
    assert all(
        candidate["verdict_token"]
        == hv.audit_verdict_token(pulled["delivery_ref"], candidate["ack_id"])
        for candidate in pulled["candidates"]
    )
    assert {row["ack_id"] for row in rows} == set(candidates_by_ack)
    for row in rows:
        candidate = candidates_by_ack[row["ack_id"]]
        assert row["source_ref"] == candidate["source_ref"]
        assert row["event_sha256"] == candidate["event_sha256"]
        assert row["note"] == f"逐条判断 {row['ack_id']}"
    cursor = hv.read_spool_cursor(state)
    output_profile = cursor["verdict_output_profile"]
    assert output_profile["schema"] == "audit-verdict-output-profile.v1"
    assert output_profile["observed_records"] == 3
    assert output_profile["serialized_bytes"] > 0
    assert output_profile["max_row_bytes"] > 0
    budget = wt._audit_batch_context_estimate(state, tool.agent)
    assert budget["verdict_output_estimate_source"] == "observed_serialization"
    assert budget["verdict_output_tokens_per_record"] == math.ceil(
        (output_profile["serialized_bytes"] / output_profile["observed_records"])
        * 1.5
        / 3.0
    )


def test_delivery_token_binding_accepts_subset_and_rejects_duplicate_or_mixed_identity(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 3, chunk=3)
    state.owner_id = "u-test"
    state.audit_root_task_id = "audit-delivery-order-shape"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-delivery-order-shape"):
        pulled = _payload(
            tool.execute(
                {"action": "pull", "watch_id": state.watch_id, "max_wait_seconds": 0}
            )
        )
        duplicate = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": token,
                        "verdict": "clear",
                        "score": 1,
                        "note": "同数目下重复一条并漏掉另一条",
                    }
                    for token in (
                        pulled["candidates"][0]["verdict_token"],
                        pulled["candidates"][0]["verdict_token"],
                        pulled["candidates"][2]["verdict_token"],
                    )
                ],
            }
        )
        mixed = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    _verdict(pulled["candidates"][0]["ack_id"]),
                    {
                        "verdict_token": pulled["candidates"][1]["verdict_token"],
                        "verdict": "clear",
                        "score": 1,
                        "note": "混合协议",
                    },
                ],
            }
        )
        partial = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": candidate["verdict_token"],
                        "verdict": "clear",
                        "score": 1,
                        "note": "本轮先完成两行",
                    }
                    for candidate in pulled["candidates"][:2]
                ],
            }
        )
        replay = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )
        stale = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": pulled["candidates"][2]["verdict_token"],
                        "verdict": "clear",
                        "score": 1,
                    }
                ],
            }
        )
        settled = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": replay["delivery_ref"],
                "verdicts": [
                    {
                        "verdict_token": replay["candidates"][0]["verdict_token"],
                        "verdict": "clear",
                        "score": 1,
                    }
                ],
            }
        )

    assert duplicate.ok is False
    assert duplicate.reported_error_code == "AUDIT_VERDICT_TOKEN_COVERAGE_INVALID"
    assert "重复" in duplicate.output
    assert mixed.ok is False
    assert mixed.reported_error_code == "AUDIT_VERDICT_IDENTITY_MODE_INVALID"
    assert "不能混合" in mixed.output or "统一使用" in mixed.output
    assert partial.ok is True
    assert _payload(partial)["acked_now"] == 2
    assert _payload(partial)["pending_remaining"] == 1
    assert "runtime_transition" not in partial.result_envelope
    assert [row["ack_id"] for row in replay["candidates"]] == [
        pulled["candidates"][2]["ack_id"]
    ]
    assert replay["delivery_ref"] != pulled["delivery_ref"]
    assert (
        replay["candidates"][0]["verdict_token"]
        != pulled["candidates"][2]["verdict_token"]
    )
    assert stale.ok is False
    assert stale.reported_error_code == "AUDIT_DELIVERY_REF_INVALID"
    assert settled.ok is True
    assert _payload(settled)["pending_remaining"] == 0
    assert settled.result_envelope["runtime_transition"] == {
        "kind": "context_refresh",
        "reason": "durable_slice_committed",
        "resume": "next_durable_slice",
    }
    assert hv.audit_receipt_facts(state)["judged"] == 3


def test_delivery_ref_rejects_explicit_shift_but_binds_omitted_identity(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 2, chunk=2)
    state.owner_id = "u-test"
    state.audit_root_task_id = "audit-delivery-ref-row-identity"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-row-identity"):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )
        first, second = pulled["candidates"]
        shifted = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "ack_id": first["ack_id"],
                        "source_ref": second["source_ref"],
                        "event_sha256": second["event_sha256"],
                        "verdict": "hit",
                        "score": 99,
                        "note": "语义结论不能错绑到相邻记录",
                    }
                ],
            }
        )
        bound = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "ack_id": first["ack_id"],
                        "verdict": "clear",
                        "score": 1,
                        "note": "宿主按当前交付绑定规范逐条身份",
                    }
                ],
            }
        )

    assert shifted.ok is False
    assert shifted.reported_error_code == "AUDIT_VERDICT_EVIDENCE_MISMATCH"
    assert bound.ok is True
    assert hv.audit_receipt_facts(state)["judged"] == 1
    row = json.loads(
        (owner_home / "watch_state" / f"{state.watch_id}.verdicts.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert row["source_ref"] == first["source_ref"]
    assert row["event_sha256"] == first["event_sha256"]


def test_delivery_ref_rejects_explicit_evidence_rebinding(owner_home, monkeypatch):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 1, chunk=1)
    state.owner_id = "u-test"
    state.audit_root_task_id = "audit-delivery-ref-rebinding"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-delivery-ref-rebinding"):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )
        candidate = pulled["candidates"][0]
        result = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "delivery_ref": pulled["delivery_ref"],
                "verdicts": [
                    {
                        "ack_id": candidate["ack_id"],
                        "source_ref": "audit://ws-0000000000/candidate/1:0",
                        "event_sha256": candidate["event_sha256"],
                        "verdict": "clear",
                        "score": 1,
                        "note": "不能改绑原文",
                    }
                ],
            }
        )

    assert result.ok is False
    assert result.reported_error_code == "AUDIT_VERDICT_EVIDENCE_MISMATCH"
    assert "逐条身份与当前交付不一致" in result.output


def test_legacy_initial_verdict_without_delivery_ref_still_requires_identity(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 1, chunk=1)
    state.owner_id = "u-test"
    state.audit_root_task_id = "audit-legacy-explicit-identity"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(tool, state, run_id="source-legacy-identity"):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )
        candidate = pulled["candidates"][0]
        missing = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "verdicts": [_verdict(candidate["ack_id"])],
            }
        )
        accepted = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "verdicts": [
                    _verdict(
                        candidate["ack_id"],
                        source_ref=candidate["source_ref"],
                        event_sha256=candidate["event_sha256"],
                    )
                ],
            }
        )

    assert missing.ok is False
    assert missing.reported_error_code == "AUDIT_VERDICT_EVIDENCE_MISMATCH"
    assert accepted.ok is True


def test_historical_review_without_delivery_ref_requires_exact_identity(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 1, chunk=1)
    state.owner_id = "u-test"
    state.audit_root_task_id = "audit-review-explicit-identity"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(owner_home, source)

    with _source_worker_context(
        tool,
        state,
        run_id="source-review-identity",
        attempt_id="review-attempt-1",
    ) as task:
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )
        candidate = pulled["candidates"][0]
        _payload(
            tool.execute(
                {
                    "action": "verdict",
                    "watch_id": state.watch_id,
                    "delivery_ref": pulled["delivery_ref"],
                    "verdicts": [_verdict(candidate["ack_id"])],
                }
            )
        )
        missing = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "verdicts": [_verdict(candidate["ack_id"], review=True)],
            }
        )
        task.runner_active_attempt_id = "review-attempt-2"
        tool.agent.subagents.save(task)
        stale = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "verdicts": [
                    _verdict(
                        candidate["ack_id"],
                        review=True,
                        source_ref=candidate["source_ref"],
                        event_sha256=candidate["event_sha256"],
                    )
                ],
            }
        )
        task.runner_active_attempt_id = "review-attempt-1"
        tool.agent.subagents.save(task)
        review_params = {
            "action": "verdict",
            "watch_id": state.watch_id,
            "verdicts": [
                _verdict(
                    candidate["ack_id"],
                    review=True,
                    source_ref=candidate["source_ref"],
                    event_sha256=candidate["event_sha256"],
                    kind="hit",
                    score=96,
                    note="后续证据证明首次结论需要更正",
                    finding={
                        "claim": "复核更正后需要升级",
                        "kind": "hit",
                        "requires_llm_report": True,
                    },
                )
            ],
        }
        reviewed = tool.execute(review_params)
        duplicate_review = tool.execute(review_params)
        duplicate_finding = execute_record_finding(tool.agent, 
            {
                "claim": "复核更正后需要升级",
                "kind": "hit",
                "evidence_refs": [candidate["source_ref"]],
                "stage": "review",
                "requires_llm_report": True,
            }
        )

    assert missing.ok is False
    assert missing.reported_error_code == "AUDIT_VERDICT_EVIDENCE_MISMATCH"
    assert stale.ok is False
    assert stale.reported_error_code == "AUDIT_SOURCE_ATTEMPT_STALE"
    assert reviewed.ok is True
    reviewed_payload = json.loads(reviewed.output)
    assert reviewed_payload["reviewed_now"] == 1
    assert json.loads(duplicate_review.output)["reviewed_now"] == 0
    duplicate_finding_payload = json.loads(duplicate_finding.output)
    assert duplicate_finding_payload["recorded"] is False
    assert duplicate_finding_payload["reused"] is True
    assert reviewed_payload["audit_receipt"]["findings_projected"] == 1
    finding_rows = [
        json.loads(line)
        for line in Path(task.agent_run_findings_jsonl).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(finding_rows) == 1
    assert finding_rows[0]["stage"] == "review"
    inspected = hv.inspect_audit_record(state, candidate["ack_id"])
    assert [row["stage"] for row in inspected["verdict_history"]] == [
        "initial",
        "review",
    ]
    assert inspected["verdict"]["verdict"] == "hit"
    assert inspected["verdict"]["finding"]["stage"] == "review"
    assert inspected["processing_status"]["acknowledged"] is True
    assert inspected["processing_status"]["review_count"] == 1


def test_delivery_ref_expires_after_partial_settlement_and_takeover(
    owner_home,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 4, chunk=4)
    first_records, first_info = hv.read_spool_records(
        state,
        max_candidates=4,
        consumer="worker-a",
    )
    first_ids = _ack_ids(first_records)
    first_ref = str(first_info["delivery_ref"])

    partial = hv.submit_verdicts(
        state,
        consumer="worker-a",
        verdicts=[_verdict(first_ids[0])],
    )
    assert partial["acked_now"] == 1
    stale = hv.submit_verdicts(
        state,
        consumer="worker-a",
        delivery_ref=first_ref,
        verdicts=[_verdict(first_ids[1], note="旧交付不应继续生效")],
    )
    assert stale["ok"] is False
    assert stale["error_code"] == "AUDIT_DELIVERY_REF_INVALID"

    replay, replay_info = hv.read_spool_records(
        state,
        max_candidates=4,
        consumer="worker-b",
    )
    assert _ack_ids(replay) == first_ids[1:]
    assert replay_info["delivery_ref"] != first_ref
    cross_worker = hv.submit_verdicts(
        state,
        consumer="worker-b",
        delivery_ref=first_ref,
        verdicts=[_verdict(first_ids[1], note="不能跨接管复用")],
    )
    assert cross_worker["ok"] is False
    settled = hv.submit_verdicts(
        state,
        consumer="worker-b",
        delivery_ref=replay_info["delivery_ref"],
        verdicts=[
            {
                **_verdict(
                    str(candidate["ack_id"]),
                    note="新的精确交付可以签收",
                ),
                "source_ref": candidate["source_ref"],
                "event_sha256": candidate["event_sha256"],
            }
            for record in replay
            for candidate in hv.ensure_ack_ids(record)
        ],
    )
    assert settled["ok"] is True
    assert settled["acked_now"] == 3


# ── 0. 批边界:完整记录不截断,也不因 48+1 制造单条补判 ──


def test_effective_audit_update_waits_for_complete_inflight_batch(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 4, chunk=4)
    state.audit_objective = "旧的生效要求"
    persist_state(state)

    first, _ = hv.read_spool_records(
        state,
        max_candidates=2,
        consumer="judge",
        effective_audit_objective="旧的生效要求",
    )
    first_ids = _ack_ids(first)
    replay, _ = hv.read_spool_records(
        state,
        max_candidates=2,
        consumer="judge",
        effective_audit_objective="新的生效要求",
    )

    assert _ack_ids(replay) == first_ids
    assert state.audit_objective == "旧的生效要求"
    settled = hv.submit_verdicts(
        state,
        consumer="judge",
        verdicts=[_verdict(ack_id) for ack_id in first_ids],
    )
    assert settled["ok"] is True

    second, _ = hv.read_spool_records(
        state,
        max_candidates=2,
        consumer="judge",
        effective_audit_objective="新的生效要求",
    )
    assert _ack_ids(second)
    assert not set(_ack_ids(second)).intersection(first_ids)
    assert state.audit_objective == "新的生效要求"


def _spool_row(seq: int, count: int) -> dict:
    return {
        "spool_seq": seq,
        "candidates": [{"ack_id": f"{seq}:{index}"} for index in range(count)],
    }


def test_spool_batch_stops_before_complete_record_that_would_cross_quota():
    first_line = json.dumps(_spool_row(1, 47)) + "\n"
    second_line = json.dumps(_spool_row(2, 2)) + "\n"
    handle = StringIO(first_line + second_line)

    records, offset, taken = hv._collect_spool_rows(
        handle,
        offset=0,
        read_seq=0,
        max_candidates=48,
    )

    assert [row["spool_seq"] for row in records] == [1]
    assert taken == 47
    assert offset == len(first_line)
    handle.seek(offset)
    remaining, _end, remaining_taken = hv._collect_spool_rows(
        handle,
        offset=offset,
        read_seq=1,
        max_candidates=48,
    )
    assert [row["spool_seq"] for row in remaining] == [2]
    assert remaining_taken == 2


def test_spool_batch_keeps_one_complete_record_even_if_it_exceeds_quota():
    line = json.dumps(_spool_row(1, 50)) + "\n"
    records, offset, taken = hv._collect_spool_rows(
        StringIO(line),
        offset=0,
        read_seq=0,
        max_candidates=48,
    )

    assert [row["spool_seq"] for row in records] == [1]
    assert taken == 50
    assert offset == len(line)


def test_spool_batch_stops_before_complete_record_that_would_cross_byte_budget():
    first_line = json.dumps(_spool_row(1, 2) | {"blob": "a" * 700}) + "\n"
    second_line = json.dumps(_spool_row(2, 2) | {"blob": "b" * 700}) + "\n"
    handle = StringIO(first_line + second_line)

    records, offset, taken = hv._collect_spool_rows(
        handle,
        offset=0,
        read_seq=0,
        max_candidates=48,
        max_bytes=len(first_line.encode("utf-8")) + 32,
    )

    assert [row["spool_seq"] for row in records] == [1]
    assert taken == 2
    assert offset == len(first_line)


def test_audit_spool_batch_counts_model_projection_not_storage_metadata(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 3, chunk=3)
    stored = [
        json.loads(line)
        for line in hv.spool_path(state).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    two_record_budget = sum(
        hv._audit_model_visible_row_bytes(record) for record in stored[:2]
    )
    assert sum(
        len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
        for record in stored[:2]
    ) > two_record_budget

    first, _ = hv.read_spool_records(
        state,
        max_candidates=3,
        max_bytes=two_record_budget,
        consumer="projection-worker",
    )
    first_ids = _ack_ids(first)
    assert len(first_ids) == 2
    settled = hv.submit_verdicts(
        state,
        consumer="projection-worker",
        verdicts=[_verdict(ack_id) for ack_id in first_ids],
    )
    assert settled["acked_now"] == 2

    second, _ = hv.read_spool_records(
        state,
        max_candidates=3,
        max_bytes=two_record_budget,
        consumer="projection-worker",
    )
    assert len(_ack_ids(second)) == 1
    assert not set(first_ids).intersection(_ack_ids(second))


def test_unrecoverable_partial_inflight_counts_only_pending_gap():
    cursor = {"candidates_acked": 7}
    inflight = {
        "count": 10,
        "pending_acks": ["8:0", "9:0", "10:0"],
    }

    updated = hv._acked_cursor(cursor, inflight, gap=True)

    assert updated["candidates_acked"] == 10
    assert updated["redelivery_gap_candidates"] == 3
    assert "inflight" not in updated


def test_audit_spool_storage_splits_only_between_complete_source_records(
    owner_home,
):
    source = _FakeSource()
    source.feed(
        9,
        make=lambda seq: {
            "seq": seq,
            "kind": "long",
            "body": f"{seq}-" + ("x" * 11_000),
        },
    )
    state = new_state(
        owner_home,
        "http://src.example/pull",
        {"full_read_per_pull": 48},
    )
    state.audit_guarantee = True
    persist_state(state)
    assert hv._harvest_cycle(state, source.handle)

    rows = [
        json.loads(line) for line in hv.spool_path(state).read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) > 1
    assert sum(len(row["candidates"]) for row in rows) == 9
    restored = [candidate["event"] for row in rows for candidate in row["candidates"]]
    assert [event["seq"] for event in restored] == list(range(9))
    assert all(len(event["body"]) > 11_000 for event in restored)

    first, _backlog = hv.read_spool_records(
        state,
        max_candidates=48,
        max_bytes=40_000,
        consumer="byte-bounded",
    )
    assert 1 <= len(_ack_ids(first)) < 9
    assert all(
        "__truncated__" not in candidate["event"]
        for row in first
        for candidate in row["candidates"]
    )


# ── 0b. fetch→spool→cursor crash boundary ──


def test_harvest_state_commit_crash_rolls_back_append_and_retries_once(
    owner_home,
    monkeypatch,
):
    """落盘后、状态提交前崩溃：半拍不可见，接替后从原游标只重做一次。"""
    source = _FakeSource()
    source.feed(4)
    state = new_state(
        owner_home,
        "http://src.example/pull",
        {"full_read_per_pull": 4},
    )
    state.audit_guarantee = True
    persist_state(state)
    original_persist = hv.persist_state
    failed = False

    def fail_first_commit(selected):
        nonlocal failed
        if selected.harvest_commit_id and not failed:
            failed = True
            raise OSError("simulated crash before canonical state replace")
        return original_persist(selected)

    monkeypatch.setattr(hv, "persist_state", fail_first_commit)
    transaction = hv._begin_harvest_transaction(state)
    with pytest.raises(OSError, match="simulated crash"):
        hv._harvest_cycle_transaction(state, source.handle, transaction)

    assert hv._harvest_transaction_path(state).exists()
    assert len(hv.spool_path(state).read_text(encoding="utf-8").splitlines()) == 4
    hidden, _backlog = hv.read_spool_records(
        state,
        max_candidates=8,
        consumer="replacement",
    )
    assert hidden == []  # 未提交行不能被判读工抢先看见

    monkeypatch.setattr(hv, "persist_state", original_persist)
    assert hv._recover_pending_harvest(state)
    assert state.cursor == 0
    assert state.spool_seq == 0
    assert state.engine.totals["events_seen"] == 0
    assert hv.spool_path(state).read_text(encoding="utf-8") == ""

    assert hv._harvest_cycle(state, source.handle)
    rows = [
        json.loads(line)
        for line in hv.spool_path(state).read_text(encoding="utf-8").splitlines()
    ]
    assert [row["spool_seq"] for row in rows] == [1, 2, 3, 4]
    assert [row["candidates"][0]["event"]["seq"] for row in rows] == [0, 1, 2, 3]
    assert state.cursor == 4


def test_harvest_crash_after_state_commit_keeps_rows_without_reappend(
    owner_home,
    monkeypatch,
):
    """状态提交后、意图清理前崩溃：提交编号证明整拍有效，接替不得回滚或重写。"""
    source = _FakeSource()
    source.feed(3)
    state = new_state(
        owner_home,
        "http://src.example/pull",
        {"full_read_per_pull": 3},
    )
    state.audit_guarantee = True
    persist_state(state)
    original_clear = hv._clear_harvest_transaction_unlocked
    failed = False

    def crash_before_marker_cleanup(selected, *, expected_transaction_id):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated crash after canonical state replace")
        return original_clear(
            selected,
            expected_transaction_id=expected_transaction_id,
        )

    monkeypatch.setattr(
        hv,
        "_clear_harvest_transaction_unlocked",
        crash_before_marker_cleanup,
    )
    transaction = hv._begin_harvest_transaction(state)
    with pytest.raises(OSError, match="simulated crash"):
        hv._harvest_cycle_transaction(state, source.handle, transaction)
    assert state.cursor == 3
    assert hv._harvest_transaction_path(state).exists()

    monkeypatch.setattr(hv, "_clear_harvest_transaction_unlocked", original_clear)
    assert hv._recover_pending_harvest(state)
    assert not hv._harvest_transaction_path(state).exists()
    assert hv._harvest_cycle(state, source.handle)  # 源已追平，不会重写旧记录
    rows = [
        json.loads(line)
        for line in hv.spool_path(state).read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 3
    assert [row["candidates"][0]["event"]["seq"] for row in rows] == [0, 1, 2]


def test_superseded_harvester_attempt_cannot_write_or_rollback_replacement(
    owner_home,
):
    """旧收割线程卡住后迟到：新事务一旦接管，旧事务既不能写，也不能回滚新事务。"""
    state = new_state(
        owner_home,
        "http://src.example/pull",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    persist_state(state)
    stale = hv._begin_harvest_transaction(state)
    assert hv._recover_pending_harvest(state)  # 接替者撤销旧半拍
    replacement = hv._begin_harvest_transaction(state)

    with pytest.raises(hv._HarvestTransactionUnavailable, match="superseded"):
        hv._assert_active_harvest_transaction(state, stale)
    assert (
        hv._recover_pending_harvest(
            state,
            expected_transaction_id=str(stale["transaction_id"]),
        )
        is False
    )
    current = hv._read_harvest_transaction(state)
    assert current is not None
    assert current["transaction_id"] == replacement["transaction_id"]
    assert hv._recover_pending_harvest(
        state,
        expected_transaction_id=str(replacement["transaction_id"]),
    )


# ── 1/2. ack-on-judge:交付立欠账,空 pull 抽不干,结论交齐才签收发新批 ──


def test_delivery_carries_ack_ids_and_pending_acks(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 6, chunk=3)
    records, _bl = hv.read_spool_records(state, max_candidates=3, consumer="w0")
    ids = _ack_ids(records)
    assert ids and all(":" in aid for aid in ids)
    cursor = hv.read_spool_cursor(state)
    assert cursor["inflight"]["pending_acks"] == ids  # 欠账=本批全体令牌


def test_stale_source_attempt_cannot_claim_or_settle_after_takeover(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 2, chunk=2)
    state.audit_root_task_id = "audit-commit-fence"
    state.source_id = "source-commit-fence"
    persist_state(state)
    worker_key = audit_source_worker_key(
        state.audit_root_task_id,
        state.watch_id,
    )
    lease = {
        "schema_version": "audit-source-worker-lease.v1",
        "audit_id": state.audit_root_task_id,
        "source_id": state.source_id,
        "watch_id": state.watch_id,
        "worker_key": worker_key,
        "run_id": "run-current",
        "attempt_id": "attempt-current",
        "epoch": 2,
        "expires_at": 9_999_999_999.0,
    }
    lease_path = ws.state_dir(owner_home) / f"{state.watch_id}.worker-lease.json"
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    current = {
        **lease,
        "lease_epoch": lease["epoch"],
    }
    stale = {
        **current,
        "run_id": "run-stale",
        "attempt_id": "attempt-stale",
        "lease_epoch": 1,
    }

    rejected, backlog = hv.read_spool_records(
        state,
        max_candidates=2,
        consumer="run-stale",
        source_authority=stale,
    )
    assert rejected == []
    assert backlog["authorization_error"]["error_code"] == "AUDIT_SOURCE_ATTEMPT_STALE"
    assert hv.read_spool_cursor(state) == {}

    records, _backlog = hv.read_spool_records(
        state,
        max_candidates=2,
        consumer="run-current",
        source_authority=current,
    )
    ids = _ack_ids(records)
    stale_settle = hv.submit_verdicts(
        state,
        consumer="run-stale",
        verdicts=[_verdict(ack_id) for ack_id in ids],
        source_authority=stale,
    )
    assert stale_settle["ok"] is False
    assert stale_settle["error_code"] == "AUDIT_SOURCE_ATTEMPT_STALE"
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 0

    settled = hv.submit_verdicts(
        state,
        consumer="run-current",
        verdicts=[_verdict(ack_id) for ack_id in ids],
        source_authority=current,
    )
    assert settled["ok"] is True
    assert settled["acked_now"] == 2


def test_empty_pull_cannot_drain_cursor_without_verdicts(owner_home):
    """上一轮崩的根因回归:空壳判读工反复 pull,游标/ack 纹丝不动,拿到的永远是同一批+欠账。"""
    source = _FakeSource()
    state = _audit_state(owner_home, source, 6, chunk=3)
    first, _ = hv.read_spool_records(state, max_candidates=3, consumer="hollow")
    first_ids = _ack_ids(first)
    for _ in range(5):  # 空壳工五连 pull(判都没判)
        again, backlog = hv.read_spool_records(state, max_candidates=3, consumer="hollow")
        assert _ack_ids(again) == first_ids  # 重投同一批
        assert backlog.get("pending_verdicts") == len(first_ids)  # 欠账清单如实
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 0  # 一条都没被"签收"


def test_verdict_ledger_commit_before_ack_cursor_retries_without_duplicate_rows(
    owner_home,
    monkeypatch,
):
    """结论已写账、ACK 游标写失败：重试复用同一结论，不重复台账也不重复签收。"""
    source = _FakeSource()
    state = _audit_state(owner_home, source, 2, chunk=2)
    records, _ = hv.read_spool_records(state, max_candidates=2, consumer="judge-a")
    ids = _ack_ids(records)
    verdicts = [_verdict(ack_id, kind="hit", score=91) for ack_id in ids]
    original_write = hv._write_spool_cursor
    failed = False

    def fail_first_ack(selected, payload):
        nonlocal failed
        if not failed:
            failed = True
            return False
        return original_write(selected, payload)

    monkeypatch.setattr(hv, "_write_spool_cursor", fail_first_ack)
    first = hv.submit_verdicts(state, consumer="judge-a", verdicts=verdicts)
    assert first["ok"] is False
    ledger = hv._verdict_ledger_path(state)
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 0

    second = hv.submit_verdicts(state, consumer="judge-a", verdicts=verdicts)
    assert second["ok"] is True
    assert second["acked_now"] == 2
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2
    cursor = hv.read_spool_cursor(state)
    assert hv.acked_candidates(cursor) == 2
    assert "inflight" not in cursor


def test_audit_spool_uses_one_complete_record_per_physical_row(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 7, chunk=48)

    rows = [
        json.loads(line)
        for line in hv.spool_path(state).read_text(encoding="utf-8").splitlines()
    ]

    assert len(rows) == 7
    assert all(len(row["candidates"]) == 1 for row in rows)
    assert [row["candidates"][0]["event"]["seq"] for row in rows] == list(range(7))


def test_audit_pull_honors_per_call_complete_record_limit(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 20, chunk=48)
    state.audit_root_task_id = "audit-batch-limit"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-batch-limit",
        },
    )
    tool.agent._current_run_params.run_id = "audit-batch-limit"

    with _source_worker_context(tool, state):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                    "target_records": 5,
                }
            )
        )

    assert len(pulled["candidates"]) == 5
    assert all("__truncated__" not in row["event"] for row in pulled["candidates"])


def test_audit_pull_can_raise_default_batch_count_within_outer_limits(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 20, chunk=48)
    state.audit_root_task_id = "audit-batch-override"
    assert state.tuning.max_candidates_per_pull == 8
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-batch-override",
        },
    )
    tool.agent._current_run_params.run_id = "audit-batch-override"

    with _source_worker_context(tool, state):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                    "target_records": 20,
                }
            )
        )

    assert len(pulled["candidates"]) == 20
    assert all("__truncated__" not in row["event"] for row in pulled["candidates"])


def test_audit_large_record_target_is_bounded_by_shared_context_budget(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    state = new_state(
        owner_home,
        "http://src.example/pull",
        {"full_read_per_pull": 48},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = "audit-context-budget"
    persist_state(state)
    source.feed(
        30,
        make=lambda seq: {
            "seq": seq,
            "kind": "long",
            "body": f"{seq}-" + ("x" * 2_000),
        },
    )
    assert hv._harvest_cycle(state, source.handle)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-context-budget",
        },
    )
    tool.agent.config = AgentConfig(
        auto_save_memory=True,
        memory_compact_auto_trigger_percent=90,
        model_context_window_tokens=10_000,
    )
    tool.agent.backend = SimpleNamespace(
        context_window_tokens=10_000,
        max_tokens=16_000,
        name="fake",
    )
    tool.agent._current_run_params.run_id = "audit-context-budget"

    with _source_worker_context(tool, state):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                    "target_records": 300,
                }
            )
        )

    batch = pulled["batch_context"]
    assert 1 <= len(pulled["candidates"]) < 30
    assert batch["requested_records"] == 300
    assert batch["delivered_records"] == len(pulled["candidates"])
    assert 0 < batch["safe_batch_tokens"] <= 9_000
    assert batch["safe_batch_tokens"] == batch["context_safe_batch_tokens"]
    assert batch["safe_batch_tokens"] == batch["context_limit_tokens"]
    assert batch["model_output_budget_tokens"] == 16_000
    assert batch["model_output_budget_k_tokens"] == 16.0
    assert batch["verdict_output_tokens_per_record"] == 64
    assert batch["verdict_output_estimate_source"] == "initial_protocol_reserve"
    assert batch["estimated_output_safe_records"] > len(pulled["candidates"])
    # The byte watermark stops only between complete physical records, so the
    # rendered JSON estimate may include the final whole record's envelope.
    assert batch["estimated_input_tokens"] <= 10_000
    assert all("__truncated__" not in row["event"] for row in pulled["candidates"])


def test_audit_first_batch_uses_explicit_resource_ceiling(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    state = new_state(
        owner_home,
        "http://src.example/pull",
        {"full_read_per_pull": 48},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = "audit-configured-batch-ceiling"
    persist_state(state)
    source.feed(
        60,
        make=lambda seq: {
            "seq": seq,
            "kind": "long",
            "body": f"{seq}-" + ("x" * 2_000),
        },
    )
    assert hv._harvest_cycle(state, source.handle)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: state.audit_root_task_id,
        },
    )
    tool.agent.config = AgentConfig(
        memory_compact_auto_trigger_percent=90,
        model_context_window_tokens=200_000,
    )
    tool.agent.backend = SimpleNamespace(
        context_window_tokens=200_000,
        name="fake",
    )
    tool.agent._current_run_params.run_id = state.audit_root_task_id

    with _source_worker_context(tool, state):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                    "target_records": 60,
                }
            )
        )

    batch = pulled["batch_context"]
    assert batch["context_safe_batch_tokens"] > 30_000
    assert batch["configured_batch_max_tokens"] == 45_000
    assert batch["safe_batch_tokens"] == 45_000
    assert 1 <= len(pulled["candidates"]) < 60
    assert batch["estimated_input_tokens"] <= 46_000
    assert all("__truncated__" not in row["event"] for row in pulled["candidates"])


def test_audit_timeout_halves_same_inflight_replay_without_dropping_records(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    state = new_state(
        owner_home,
        "http://src.example/pull",
        {
            "full_read_per_pull": 48,
            "guarantee_batch_max_tokens": 60_000,
        },
    )
    state.audit_guarantee = True
    state.audit_root_task_id = "audit-timeout-smaller-replay"
    persist_state(state)
    source.feed(
        60,
        make=lambda seq: {
            "seq": seq,
            "kind": "long",
            "body": f"{seq}-" + ("x" * 2_000),
        },
    )
    assert hv._harvest_cycle(state, source.handle)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: state.audit_root_task_id,
        },
    )
    tool.agent.config = AgentConfig(
        memory_compact_auto_trigger_percent=90,
        model_context_window_tokens=200_000,
    )
    tool.agent.backend = SimpleNamespace(
        context_window_tokens=200_000,
        name="fake",
    )
    tool.agent._current_run_params.run_id = state.audit_root_task_id

    with _source_worker_context(tool, state):
        first = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                    "target_records": 60,
                }
            )
        )
    first_count = len(first["candidates"])
    first_tokens = int(first["batch_context"]["estimated_input_tokens"])
    assert first_count > 1
    assert hv.record_audit_batch_failure(state, reason="provider_timeout")
    # The same supervisor observation is idempotent; it cannot repeatedly
    # halve before a successor has received a new delivery.
    assert hv.record_audit_batch_failure(state, reason="provider_timeout")
    cursor = hv.read_spool_cursor(state)
    recovery = cursor["batch_recovery"]
    assert recovery["failure_count"] == 1
    assert recovery["max_batch_tokens"] == first_tokens // 2

    budget = wt._audit_batch_context_estimate(state, tool.agent)
    assert budget["recovery_batch_max_tokens"] == first_tokens // 2
    assert budget["safe_batch_tokens"] == first_tokens // 2
    replay, backlog = hv.read_spool_records(
        state,
        max_candidates=60,
        consumer="successor-run",
        max_bytes=int(budget["safe_batch_tokens"]) * 3,
    )
    replay_rows = [
        row
        for record in replay
        for row in record.get("candidates") or []
    ]
    assert 1 <= len(replay_rows) < first_count
    assert backlog["pending_verdicts_total"] == first_count
    assert backlog["pending_verdicts"] == len(replay_rows)
    assert all("__truncated__" not in row["event"] for row in replay_rows)


def test_audit_slice_rotation_does_not_shrink_a_partially_settled_delivery(
    owner_home,
):
    state = new_state(owner_home, "http://src.example/pull", {})
    state.audit_guarantee = True
    state.audit_root_task_id = "audit-partial-slice"
    persist_state(state)
    assert hv._write_spool_cursor(
        state,
        {
            "generation": 0,
            "inflight": {
                "delivery_ref": "ad-111111111111111111111111",
                "delivery_attempt": 1,
                "delivered_ack_ids": ["1:0", "2:0"],
                "pending_acks": ["2:0"],
                "estimated_input_tokens": 10_000,
            },
        },
    )

    assert not hv.record_audit_batch_failure(state, reason="slice_rotation")
    assert "batch_recovery" not in hv.read_spool_cursor(state)


def test_audit_failure_does_not_create_permanent_backoff_for_one_record(
    owner_home,
):
    state = new_state(owner_home, "http://src.example/pull", {})
    state.audit_guarantee = True
    state.audit_root_task_id = "audit-single-record-retry"
    persist_state(state)
    assert hv._write_spool_cursor(
        state,
        {
            "generation": 0,
            "inflight": {
                "delivery_ref": "ad-222222222222222222222222",
                "delivery_attempt": 1,
                "delivered_ack_ids": ["1:0"],
                "pending_acks": ["1:0"],
                "estimated_input_tokens": 331,
            },
        },
    )

    assert not hv.record_audit_batch_failure(state, reason="provider_timeout")
    assert "batch_recovery" not in hv.read_spool_cursor(state)


def test_audit_batch_backoff_relaxes_after_a_fully_settled_replay():
    updated = hv._finalize_delivery_throughput(
        {
            "batch_recovery": {
                "schema": "audit-batch-recovery.v1",
                "max_batch_tokens": 10_000,
                "failure_count": 1,
            }
        },
        {
            "delivered_at": 100.0,
            "delivered_ack_ids": ["1:0", "2:0"],
            "pending_acks": [],
            "estimated_input_tokens": 9_000,
        },
        now=110.0,
    )

    recovery = updated["batch_recovery"]
    assert recovery["max_batch_tokens"] == 20_000
    assert recovery["successful_delivery_count"] == 1
    assert recovery["last_success_input_tokens"] == 9_000


def test_audit_batch_backoff_growth_stops_at_configured_ceiling():
    updated = hv._finalize_delivery_throughput(
        {
            "batch_recovery": {
                "schema": "audit-batch-recovery.v1",
                "max_batch_tokens": 20_000,
                "failure_count": 1,
            }
        },
        {
            "delivered_at": 100.0,
            "delivered_ack_ids": ["1:0", "2:0"],
            "pending_acks": [],
            "estimated_input_tokens": 18_000,
        },
        now=110.0,
        recovery_max_tokens=30_000,
    )

    assert updated["batch_recovery"]["max_batch_tokens"] == 30_000


def test_audit_batch_backoff_relaxes_before_original_inflight_is_empty(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 8, chunk=8)
    first, _ = hv.read_spool_records(state, max_candidates=8, consumer="worker")
    ids = _ack_ids(first)
    cursor = hv.read_spool_cursor(state)
    cursor["batch_recovery"] = {
        "schema": "audit-batch-recovery.v1",
        "max_batch_tokens": 1_000,
        "failure_count": 1,
    }
    assert hv._write_spool_cursor(state, cursor)

    replay, _ = hv.read_spool_records(
        state,
        max_candidates=3,
        consumer="worker",
    )
    replay_ids = _ack_ids(replay)
    assert replay_ids == ids[:3]
    result = hv.submit_verdicts(
        state,
        consumer="worker",
        verdicts=[_verdict(ack_id) for ack_id in replay_ids],
        model_output_records=3,
        model_output_bytes=450,
        model_output_max_row_bytes=160,
    )

    assert result["pending_remaining"] == 5
    cursor = hv.read_spool_cursor(state)
    assert cursor["batch_recovery"]["max_batch_tokens"] == 2_000
    assert cursor["batch_recovery"]["successful_delivery_count"] == 1
    assert cursor["processing_throughput"]["settled_records"] == 3
    assert cursor["verdict_output_profile"]["observed_records"] == 3


def test_audit_sources_have_separate_workers_and_context_budgets(
    owner_home,
    monkeypatch,
):
    first_source = _FakeSource()
    second_source = _FakeSource()
    first = new_state(
        owner_home,
        "http://first.example/pull",
        {"full_read_per_pull": 48},
    )
    second = new_state(
        owner_home,
        "http://second.example/pull",
        {"full_read_per_pull": 48},
    )
    for state in (first, second):
        state.audit_guarantee = True
        state.audit_root_task_id = "audit-separate-worker-budget"
        persist_state(state)
    first_source.feed(
        3,
        make=lambda seq: {
            "seq": seq,
            "kind": "long",
            "body": f"{seq}-" + ("x" * 4_300),
        },
    )
    second_source.feed(3)
    assert hv._harvest_cycle(first, first_source.handle)
    assert hv._harvest_cycle(second, second_source.handle)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    first_tool = _tool(
        owner_home,
        first_source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-separate-worker-budget",
        },
    )
    first_tool.agent.config = AgentConfig(
        auto_save_memory=True,
        memory_compact_auto_trigger_percent=90,
        model_context_window_tokens=10_000,
    )
    first_tool.agent.backend = SimpleNamespace(context_window_tokens=10_000, name="fake")
    first_tool.agent._current_run_params.run_id = "first-consumer"

    with _source_worker_context(first_tool, first, run_id="first-consumer"):
        first_pull = _payload(
            first_tool.execute(
                {
                    "action": "pull",
                    "watch_id": first.watch_id,
                    "max_wait_seconds": 0,
                    "target_records": 50,
                }
            )
        )
        denied = first_tool.execute(
            {
                "action": "pull",
                "watch_id": second.watch_id,
                "max_wait_seconds": 0,
                "target_records": 50,
            }
        )
        assert denied.ok is False
        assert denied.error_code == "TOOL_PERMISSION_DENIED"
    assert first_pull["candidates"]
    assert first_pull["batch_context"]["context_limit_tokens"] > 0
    assert "consumer_inflight_watch_ids" not in first_pull["batch_context"]

    second_tool = _tool(
        owner_home,
        second_source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-separate-worker-budget",
        },
    )
    second_tool.agent.config = first_tool.agent.config
    second_tool.agent.backend = first_tool.agent.backend
    with _source_worker_context(second_tool, second, run_id="second-consumer"):
        independent = _payload(
            second_tool.execute(
                {
                    "action": "pull",
                    "watch_id": second.watch_id,
                    "max_wait_seconds": 0,
                    "target_records": 50,
                }
            )
        )
    assert independent["candidates"]


def test_audit_pull_rejects_invalid_per_call_record_limit(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 1, chunk=1)
    tool = _tool(owner_home, source, task_attributes={AUDIT_ATTR: True})

    with _source_worker_context(tool, state):
        result = tool.execute(
            {
                "action": "pull",
                "watch_id": state.watch_id,
                "target_records": 0,
            }
        )

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_watch_action_rejects_parameters_owned_by_another_action(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 1, chunk=1)
    tool = _tool(owner_home, source, task_attributes={AUDIT_ATTR: True})

    with _source_worker_context(tool, state):
        result = tool.execute(
            {
                "action": "pull",
                "watch_id": state.watch_id,
                "delivery_ref": "ad-123456789012345678901234",
                "max_wait_seconds": 0,
            }
        )

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.reported_error_code == "TOOL_ACTION_PARAMETER_MISMATCH"
    assert "delivery_ref" in result.output


def test_watch_action_accepts_exact_registry_execution_identity(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source, task_attributes={AUDIT_ATTR: True})

    result = tool.execute(
        {
            "action": "list",
            "__run_scope": {
                "request_id": "request-a",
                "task_id": "audit-a",
                "run_id": "run-a",
            },
            "__tool_call_id": "call-a",
        }
    )

    assert result.ok is True


def test_nonaudit_next_pull_still_acks_baseline(owner_home):
    """对照(零回归口径):非保证档同人第二次 pull 即确认上一批——旧路语义不变。"""
    source = _FakeSource()
    state = new_state(
        owner_home, "http://src.example/pull", {"full_read_per_pull": 3}
    )
    persist_state(state)
    source.feed(6)
    assert hv._harvest_cycle(state, source.handle)
    hv.read_spool_records(state, max_candidates=3, consumer="w0")
    hv.read_spool_records(state, max_candidates=3, consumer="w0")
    assert hv.acked_candidates(hv.read_spool_cursor(state)) > 0


def test_verdicts_settle_batch_and_release_next(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 8, chunk=2)
    first, _ = hv.read_spool_records(state, max_candidates=4, consumer="w0")
    ids = _ack_ids(first)
    assert len(ids) == 4
    # 部分交:acked 逐条推进、在途保留、欠账缩水
    part = hv.submit_verdicts(
        state,
        consumer="w0",
        verdicts=[
            _verdict(ids[0], "hit", 98, "结果端明确生效"),
            _verdict(ids[1], score=3),
        ],
        model_output_records=2,
        model_output_bytes=360,
        model_output_max_row_bytes=170,
    )
    assert part["ok"] and part["acked_now"] == 2 and part["pending_remaining"] == 2
    cursor = hv.read_spool_cursor(state)
    assert hv.acked_candidates(cursor) == 2
    assert set(cursor["inflight"]["pending_acks"]) == set(ids[2:])
    first_profile = cursor["verdict_output_profile"]
    assert first_profile["observed_records"] == 2
    assert first_profile["serialized_bytes"] == 360
    assert first_profile["completed_delivery_samples"] == 1
    assert cursor["inflight"]["model_output_records"] == 0
    assert cursor["inflight"]["model_output_bytes"] == 0
    # 欠账未清:pull 只重投尚未写账的完整记录，不把已判过的内容再次塞回上下文。
    again, backlog = hv.read_spool_records(state, max_candidates=4, consumer="w0")
    assert _ack_ids(again) == ids[2:]
    assert backlog.get("pending_verdicts") == 2
    assert backlog.get("pending_verdicts_total") == 2
    # 交齐:在途摘除,下一次 pull 发新批
    rest = hv.submit_verdicts(
        state,
        consumer="w0",
        verdicts=[
            {"ack_id": ids[2], "verdict": "unsure", "score": 50, "note": "两端对不齐,存疑"},
            _verdict(ids[3], score=6),
        ],
        model_output_records=2,
        model_output_bytes=420,
        model_output_max_row_bytes=210,
    )
    assert rest["ok"] and rest["pending_remaining"] == 0
    cursor = hv.read_spool_cursor(state)
    assert "inflight" not in cursor
    output_profile = cursor["verdict_output_profile"]
    assert output_profile["schema"] == "audit-verdict-output-profile.v1"
    assert output_profile["observed_records"] == 4
    assert output_profile["serialized_bytes"] == 780
    assert output_profile["average_bytes_per_record"] == 195.0
    assert output_profile["max_row_bytes"] == 210
    assert output_profile["completed_delivery_samples"] == 2
    assert output_profile["updated_at"] > 0
    fresh, _ = hv.read_spool_records(state, max_candidates=4, consumer="w0")
    fresh_ids = _ack_ids(fresh)
    assert fresh_ids and not (set(fresh_ids) & set(ids))
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 4


def test_partial_inflight_budget_uses_pending_debt_not_fresh_unread(
    owner_home,
    monkeypatch,
):
    """部分销账后的续投仍按整份欠账预算，不能退化成每轮一条。"""

    source = _FakeSource()
    state = _audit_state(owner_home, source, 10, chunk=10)
    state.audit_root_task_id = "audit-partial-budget"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: state.audit_root_task_id,
        },
    )
    tool.agent.config = AgentConfig(
        memory_compact_auto_trigger_percent=90,
        model_context_window_tokens=200_000,
    )
    tool.agent.backend = SimpleNamespace(
        context_window_tokens=200_000,
        max_tokens=16_000,
        name="fake",
    )

    with _source_worker_context(tool, state) as task:
        first = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                    "target_records": 10,
                }
            )
        )
        ids = [row["ack_id"] for row in first["candidates"]]
        assert len(ids) == 10
        partial = hv.submit_verdicts(
            state,
            consumer=task.id,
            verdicts=[_verdict(ack_id) for ack_id in ids[:4]],
        )
        assert partial["acked_now"] == 4
        assert partial["pending_remaining"] == 6
        assert hv.spool_unread(state) == 0

        budget = wt._audit_batch_context_estimate(state, tool.agent)
        assert budget["unread_records"] == 0
        assert budget["delivery_candidate_records"] == 6
        assert budget["estimated_output_safe_records"] == 6

        replay = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )

    assert [row["ack_id"] for row in replay["candidates"]] == ids[4:]
    assert replay["batch_context"]["effective_record_limit"] == 6


def test_unknown_ack_ids_rejected_not_credited(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 4, chunk=2)
    records, _ = hv.read_spool_records(state, max_candidates=2, consumer="w0")
    ids = _ack_ids(records)
    result = hv.submit_verdicts(
        state,
        consumer="w0",
        verdicts=[
            _verdict("999:7"),  # 手编令牌
            _verdict(ids[0], "hit", 97),
            _verdict(ids[1], "bogus-kind", 50),  # 非法结论
        ],
    )
    assert result["ok"] is False
    assert result["acked_now"] == 0
    assert result["malformed"] == 1
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 0

    repaired = hv.submit_verdicts(
        state,
        consumer="w0",
        verdicts=[_verdict("999:7"), _verdict(ids[0], "hit", 97)],
    )
    assert repaired["acked_now"] == 1
    assert repaired["unknown_ack_ids"] == ["999:7"]
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 1


def test_missing_or_out_of_range_scores_are_not_acked(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 3, chunk=3)
    records, _ = hv.read_spool_records(state, max_candidates=3, consumer="w0")
    ids = _ack_ids(records)
    result = hv.submit_verdicts(
        state,
        consumer="w0",
        verdicts=[
            {"ack_id": ids[0], "verdict": "clear"},
            {"ack_id": ids[1], "verdict": "hit", "score": 101},
            _verdict(ids[2], "unsure", 50),
        ],
    )
    assert result["ok"] is False
    assert result["acked_now"] == 0
    assert result["malformed"] == 2
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 0

    repaired = hv.submit_verdicts(
        state,
        consumer="w0",
        verdicts=[_verdict(ids[2], "unsure", 50)],
    )
    assert repaired["acked_now"] == 1
    assert repaired["pending_remaining"] == 2


def test_current_delivery_settles_valid_rows_and_redelivers_only_malformed_row(
    owner_home,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 3, chunk=3)
    records, _ = hv.read_spool_records(state, max_candidates=3, consumer="w0")
    ids = _ack_ids(records)
    cursor = hv.read_spool_cursor(state)
    delivery_ref = str(cursor["inflight"]["delivery_ref"])

    result = hv.submit_verdicts(
        state,
        consumer="w0",
        delivery_ref=delivery_ref,
        verdicts=[
            _verdict(ids[0], "clear", 5),
            {
                "ack_id": ids[1],
                "verdict": "hit",
                "score": 95,
                "note": "确认命中，但模型漏了结构化 finding",
            },
            _verdict(ids[2], "unsure", 50),
        ],
    )

    assert result["ok"] is True
    assert result["partial"] is True
    assert result["acked_now"] == 2
    assert result["malformed"] == 1
    assert result["malformed_ack_ids"] == [ids[1]]
    assert result["pending_remaining"] == 1
    assert hv.acked_candidates(hv.read_spool_cursor(state)) == 2

    replay, backlog = hv.read_spool_records(
        state,
        max_candidates=3,
        consumer="w0",
    )
    assert _ack_ids(replay) == [ids[1]]
    assert backlog["pending_verdicts_total"] == 1


def test_takeover_redelivers_with_pending_acks(owner_home):
    """换人重投带欠账:继任者拿到同一批+同一份欠账,交结论照常销账(问责跨换人成立)。"""
    source = _FakeSource()
    state = _audit_state(owner_home, source, 4, chunk=2)
    first, _ = hv.read_spool_records(state, max_candidates=2, consumer="died")
    ids = _ack_ids(first)
    taken, backlog = hv.read_spool_records(state, max_candidates=2, consumer="successor")
    assert _ack_ids(taken) == ids
    assert backlog.get("pending_verdicts") == len(ids)
    done = hv.submit_verdicts(
        state, consumer="successor", verdicts=[_verdict(aid, score=4) for aid in ids]
    )
    assert done["ok"] and done["pending_remaining"] == 0


def test_partial_takeover_replays_only_pending_rows_and_respects_dynamic_view_limit(
    owner_home,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 10, chunk=10)
    first, _ = hv.read_spool_records(state, max_candidates=10, consumer="died")
    ids = _ack_ids(first)
    part = hv.submit_verdicts(
        state,
        consumer="died",
        verdicts=[_verdict(ack_id) for ack_id in ids[:4]],
    )
    assert part["acked_now"] == 4 and part["pending_remaining"] == 6

    replay, backlog = hv.read_spool_records(
        state,
        max_candidates=3,
        consumer="successor",
    )
    assert _ack_ids(replay) == ids[4:7]
    assert backlog["pending_verdicts"] == 3
    assert backlog["pending_verdicts_total"] == 6
    cursor = hv.read_spool_cursor(state)
    assert cursor["inflight"]["pending_acks"] == ids[4:]
    assert cursor["inflight"]["delivered_ack_ids"] == ids[4:7]

    settled = hv.submit_verdicts(
        state,
        consumer="successor",
        verdicts=[_verdict(ack_id) for ack_id in ids[4:7]],
    )
    assert settled["acked_now"] == 3 and settled["pending_remaining"] == 3
    final_replay, final_backlog = hv.read_spool_records(
        state,
        max_candidates=3,
        consumer="successor",
    )
    assert _ack_ids(final_replay) == ids[7:]
    assert final_backlog["pending_verdicts_total"] == 3


def test_processing_rate_never_mutates_model_selected_durable_batch():
    context = {
        "observed_settled_records": 100,
        "observed_active_seconds": 300.0,
        "estimated_time_safe_records": 83,
        "estimated_output_safe_records": 90,
    }
    assert wt._effective_audit_record_limit(
        requested_records=150,
        batch_context=context,
    ) == 90
    assert wt._effective_audit_record_limit(
        requested_records=20,
        batch_context=context,
    ) == 20
    assert wt._effective_audit_record_limit(
        requested_records=150,
        batch_context={
            **context,
            "estimated_time_safe_records": 1,
            "estimated_output_safe_records": 155,
        },
    ) == 150
    assert wt._effective_audit_record_limit(
        requested_records=150,
        batch_context={
            "observed_settled_records": 0,
            "observed_active_seconds": 0.0,
            "estimated_time_safe_records": 0,
            "estimated_output_safe_records": 75,
        },
    ) == 75


# ── 3. 引擎零丢弃:normal 规则命中也逐条入队;无压组无溢出 ──


def test_guarantee_engine_lifts_normal_rule_hits(owner_home):
    from agent.ingestion.config import IngestTuning
    from agent.ingestion.engine import StreamDigestEngine
    from agent.ingestion.source_spec import parse_source_spec

    tuning = IngestTuning(full_read_per_pull=48)
    plain = StreamDigestEngine(tuning)
    guarded = StreamDigestEngine(tuning)
    spec = parse_source_spec({"result_field": "state", "normal_values": ["ok"]})
    plain.apply_spec(spec)
    guarded.apply_spec(spec)
    # 同签名事件(无逐条变化字段):稀有兜底只抬前几条,规则减负路径才走得到
    events = [(i, {"state": "ok", "kind": "beat"}) for i in range(12)]
    baseline = plain.process(list(events), 1000.0)
    receipt = guarded.process(list(events), 1000.0, guarantee=True)
    # 非保证档:normal 规则命中走压组减负(候选少于全量);保证档:每条都成候选
    assert len(receipt.candidates) == 12
    assert receipt.suppressed_total == 0 and not receipt.overflow
    assert len(baseline.candidates) < 12  # 对照:旧路确实在减负(规则本身有效)
    # 规则命中账两边都在记(保证档只是不拿它筛,记账不打折)
    assert guarded.totals["spec_normal_rule_hits"] > 0


def test_guarantee_forces_full_read_even_with_zero_escape_valve(owner_home):
    """full_read_per_pull=0 是非保证档的逃生阀(回落有损分诊);保证档没有这条路。"""
    from agent.ingestion.config import IngestTuning
    from agent.ingestion.engine import StreamDigestEngine

    engine = StreamDigestEngine(IngestTuning(full_read_per_pull=0))
    events = [(i, {"seq": i, "kind": "beat", "note": f"n{i}"}) for i in range(20)]
    digest = engine.process(events, 1000.0, guarantee=True)
    assert len(digest.candidates) == 20
    assert digest.suppressed_total == 0 and not digest.overflow


# ── 4. 覆盖回执:入队/已判/待判/丢弃 对账;丢弃恒 0 ──


def test_audit_receipt_reconciles(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 6, chunk=3)
    receipt = hv.audit_receipt_facts(state)
    assert receipt["enqueued"] == 6 and receipt["judged"] == 0
    assert receipt["pending"] == 6 and receipt["dropped"] == 0
    records, _ = hv.read_spool_records(state, max_candidates=3, consumer="w0")
    ids = _ack_ids(records)
    hv.submit_verdicts(
        state,
        consumer="w0",
        verdicts=[
            _verdict(ids[0], "hit", 99),
            _verdict(ids[1], "clear", 2),
            _verdict(ids[2], "unsure", 50),
        ],
    )
    receipt = hv.audit_receipt_facts(state)
    assert (receipt["judged"], receipt["scored"], receipt["pending"], receipt["dropped"]) == (
        3,
        3,
        3,
        0,
    )
    assert receipt["verdicts"] == {"hit": 1, "clear": 1, "unsure": 1}


def test_audit_capacity_facts_expose_backlog_age_rate_and_recent_latency(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 2, chunk=2)
    state.tuning = replace(
        state.tuning,
        capacity_alert_backlog_records=1,
        capacity_alert_oldest_seconds=1,
    )

    before = hv.audit_capacity_facts(state, now=time.time() + 2.0)

    assert before["pending"] == 2
    assert before["oldest_pending_age_seconds"] >= 1.0
    assert before["capacity_alert"]["active"] is True
    records, _ = hv.read_spool_records(state, max_candidates=2, consumer="capacity-worker")
    ids = _ack_ids(records)
    hv.submit_verdicts(
        state,
        consumer="capacity-worker",
        verdicts=[_verdict(aid, score=1) for aid in ids],
    )

    after = hv.audit_capacity_facts(state)

    assert after["pending"] == 0
    assert after["capacity_alert"]["active"] is False
    assert after["processing_throughput"]["settled_records"] == 2
    assert after["processing_latency"]["sample_count"] == 2


def test_audit_capacity_uses_recent_ingest_rate_and_zeroes_stale_or_finalized_feed(
    owner_home,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 2, chunk=2)
    observed_at = time.time()
    state.window_finalized_at = 0.0
    state.ingest_records_per_second = 5.25
    state.ingest_rate_updated_at = observed_at

    current = hv.audit_capacity_facts(state, now=observed_at + 1.0)
    stale = hv.audit_capacity_facts(state, now=observed_at + 31.0)
    state.window_finalized_at = observed_at + 2.0
    finalized = hv.audit_capacity_facts(state, now=observed_at + 3.0)

    assert current["ingest_records_per_second"] == 5.25
    assert stale["ingest_records_per_second"] == 0.0
    assert finalized["ingest_records_per_second"] == 0.0


def test_verdict_ledger_appended(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 2, chunk=2)
    records, _ = hv.read_spool_records(state, max_candidates=2, consumer="w0")
    ids = _ack_ids(records)
    hv.submit_verdicts(
        state,
        consumer="w0",
        verdicts=[_verdict(aid, score=7) for aid in ids],
    )
    ledger = ws.state_dir(owner_home) / f"{state.watch_id}.verdicts.ndjson"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [row["ack_id"] for row in rows] == ids
    assert all(
        row["verdict"] == "clear" and row["score"] == 7 and row["by"] == "w0" for row in rows
    )


def test_custom_score_ranges_and_dimensions_round_trip(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 1, chunk=1)
    records, _ = hv.read_spool_records(state, max_candidates=1, consumer="judge")
    ack_id = _ack_ids(records)[0]

    result = hv.submit_verdicts(
        state,
        consumer="judge",
        verdicts=[
            _verdict(
                ack_id,
                "unsure",
                17.5,
                "按用户定义的两个维度记录，程序不解释总分",
                score_range={"min": -10, "max": 30},
                dimensions=[
                    {
                        "name": "证据完整度",
                        "score": 7.5,
                        "range": {"min": 0, "max": 10},
                        "reason": "只有一侧记录",
                    },
                    {
                        "name": "关联强度",
                        "score": 10,
                        "range": {"min": 0, "max": 20},
                        "reason": "时间与主体一致",
                    },
                ],
            )
        ],
    )
    assert result["ok"] is True and result["acked_now"] == 1

    inspected = hv.inspect_audit_record(state, ack_id)
    verdict = inspected["verdict"]
    assert verdict["score"] == 17.5
    assert verdict["score_range"] == {"min": -10, "max": 30}
    assert [row["name"] for row in verdict["dimensions"]] == [
        "证据完整度",
        "关联强度",
    ]
    assert inspected["raw_complete"] is True
    assert inspected["event_sha256"]


def test_inspect_returns_exact_raw_event_and_judgment_provenance(owner_home):
    source = _FakeSource()
    long_body = "x" * 4000
    state = new_state(
        owner_home,
        "http://src.example/pull",
        {"full_read_per_pull": 2},
    )
    state.audit_guarantee = True
    persist_state(state)
    source.feed(
        1,
        make=lambda seq: {
            "seq": seq,
            "event_id": "evt-exact-1",
            "response": long_body,
        },
    )
    assert hv._harvest_cycle(state, source.handle)
    records, _ = hv.read_spool_records(
        state,
        max_candidates=2,
        consumer="judge-1",
    )
    ack_id = _ack_ids(records)[0]
    result = hv.submit_verdicts(
        state,
        consumer="judge-1",
        verdicts=[
                {
                    "ack_id": ack_id,
                    "verdict": "hit",
                    "score": 99,
                    "note": "结果端已生效",
                    "finding": {
                        "claim": "结果端已生效",
                        "kind": "hit",
                        "requires_llm_report": True,
                    },
                    "judge_mode": "fresh_batch",
                "judge_model": "MiniMax-M2.7",
                "judge_backend": "anthropic_compatible",
            }
        ],
    )
    assert result["acked_ids"] == [ack_id]

    inspected = hv.inspect_audit_record(state, ack_id)
    assert inspected["ok"] is True
    assert inspected["raw_complete"] is True
    assert inspected["raw_event"]["response"] == long_body
    assert len(inspected["event_sha256"]) == 64
    assert inspected["source_ref"].endswith(f"/{ack_id}")
    assert inspected["verdict"]["judge_model"] == "MiniMax-M2.7"
    assert inspected["verdict"]["score"] == 99
    assert inspected["event_bytes"] > 4000
    assert inspected["source_cursor"] == 1
    assert inspected["stream_pos"] == 0
    assert inspected["processing_status"] == {
        "phase": "judged",
        "persisted": True,
        "acknowledged": True,
        "reviewed": False,
        "reported": False,
        "review_count": 0,
        "report_count": 0,
        "last_reviewed_at": None,
        "last_reported_at": None,
        "delivery_receipt_ids": [],
    }

    # The public tool action is owner-scoped and read-only, and returns the same
    # immutable evidence without advancing the spool cursor.
    cursor_before = hv.read_spool_cursor(state)
    tool = _tool(owner_home, source, task_attributes={AUDIT_ATTR: True})
    through_tool = _payload(
        tool.execute(
            {
                "action": "inspect",
                "watch_id": state.watch_id,
                "ack_id": ack_id,
            }
        )
    )
    assert through_tool["raw_event"]["response"] == long_body
    assert through_tool["source_ref"] == inspected["source_ref"]
    assert hv.read_spool_cursor(state) == cursor_before


def test_optional_review_and_successful_delivery_are_traced(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 1, chunk=1)
    records, _ = hv.read_spool_records(
        state,
        max_candidates=1,
        consumer="judge",
    )
    ack_id = _ack_ids(records)[0]
    initial = hv.submit_verdicts(
        state,
        consumer="judge",
        verdicts=[_verdict(ack_id, "unsure", 55, "首次证据不足")],
    )
    assert initial["acked_now"] == 1
    receipt_before = hv.audit_receipt_facts(state)

    reviewed = hv.submit_verdicts(
        state,
        consumer="reviewer",
        verdicts=[
            _verdict(
                ack_id,
                "hit",
                91,
                "补充证据后复核为需要关注",
                review=True,
                judge_model="MiniMax-M2.7",
            )
        ],
    )
    assert reviewed["ok"] is True
    assert reviewed["acked_now"] == 0
    assert reviewed["reviewed_now"] == 1
    assert reviewed["reviewed_ids"] == [ack_id]
    assert hv.audit_receipt_facts(state) == receipt_before

    source_ref = hv.audit_source_ref(state, ack_id)
    recorded = hv.record_audit_delivery_refs(
        owner_home,
        [source_ref],
        receipt_id="receipt-1",
        channel="feishu",
        delivered_at=1234.5,
    )
    assert recorded == [source_ref]
    # Same provider receipt is idempotent.
    assert hv.record_audit_delivery_refs(
        owner_home,
        [source_ref],
        receipt_id="receipt-1",
        channel="feishu",
        delivered_at=1235.0,
    ) == [source_ref]

    inspected = hv.inspect_audit_record(state, ack_id)
    assert [row["stage"] for row in inspected["verdict_history"]] == [
        "initial",
        "review",
    ]
    assert inspected["verdict"]["verdict"] == "hit"
    assert inspected["processing_status"]["phase"] == "reported"
    assert inspected["processing_status"]["acknowledged"] is True
    assert inspected["processing_status"]["reviewed"] is True
    assert inspected["processing_status"]["reported"] is True
    assert inspected["processing_status"]["review_count"] == 1
    assert inspected["processing_status"]["report_count"] == 1
    assert inspected["processing_status"]["delivery_receipt_ids"] == ["receipt-1"]


def test_review_and_report_refs_fail_closed_outside_owner(owner_home, tmp_path):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 1, chunk=1)
    records, _ = hv.read_spool_records(
        state,
        max_candidates=1,
        consumer="judge",
    )
    ack_id = _ack_ids(records)[0]
    before = hv.submit_verdicts(
        state,
        consumer="reviewer",
        verdicts=[
            _verdict(
                ack_id,
                "hit",
                90,
                "不能跳过首次签收",
                review=True,
            )
        ],
    )
    assert before["ok"] is False and before["reviewed_now"] == 0

    source_ref = hv.audit_source_ref(state, ack_id)
    other_owner = tmp_path / "another-owner"
    assert (
        hv.record_audit_delivery_refs(
            other_owner,
            [source_ref],
            receipt_id="wrong-owner-receipt",
            channel="feishu",
        )
        == []
    )
    assert hv.inspect_audit_record(state, ack_id)["processing_status"]["reported"] is False


# ── 5. 判完归档:轮转切走的行进 archive 留痕 ──


def test_rotation_archives_judged_records_in_guarantee(owner_home, monkeypatch):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 6, chunk=2)
    # 判完全部(交结论签收),归档窗口出现
    for _ in range(6):
        records, _ = hv.read_spool_records(state, max_candidates=2, consumer="w0")
        if not records:
            break
        hv.submit_verdicts(
            state, consumer="w0", verdicts=[_verdict(aid, score=3) for aid in _ack_ids(records)]
        )
    monkeypatch.setattr(hv, "_SPOOL_ROTATE_BYTES", 1)
    # New Audit spools use one complete source record per physical row.  Keep
    # one unread row so rotation can archive only fully judged rows and retain
    # the exact unread row without dropping data.
    source.feed(1)
    assert hv._harvest_cycle(state, source.handle)
    assert state.spool_generation >= 1
    archive = ws.state_dir(owner_home) / f"{state.watch_id}.archive.ndjson"
    assert archive.exists()
    archived = [json.loads(line) for line in archive.read_text().splitlines()]
    assert archived and all(row.get("candidates") for row in archived)  # 已判记录原样留痕


# ── 6. 抬取背压:保证档不按判读积压停抬,只按磁盘水位 ──


def test_guarantee_ignores_judge_backlog_backpressure(owner_home):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 200, chunk=4)  # 未判积压远超 8×quota
    assert hv.spool_unread(state) > 0
    assert not hv._backpressured(state)  # 判读积压不背压(队列涨,不留源端)


def test_guarantee_disk_watermark_halts_harvest(owner_home):
    source = _FakeSource()
    state = new_state(
        owner_home,
        "http://src.example/pull",
        {
            "full_read_per_pull": 4,
            "guarantee_spool_max_mb": 1,
        },
    )
    state.audit_guarantee = True
    persist_state(state)
    source.feed(4)
    assert hv._harvest_cycle(state, source.handle)
    # 把 spool 撑过 1MB 水位 → 下一拍整拍停抬,单独记账
    hv.spool_path(state).open("a", encoding="utf-8").write("x" * (1024 * 1024 + 1) + "\n")
    assert hv._backpressured(state)
    before = int(state.totals.get("disk_backpressure_skips", 0))
    claimed, lease = hv._claim_harvester_lease(state)
    assert claimed
    try:
        assert (
            hv._locked_harvest_step(
                state,
                source.handle,
                lease_id=str(lease["lease_id"]),
            )
            == "ok"
        )
    finally:
        hv._clear_lease(state, str(lease["lease_id"]))
    assert int(state.totals["disk_backpressure_skips"]) == before + 1


# ── 7. 工具面:结构化 /audit 置位、棘轮、verdict 动作、inline 不回落 ──


def test_open_structured_audit_sets_guarantee_and_persists(owner_home):
    source = _FakeSource()
    tool = _audit_source_tool(owner_home, source)
    result = tool.execute({"action": "open", "url": _TOOL_URL, "mode": "cursor"})
    opened = _payload(result)
    assert opened["audit_guarantee"] is True and opened["audit_note"]
    assert opened["named_audit_effect"] == {
        "source_bound": True,
        "effective_prompt_updated": False,
    }
    lane = ws.load_state(owner_home, opened["watch_id"])
    assert lane is not None and lane.audit_guarantee is True
    assert lane.source_mode == ""
    assert opened["source_envelope"]["mode"] == "cursor"
    assert result.result_envelope["runtime_transition"] == {
        "kind": "context_refresh",
        "reason": "durable_tool_scope_changed",
        "resume": "next_durable_slice",
    }


def test_audit_source_open_without_exact_child_goal_fails_closed(owner_home):
    source = _FakeSource()
    tool = _audit_source_tool(owner_home, source, source_goal="")

    result = tool.execute({"action": "open", "url": _TOOL_URL})

    assert result.ok is False
    assert result.error_code == "AUDIT_SOURCE_TASK_CONTEXT_UNAVAILABLE"
    assert ws.list_states(owner_home) == []


def test_audit_source_binding_is_pinned_and_visible(owner_home, monkeypatch):
    source = _FakeSource()
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    attrs = {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: "audit-binding-1",
    }
    tool = _audit_source_tool(
        owner_home,
        source,
        task_attributes=attrs,
        source_goal="按已确认的 WAF 资料持续研判这一条来源",
    )
    opened = _payload(
        tool.execute(
            {
                "action": "open",
                "url": _TOOL_URL,
                "source_id": "waf-primary",
                "source_profile_ref": "docs/sources/waf.md#v3",
                "document_refs": [
                    "docs/sources/waf-fields.xlsx#sha256:abc",
                    "docs/sources/waf-api.md#v2",
                ],
            }
        )
    )

    binding = opened["source_binding"]
    assert binding["source_id"] == "waf-primary"
    assert binding["source_profile_ref"] == "docs/sources/waf.md#v3"
    assert binding["document_refs"] == [
        "docs/sources/waf-fields.xlsx#sha256:abc",
        "docs/sources/waf-api.md#v2",
    ]
    assert binding["source_config_version"].startswith("sha256:")
    persisted = ws.load_state(owner_home, opened["watch_id"])
    assert persisted is not None
    assert persisted.source_id == "waf-primary"
    assert persisted.document_refs == binding["document_refs"]

    status = _payload(
        tool.execute({"action": "status", "watch_id": opened["watch_id"]})
    )
    assert status["source_binding"] == binding
    conflicting = _audit_source_tool(
        owner_home,
        source,
        task_attributes=attrs,
        source_goal="按已确认的 WAF 资料持续研判这一条来源",
        run_id="source-binding-conflict",
        attempt_id="source-binding-conflict-attempt",
    )
    rejected = conflicting.execute(
        {
            "action": "open",
            "url": _TOOL_URL,
            "document_refs": ["docs/sources/waf-fields.xlsx#sha256:new"],
        }
    )
    assert rejected.ok is False
    assert rejected.error_code == "TOOL_INVALID_ARGUMENTS"
    assert rejected.reported_error_code == "AUDIT_SOURCE_BINDING_IMMUTABLE"
    unchanged = ws.load_state(owner_home, opened["watch_id"])
    assert unchanged is not None
    assert unchanged.document_refs == binding["document_refs"]


def test_open_uses_stamped_task_attribute_not_prompt_text(owner_home):
    source = _FakeSource()
    tool = _audit_source_tool(
        owner_home,
        source,
        user_prompt="/audit 盯这 5 个 API 几个月逐条研判",
        task_attributes={AUDIT_ATTR: True},
    )
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    assert opened["audit_guarantee"] is True


def test_audit_default_pull_wait_coalesces_but_explicit_zero_stays_immediate(
    owner_home,
):
    audit_state = new_state(owner_home, "http://src.example/audit", {})
    audit_state.audit_guarantee = True
    plain_state = new_state(owner_home, "http://src.example/plain", {})

    assert wt._resolved_pull_wait(audit_state, {}) == 15.0
    assert wt._resolved_pull_wait(audit_state, {"max_wait_seconds": 0}) == 0.0
    assert wt._resolved_pull_wait(plain_state, {}) == 0.0


def test_audit_open_clears_legacy_source_judgment_state(owner_home):
    source = _FakeSource()
    audit_id = "audit-legacy-clean"
    state = new_state(
        owner_home,
        "http://127.0.0.1:9/pull",
        {},
        watch_id=ws.watch_id_for(
            owner_home,
            "http://127.0.0.1:9/pull",
            audit_id,
        ),
    )
    state.audit_guarantee = True
    state.audit_root_task_id = audit_id
    state.audit_objective = "判断记录是否已经造成实际影响"
    state.source_envelope = {
        **state.source_envelope,
        "schema_note": "result 字段记录接收端事实",
    }
    state.judgment_note = "模型自己猜：只看某个高频关键词"
    state.source_spec = {"passthrough": True}
    persist_state(state)
    ws.registry.put(state)
    tool = _audit_source_tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            AUDIT_OBJECTIVE_ATTR: state.audit_objective,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
        },
    )

    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    current = ws.registry.get(state.watch_id)
    assert current is not None
    assert current.audit_objective == "判断记录是否已经造成实际影响"
    assert current.source_spec is None
    assert current.judgment_note == ""
    assert opened["source_spec"] is None
    assert opened["source_envelope"]["mode"] == "cursor"
    assert "schema_note" not in opened["source_envelope"]

    configured = tool.execute(
        {
            "action": "configure",
            "watch_id": state.watch_id,
            "judgment_note": "不应写入",
        }
    )
    assert configured.ok is False
    assert current.judgment_note == ""


def test_distinct_audits_of_same_url_have_independent_ledgers_and_exact_clear(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)

    def source_tool(task_id: str, objective: str) -> WatchStreamTool:
        return _audit_source_tool(
            owner_home,
            source,
            task_attributes={
                AUDIT_ATTR: True,
                AUDIT_OBJECTIVE_ATTR: objective,
                CONVERSATION_REQUEST_ID_ATTR: task_id,
            },
            source_goal=f"只负责 {objective} 对应的这一条来源",
            run_id=f"{task_id}-source",
            attempt_id=f"{task_id}-attempt",
        )

    first_tool = source_tool("audit-task-a", "检查登录日志")
    second_tool = source_tool("audit-task-b", "检查网络日志")
    first = _payload(first_tool.execute({"action": "open", "url": _TOOL_URL}))
    second = _payload(second_tool.execute({"action": "open", "url": _TOOL_URL}))

    assert first["watch_id"] != second["watch_id"]
    first_state = ws.load_state(owner_home, first["watch_id"])
    second_state = ws.load_state(owner_home, second["watch_id"])
    assert first_state is not None and second_state is not None
    assert first_state.audit_root_task_id == "audit-task-a"
    assert first_state.audit_objective == "检查登录日志"
    assert second_state.audit_root_task_id == "audit-task-b"

    # 旧版 Audit 没有根 task_id。它既不能出现在新任务的目录中，也不能因为
    # “根编号为空”而被当前任务用已知 ID 直接消费或修改。
    legacy_state = new_state(owner_home, "http://127.0.0.1:8/legacy", {})
    legacy_state.audit_guarantee = True
    persist_state(legacy_state)

    first_list = _payload(first_tool.execute({"action": "list"}))
    second_list = _payload(second_tool.execute({"action": "list"}))
    assert [row["watch_id"] for row in first_list["watches"]] == [first["watch_id"]]
    assert [row["watch_id"] for row in second_list["watches"]] == [second["watch_id"]]
    # The pre-binding turn keeps an immutable least-privilege snapshot.  If its
    # Audit mode flag disappears, even a matching request id is not enough to
    # recover resource authority inside that stale turn.  The canonical next
    # slice reloads the fully bound worker attributes from disk instead.
    second_tool.agent._current_run_params.task_attributes.pop(AUDIT_ATTR)
    resumed_list = _payload(second_tool.execute({"action": "list"}))
    assert resumed_list["watches"] == []
    second_tool.agent._current_run_params.task_attributes[AUDIT_ATTR] = True
    denied = second_tool.execute({"action": "status", "watch_id": first["watch_id"]})
    assert denied.ok is False and denied.error_code == "TOOL_PERMISSION_DENIED"
    legacy_denied = second_tool.execute({"action": "status", "watch_id": legacy_state.watch_id})
    assert legacy_denied.ok is False
    assert legacy_denied.error_code == "TOOL_PERMISSION_DENIED"
    missing = second_tool.execute({"action": "status", "watch_id": "ws-0000000000"})
    assert missing.ok is False
    assert first["watch_id"] not in missing.output
    assert legacy_state.watch_id not in missing.output
    assert second["watch_id"] in missing.output

    # 普通 watch 不依赖 Audit 根编号；同一个 owner 的原有普通 list 语义保留，
    # 但不会把任何命名 Audit 的内部资源暴露给普通任务。
    ordinary_tool = _tool(
        owner_home,
        source,
        task_attributes={CONVERSATION_REQUEST_ID_ATTR: "ordinary-task"},
    )
    ordinary = _payload(
        ordinary_tool.execute({"action": "open", "url": "http://127.0.0.1:7/plain?since=<next>&limit=<limit>"})
    )
    ordinary_list = _payload(ordinary_tool.execute({"action": "list"}))
    assert [row["watch_id"] for row in ordinary_list["watches"]] == [ordinary["watch_id"]]
    assert ordinary_list["scope"] == {
        "kind": "ordinary_turn",
        "is_global": False,
        "includes_named_audit_resources": False,
        "excluded_resource_kinds": ["named_audit"],
    }

    source.feed(1)
    assert hv._harvest_cycle(first_state, source.handle)
    raw_spool = hv.spool_path(first_state)
    assert raw_spool.exists()
    assert ws.close_audit_watches_for_task(owner_home, "audit-task-a") == (first["watch_id"],)
    assert raw_spool.exists()
    assert ws.load_state(owner_home, first["watch_id"]).closed is True
    assert ws.load_state(owner_home, second["watch_id"]).closed is False


def test_prepare_turn_lists_and_reads_only_its_named_audit_resources(
    owner_home,
) -> None:
    source = _FakeSource()
    first = new_state(owner_home, "http://127.0.0.1:8/first", {})
    first.audit_guarantee = True
    first.audit_root_task_id = "audit-task-a"
    persist_state(first)
    second = new_state(owner_home, "http://127.0.0.1:8/second", {})
    second.audit_guarantee = True
    second.audit_root_task_id = "audit-task-b"
    persist_state(second)
    probe = new_state(owner_home, "http://127.0.0.1:8/probe", {})
    probe.prepare_root_task_id = "audit-task-a"
    persist_state(probe)
    tool = _tool(
        owner_home,
        source,
        task_attributes={
            CONVERSATION_AUDIT_PREPARE_ATTR: True,
            CONVERSATION_TRANSIENT_WORKSPACE_ATTR: True,
            "conversation_task_id": "audit-task-a",
        },
    )

    listed = _payload(tool.execute({"action": "list"}))
    assert {row["watch_id"] for row in listed["watches"]} == {
        first.watch_id,
        probe.watch_id,
    }
    assert listed["scope"] == {
        "kind": "current_named_audit",
        "is_global": False,
        "includes_named_audit_resources": True,
        "excluded_resource_kinds": [],
    }
    own = tool.execute({"action": "status", "watch_id": first.watch_id})
    denied = tool.execute({"action": "status", "watch_id": second.watch_id})

    assert own.ok is True
    assert denied.ok is False
    assert denied.error_code == "TOOL_PERMISSION_DENIED"
    assert second.watch_id not in denied.output

    ordinary = _tool(owner_home, source, task_attributes={})
    ordinary_list = _payload(ordinary.execute({"action": "list"}))
    ordinary_probe = ordinary.execute({"action": "status", "watch_id": probe.watch_id})
    assert ordinary_list["watches"] == []
    assert ordinary_list["scope"]["is_global"] is False
    assert ordinary_list["scope"]["includes_named_audit_resources"] is False
    assert ordinary_probe.ok is False
    assert ordinary_probe.error_code == "TOOL_PERMISSION_DENIED"


def test_audit_root_cannot_pull_source_worker_records(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    attrs = {
        AUDIT_ATTR: True,
        AUDIT_OBJECTIVE_ATTR: "逐条研判当前来源",
        CONVERSATION_REQUEST_ID_ATTR: "audit-root-1",
    }
    root = _tool(owner_home, source, task_attributes=attrs)
    root.agent._current_run_params.run_id = "audit-root-1"
    root.agent._current_run_params.request_id = "audit-root-1"
    open_denied = root.execute({"action": "open", "url": _TOOL_URL})
    assert open_denied.ok is False
    assert (
        open_denied.reported_error_code
        == "AUDIT_COORDINATOR_SOURCE_ACTION_FORBIDDEN"
    )
    source_tool = _audit_source_tool(
        owner_home,
        source,
        task_attributes=attrs,
        source_goal="只负责当前来源的持续研判",
        run_id="audit-root-1-source",
        attempt_id="audit-root-1-source-attempt",
    )
    opened = _payload(source_tool.execute({"action": "open", "url": _TOOL_URL}))
    state = ws.load_state(owner_home, opened["watch_id"])
    assert state is not None
    source.feed(2)
    assert hv._harvest_cycle(state, source.handle)
    before = hv.read_spool_cursor(state)

    denied = root.execute(
        {
            "action": "pull",
            "watch_id": opened["watch_id"],
            "max_wait_seconds": 0,
        }
    )
    assert denied.ok is False
    assert denied.error_code == "TOOL_PERMISSION_DENIED"
    assert (
        denied.reported_error_code
        == "AUDIT_COORDINATOR_SOURCE_ACTION_FORBIDDEN"
    )
    after = hv.read_spool_cursor(state)
    assert after == before


def test_audit_coordinator_can_inspect_but_cannot_submit_any_verdict(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 2, chunk=2)
    state.audit_root_task_id = "audit-review"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    coordinator = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-review",
        },
    )
    with _source_worker_context(coordinator, state):
        pulled = _payload(
            coordinator.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )
        first_ack, second_ack = [row["ack_id"] for row in pulled["candidates"]]
        initial = _payload(
            coordinator.execute(
                {
                    "action": "verdict",
                    "watch_id": state.watch_id,
                    "verdicts": [
                        _verdict(
                            first_ack,
                            source_ref=pulled["candidates"][0]["source_ref"],
                            event_sha256=pulled["candidates"][0]["event_sha256"],
                        )
                    ],
                }
            )
        )
        assert initial["acked_now"] == 1

    denied = coordinator.execute(
        {
            "action": "verdict",
            "watch_id": state.watch_id,
            "verdicts": [_verdict(second_ack)],
        }
    )
    assert denied.ok is False
    assert (
        denied.reported_error_code
        == "AUDIT_COORDINATOR_SOURCE_ACTION_FORBIDDEN"
    )
    inspected = _payload(
        coordinator.execute(
            {
                "action": "inspect",
                "watch_id": state.watch_id,
                "ack_id": first_ack,
            }
        )
    )
    assert inspected["ack_id"] == first_ack
    inspected_by_ref = _payload(
        coordinator.execute(
            {
                "action": "inspect",
                "source_ref": pulled["candidates"][0]["source_ref"],
            }
        )
    )
    assert inspected_by_ref["ack_id"] == first_ack
    mismatch = coordinator.execute(
        {
            "action": "inspect",
            "source_ref": pulled["candidates"][0]["source_ref"],
            "ack_id": second_ack,
        }
    )
    assert mismatch.ok is False
    assert mismatch.reported_error_code == "AUDIT_SOURCE_REF_MISMATCH"
    review_denied = coordinator.execute(
        {
            "action": "verdict",
            "watch_id": state.watch_id,
            "verdicts": [
                _verdict(
                    first_ack,
                    kind="hit",
                    score=88,
                    note="协调层基于补充证据复核",
                    review=True,
                )
            ],
        }
    )
    assert review_denied.ok is False
    assert (
        review_denied.reported_error_code
        == "AUDIT_COORDINATOR_SOURCE_ACTION_FORBIDDEN"
    )


def test_source_worker_finding_is_bound_to_acked_source_ref_and_idempotent(
    owner_home,
    tmp_path,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 1, chunk=1)
    state.owner_id = "u-test"
    state.audit_root_task_id = "audit-finding"
    persist_state(state)
    tool = _tool(owner_home, source)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u-test",
            "channel": "internal",
            "channel_conversation_id": "audit-finding-thread",
            "channel_user_id": "u-test",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-finding",
            "goal": "持续研判来源",
            "work_kind": "audit",
            "work_name": "finding-test",
        }
    )
    tool.agent.conversation_store = store

    with _source_worker_context(tool, state, run_id="source-finding"):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )
        candidate = pulled["candidates"][0]
        _payload(
            tool.execute(
                {
                    "action": "verdict",
                    "watch_id": state.watch_id,
                    "verdicts": [
                        _verdict(
                            candidate["ack_id"],
                            kind="hit",
                            score=91,
                            note="结果端事实支持命中",
                            source_ref=candidate["source_ref"],
                            event_sha256=candidate["event_sha256"],
                            finding={
                                "claim": "事件已经满足本次 Audit 的汇报条件",
                                "kind": "hit",
                                "requires_llm_report": True,
                                "evidence_refs": [
                                    "https://evidence.invalid/case/42"
                                ],
                            },
                        )
                    ],
                }
            )
        )
        params = {
            "claim": "事件已经满足本次 Audit 的汇报条件",
            "kind": "hit",
            "evidence_refs": [
                candidate["source_ref"],
                "https://evidence.invalid/case/42",
            ],
            "requires_llm_report": True,
        }
        first = _payload(execute_record_finding(tool.agent, params))
        second = _payload(execute_record_finding(tool.agent, params))
        denied = execute_record_finding(tool.agent, 
            {
                **params,
                "evidence_refs": ["audit://ws-0000000000/candidate/1:0"],
            }
        )
        missing_audit_ref = execute_record_finding(tool.agent, 
            {
                **params,
                "evidence_refs": ["https://evidence.invalid/case/42"],
            }
        )

    assert first["finding_id"].startswith("af-")
    assert first["recorded"] is True
    assert first["revision"] == 2
    assert second["finding_id"] == first["finding_id"]
    assert second["recorded"] is False
    assert second["revision"] == 2
    assert first["coordination_event"]["state"] == "caught_up"
    assert first["coordination_event"]["published"] == 1
    assert second["coordination_event"]["state"] == "caught_up"
    assert second["coordination_event"]["published"] == 0
    assert denied.ok is False
    assert missing_audit_ref.ok is False
    assert missing_audit_ref.error_code == "TOOL_PERMISSION_DENIED"
    ledger = (
        owner_home
        / "tasks"
        / "audit-finding"
        / "work"
        / "agents"
        / "source-finding"
        / "findings.jsonl"
    )
    rows = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 2
    assert rows[0]["owner_id"] == "u-test"
    assert rows[0]["source_id"] == state.source_id
    assert rows[0]["watch_id"] == state.watch_id
    assert rows[0]["score"] == 91
    assert rows[0]["source_refs"] == [candidate["source_ref"]]
    assert rows[1]["source_refs"] == [
        candidate["source_ref"],
        "https://evidence.invalid/case/42",
    ]
    assert rows[0]["id"] == rows[1]["id"] == first["finding_id"]
    assert [row["revision"] for row in rows] == [1, 2]
    signals = store.pending_wake_signals()
    assert len(signals) == 2
    assert all(signal.reason == "audit_finding" for signal in signals)
    assert {signal.metadata["finding_id"] for signal in signals} == {
        first["finding_id"]
    }
    assert {signal.metadata["revision"] for signal in signals} == {1, 2}


def test_verdict_rejects_cross_record_source_ref_and_hash(
    owner_home,
    monkeypatch,
) -> None:
    """A correct-looking note cannot be signed against another candidate."""
    source = _FakeSource()
    state = _audit_state(owner_home, source, 2, chunk=2)
    state.audit_root_task_id = "audit-evidence-fence"
    persist_state(state)
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    tool = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-evidence-fence",
        },
    )
    with _source_worker_context(tool, state):
        pulled = _payload(
            tool.execute(
                {
                    "action": "pull",
                    "watch_id": state.watch_id,
                    "max_wait_seconds": 0,
                }
            )
        )
        first, second = pulled["candidates"]
        rejected = tool.execute(
            {
                "action": "verdict",
                "watch_id": state.watch_id,
                "verdicts": [
                    _verdict(
                        first["ack_id"],
                        source_ref=second["source_ref"],
                        event_sha256=second["event_sha256"],
                    )
                ],
            }
        )
        assert rejected.ok is False
        assert rejected.reported_error_code == "AUDIT_VERDICT_EVIDENCE_MISMATCH"
        assert hv.acked_candidates(hv.read_spool_cursor(state)) == 0

        accepted = _payload(
            tool.execute(
                {
                    "action": "verdict",
                    "watch_id": state.watch_id,
                    "verdicts": [
                        _verdict(
                            first["ack_id"],
                            source_ref=first["source_ref"],
                            event_sha256=first["event_sha256"],
                        )
                    ],
                }
            )
        )
        assert accepted["acked_now"] == 1


def test_plain_watch_is_not_silently_promoted_by_audit_coordinator(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    assert "audit_guarantee" not in opened  # 普通档不误开
    # 命名 Audit 根只能协调，不能把旧普通 watch 静默升级成 Audit 来源。
    tool.agent._current_run_params.task_attributes = {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: "audit-promote-test",
    }
    denied = tool.execute({"action": "open", "url": _TOOL_URL})
    assert denied.ok is False
    assert denied.reported_error_code == "AUDIT_COORDINATOR_SOURCE_ACTION_FORBIDDEN"
    persisted_plain = ws.load_state(owner_home, opened["watch_id"])
    assert persisted_plain is not None and persisted_plain.audit_guarantee is False

    source_tool = _audit_source_tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-promote-test",
        },
        source_goal="将同一地址作为本次命名 Audit 的独立来源持续研判",
    )
    audit_opened = _payload(source_tool.execute({"action": "open", "url": _TOOL_URL}))
    assert audit_opened["audit_guarantee"] is True
    assert audit_opened["watch_id"] != opened["watch_id"]


def test_verdict_action_rejected_on_plain_watch(owner_home):
    source = _FakeSource()
    tool = _tool(owner_home, source)
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    result = tool.execute(
        {
            "action": "verdict",
            "watch_id": opened["watch_id"],
            "verdicts": [{"ack_id": "1:0", "verdict": "clear", "score": 1}],
        }
    )
    assert not result.ok


def test_audit_pull_uses_guarantee_contract_and_receipt(owner_home):
    source = _FakeSource()
    objective = (
        "每条记录按证据完整度 0 到 10 分、关联强度 0 到 20 分分别评分，"
        "保存维度分、范围和理由"
    )
    tool = _audit_source_tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            AUDIT_OBJECTIVE_ATTR: objective,
        },
    )
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    assert "audit_objective" not in opened
    state = ws.load_state(owner_home, opened["watch_id"])
    assert state is not None
    source.feed(4)
    with _source_worker_context(tool, state):
        pulled = _payload(
            tool.execute({"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 3})
        )
        assert "/audit" in pulled["guidance"]
        assert "audit_objective" not in pulled
        assert pulled["coverage"]["audit_receipt"]["dropped"] == 0
        assert all(row.get("ack_id") for row in pulled["candidates"])
        status = _payload(tool.execute({"action": "status", "watch_id": opened["watch_id"]}))
    # 来源工作者只看自己的 profile/run prompt，覆盖回执仍完整。
    assert "audit_objective" not in status
    assert status["coverage"]["audit_receipt"]["enqueued"] >= 4
    coordinator = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            AUDIT_OBJECTIVE_ATTR: objective,
            CONVERSATION_REQUEST_ID_ATTR: "audit-test",
        },
    )
    coordinator_status = _payload(
        coordinator.execute({"action": "status", "watch_id": opened["watch_id"]})
    )
    assert coordinator_status["audit_objective"] == objective
    # 命名 Audit 不能被模型用通用 close 提前终止。
    denied = coordinator.execute({"action": "close", "watch_id": opened["watch_id"]})
    assert denied.ok is False
    assert denied.reported_error_code == "AUDIT_NAMED_CLEAR_REQUIRED"
    state = ws.load_state(owner_home, opened["watch_id"])
    assert state is not None and state.closed is False


def test_audit_payload_projects_only_bounded_notes_from_escaped_host_wrapper(
    owner_home,
):
    from agent_py_agent.agent.conversation.audit_requirements import (
        published_audit_requirement,
    )

    source = _FakeSource()
    wrapped = published_audit_requirement(
        validated_notes="当前全局要求：保留逐条引用。",
        user_prepare_history="来源甲要求。\n\n--- 后续 prepare ---\n\n来源乙要求。",
    )
    tool = _audit_source_tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            AUDIT_OBJECTIVE_ATTR: wrapped.replace("\n", r"\n"),
        },
    )
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    assert "audit_objective" not in opened
    assert "来源乙要求" not in json.dumps(opened, ensure_ascii=False)
    coordinator = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            AUDIT_OBJECTIVE_ATTR: wrapped.replace("\n", r"\n"),
            CONVERSATION_REQUEST_ID_ATTR: "audit-test",
        },
    )
    status = _payload(
        coordinator.execute({"action": "status", "watch_id": opened["watch_id"]})
    )
    assert status["audit_objective"] == "当前全局要求：保留逐条引用。"
    assert "来源乙要求" not in json.dumps(status, ensure_ascii=False)


def test_source_worker_current_task_revision_is_next_batch_fallback(owner_home):
    from agent.conversation.audit_requirements import published_audit_requirement

    source = _FakeSource()
    state = new_state(owner_home, _TOOL_URL, {}, watch_id="ws-runtime-revision")
    state.audit_guarantee = True
    state.audit_root_task_id = "audit-current"
    state.audit_objective = "旧要求"
    current = published_audit_requirement(
        validated_notes="下一批的新要求",
        user_prepare_history="完整用户历史",
    )
    tool = _tool(
        owner_home,
        source,
        task_attributes={AUDIT_OBJECTIVE_ATTR: current},
    )

    assert wt._effective_audit_objective(tool.agent, state) == "下一批的新要求"


def test_audit_list_exposes_pending_receipt_without_requiring_watch_guess(
    owner_home,
):
    source = _FakeSource()
    state = _audit_state(owner_home, source, 4)
    state.audit_root_task_id = "audit-list-receipt"
    persist_state(state)
    tool = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-list-receipt",
        },
    )

    listed = _payload(tool.execute({"action": "list"}))
    row = next(item for item in listed["watches"] if item["watch_id"] == state.watch_id)
    assert row["audit_receipt"]["enqueued"] == 4
    assert row["audit_receipt"]["judged"] == 0
    assert row["audit_receipt"]["pending"] == 4


def test_contract_and_worker_binding_survive_registry_reload(owner_home, monkeypatch):
    """保证档和专属 worker 绑定都随持久事实恢复；冷加载不会放宽成任意后代可消费。"""
    source = _FakeSource()
    root_task_id = "audit-root-contract"
    tool = _audit_source_tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: root_task_id,
        },
    )
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    watch_id = opened["watch_id"]
    state = ws.load_state(owner_home, watch_id)
    assert state is not None
    source.feed(4)
    with _source_worker_context(
        tool,
        state,
        run_id="source-worker-contract",
        attempt_id="attempt-contract",
    ):
        _payload(tool.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 3}))
    # 换一个"进程":新 registry 冷加载，同一持久 worker attempt 继续。
    monkeypatch.setattr(ws, "registry", ws.WatchRegistry())
    monkeypatch.setattr(wt, "registry", ws.registry)
    reloaded = ws.load_state(owner_home, watch_id)
    assert reloaded is not None and reloaded.audit_guarantee is True  # 契约从盘上继承
    successor = _tool(
        owner_home,
        source,
        task_attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: root_task_id,
        },
    )
    with _source_worker_context(
        successor,
        reloaded,
        run_id="source-worker-contract",
        attempt_id="attempt-contract",
    ):
        pulled = _payload(
            successor.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 3})
        )
        assert "/audit" in pulled["guidance"]
        assert pulled["candidates"] and all(row.get("ack_id") for row in pulled["candidates"])
        # 不交结论就再 pull:游标推不动,同一 worker 拿回同一批+欠账。
        again = _payload(
            successor.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 3})
        )
        assert again.get("pending_verdicts", 0) > 0


def test_audit_pull_never_falls_back_to_inline(owner_home, monkeypatch):
    """收割线程起不来且 spool 无积压时:保证档 pull 空批如实返回(spool 路载荷),绝不
    回落 inline drain(那条路事件不入 durable 队列、没有逐条签收对账);非保证档同景
    照旧回落 inline(对照,零回归)。"""
    source = _FakeSource()
    tool = _audit_source_tool(owner_home, source)
    # background_harvest is no longer a public tool argument.  Mutate the
    # runtime tuning directly to keep the fail-closed regression without
    # preserving an undocumented compatibility parameter.
    monkeypatch.setattr(hv, "ensure_harvester", lambda *_args, **_kwargs: None)
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    state = ws.load_state(owner_home, opened["watch_id"])
    assert state is not None
    state.tuning = replace(state.tuning, background_harvest=0)
    persist_state(state)
    source.feed(3)
    with _source_worker_context(tool, state):
        result = tool.execute(
            {"action": "pull", "watch_id": opened["watch_id"], "max_wait_seconds": 0}
        )
        pulled = _payload(result)
    assert pulled["candidates"] == []  # 空批(慢=延迟)而不是 inline 抬回来的无账候选
    assert "harvester" in pulled  # spool 路载荷(inline 路没有 harvester 块)
    assert "runtime_transition" not in result.result_envelope
    # 对照:非保证档同样条件回落 inline,从源端把事件抬上来(旧路语义不变)
    plain_tool = _tool(owner_home, source)
    plain = _payload(plain_tool.execute({"action": "open", "url": "http://127.0.0.1:9/plain?since=<next>&limit=<limit>"}))
    hv.stop_harvester(plain["watch_id"])
    pulled_plain = _payload(
        plain_tool.execute({"action": "pull", "watch_id": plain["watch_id"], "max_wait_seconds": 0})
    )
    assert pulled_plain["candidates"] and "harvester" not in pulled_plain


def test_audit_source_empty_pull_yields_until_collector_wakes_same_worker(
    owner_home,
    monkeypatch,
):
    source = _FakeSource()
    tool = _audit_source_tool(owner_home, source)
    monkeypatch.setattr(
        hv,
        "ensure_harvester",
        lambda *_args, **_kwargs: {"mode": "local"},
    )
    opened = _payload(tool.execute({"action": "open", "url": _TOOL_URL}))
    state = ws.load_state(owner_home, opened["watch_id"])
    assert state is not None

    with _source_worker_context(tool, state, run_id="source-empty-yield"):
        result = tool.execute(
            {
                "action": "pull",
                "watch_id": opened["watch_id"],
                "max_wait_seconds": 0,
            }
        )

    assert _payload(result)["candidates"] == []
    assert result.result_envelope["runtime_transition"] == {
        "kind": "context_refresh",
        "reason": "audit_source_waiting_for_records",
        "resume": "next_durable_slice",
    }
