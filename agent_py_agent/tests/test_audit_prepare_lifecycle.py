from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.agent_core.orchestration.create_policy import (
    prepare_audit_child_creation_scope,
)
from agent.agent_core.run_task_workspace_writer import _sync_conversation_task_workspace
from agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_OBJECTIVE_ATTR,
    AUDIT_RUN_EPOCH_ATTR,
    AUDIT_RUN_PROMPT_ATTR,
    AUDIT_SOURCE_BINDINGS_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OPEN_ATTR,
    audit_watch_scope_id,
)
from agent.conversation.audit_lifecycle import (
    audit_scope_payload,
    project_audit_runtime_attributes,
    start_named_audit,
)
from agent.conversation.audit_tools import (
    PublishAuditUpdateTool,
    _active_source_rebind_error,
)
from agent.conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
)
from agent.conversation.control_commands import (
    ConversationControlCommand,
    parse_conversation_command,
)
from agent.conversation.workspace_paths import audit_workspace_path
from agent.core import SimpleAgent
from agent.gateway_parts.request_execution import (
    _gateway_run_task_attributes,
    _register_named_system_task,
)
from agent.ingestion.source_binding import (
    audit_prepare_probe_scope_id,
    normalize_audit_source_bindings,
)
from agent.ingestion.watch_state import load_state, new_state, persist_state, watch_id_for
from agent.ingestion.watch_tool import WatchStreamTool
from agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent.settings import AgentConfig

from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayAskRunContext,
    GatewayConversationLoadRequest,
    gateway_conversation_context,
)
from agent_py_agent.agent.gateway_parts.request_prompt import _audit_prepare_prompt_section


@pytest.fixture(autouse=True)
def _stop_harvester_threads():
    """每个测试后停掉本测试 open 的 watch 收割线程。

    这些测试直接用真 SimpleAgent + 真实 home 路径 open audit 保证档 watch(无
    inline_watch_open fixture → 后台 lane 起 watch-harvester 线程)。测试不停止就
    泄漏进后续测试:失败测试的 monkeypatch 窗口内泄漏线程先置位 failed 导致
    DID NOT RAISE。stop 对不存在的 watch_id 是 no-op,对只读测试无害。"""
    yield
    from agent.ingestion import harvester as hv
    from agent.ingestion import watch_state as ws

    for watch_id in ws.registry.ids():
        hv.stop_harvester(watch_id)


def _agent(tmp_path: Path) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )


def test_publish_audit_update_spec_compiles_for_native_tool_protocol(
    tmp_path: Path,
) -> None:
    schema = PublishAuditUpdateTool(_agent(tmp_path)).model_spec.input_schema
    removal = schema["properties"]["remove_source_ids"]

    assert removal["type"] == "array"
    assert removal["maxItems"] == 256
    assert removal["items"] == {
        "type": "string",
        "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    }
    assert "uniqueItems" not in removal


def _register(
    agent: SimpleAgent,
    tmp_path: Path,
    *,
    request_id: str,
    command_text: str,
) -> tuple[dict, object, dict[str, object]]:
    command = parse_conversation_command(command_text)
    assert command is not None and command.valid
    is_start = isinstance(command, ConversationControlCommand)
    prompt = command.value if is_start else command.prompt
    request = {
        "id": request_id,
        "goal": prompt,
        "conversation": {
            "channel": "chat",
            "channel_conversation_id": "audit-preparation",
            "channel_user_id": "local-agent",
            "canonical_user_id": "local-agent",
        },
    }
    if not is_start:
        request["system_task"] = command.to_request_payload()
    request_path = tmp_path / f"{request_id}.json"
    response_path = tmp_path / f"{request_id}.response.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    conversation = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, request_id, prompt)
    )
    context = GatewayAskRunContext(
        agent,
        request,
        request_path,
        response_path,
        request_id,
        lambda _chunk: None,
    )
    if is_start:
        assert command.kind == "audit" and command.operation == "start"
        link = start_named_audit(
            agent,
            agent.conversation_store,
            thread_id=conversation.thread_id,
            work_name=command.name,
            prompt=prompt,
            duration_seconds=int(command.duration_seconds or 0),
        )
        scope = audit_scope_payload(link)
        request["conversation_audit_scope"] = scope
        attrs = project_audit_runtime_attributes(
            {AUDIT_ATTR: True},
            scope,
            thread_id=conversation.thread_id,
            turn_request_id=request_id,
        )
    else:
        _register_named_system_task(context, conversation, prompt)
        attrs = _gateway_run_task_attributes(conversation, request, request_id)
    assert attrs is not None
    return request, conversation, attrs


def _link(agent: SimpleAgent, thread_id: str, name: str):
    return next(
        link
        for link in agent.conversation_store.tasks.list(thread_id)
        if link.work_kind == "audit" and link.work_name == name
    )


def _persist_verified_probe(
    agent: SimpleAgent,
    link: object,
    binding: dict[str, object],
    *,
    watch_suffix: str,
) -> tuple[dict[str, object], str]:
    row = dict(normalize_audit_source_bindings([binding])[0])
    owner_home = Path(agent.home_paths.owner_home_dir)
    task_id = str(getattr(link, "task_id", "") or "").strip()
    prepare_request_id = str(
        getattr(link, "pending_prepare_request_id", "")
        or getattr(link, "effective_prepare_request_id", "")
        or ""
    ).strip()
    assert task_id
    mode = "" if str(row.get("mode") or "cursor") == "cursor" else str(row.get("mode") or "")
    envelope_request = dict(row.get("http_request") or {})
    adapter = dict(row.get("source_adapter") or {})
    boundary = dict(row.get("record_boundary") or {}) or None
    probe_scope = audit_prepare_probe_scope_id(
        task_id=task_id,
        prepare_request_id=prepare_request_id,
        source_id=row["source_id"],
        source_mode=mode,
        request_facts=envelope_request,
        adapter_facts=adapter,
        record_boundary=boundary,
        poll_query_seconds=60,
    )
    watch_id = watch_id_for(owner_home, str(row["url"]), probe_scope)
    state = new_state(
        owner_home,
        str(row["url"]),
        {},
        watch_id=watch_id,
    )
    state.prepare_root_task_id = task_id
    state.source_id = str(row["source_id"])
    state.source_profile_ref = str(row.get("source_profile_ref") or "")
    state.document_refs = list(row.get("document_refs", []) or [])
    state.source_config_version = f"sha256:proof-{watch_suffix}"
    if str(row["url"]).startswith("file://"):
        state.source_envelope = {
            "mode": "file",
            "record_boundary": dict(row["record_boundary"]),
        }
    else:
        state.source_envelope = {
            "mode": str(row.get("mode") or "cursor"),
            "record_boundary": "array_item",
            "request": dict(row["http_request"]),
            "record_list_key": str(row.get("record_list_field") or "items"),
            "cursor_field": str(row.get("cursor_field") or "next_cursor"),
            "cursor_semantics": str(row.get("cursor_semantics") or "next_position"),
            "has_more_field": row.get("has_more_field"),
            "valid": True,
            "continuation_verified": True,
        }
    persist_state(state)
    return row, watch_id


def test_audit_prepare_prompt_keeps_task_requirements_out_of_owner_profile() -> None:
    section = _audit_prepare_prompt_section(
        {
            "system_task": {
                "kind": "audit_prepare",
                "attributes": {"conversation_work_name": "现场审计"},
            },
            "conversation_audit_scope": {
                "name": "现场审计",
                "status": "preparing",
                "pending_prompt": "增加一条长期研判要求",
            },
        }
    )

    assert "scoped only to this named Audit" in section
    assert "not an owner persona or long-term user-memory update" in section


