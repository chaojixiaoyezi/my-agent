"""预检经真实 ToolExecutor 与 scheduler tick，证明无变化不调模型、失败不提交 checkpoint。"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent.scheduler.precheck import _json_pointer
from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from agent_py_agent.tests.test_scheduler_execution import execution_case


# LLM: 声明与执行均进入真实 Registry/Executor；只替换观察内容，不替换调度、权限或检查点。
# 类用途: 提供可控变化与失败的合成只读数据源。
class VersionTool(BaseTool):
    model_spec = ToolModelSpec("test_version", "合成版本源", {"type": "object", "properties": {}, "additionalProperties": False})
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy(default_effect="read_only"), approval_policy=ApprovalPolicy("dangerous"))

    # LLM: 每个夹具单独计数，不写生产文件或访问网络。
    # 函数用途: 初始化合成版本和执行计数。
    def __init__(self):
        self.version, self.calls, self.fail = 1, 0, False

    # LLM: 成功返回稳定 JSON；失败返回工具结构化失败，不能用正文代替 ok 字段。
    # 函数用途: 模拟只读检查有变化、无变化和明确失败。
    def execute(self, params):
        self.calls += 1
        return ToolHandlerOutcome("test_version", not self.fail, json.dumps({"version": self.version, "unstable": self.calls}),
                                  error_code="TOOL_EXECUTION_FAILED" if self.fail else "")


# LLM: 只向测试 Agent 的原 Registry 注册声明完整的替身，运行入口仍是后台 tick。
# 函数用途: 装配一项按版本字段检查的独立计划。
def precheck_case(tmp_path):
    case = execution_case(tmp_path, precheck={"tool_name": "test_version", "arguments": {}, "json_pointer": "/version"})
    case.tool = VersionTool()
    case.agent.tools.register(case.tool)
    case.channels.status = "sent"
    return case


def test_unchanged_cycle_skips_model_and_changed_cycle_advances_checkpoint_after_done(tmp_path):
    case = precheck_case(tmp_path)
    repo = case.agent.scheduler_repository
    case.scheduler.tick(now=1000)
    first = repo.get_job(case.job["job_id"])
    assert first["last_run_status"] == "done"
    checkpoint = first["precheck_checkpoint"]
    assert len(case.backend.prompts) == case.tool.calls == 1
    case.scheduler.tick(now=1600)
    assert case.tool.calls == 2 and len(case.backend.prompts) == 1
    assert repo.get_job(case.job["job_id"])["precheck_checkpoint"] == checkpoint
    rows, errors = repo.history()
    skipped = next(row for row in rows if row["run_id"] != case.signal.root_task_id)
    assert errors == [] and skipped["status"] == "skipped"
    assert skipped["precheck_observation"]["status"] == "no_change"
    case.tool.version = 2
    case.scheduler.tick(now=2200)
    assert case.tool.calls == 3 and len(case.backend.prompts) == 2
    assert repo.get_job(case.job["job_id"])["precheck_checkpoint"]["fingerprint"] != checkpoint["fingerprint"]


def test_failed_precheck_and_mutating_tool_do_not_run_model_or_save_checkpoint(tmp_path):
    case = precheck_case(tmp_path)
    case.tool.fail = True
    case.scheduler.tick(now=1000)
    repo = case.agent.scheduler_repository
    history, _ = repo.history()
    assert history[0]["status"] == "failed" and history[0]["precheck_observation"]["status"] == "failed"
    assert "precheck_checkpoint" not in repo.get_job(case.job["job_id"])
    assert case.backend.prompts == [] and case.tool.calls == 2  # 原工具执行器允许一次只读瞬态重试。
    case.tool.runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy(default_effect="mutating"), approval_policy=ApprovalPolicy("dangerous"))
    case.scheduler.tick(now=1600)
    assert case.backend.prompts == [] and case.tool.calls == 2


def test_delivery_failure_retains_observation_until_frozen_result_delivered(tmp_path):
    case = precheck_case(tmp_path)
    case.channels.status = "rejected"
    case.scheduler.tick(now=1000)
    repo = case.agent.scheduler_repository
    assert "precheck_checkpoint" not in repo.get_job(case.job["job_id"])
    case.channels.status = "sent"
    case.scheduler.tick(now=1031)
    assert case.tool.calls == 1 and len(case.backend.prompts) == 1
    assert repo.get_job(case.job["job_id"])["precheck_checkpoint"]["run_id"] == case.signal.root_task_id


def test_precheck_uses_original_background_interrupt_and_claim(tmp_path):
    from agent_py_agent.agent.concurrency.interrupt import interrupt_by_name, wait_interruptibly
    from agent_py_agent.agent.conversation.run_claim import (
        conversation_claim_interrupt_name,
    )

    case = precheck_case(tmp_path)
    entered = threading.Event()
    def observe(params):
        case.tool.calls += 1
        entered.set()
        wait_interruptibly(10)
        return ToolHandlerOutcome("test_version", True, '{"version":1}')
    case.tool.execute = observe
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(case.scheduler.tick, now=1000)
        assert entered.wait(5)
        claim = case.store.claims.load(case.signal.thread_id)
        assert claim["runtime_authority"]["task_id"] == case.signal.root_task_id
        assert claim["runtime_authority"]["attempt_id"]
        assert interrupt_by_name(conversation_claim_interrupt_name(claim["claim_id"]))
        future.result(timeout=5)
    assert case.backend.prompts == [] and case.tool.calls == 1
    repo = case.agent.scheduler_repository
    assert "precheck_checkpoint" not in repo.get_job(case.job["job_id"])
    run = repo.get_active_run(case.signal.root_task_id)
    rows, _ = repo.history()
    actual = run or next(row for row in rows if row["run_id"] == case.signal.root_task_id)
    assert "precheck_observation" not in actual


def test_precheck_observation_survives_service_restart_without_reexecution(tmp_path):
    from agent_py_agent.agent.conversation import BackgroundMainAgentScheduler
    from agent_py_agent.agent.scheduler.service import SchedulerService

    case = precheck_case(tmp_path)
    case.channels.status = "rejected"
    case.scheduler.tick(now=1000)
    repo = case.agent.scheduler_repository
    case.agent.scheduler_service = SchedulerService(repo, conversation_store=case.store)
    restarted = BackgroundMainAgentScheduler({"runtime": case.scheduler.runtime, "store": case.store,
                                              "scheduler_service": case.agent.scheduler_service})
    case.channels.status = "sent"
    restarted.tick(now=1031)
    assert case.tool.calls == len(case.backend.prompts) == 1
    assert repo.get_job(case.job["job_id"])["precheck_checkpoint"]["run_id"] == case.signal.root_task_id


def test_editing_precheck_during_action_does_not_commit_old_checkpoint(tmp_path):
    case = precheck_case(tmp_path)
    case.channels.status = "rejected"
    case.scheduler.tick(now=1000)
    repo = case.agent.scheduler_repository
    job = repo.get_job(case.job["job_id"])
    repo.update_job(job["job_id"], expected_version=job["version"], patch={
        "precheck": {"tool_name": "test_version", "arguments": {}, "json_pointer": "/unstable"},
    })
    case.channels.status = "sent"
    case.scheduler.tick(now=1031)
    assert "precheck_checkpoint" not in repo.get_job(job["job_id"])
    assert case.tool.calls == len(case.backend.prompts) == 1


def test_precheck_archives_once_and_compares_complete_output_beyond_preview(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service
    from agent_py_agent.agent.memory_archive.artifact.reader import (
        ReadToolOutputArtifactRequest,
        read_tool_output_artifact,
    )

    case = precheck_case(tmp_path)
    records = []
    original = _tool_loop_service._record_tool_call

    def record(agent, request):
        original(agent, request)
        records.extend(request.params.archive_tool_calls)

    def observe(params):
        case.tool.calls += 1
        return ToolHandlerOutcome("test_version", True, json.dumps({"padding": "资料" * 80_000, "version": case.tool.version}))

    monkeypatch.setattr(_tool_loop_service, "_record_tool_call", record)
    case.tool.execute = observe
    case.scheduler.tick(now=1000)
    assert len(records) == len(case.backend.prompts) == case.tool.calls == 1
    entry = records[0]
    assert entry["call_id"] == f"host-tool-{case.signal.root_task_id}"
    assert entry["actor"] == "host" and entry["run_id"] == case.signal.root_task_id
    assert entry["output_externalized"] is True
    # 使用真实reader同时核对JSON包装和blob正文，不能假定大输出仍在旧content字段内。
    artifact = read_tool_output_artifact(ReadToolOutputArtifactRequest(
        root=case.agent.home_paths.owner_home_dir, artifact_ref=entry["scoped_call_id"], max_chars=0))
    assert artifact["ok"] is True and artifact["content_hash_verified"] is True
    assert artifact["truncated"] is False
    assert json.loads(artifact["content"])["version"] == 1
    db = case.agent.subagents.runtime_db
    row = db.agent_run_for_run_id(case.signal.root_task_id)
    events = db.events_for_agent_run(row["agent_run_id"], event_type="tool_completed")
    assert len(events) == 1 and events[0]["payload"]["actor"] == "host"
    case.scheduler.tick(now=1600)
    assert case.tool.calls == 2 and len(case.backend.prompts) == 1
    case.tool.version = 2
    case.scheduler.tick(now=2200)
    assert case.tool.calls == 3 and len(case.backend.prompts) == 2


@pytest.mark.parametrize("ok", [True, False])
def test_precheck_recovery_summary_preserves_status(tmp_path, monkeypatch, ok):
    from agent_py_agent.agent.agent_core import _tool_loop_service

    case = precheck_case(tmp_path)
    records = []
    original = _tool_loop_service._record_tool_call

    def record(agent, request):
        original(agent, request)
        records.extend(request.params.archive_tool_calls)

    case.tool.execute = lambda _params: ToolHandlerOutcome("test_version", ok, json.dumps({
        "version": 1, "artifact_integrity": {"ok": ok}, "padding": "x" * 10_000}),
        error_code="" if ok else "INVALID_ARGUMENTS")
    monkeypatch.setattr(_tool_loop_service, "_record_tool_call", record)
    case.scheduler.tick(now=1000)
    assert len(records) == 1 and records[0]["ok"] is ok
    assert ("status=ok" if ok else "status=error") in records[0]["model_summary"]
    if not ok:
        assert records[0]["error_code"] in records[0]["model_summary"]


def test_archive_summary_does_not_downgrade_canonical_external_data():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_call_archive_record import _attach_model_summary

    result = SimpleNamespace(tool_name="create_subagents", ok=True,
        output=json.dumps({"created_run_ids": ["child-one"]}),
        output_trust="external_data", output_redaction="default",
        metadata={"handler_details": {"tool_output_policy": {
            "trust": "runtime", "redaction": "none", "live_prompt_output": "Synthetic-Archive-Canary"}}})
    record = {"ok": True}
    _attach_model_summary(record, result)
    assert "model_summary" not in record


def test_precheck_archive_failure_is_failed_without_advancing_checkpoint(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import tool_call_archive_record

    case = precheck_case(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("synthetic archive unavailable")

    monkeypatch.setattr(tool_call_archive_record, "archive_tool_output_projection", fail)
    case.scheduler.tick(now=1000)
    rows, _ = case.agent.scheduler_repository.history()
    assert rows[0]["status"] == "failed"
    assert "precheck_checkpoint" not in case.agent.scheduler_repository.get_job(case.job["job_id"])
    assert case.backend.prompts == []


def test_real_read_file_precheck_uses_thread_directory_and_unchanged_cycle_has_no_model(tmp_path):
    work = tmp_path / "home/owners/local/main/workspace/task-workspace"
    work.mkdir(parents=True)
    material = work / "revision.txt"
    material.write_text("revision=1\n")
    case = execution_case(tmp_path, cwd=work, precheck={"tool_name": "read_file", "arguments": {"path": "revision.txt"}})
    case.channels.status = "sent"
    repo = case.agent.scheduler_repository
    case.scheduler.tick(now=1000)
    assert repo.get_job(case.job["job_id"])["last_run_status"] == "done", json.dumps([r.get("precheck_observation") for r in repo.history()[0]])
    assert len(case.backend.prompts) == 1
    case.scheduler.tick(now=1600)
    assert len(case.backend.prompts) == 1
    material.write_text("revision=2\n")
    case.scheduler.tick(now=2200)
    assert len(case.backend.prompts) == 2
    material.unlink()
    case.scheduler.tick(now=2800)
    assert len(case.backend.prompts) == 2
    assert repo.get_job(case.job["job_id"])["last_run_status"] == "failed"
    history, _ = repo.history()
    failed = next(row for row in history if row["status"] == "failed")
    assert failed["precheck_observation"]["tool_error_code"] == "PATH_NOT_FOUND"


@pytest.mark.parametrize("pointer", ["/missing", "/rows/01", "/rows/-1", "/bad~9key"])
def test_invalid_pointer_is_failure_not_unchanged(pointer):
    with pytest.raises((KeyError, ValueError, TypeError)):
        _json_pointer({"rows": [1, 2]}, pointer)