def test_publish_immediately_runs_existing_source_reconciler(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent.ingestion import source_worker

    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-immediate-runtime-sync",
        command_text="/audit 即时同步 prepare 更新正在运行的说明",
    )
    prepared = _link(agent, conversation.thread_id, "即时同步")
    calls: list[object] = []

    def reconcile(selected):
        calls.append(selected)
        return {"checked": 2, "degraded": 1}

    monkeypatch.setattr(source_worker, "reconcile_audit_source_workers", reconcile)
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        published = PublishAuditUpdateTool(agent).execute(
            {
                "effective_prompt": "使用更新后的说明处理下一批完整记录",
                "validation_status": "not_required",
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    payload = json.loads(published.output)
    assert published.ok is True
    assert calls == [agent]
    assert payload["runtime_sync_checked"] == 2
    assert payload["runtime_sync_degraded"] == 1
    assert agent.conversation_store.tasks.load(prepared.task_id) is not None


def test_running_source_rejects_identity_rebind_but_accepts_request_revision() -> None:
    current = dict(
        normalize_audit_source_bindings(
            [
                {
                    "source_id": "waf-a",
                    "source_profile_ref": "work/waf/profile.md",
                    "url": "https://waf.example.invalid/events",
                    "mode": "adapter",
                    "http_request": {"method": "POST", "json_body": {}},
                    "source_adapter": {
                        "path": "work/waf/adapter.py",
                        "sha256": "a" * 64,
                    },
                }
            ]
        )[0]
    )
    revised_request = dict(
        normalize_audit_source_bindings(
            [
                {
                    **current,
                    "http_request": {
                        "method": "POST",
                        "json_body": {"page_size": 100},
                    },
                    "source_adapter": {
                        "path": "work/waf/adapter.py",
                        "sha256": "b" * 64,
                    },
                }
            ]
        )[0]
    )
    changed_origin = dict(
        normalize_audit_source_bindings(
            [
                {
                    **current,
                    "url": "https://replacement.example.invalid/events",
                }
            ]
        )[0]
    )
    link = SimpleNamespace(
        status="active",
        effective_source_bindings=(current,),
    )

    assert _active_source_rebind_error(link, (revised_request,)) is None
    rejected = _active_source_rebind_error(link, (changed_origin,))
    assert rejected is not None
    assert rejected.error_code == "AUDIT_SOURCE_REBIND_REQUIRED"


def test_interleaved_prepare_uses_exact_independent_transient_workspaces(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    request_a, conversation, attrs_a = _register(
        agent,
        tmp_path,
        request_id="prepare-a",
        command_text="/audit ABC prepare 阅读第一份接口说明，只分析不要应用",
    )
    request_b, _, attrs_b = _register(
        agent,
        tmp_path,
        request_id="prepare-b",
        command_text="/audit abc prepare 编写第二份来源的探针，只测试不要应用",
    )
    link_a = _link(agent, conversation.thread_id, "ABC")
    link_b = _link(agent, conversation.thread_id, "abc")

    assert link_a.task_id != link_b.task_id
    assert Path(link_a.task_path).parent.name == "audits"
    assert Path(link_b.task_path).parent.name == "audits"
    assert attrs_a[CONVERSATION_AUDIT_PREPARE_ATTR] is True
    assert attrs_b[CONVERSATION_AUDIT_PREPARE_ATTR] is True
    assert attrs_a[CONVERSATION_TRANSIENT_WORKSPACE_ATTR] is True
    assert attrs_b[CONVERSATION_TRANSIENT_WORKSPACE_ATTR] is True
    assert attrs_a["conversation_task_id"] == link_a.task_id
    assert attrs_b["conversation_task_id"] == link_b.task_id
    assert attrs_a["run_workspace"]["task_root"] == link_a.task_path
    assert attrs_b["run_workspace"]["task_root"] == link_b.task_path
    assert request_a["conversation_audit_scope"]["name"] == "ABC"
    assert request_b["conversation_audit_scope"]["name"] == "abc"
    assert link_a.goal == "" and link_b.goal == ""
    assert "第一份接口说明" in link_a.pending_prompt
    assert "第二份来源" in link_b.pending_prompt

    _sync_conversation_task_workspace(
        agent,
        SimpleNamespace(
            task_attributes=attrs_a,
            user_prompt="阅读第一份接口说明，只分析不要应用",
        ),
        link_a.task_id,
        Path(link_a.task_path),
    )
    after_archive_sync = agent.conversation_store.tasks.load(link_a.task_id)
    assert after_archive_sync is not None
    assert after_archive_sync.goal == ""
    assert "第一份接口说明" in after_archive_sync.pending_prompt

    ordinary_request = {
        "conversation": {
            "channel": "chat",
            "channel_conversation_id": "audit-preparation",
            "channel_user_id": "local-agent",
            "canonical_user_id": "local-agent",
        }
    }
    ordinary = gateway_conversation_context(
        GatewayConversationLoadRequest(
            agent,
            ordinary_request,
            "ordinary-chat",
            "我们先聊点别的",
        )
    )
    ordinary_attrs = _gateway_run_task_attributes(
        ordinary,
        ordinary_request,
        "ordinary-chat",
    )
    assert ordinary_attrs is not None
    assert CONVERSATION_AUDIT_PREPARE_ATTR not in ordinary_attrs
    assert CONVERSATION_TRANSIENT_WORKSPACE_ATTR not in ordinary_attrs
    assert "conversation_task_id" not in ordinary_attrs
    assert "run_workspace" not in ordinary_attrs


def test_same_audit_accumulates_unpublished_prepare_turns_in_order(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, _ = _register(
        agent,
        tmp_path,
        request_id="prepare-same-1",
        command_text="/audit 同名准备 prepare 第一条来源规则",
    )
    _register(
        agent,
        tmp_path,
        request_id="prepare-same-2",
        command_text="/audit 同名准备 prepare 第二条探针结果",
    )

    link = _link(agent, conversation.thread_id, "同名准备")
    assert "第一条来源规则" in link.pending_prompt
    assert "第二条探针结果" in link.pending_prompt
    assert link.pending_prompt.index("第一条来源规则") < link.pending_prompt.index("第二条探针结果")

    tool = PublishAuditUpdateTool(agent)
    effective_prompt = tool.model_spec.input_schema["properties"]["effective_prompt"]
    assert "Durable cross-source operational notes only" in effective_prompt["description"]
    assert "Notes, scripts, tests, skills and documents" in tool.model_spec.description
    attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-same-3",
        command_text="/audit 同名准备 prepare 第三条最终确认",
    )[2]
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        published = tool.execute(
            {
                "effective_prompt": "三轮验证后形成的说明",
                "validation_status": "not_required",
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert published.ok, published.output
    stored = agent.conversation_store.tasks.load(link.task_id)
    assert stored is not None
    assert stored.pending_prompt == ""
    assert "第一条来源规则" in stored.effective_user_prompt
    assert "第二条探针结果" in stored.effective_user_prompt
    assert "第三条最终确认" in stored.effective_user_prompt


def test_publish_is_structured_exact_and_failed_validation_keeps_effective_prompt(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs_a = _register(
        agent,
        tmp_path,
        request_id="prepare-a",
        command_text="/audit A prepare 测试新方案但暂时不要生效",
    )
    _register(
        agent,
        tmp_path,
        request_id="prepare-b",
        command_text="/audit B prepare 保持另一套方案",
    )
    link_a = _link(agent, conversation.thread_id, "A")
    link_b = _link(agent, conversation.thread_id, "B")
    tool = PublishAuditUpdateTool(agent)

    assert tool.availability().available is False
    agent._current_run_params = SimpleNamespace(task_attributes=attrs_a)
    try:
        assert tool.availability().available is True
        failed = tool.execute(
            {
                "effective_prompt": "采用尚未通过的方案",
                "validation_status": "failed",
            }
        )
        invalid_evidence = tool.execute(
            {
                "effective_prompt": "采用没有证据的方案",
                "validation_status": "passed",
                "validation_refs": ["missing-result.json"],
            }
        )
        after_failed = agent.conversation_store.tasks.load(link_a.task_id)
        assert failed.ok is True
        failed_payload = json.loads(failed.output)
        assert failed_payload["applied"] is False
        assert failed_payload["monitoring_started"] is False
        assert failed_payload["audit_status"] == "preparing"
        assert invalid_evidence.ok is False
        assert after_failed is not None and after_failed.goal == ""
        assert after_failed.pending_prompt == link_a.pending_prompt

        evidence = Path(link_a.task_path) / "output" / "probe-result.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text('{"ok":true}\n', encoding="utf-8")
        published = tool.execute(
            {
                "effective_prompt": "采用已经通过探针测试的方案",
                "validation_status": "passed",
                "validation_refs": ["output/probe-result.json"],
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    updated_a = agent.conversation_store.tasks.load(link_a.task_id)
    unchanged_b = agent.conversation_store.tasks.load(link_b.task_id)
    assert published.ok is True
    published_payload = json.loads(published.output)
    assert published_payload["applied"] is True
    assert published_payload["monitoring_started"] is False
    assert published_payload["audit_status"] == "preparing"
    assert updated_a is not None
    assert "采用已经通过探针测试的方案" in updated_a.goal
    assert "测试新方案但暂时不要生效" in updated_a.goal
    assert updated_a.goal.index("采用已经通过探针测试的方案") < updated_a.goal.index(
        "测试新方案但暂时不要生效"
    )
    assert "用户本轮 prepare 原文（业务含义的最终权威）" in updated_a.goal
    assert updated_a.pending_prompt == ""
    assert updated_a.effective_user_prompt == "测试新方案但暂时不要生效"
    assert updated_a.effective_revision == 1
    assert updated_a.effective_evidence_refs == (str(evidence.resolve()),)
    assert unchanged_b is not None and unchanged_b.goal == ""
    assert "另一套方案" in unchanged_b.pending_prompt


def test_repeated_publish_canonicalizes_the_host_audit_wrapper(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _, conversation, first_attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-a-1",
        command_text="/audit A prepare 第一轮用户要求",
    )
    tool = PublishAuditUpdateTool(agent)
    agent._current_run_params = SimpleNamespace(task_attributes=first_attrs)
    try:
        first = tool.execute(
            {
                "effective_prompt": "第一版完整派生说明",
                "validation_status": "not_required",
            }
        )
    finally:
        delattr(agent, "_current_run_params")
    assert first.ok is True
    first_link = _link(agent, conversation.thread_id, "A")

    _, _, second_attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-a-2",
        command_text="/audit A prepare 第二轮纠正要求",
    )
    recursively_wrapped = first_link.goal.replace(
        "第一版完整派生说明",
        "第二版完整派生说明",
    )
    agent._current_run_params = SimpleNamespace(task_attributes=second_attrs)
    try:
        second = tool.execute(
            {
                "effective_prompt": recursively_wrapped,
                "validation_status": "not_required",
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert second.ok is True
    updated = _link(agent, conversation.thread_id, "A")
    assert updated.effective_revision == 2
    assert "第一轮用户要求" in updated.effective_user_prompt
    assert "第二轮纠正要求" in updated.effective_user_prompt
    assert updated.effective_user_prompt.index("第一轮用户要求") < (
        updated.effective_user_prompt.index("第二轮纠正要求")
    )
    assert "第二版完整派生说明" in updated.goal
    assert "第二轮纠正要求" in updated.goal
    assert updated.goal.count("# Audit 生效上下文") == 1
    assert updated.goal.count("## 代理验证说明（派生内容，不能覆盖下方用户原文）") == 1
    assert updated.goal.count("## 用户本轮 prepare 原文（业务含义的最终权威）") == 1


def test_publish_keeps_host_outer_title_single_when_model_composes_one(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-composed-title",
        command_text="/audit A prepare 更新完整的三路说明",
    )
    tool = PublishAuditUpdateTool(agent)
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        published = tool.execute(
            {
                "effective_prompt": (
                    "# Audit 生效上下文\n\n## 三路持续研判要求\n每路由自己的来源工作者处理。"
                ),
                "validation_status": "not_required",
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert published.ok is True
    updated = _link(agent, conversation.thread_id, "A")
    assert updated.goal.count("# Audit 生效上下文") == 1
    assert "## 三路持续研判要求" in updated.goal


def test_publish_rejects_incomplete_model_copy_of_host_sections(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-malformed-host-copy",
        command_text="/audit A prepare 更新正在运行的判断要求",
    )
    tool = PublishAuditUpdateTool(agent)
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        rejected = tool.execute(
            {
                "effective_prompt": (
                    "## 代理验证说明（派生内容，不能覆盖下方用户原文）\n"
                    "旧说明\n\n"
                    "## 用户本轮 prepare 原文（业务含义的最终权威）\n"
                    "旧用户历史\n\n新的补充说明"
                ),
                "validation_status": "not_required",
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert rejected.ok is False
    payload = json.loads(rejected.output)
    assert payload["error_code"] == "AUDIT_EFFECTIVE_PROMPT_HOST_MARKER_FORBIDDEN"
    stored = _link(agent, conversation.thread_id, "A")
    assert stored.goal == ""
    assert "更新正在运行的判断要求" in stored.pending_prompt


def test_start_reuses_prepared_audit_identity_and_published_prompt(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-a",
        command_text="/audit A prepare 准备经过测试的方案",
    )
    prepared = _link(agent, conversation.thread_id, "A")
    tool = PublishAuditUpdateTool(agent)
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        published = tool.execute(
            {
                "effective_prompt": "正式生效的研判要求",
                "validation_status": "not_required",
            }
        )
    finally:
        delattr(agent, "_current_run_params")
    assert published.ok is True

    # Simulate a transient cleanup failure after the durable publish.  Audit
    # activation must retry the same typed prepare-task scope so the probe
    # cannot keep collecting beside the formal source worker.
    owner_home = Path(agent.home_paths.owner_home_dir)
    probe_url = "https://prepare-probe.example.invalid/events"
    probe = new_state(
        owner_home,
        probe_url,
        {"background_harvest": 0},
        watch_id=watch_id_for(
            owner_home,
            probe_url,
            f"prepare:{prepared.task_id}",
        ),
    )
    probe.prepare_root_task_id = prepared.task_id
    persist_state(probe)

    _, _, started_attrs = _register(
        agent,
        tmp_path,
        request_id="start-a",
        command_text="/audit 10m A 启动已经准备好的监测",
    )
    active = _link(agent, conversation.thread_id, "A")

    assert active.task_id == prepared.task_id
    assert active.status == "active"
    assert "正式生效的研判要求" in active.goal
    assert "准备经过测试的方案" in active.goal
    assert active.duration_seconds == 600
    assert active.expires_at > active.created_at
    assert active.run_epoch == 1
    assert active.run_prompt == "启动已经准备好的监测"
    assert started_attrs["conversation_task_id"] == prepared.task_id
    assert started_attrs["conversation_request_id"] == prepared.task_id
    assert started_attrs["audit_guarantee"] is True
    assert started_attrs[AUDIT_RUN_EPOCH_ATTR] == 1
    assert started_attrs[AUDIT_OBJECTIVE_ATTR] == active.goal
    assert started_attrs[AUDIT_RUN_PROMPT_ATTR] == active.run_prompt
    retired_probe = load_state(owner_home, probe.watch_id)
    assert retired_probe is not None
    assert retired_probe.closed is True
    assert retired_probe.close_reason == "audit_prepare_published"


def test_completed_named_audit_restart_reuses_config_but_mints_fresh_epoch(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, _attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-restart",
        command_text="/audit 可重复巡检 prepare 学习来源和判断要求",
    )
    prepared = _link(agent, conversation.thread_id, "可重复巡检")
    profile = Path(prepared.task_path) / "work" / "sources" / "source-a.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id: source-a\n", encoding="utf-8")
    published = agent.conversation_store.audits.publish_effective_prompt(
        {
            "task_id": prepared.task_id,
            "prompt": "按已经验证的来源资料持续研判",
            "prepare_request_id": "prepare-restart",
            "source_bindings": [
                {
                    "source_id": "source-a",
                    "url": "https://audit.example.invalid/events",
                    "source_profile_ref": str(profile),
                    "mode": "cursor",
                    "http_request": {
                        "method": "GET",
                        "cursor_binding": {
                            "location": "query",
                            "name": "since",
                            "initial": 0,
                        },
                    },
                }
            ],
        }
    )
    assert published is not None

    _, _, first_attrs = _register(
        agent,
        tmp_path,
        request_id="start-restart-1",
        command_text="/audit 2m 可重复巡检 第一次启动说明",
    )
    first = _link(agent, conversation.thread_id, "可重复巡检")
    first_goal = first.goal
    first_path = first.task_path
    first_scope = audit_watch_scope_id(first.task_id, first.run_epoch)
    assert first.run_epoch == 1
    assert first.run_prompt == "第一次启动说明"
    assert first_attrs[AUDIT_RUN_EPOCH_ATTR] == 1
    assert first_attrs[AUDIT_RUN_PROMPT_ATTR] == "第一次启动说明"
    assert first_attrs[AUDIT_OBJECTIVE_ATTR] == first_goal
    assert "第一次启动说明" not in first_goal

    terminal = agent.conversation_store.tasks.update_status(
        {
            "task_id": first.task_id,
            "status": "completed",
            "expected_status": "active",
        }
    )
    assert terminal is not None
    _, _, second_attrs = _register(
        agent,
        tmp_path,
        request_id="start-restart-2",
        command_text="/audit 3m 可重复巡检 第二次启动说明",
    )
    second = _link(agent, conversation.thread_id, "可重复巡检")

    assert second.task_id == first.task_id
    assert second.task_path == first_path
    assert second.goal == first_goal
    assert second.effective_source_bindings == first.effective_source_bindings
    assert second.pending_prompt == ""
    assert second.run_epoch == 2
    assert second.run_prompt == "第二次启动说明"
    assert second.duration_seconds == 180
    assert second_attrs[AUDIT_RUN_EPOCH_ATTR] == 2
    assert second_attrs[AUDIT_RUN_PROMPT_ATTR] == "第二次启动说明"
    assert second_attrs[AUDIT_OBJECTIVE_ATTR] == second.goal
    assert audit_watch_scope_id(second.task_id, second.run_epoch) == first_scope
    assert "第二次启动说明" not in second.goal


def test_expired_named_audit_restart_settles_previous_run_without_status_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, _attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-expired-restart",
        command_text="/audit 到期巡检 prepare 保存已经验证的来源要求",
    )
    prepared = _link(agent, conversation.thread_id, "到期巡检")
    profile = Path(prepared.task_path) / "work" / "sources" / "source-a.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id: source-a\n", encoding="utf-8")
    published = agent.conversation_store.audits.publish_effective_prompt(
        {
            "task_id": prepared.task_id,
            "prompt": "按已验证来源持续研判",
            "prepare_request_id": "prepare-expired-restart",
            "source_bindings": [
                {
                    "source_id": "source-a",
                    "url": "https://audit.example.invalid/events",
                    "source_profile_ref": str(profile),
                    "mode": "cursor",
                    "http_request": {
                        "method": "GET",
                        "cursor_binding": {
                            "location": "query",
                            "name": "since",
                            "initial": 0,
                        },
                    },
                }
            ],
        }
    )
    assert published is not None
    _register(
        agent,
        tmp_path,
        request_id="start-expired-restart-1",
        command_text="/audit 2m 到期巡检 第一次启动",
    )
    first = _link(agent, conversation.thread_id, "到期巡检")
    assert first.status == "active"
    assert first.expires_at is not None

    after_deadline = float(first.expires_at) + 1.0
    monkeypatch.setattr(
        "agent.conversation.audit_lifecycle.time.time",
        lambda: after_deadline,
    )
    monkeypatch.setattr(
        "agent.conversation.task_promotion.time.time",
        lambda: after_deadline,
    )
    _, _, second_attrs = _register(
        agent,
        tmp_path,
        request_id="start-expired-restart-2",
        command_text="/audit 3m 到期巡检 第二次启动",
    )
    second = _link(agent, conversation.thread_id, "到期巡检")

    assert second.task_id == first.task_id
    assert second.task_path == first.task_path
    assert second.status == "active"
    assert second.run_epoch == 2
    assert second.duration_seconds == 180
    assert second.run_prompt == "第二次启动"
    assert second.effective_source_bindings == first.effective_source_bindings
    assert second_attrs[AUDIT_RUN_EPOCH_ATTR] == 2


def test_completed_named_audit_prepare_reopens_same_workspace_and_config(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, _attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-reopen-1",
        command_text="/audit 可修订巡检 prepare 第一版要求",
    )
    original = _link(agent, conversation.thread_id, "可修订巡检")
    profile = Path(original.task_path) / "work" / "sources" / "source-a.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id: source-a\n", encoding="utf-8")
    published = agent.conversation_store.audits.publish_effective_prompt(
        {
            "task_id": original.task_id,
            "prompt": "第一版已验证说明",
            "prepare_request_id": "prepare-reopen-1",
            "source_bindings": [
                {
                    "source_id": "source-a",
                    "url": "https://a.example.invalid/events",
                    "mode": "poll",
                    "source_profile_ref": str(profile),
                }
            ],
        }
    )
    assert published is not None
    started = agent.conversation_store.audits.activate(
        {
            "task_id": original.task_id,
            "goal": "不应覆盖已发布要求",
            "duration_seconds": 60,
        }
    )
    assert started is not None and started.run_epoch == 1
    assert agent.conversation_store.tasks.update_status(
        {
            "task_id": original.task_id,
            "status": "completed",
            "expected_status": "active",
        }
    ) is not None

    _, _, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-reopen-2",
        command_text="/audit 可修订巡检 prepare 运行结束后补充一条新要求",
    )
    reopened = _link(agent, conversation.thread_id, "可修订巡检")

    assert reopened.task_id == original.task_id
    assert reopened.task_path == original.task_path
    assert reopened.status == "preparing"
    assert reopened.run_epoch == 1
    assert reopened.goal == published.goal
    assert reopened.effective_source_bindings == published.effective_source_bindings
    assert "运行结束后补充一条新要求" in reopened.pending_prompt
    assert attrs[AUDIT_RUN_EPOCH_ATTR] == 1


def test_restart_migration_prefers_older_published_config_over_newer_empty_attempt(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "audit-preparation",
            "channel_user_id": "local-agent",
            "now": 1.0,
        }
    )
    owner_home = Path(agent.home_paths.owner_home_dir)
    configured_path = audit_workspace_path(str(owner_home), "audit-configured")
    empty_path = audit_workspace_path(str(owner_home), "audit-empty-attempt")
    configured_profile = configured_path / "work" / "sources" / "stable.md"
    configured_profile.parent.mkdir(parents=True, exist_ok=True)
    configured_profile.write_text("source_id: stable\n", encoding="utf-8")
    configured = agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-configured",
            "goal": "已验证并发布的要求",
            "status": "completed",
            "task_path": str(configured_path),
            "work_kind": "audit",
            "work_name": "迁移巡检",
            "effective_revision": 3,
            "effective_user_prompt": "用户此前确认的要求",
            "effective_prepare_request_id": "old-publish",
            "effective_source_bindings": [
                {
                    "source_id": "stable",
                    "url": "https://stable.example.invalid/events",
                    "source_profile_ref": str(configured_profile),
                }
            ],
            "run_epoch": 4,
            "now": 10.0,
        }
    )
    agent.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-empty-attempt",
            "goal": "误创建的空启动",
            "status": "failed",
            "task_path": str(empty_path),
            "work_kind": "audit",
            "work_name": "迁移巡检",
            "now": 20.0,
        }
    )

    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="migration-restart",
        command_text="/audit 1m 迁移巡检 重新开始",
    )
    active = _link(agent, conversation.thread_id, "迁移巡检")

    assert active.task_id == configured.task_id
    assert active.task_path == str(configured_path)
    assert active.goal == configured.goal
    assert active.run_epoch == 5
    assert attrs["conversation_task_id"] == configured.task_id
    assert attrs[AUDIT_RUN_EPOCH_ATTR] == 5


def test_published_source_bindings_are_exact_preserved_and_visible_by_id(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-sources",
        command_text="/audit 来源组 prepare 验证三个现场来源并应用",
    )
    prepared = _link(agent, conversation.thread_id, "来源组")
    evidence = Path(prepared.task_path) / "output" / "source-probes.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text('{"ok":true,"source_count":3}\n', encoding="utf-8")
    profile_dir = Path(prepared.task_path) / "work" / "sources"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profiles = {
        source_id: profile_dir / f"{source_id}.md"
        for source_id in ("login-api", "payment-api", "host-file")
    }
    for source_id, path in profiles.items():
        path.write_text(f"# Source profile\nsource_id: {source_id}\n", encoding="utf-8")
    source_file = tmp_path / "growing.log"
    source_file.write_text("first\n", encoding="utf-8")
    source_bindings = [
        {
            "source_id": "login-api",
            "url": "https://logs.example.invalid/events?position=<next>&page_size=<limit>",
            "record_list_field": "$.items[]",
            "cursor_field": "$.next_position",
            "source_profile_ref": "work/sources/login-api.md",
        },
        {
            "source_id": "payment-api",
            "url": "https://billing.example.invalid/query",
            "http_request": {
                "method": "POST",
                "json_body": {"page": {}},
                "cursor_binding": {
                    "location": "json_body",
                    "path": ["page", "cursor"],
                    "initial": 0,
                },
                "page_size_binding": {
                    "location": "json_body",
                    "path": ["page", "size"],
                },
            },
            "record_list_field": "records",
            "cursor_field": "next",
            "source_profile_ref": "work/sources/payment-api.md",
        },
        {
            "source_id": "host-file",
            "url": str(source_file),
            "record_boundary": {"mode": "line"},
            "source_profile_ref": "work/sources/host-file.md",
        },
    ]
    source_probe_refs = [
        _persist_verified_probe(
            agent,
            prepared,
            binding,
            watch_suffix=str(index),
        )[1]
        for index, binding in enumerate(source_bindings)
    ]
    tool = PublishAuditUpdateTool(agent)
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        published = tool.execute(
            {
                "effective_prompt": "分别持续研判三条已经现场试通的来源",
                "validation_status": "passed",
                "validation_refs": [
                    "output/source-probes.json",
                    "work/sources/login-api.md",
                    "work/sources/payment-api.md",
                    "work/sources/host-file.md",
                ],
                "source_probe_refs": source_probe_refs,
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert published.ok is True
    published_payload = json.loads(published.output)
    assert published_payload["source_count"] == 3
    assert published_payload["source_binding_change_applied"] is True
    assert published_payload["applied_source_probe_count"] == 3
    assert published_payload["applied_source_ids"] == [
        "login-api",
        "payment-api",
        "host-file",
    ]
    assert published.result_envelope["audit_publish"] == published_payload
    stored = agent.conversation_store.tasks.load(prepared.task_id)
    assert stored is not None
    assert [item["source_id"] for item in stored.effective_source_bindings] == [
        "login-api",
        "payment-api",
        "host-file",
    ]
    assert stored.effective_source_bindings[0]["url"] == ("https://logs.example.invalid/events")
    assert stored.effective_source_bindings[0]["http_request"]["cursor_binding"] == {
        "location": "query",
        "name": "position",
        "initial": 0,
    }
    assert stored.effective_source_bindings[0]["record_list_field"] == "items"
    assert stored.effective_source_bindings[0]["cursor_field"] == "next_position"
    assert stored.effective_source_bindings[0]["source_profile_ref"] == str(
        profiles["login-api"].resolve()
    )

    # A background coordinator wake may carry only the durable Audit id.  It
    # must still inherit the published binding set from the named-task link and
    # cannot create an unconstrained repair child from prose.
    agent._current_run_params = SimpleNamespace(
        task_attributes={
            AUDIT_ATTR: True,
            "conversation_request_id": prepared.task_id,
        }
    )
    try:
        _, missing_source_error = prepare_audit_child_creation_scope(
            agent,
            {"goal": "继续处理来源", "role": "worker"},
        )
        scoped_child, scoped_error = prepare_audit_child_creation_scope(
            agent,
            {
                "goal": "继续处理登录来源",
                "role": "worker",
                "audit_source_id": "login-api",
            },
        )
    finally:
        delattr(agent, "_current_run_params")
    assert "必须用 audit_source_id" in missing_source_error
    assert scoped_error == ""
    child_attrs = scoped_child["attributes"]
    assert child_attrs[AUDIT_SOURCE_ID_ATTR] == "login-api"
    assert child_attrs[AUDIT_SOURCE_OPEN_ATTR]["url"] == ("https://logs.example.invalid/events")

    _, _, update_attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-sources-note",
        command_text="/audit 来源组 prepare 只更新研判说明，不改三个来源",
    )
    agent._current_run_params = SimpleNamespace(task_attributes=update_attrs)
    try:
        prompt_only = tool.execute(
            {
                "effective_prompt": "更新后的研判说明，来源保持不变",
                "validation_status": "not_required",
            }
        )
    finally:
        delattr(agent, "_current_run_params")
    assert prompt_only.ok is True
    preserved = agent.conversation_store.tasks.load(prepared.task_id)
    assert preserved is not None
    assert preserved.effective_source_bindings == stored.effective_source_bindings

    _, _, empty_update_attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-sources-empty-list",
        command_text="/audit 来源组 prepare 更新说明并保留当前来源",
    )
    agent._current_run_params = SimpleNamespace(task_attributes=empty_update_attrs)
    try:
        empty_list_update = tool.execute(
            {
                "effective_prompt": "第二次更新研判说明，来源仍保持不变",
                "validation_status": "not_required",
                "source_probe_refs": [],
            }
        )
    finally:
        delattr(agent, "_current_run_params")
    assert empty_list_update.ok is True
    empty_payload = json.loads(empty_list_update.output)
    assert empty_payload["source_count"] == 3
    assert empty_payload["source_binding_change_applied"] is False
    assert empty_payload["applied_source_probe_count"] == 0
    assert empty_payload["applied_source_ids"] == []
    assert empty_payload["effective_sources"] == [
        {
            "source_id": "login-api",
            "url": "https://logs.example.invalid/events",
        },
        {
            "source_id": "payment-api",
            "url": "https://billing.example.invalid/query",
        },
        {"source_id": "host-file", "url": source_file.resolve().as_uri()},
    ]
    preserved_after_empty = agent.conversation_store.tasks.load(prepared.task_id)
    assert preserved_after_empty is not None
    assert preserved_after_empty.effective_source_bindings == preserved.effective_source_bindings

    _start_request, _started_conversation, started_attrs = _register(
        agent,
        tmp_path,
        request_id="start-sources",
        command_text="/audit 10m 来源组 开始按已经准备好的要求持续监测",
    )
    assert started_attrs[AUDIT_SOURCE_BINDINGS_ATTR] == [
        dict(item) for item in preserved_after_empty.effective_source_bindings
    ]
    started_link = agent.conversation_store.tasks.load(prepared.task_id)
    assert started_link is not None and started_link.status == "active"
    assert started_link.run_prompt == "开始按已经准备好的要求持续监测"


def test_invalid_source_probe_refs_fail_without_advancing_audit_revision(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-invalid-sources",
        command_text="/audit 无效来源 prepare 尝试发布重复来源",
    )
    prepared = _link(agent, conversation.thread_id, "无效来源")
    evidence = Path(prepared.task_path) / "output" / "probe.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text('{"ok":true}\n', encoding="utf-8")
    profile = Path(prepared.task_path) / "work" / "same.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id: same\n", encoding="utf-8")
    _, watch_id = _persist_verified_probe(
        agent,
        prepared,
        {
            "source_id": "same",
            "url": "https://one.example.invalid/events?cursor=<next>",
            "source_profile_ref": str(profile),
        },
        watch_suffix="duplicate",
    )
    tool = PublishAuditUpdateTool(agent)
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        rejected = tool.execute(
            {
                "effective_prompt": "不应生效",
                "validation_status": "passed",
                "validation_refs": ["output/probe.json", "work/same.md"],
                "source_probe_refs": [watch_id, watch_id],
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert rejected.ok is False
    assert rejected.error_code == "AUDIT_SOURCE_PROBE_REFS_INVALID"
    assert "重复" in json.loads(rejected.output)["error_detail"]
    unchanged = agent.conversation_store.tasks.load(prepared.task_id)
    assert unchanged is not None
    assert unchanged.effective_revision == 0
    assert unchanged.effective_source_bindings == ()


def test_source_publish_rejects_probe_from_another_named_audit(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-probe-mismatch",
        command_text="/audit 探针不一致 prepare 保存已验证来源",
    )
    prepared = _link(agent, conversation.thread_id, "探针不一致")
    _register(
        agent,
        tmp_path,
        request_id="prepare-probe-other",
        command_text="/audit 另一任务 prepare 检查同一地址",
    )
    other = _link(agent, conversation.thread_id, "另一任务")
    profile = Path(prepared.task_path) / "output" / "source.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id: source-a\n", encoding="utf-8")
    proven = {
        "source_id": "source-a",
        "url": "https://a.example.invalid/events?since=<next>&limit=<limit>",
        "record_list_field": "items",
        "cursor_field": "next_cursor",
        "cursor_semantics": "next_position",
        "source_profile_ref": str(profile),
    }
    _, foreign_watch_id = _persist_verified_probe(
        agent,
        other,
        proven,
        watch_suffix="other-audit",
    )

    tool = PublishAuditUpdateTool(agent)
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        rejected = tool.execute(
            {
                "effective_prompt": "保存来源",
                "validation_status": "passed",
                "validation_refs": [str(profile)],
                "source_probe_refs": [foreign_watch_id],
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert rejected.ok is False
    assert rejected.error_code == "AUDIT_SOURCE_PROBE_REFS_INVALID"
    assert "不属于当前命名 Audit" in json.loads(rejected.output)["error_detail"]
    unchanged = agent.conversation_store.tasks.load(prepared.task_id)
    assert unchanged is not None
    assert unchanged.effective_revision == 0
    assert unchanged.effective_source_bindings == ()


def test_same_source_url_has_independent_probe_identity_per_named_audit(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs_a = _register(
        agent,
        tmp_path,
        request_id="prepare-same-url-a",
        command_text="/audit A prepare 检查共享地址",
    )
    _, _, attrs_b = _register(
        agent,
        tmp_path,
        request_id="prepare-same-url-b",
        command_text="/audit B prepare 独立检查共享地址",
    )
    link_a = _link(agent, conversation.thread_id, "A")
    link_b = _link(agent, conversation.thread_id, "B")
    source = Path(link_a.task_path) / "output" / "shared.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"id":"one"}\n', encoding="utf-8")
    profile_a = Path(link_a.task_path) / "output" / "source-a.md"
    profile_b = Path(link_b.task_path) / "output" / "source-b.md"
    profile_a.parent.mkdir(parents=True, exist_ok=True)
    profile_b.parent.mkdir(parents=True, exist_ok=True)
    profile_a.write_text("source_id: source-a\n", encoding="utf-8")
    profile_b.write_text("source_id: source-b\n", encoding="utf-8")

    agent._current_run_params = SimpleNamespace(task_attributes=attrs_a)
    try:
        opened_a = WatchStreamTool(agent).execute(
            {
                "action": "open",
                "source_id": "source-a",
                "url": str(source),
                "source_profile_ref": str(profile_a),
                "record_boundary": {"mode": "line"},
            }
        )
    finally:
        delattr(agent, "_current_run_params")
    agent._current_run_params = SimpleNamespace(task_attributes=attrs_b)
    try:
        opened_b = WatchStreamTool(agent).execute(
            {
                "action": "open",
                "source_id": "source-b",
                "url": str(source),
                "source_profile_ref": str(profile_b),
                "record_boundary": {"mode": "line"},
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert opened_a.ok, opened_a.output
    assert opened_b.ok, opened_b.output
    watch_a = str(json.loads(opened_a.output)["watch_id"])
    watch_b = str(json.loads(opened_b.output)["watch_id"])
    assert watch_a != watch_b
    state_a = load_state(Path(agent.home_paths.owner_home_dir), watch_a)
    state_b = load_state(Path(agent.home_paths.owner_home_dir), watch_b)
    assert state_a is not None and state_a.prepare_root_task_id == link_a.task_id
    assert state_b is not None and state_b.prepare_root_task_id == link_b.task_id


def test_prepare_open_requires_explicit_source_identity_before_creating_probe(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-explicit-source-identity",
        command_text="/audit 明确来源 prepare 现场验证一条来源",
    )
    prepared = _link(agent, conversation.thread_id, "明确来源")
    source = Path(prepared.task_path) / "output" / "events.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"event_id":"one"}\n', encoding="utf-8")
    profile = Path(prepared.task_path) / "work" / "source-a.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id: source-a\n", encoding="utf-8")

    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        missing_both = WatchStreamTool(agent).execute(
            {"action": "open", "url": str(source), "record_boundary": {"mode": "line"}}
        )
        missing_profile = WatchStreamTool(agent).execute(
            {
                "action": "open",
                "url": str(source),
                "record_boundary": {"mode": "line"},
                "source_id": "source-a",
            }
        )
        opened = WatchStreamTool(agent).execute(
            {
                "action": "open",
                "url": str(source),
                "record_boundary": {"mode": "line"},
                "source_id": "source-a",
                "source_profile_ref": str(profile),
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert missing_both.ok is False
    assert missing_both.error_code == "TOOL_PARAMETER_REQUIRED"
    assert missing_both.reported_error_code == "AUDIT_SOURCE_IDENTITY_REQUIRED"
    assert "source_id" in missing_both.output
    assert "source_profile_ref" not in missing_both.output
    assert missing_profile.ok is True
    assert opened.ok, opened.output


def test_ordinary_open_may_still_derive_source_identity(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    source = Path(agent.home_paths.owner_home_dir) / "workspace" / "ordinary.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"event_id":"one"}\n', encoding="utf-8")

    opened = WatchStreamTool(agent).execute(
        {"action": "open", "url": str(source), "record_boundary": {"mode": "line"}}
    )

    assert opened.ok, opened.output
    state = load_state(
        Path(agent.home_paths.owner_home_dir),
        str(json.loads(opened.output)["watch_id"]),
    )
    assert state is not None
    assert state.source_id


def test_prepare_can_publish_profile_bound_by_real_watch_open(tmp_path: Path) -> None:
    """One prepare turn may safely amend an early publish with a later probe."""

    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-real-profile-probe",
        command_text="/audit 真实探针 prepare 保存已经试通的文件来源",
    )
    prepared = _link(agent, conversation.thread_id, "真实探针")
    output = Path(prepared.task_path) / "output"
    output.mkdir(parents=True, exist_ok=True)
    profile = output / "source-file.md"
    profile.write_text("source_id: source-file\n", encoding="utf-8")
    source = output / "events.jsonl"
    source.write_text('{"event_id":"one"}\n', encoding="utf-8")
    binding = {
        "source_id": "source-file",
        "url": str(source),
        "source_profile_ref": str(profile),
        "record_boundary": {"mode": "line"},
    }

    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        early_publish = PublishAuditUpdateTool(agent).execute(
            {
                "effective_prompt": "逐条读取文件并按用户规则判断",
                "validation_status": "not_required",
            }
        )
        availability_after_early_publish = PublishAuditUpdateTool(agent).availability()
        opened = WatchStreamTool(agent).execute({"action": "open", **binding})
        assert opened.ok, opened.output
        watch_id = str(json.loads(opened.output)["watch_id"])
        published = PublishAuditUpdateTool(agent).execute(
            {
                "effective_prompt": "逐条读取文件并按用户规则判断",
                "validation_status": "passed",
                "validation_refs": [str(profile)],
                "source_probe_refs": [watch_id],
            }
        )
        availability_after_publish = PublishAuditUpdateTool(agent).availability()
        stale_attrs = dict(attrs)
        stale_attrs["conversation_turn_request_id"] = "stale-prepare-turn"
        agent._current_run_params = SimpleNamespace(task_attributes=stale_attrs)
        stale = PublishAuditUpdateTool(agent).execute(
            {
                "effective_prompt": "旧轮次不能覆盖",
                "validation_status": "passed",
                "validation_refs": [str(profile)],
                "source_probe_refs": [watch_id],
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert early_publish.ok, early_publish.output
    assert availability_after_early_publish.available is True
    assert published.ok, published.output
    assert json.loads(published.output)["prepare_consumed"] is True
    assert json.loads(published.output)["retired_source_probe_count"] == 1
    assert availability_after_publish.available is True
    assert stale.ok is False
    assert stale.error_code == "AUDIT_PREPARE_ALREADY_PUBLISHED"
    stored = agent.conversation_store.tasks.load(prepared.task_id)
    assert stored is not None
    assert stored.effective_revision == 2
    assert len(stored.effective_source_bindings) == 1
    assert stored.effective_source_bindings[0]["source_profile_ref"] == str(profile.resolve())
    retired_probe = load_state(
        Path(agent.home_paths.owner_home_dir),
        watch_id,
    )
    assert retired_probe is not None
    assert retired_probe.closed is True
    assert retired_probe.close_reason == "audit_prepare_published"

    _, _, second_attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-real-profile-probe-second",
        command_text="/audit 真实探针 prepare 增加第二路已经试通的文件来源",
    )
    second_profile = output / "source-file-2.md"
    second_profile.write_text("source_id: source-file-2\n", encoding="utf-8")
    second_source = output / "events-2.jsonl"
    second_source.write_text('{"event_id":"two"}\n', encoding="utf-8")
    second_binding = {
        "source_id": "source-file-2",
        "url": str(second_source),
        "source_profile_ref": str(second_profile),
        "record_boundary": {"mode": "line"},
    }
    agent._current_run_params = SimpleNamespace(task_attributes=second_attrs)
    try:
        second_opened = WatchStreamTool(agent).execute({"action": "open", **second_binding})
        assert second_opened.ok, second_opened.output
        second_watch_id = str(json.loads(second_opened.output)["watch_id"])
        second_published = PublishAuditUpdateTool(agent).execute(
            {
                "effective_prompt": "在原来源之外增加第二路文件",
                "validation_status": "passed",
                "validation_refs": [str(second_profile)],
                "source_probe_refs": [second_watch_id],
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert second_published.ok, second_published.output
    merged = agent.conversation_store.tasks.load(prepared.task_id)
    assert merged is not None
    assert [row["source_id"] for row in merged.effective_source_bindings] == [
        "source-file",
        "source-file-2",
    ]


def test_http_probe_rejects_file_only_record_boundary_before_publish(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-http-record-boundary",
        command_text="/audit HTTP来源 prepare 保存已验证来源",
    )
    prepared = _link(agent, conversation.thread_id, "HTTP来源")
    profile = Path(prepared.task_path) / "work" / "http-source.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id: http-source\n", encoding="utf-8")
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        rejected = WatchStreamTool(agent).execute(
            {
                "action": "open",
                "source_id": "http-source",
                "url": "https://source.example.invalid/events?since=<next>",
                "record_list_field": "items",
                "record_boundary": {"mode": "line"},
                "source_profile_ref": str(profile),
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert rejected.ok is False
    assert rejected.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "record_boundary" in rejected.output
    unchanged = agent.conversation_store.tasks.load(prepared.task_id)
    assert unchanged is not None
    assert unchanged.effective_revision == 0


def test_new_source_binding_cannot_borrow_another_audit_profile(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs_a = _register(
        agent,
        tmp_path,
        request_id="prepare-source-a",
        command_text="/audit A prepare 验证来源 A",
    )
    _register(
        agent,
        tmp_path,
        request_id="prepare-source-b",
        command_text="/audit B prepare 验证来源 B",
    )
    link_a = _link(agent, conversation.thread_id, "A")
    link_b = _link(agent, conversation.thread_id, "B")
    own_evidence = Path(link_a.task_path) / "work" / "source-a.md"
    own_evidence.parent.mkdir(parents=True, exist_ok=True)
    own_evidence.write_text("source_id: source-a\n", encoding="utf-8")
    foreign_profile = Path(link_b.task_path) / "work" / "source-b.md"
    foreign_profile.parent.mkdir(parents=True, exist_ok=True)
    foreign_profile.write_text("source_id: source-b\n", encoding="utf-8")
    tool = PublishAuditUpdateTool(agent)
    agent._current_run_params = SimpleNamespace(task_attributes=attrs_a)
    try:
        rejected = tool.execute(
            {
                "effective_prompt": "发布当前 Audit 的验证结果",
                "validation_status": "passed",
                "validation_refs": [str(own_evidence), str(foreign_profile)],
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert rejected.ok is False
    assert rejected.error_code == "AUDIT_VALIDATION_EVIDENCE_INVALID"
    assert "work/<文件名>" in rejected.output
    assert "write_file" in rejected.output
    unchanged = agent.conversation_store.tasks.load(link_a.task_id)
    assert unchanged is not None
    assert unchanged.effective_revision == 0
    assert unchanged.effective_source_bindings == ()


def test_source_probe_profile_is_optional_workspace_material(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-scoped-sources",
        command_text="/audit scoped prepare 先验证 source-a",
    )
    prepared = _link(agent, conversation.thread_id, "scoped")
    profile = Path(prepared.task_path) / "work" / "source-a.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("source_id: source-a\n", encoding="utf-8")
    _, watch_id = _persist_verified_probe(
        agent,
        prepared,
        {
            "source_id": "source-a",
            "url": "https://a.example.invalid/events?cursor=<next>",
            "source_profile_ref": str(profile),
        },
        watch_suffix="missing-profile",
    )
    state = load_state(Path(agent.home_paths.owner_home_dir), watch_id)
    assert state is not None
    state.source_profile_ref = ""
    persist_state(state)
    tool = PublishAuditUpdateTool(agent)
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        published = tool.execute(
            {
                "effective_prompt": "只验证过 source-a，不能带入别处的 source-b",
                "validation_status": "passed",
                "validation_refs": [str(profile)],
                "source_probe_refs": [watch_id],
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert published.ok is True
    stored = agent.conversation_store.tasks.load(prepared.task_id)
    assert stored is not None
    assert stored.effective_revision == 1
    assert len(stored.effective_source_bindings) == 1
    assert "source_profile_ref" not in stored.effective_source_bindings[0]


def test_publish_replace_makes_probe_refs_the_exact_source_set(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-replace-set-1",
        command_text="/audit 来源集合 prepare 先验证两路来源",
    )
    prepared = _link(agent, conversation.thread_id, "来源集合")
    bindings = [
        {
            "source_id": source_id,
            "url": f"https://{source_id}.example.invalid/events?cursor=<next>",
        }
        for source_id in ("source-a", "source-b")
    ]
    probes = [
        _persist_verified_probe(
            agent,
            prepared,
            binding,
            watch_suffix=source_id,
        )[1]
        for source_id, binding in zip(("source-a", "source-b"), bindings, strict=True)
    ]
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        first = PublishAuditUpdateTool(agent).execute(
            {
                "effective_prompt": "两路来源均已现场验证",
                "validation_status": "passed",
                "source_probe_refs": probes,
                "source_update_mode": "replace",
            }
        )
    finally:
        delattr(agent, "_current_run_params")
    assert first.ok, first.output

    _register(
        agent,
        tmp_path,
        request_id="start-replace-set",
        command_text="/audit 10m 来源集合 启动两路监测",
    )
    active = _link(agent, conversation.thread_id, "来源集合")
    owner_home = Path(agent.home_paths.owner_home_dir)
    retired_watch = new_state(
        owner_home,
        str(bindings[0]["url"]),
        {},
        watch_id=watch_id_for(owner_home, str(bindings[0]["url"]), active.task_id),
    )
    retired_watch.source_id = "source-a"
    retired_watch.audit_guarantee = True
    retired_watch.audit_root_task_id = active.task_id
    retired_watch.audit_run_epoch = active.run_epoch
    persist_state(retired_watch)

    _, _, second_attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-replace-set-2",
        command_text="/audit 来源集合 prepare 现在只保留 source-b",
    )
    current = _link(agent, conversation.thread_id, "来源集合")
    source_b_probe = _persist_verified_probe(
        agent,
        current,
        bindings[1],
        watch_suffix="source-b-revised",
    )[1]
    agent._current_run_params = SimpleNamespace(task_attributes=second_attrs)
    try:
        rejected_unconfirmed_removal = PublishAuditUpdateTool(agent).execute(
            {
                "effective_prompt": "当前有效集合只包含 source-b",
                "validation_status": "passed",
                "source_probe_refs": [source_b_probe],
                "source_update_mode": "replace",
            }
        )
        unchanged_after_rejection = agent.conversation_store.tasks.load(
            prepared.task_id
        )
        replaced = PublishAuditUpdateTool(agent).execute(
            {
                "effective_prompt": "当前有效集合只包含 source-b",
                "validation_status": "passed",
                "source_probe_refs": [source_b_probe],
                "source_update_mode": "replace",
                "remove_source_ids": ["source-a"],
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert rejected_unconfirmed_removal.ok is False
    assert rejected_unconfirmed_removal.error_code == "AUDIT_SOURCE_BINDINGS_INVALID"
    assert "remove_source_ids" in rejected_unconfirmed_removal.output
    assert unchanged_after_rejection is not None
    assert [row["source_id"] for row in unchanged_after_rejection.effective_source_bindings] == [
        "source-a",
        "source-b",
    ]
    assert replaced.ok, replaced.output
    assert json.loads(replaced.output)["retired_active_source_count"] == 1
    stored = agent.conversation_store.tasks.load(prepared.task_id)
    assert stored is not None
    assert [row["source_id"] for row in stored.effective_source_bindings] == ["source-b"]
    retired_state = load_state(owner_home, retired_watch.watch_id)
    assert retired_state is not None
    assert retired_state.closed is True
    assert retired_state.close_reason == "audit_source_membership_removed"


def test_publish_rejects_unknown_source_update_mode(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _, _conversation, attrs = _register(
        agent,
        tmp_path,
        request_id="prepare-invalid-source-mode",
        command_text="/audit 来源模式 prepare 检查发布动作",
    )
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    try:
        result = PublishAuditUpdateTool(agent).execute(
            {
                "effective_prompt": "保持现有要求",
                "validation_status": "not_required",
                "source_update_mode": "guess",
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert result.ok is False
    assert result.error_code == "AUDIT_SOURCE_UPDATE_MODE_INVALID"


def test_passed_validation_accepts_only_successful_artifact_from_exact_audit(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    _, conversation, attrs_a = _register(
        agent,
        tmp_path,
        request_id="prepare-artifact-a",
        command_text="/audit A prepare 验证来源 A",
    )
    _register(
        agent,
        tmp_path,
        request_id="prepare-artifact-b",
        command_text="/audit B prepare 验证来源 B",
    )
    link_a = _link(agent, conversation.thread_id, "A")
    link_b = _link(agent, conversation.thread_id, "B")

    valid = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=Path(link_a.task_path) / "work",
            tool="web_fetch",
            call_id="probe-ok",
            output='{"ok":true,"items":[1]}',
            ok=True,
            run_id="prepare-artifact-a",
            task_id=link_a.task_id,
            request_id="prepare-artifact-a",
            min_chars=0,
        )
    )
    failed = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=Path(link_a.task_path) / "work",
            tool="web_fetch",
            call_id="probe-failed",
            output='{"ok":false,"error":"timeout"}',
            ok=False,
            run_id="prepare-artifact-a",
            task_id=link_a.task_id,
            request_id="prepare-artifact-a",
            min_chars=0,
        )
    )
    sibling = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=Path(link_b.task_path) / "work",
            tool="web_fetch",
            call_id="probe-sibling",
            output='{"ok":true,"items":[2]}',
            ok=True,
            run_id="prepare-artifact-b",
            task_id=link_b.task_id,
            request_id="prepare-artifact-b",
            min_chars=0,
        )
    )

    tool = PublishAuditUpdateTool(agent)
    agent._current_run_params = SimpleNamespace(task_attributes=attrs_a)
    try:
        rejected_failed = tool.execute(
            {
                "effective_prompt": "不能采用失败探针",
                "validation_status": "passed",
                "validation_refs": [failed["artifact_ref"]],
            }
        )
        rejected_sibling = tool.execute(
            {
                "effective_prompt": "不能采用兄弟 Audit 探针",
                "validation_status": "passed",
                "validation_refs": [sibling["artifact_ref"]],
            }
        )
        published = tool.execute(
            {
                "effective_prompt": "采用当前 Audit 已通过的真实探针",
                "validation_status": "passed",
                "validation_refs": [valid["scoped_call_id"]],
            }
        )
    finally:
        delattr(agent, "_current_run_params")

    assert rejected_failed.ok is False
    assert rejected_failed.error_code == "AUDIT_VALIDATION_EVIDENCE_INVALID"
    assert rejected_sibling.ok is False
    assert rejected_sibling.error_code == "AUDIT_VALIDATION_EVIDENCE_INVALID"
    assert published.ok is True
    updated = agent.conversation_store.tasks.load(link_a.task_id)
    assert updated is not None
    assert updated.effective_evidence_refs == (str(Path(valid["artifact_ref"]).resolve()),)
