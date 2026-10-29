# LLM: 只在 pytest 临时 owner 内验收账本真实落盘、边界和聚合，不修改生产 owner。
# 模块用途: 覆盖工具遥测与任务调用详情的持久化合同。
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model import call_runtime
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger, ModelCallLedgerContext
from agent_py_agent.agent.observability import task_call_inspector as inspector_module
from agent_py_agent.agent.observability.task_call_inspector import TaskCallInspector, TaskCallRecord
from agent_py_agent.agent.observability.telemetry_ledger import ToolTelemetryLedger


# LLM: 创建确定时间的普通工具调用，方便复算过滤和分位。
# 函数用途: 提供不含真实凭据的记录输入。
def call(index=0, **changes):
    return dict(call_id=f"c{index}", tool_name="read", task_id="t1", started_at=10 + index,
                ended_at=10.1 + index, **changes)


# LLM: 构造与空查询不应写 owner 数据。
# 函数用途: 验证空账本是只读的。
def test_empty_is_read_only(tmp_path):
    ledger = ToolTelemetryLedger(tmp_path, owner_id="one")
    assert ledger.query_tool_calls() == []
    assert ledger.get_tool_call("missing") is None
    assert not (tmp_path / "data").exists()


# LLM: 实际文件中的参数必须已脱敏，返回对象不得修改原输入。
# 函数用途: 检查嵌套脱敏、时长和重新打开后的查询。
def test_roundtrip_redaction(tmp_path):
    ledger = ToolTelemetryLedger(tmp_path, owner_id="one")
    args = {"Authorization": "private", "nested": [{"apiKey": "private", "refresh_token": "private"}], "text": "x" * 900}
    row = ledger.record_tool_call(call(args_summary=args))
    assert row.duration_ms == 100
    assert row.owner_id == "one"
    text = (tmp_path / "data/telemetry/tool_calls.jsonl").read_text()
    assert "private" not in text
    assert args["Authorization"] == "private"
    assert len(row.args_summary["text"]) <= 200
    assert ToolTelemetryLedger(tmp_path, owner_id="one").get_tool_call("c0") == row.to_dict()


# LLM: 查询以 started_at 闭区间过滤，cursor 为匹配结果的偏移。
# 函数用途: 验证分页、筛选与统计数值。
def test_filters_and_statistics(tmp_path):
    ledger = ToolTelemetryLedger(tmp_path)
    for i, status in enumerate(["ok", "error", "denied", "timeout"]):
        ledger.record_tool_call(call(i, status=status, duration_ms=(i + 1) * 10, error_code="E" if i else ""))
    assert len(ledger.query_tool_calls(task_id="t1", since=11, until=12)) == 2
    assert ledger.query_tool_calls(limit=1, cursor=2)[0]["call_id"] == "c2"
    assert len(ledger.query_tool_calls(min_duration_ms=30)) == 2
    assert len(ledger.query_tool_calls(status="ok", tool_name="read")) == 1
    assert ledger.query_tool_calls(run_id="other", workflow_run_id="other") == []
    stats = ledger.aggregate_tool_stats()["tools"]["read"]
    assert (stats["calls"], stats["succeeded"], stats["failed"]) == (4, 1, 3)
    assert stats["success_rate"] == .25
    assert [stats[k] for k in ("avg_duration_ms", "p50_duration_ms", "p95_duration_ms", "max_duration_ms")] == [25, 20, 40, 40]
    assert stats["error_codes"] == {"E": 3}
    assert ledger.aggregate_tool_stats(since=50)["total_calls"] == 0


# LLM: 结构化状态和 owner 冲突必须在写前拒绝。
# 函数用途: 拒绝非法记录且不留下账本。
@pytest.mark.parametrize("changes", [{"status": "success"}, {"duration_ms": float("nan")}, {"owner_id": "other"}, {"output_bytes": -1}])
def test_invalid_record(tmp_path, changes):
    ledger = ToolTelemetryLedger(tmp_path, owner_id="one")
    with pytest.raises(ValueError):
        ledger.record_tool_call(call(**changes))
    assert ledger.query_tool_calls() == []


# LLM: 不允许遥测父目录指向 owner 之外。
# 函数用途: 验证 no-follow 子链约束。
def test_symlink_refused(tmp_path):
    home, outside = tmp_path / "home", tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / "data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        ToolTelemetryLedger(home).record_tool_call(call())
    assert list(outside.iterdir()) == []


# LLM: 多实例写同一文件必须由领域锁串行，不丢失或拼接行。
# 函数用途: 实测线程并发追加。
def test_parallel_writers(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: ToolTelemetryLedger(tmp_path).record_tool_call(call(i)), range(32)))
    assert len(ToolTelemetryLedger(tmp_path).query_tool_calls()) == 32


# LLM: 从公开接口验证任务写入、模型轮次和跨页工具明细，公共类型导入集中在模块头。
# 函数用途: 测试任务完整生命周期。
def test_task_lifecycle_and_timeline(tmp_path):
    inspector = TaskCallInspector(tmp_path, owner_id="one")
    assert inspector.list_task_calls() == []
    assert inspector.get_task_call_detail("t1") is None
    assert not (tmp_path / "data").exists()
    inspector.upsert_task_call(dict(task_id="t1", started_at=10, title="任务", runs=[dict(run_id="r1", parent_run_id="", role="main", model_name="m", status="running", duration_ms=5)]))
    inspector.append_round("t1", dict(duration_ms=100, prompt_tokens=10, completion_tokens=3, cached_tokens=4, tool_call_ids=["c0"]))
    ledger = ToolTelemetryLedger(tmp_path, owner_id="one")
    for i in range(105):
        ledger.record_tool_call(call(i))
    inspector.complete_task_call("t1", "succeeded", ended_at=20)
    detail = inspector.get_task_call_detail("t1", ledger)
    assert detail["status"] == "succeeded"
    assert detail["duration_ms"] == 10000
    assert detail["token_usage"] == dict(prompt_tokens=10, completion_tokens=3, cached_tokens=4, total_tokens=13)
    assert len(detail["tool_calls"]) == 105
    assert detail["runs"][0]["model_name"] == "m"
    assert inspector.list_task_calls(status="succeeded", since=10, until=10)[0]["task_id"] == "t1"
    inspector.upsert_task_call(dict(task_id="t1", title="更新"))
    assert len(inspector.list_task_calls()) == 1
    assert len(inspector.get_task_call_detail("t1")["rounds"]) == 1


# LLM: 路径输入与 owner 冲突不得产生越界文件。
# 函数用途: 验证任务查询和写入的身份边界。
@pytest.mark.parametrize("task_id", ["../escape", "/tmp/escape", "a/b", "a\\b", "..", ""])
def test_task_path_rejection(tmp_path, task_id):
    inspector = TaskCallInspector(tmp_path, owner_id="one")
    with pytest.raises(ValueError):
        inspector.upsert_task_call(dict(task_id=task_id))
    with pytest.raises(ValueError):
        inspector.get_task_call_detail(task_id)


# LLM: 任务 JSON 是权威，索引丢失或陈旧不能导致任务丢失。
# 函数用途: 验证重启、索引修复以及多实例并发追加。
def test_task_restart_index_and_concurrent_rounds(tmp_path):
    inspector = TaskCallInspector(tmp_path, owner_id="one")
    inspector.upsert_task_call(dict(task_id="t1", started_at=10))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: TaskCallInspector(tmp_path, owner_id="one").append_round("t1", dict(round_id=i, prompt_tokens=2, completion_tokens=1)), range(12)))
    index = tmp_path / "data/telemetry/task_calls_index.jsonl"
    index.write_text("")
    reopened = TaskCallInspector(tmp_path, owner_id="one")
    assert len(reopened.list_task_calls()) == 1
    assert reopened.get_task_call_detail("t1")["token_usage"]["total_tokens"] == 36
    reopened.complete_task_call("t1", "failed", ended_at=11)
    assert '"task_id": "t1"' in index.read_text()
    with pytest.raises(ValueError):
        reopened.upsert_task_call(dict(task_id="t1", owner_id="other"))
    with pytest.raises(ValueError):
        reopened.get_task_call_detail("t1", ToolTelemetryLedger(tmp_path / "other"))


# LLM: 使用独立 Python 子进程写同一临时 owner，测系统锁而不只测线程锁。
# 函数用途: 验证多进程调用记录不丢失。
def test_process_writers(tmp_path):
    import subprocess
    import sys
    code = """import sys
from agent_py_agent.agent.observability.telemetry_ledger import ToolTelemetryLedger
ledger = ToolTelemetryLedger(sys.argv[1])
for i in range(10):
    ledger.record_tool_call(dict(call_id=sys.argv[2]+str(i), tool_name='read'))
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(i)]) for i in range(3)]
    assert [p.wait(timeout=30) for p in processes] == [0, 0, 0]
    rows = ToolTelemetryLedger(tmp_path).query_tool_calls()
    assert len({r["call_id"] for r in rows}) == 30


# LLM: 文件破损不得在统计中被静默跳过。
# 函数用途: 检查损坏账本显式报错。
def test_corrupt_ledger_not_silently_skipped(tmp_path):
    ledger = ToolTelemetryLedger(tmp_path)
    ledger.record_tool_call(call())
    with (tmp_path / "data/telemetry/tool_calls.jsonl").open("a") as stream:
        stream.write("broken\n")
    with pytest.raises(ValueError):
        ledger.aggregate_tool_stats()


# LLM: 记录为 dataclass 输入时不得在规范化过程中修改调用方对象。
# 函数用途: 检查输入独立性及所有关联字段。
def test_dataclass_input_and_associations(tmp_path):
    from agent_py_agent.agent.observability.telemetry_ledger import ToolCallRecord
    original = ToolCallRecord("d", "write", run_id="run", parent_run_id="parent", root_run_id="root", workflow_run_id="flow", step_id="step", args_summary={"cookie": "private"})
    ledger = ToolTelemetryLedger(tmp_path, owner_id="one")
    ledger.record_tool_call(original)
    assert original.owner_id == ""
    assert original.args_summary["cookie"] == "private"
    row = ledger.query_tool_calls(run_id="run", workflow_run_id="flow")[0]
    assert (row["parent_run_id"], row["root_run_id"], row["step_id"]) == ("parent", "root", "step")


# LLM: 状态、来源、token异常不得覆盖已有任务的合法快照。
# 函数用途: 验证非法任务更新的原子拒绝。
@pytest.mark.parametrize("change", [{"status": "ok"}, {"trigger_kind": "unknown"}, {"token_usage": {"prompt_tokens": -1}}, {"ended_at": 9}, {"runs": {}}])
def test_task_invalid_update(tmp_path, change):
    inspector = TaskCallInspector(tmp_path)
    inspector.upsert_task_call(dict(task_id="t1", started_at=10))
    before = inspector.get_task_call_detail("t1")
    with pytest.raises(ValueError):
        inspector.upsert_task_call({"task_id": "t1", **change})
    assert inspector.get_task_call_detail("t1") == before


# LLM: 四类触发源使用相同存储与过滤合同，引用列表不能丢失。
# 函数用途: 检查所有触发类型、资产和任务分页。
def test_trigger_assets_and_task_pages(tmp_path):
    inspector = TaskCallInspector(tmp_path)
    for i, kind in enumerate(["interactive", "monitor", "workflow", "plugin_api"]):
        inspector.upsert_task_call(TaskCallRecord(str(i), trigger_kind=kind, started_at=i, mounted_assets={"monitor_id": "m", "workflow_id": "w", "script_ids": ["s"], "skill_ids": ["k"]}, artifacts=["artifact://test"]))
    assert [r["task_id"] for r in inspector.list_task_calls(limit=2, cursor=1)] == ["1", "2"]
    assert inspector.list_task_calls(trigger_kind="plugin_api")[0]["task_id"] == "3"
    detail = inspector.get_task_call_detail("2")
    assert detail["mounted_assets"]["skill_ids"] == ["k"]
    assert detail["artifacts"] == ["artifact://test"]


# LLM: 目标叶子或父链软链接不能越界读写。
# 函数用途: 检查任务叶子链接拒绝。
def test_task_symlink_leaf(tmp_path):
    inspector = TaskCallInspector(tmp_path)
    inspector.upsert_task_call(dict(task_id="t1"))
    external = tmp_path / "outside.json"
    external.write_text("untouched")
    (tmp_path / "data/telemetry/task_calls/t2.json").symlink_to(external)
    with pytest.raises(OSError):
        inspector.upsert_task_call(dict(task_id="t2"))
    assert external.read_text() == "untouched"


# LLM: 全是假凭据；同时检查返回值和磁盘，避免只在展示层遮蔽。
# 函数用途: 验证命令内秘密与 Token 计数的差别处理。
def test_embedded_secrets_and_numeric_token_counts(tmp_path):
    secrets = ["dummy-password", "dummy-url-pass", "dummy-bearer", "sk-" + "dummykey123456", "dummy-cookie"]
    command = f"curl --password={secrets[0]} https://u:{secrets[1]}@host/api -H 'Authorization: Bearer {secrets[2]}'"
    args = dict(command=command, nested=[{"text": secrets[3]}, {"text": "Cookie: sid=" + secrets[4]}],
                max_tokens=4096, input_tokens=20, output_tokens=3, token=42)
    row = ToolTelemetryLedger(tmp_path).record_tool_call(call(args_summary=args))
    text = (tmp_path / "data/telemetry/tool_calls.jsonl").read_text()
    assert all(secret not in text for secret in secrets)
    assert [row.args_summary[k] for k in ("max_tokens", "input_tokens", "output_tokens")] == [4096, 20, 3]
    assert row.args_summary["token"] == "[REDACTED]"
    assert args["command"] == command


# LLM: 保存和追加轮次必须使用同一脱敏出口，计数不受影响。
# 函数用途: 验证任务各嵌套字段在真实文件中不泄露秘密。
def test_task_recursive_persistence_redaction(tmp_path):
    inspector = TaskCallInspector(tmp_path)
    secret = "dummy-task-secret"
    payload = dict(task_id="redact", runs=[{"password": secret}], rounds=[{"note": "secret=" + secret}],
                   mounted_assets={"config": {"api_key": secret}}, artifacts=["https://u:" + secret + "@host"])
    inspector.upsert_task_call(payload)
    inspector.append_round("redact", {"note": "Authorization: Bearer " + secret, "prompt_tokens": 9})
    text = (tmp_path / "data/telemetry/task_calls/redact.json").read_text()
    assert secret not in text
    assert inspector.get_task_call_detail("redact")["token_usage"]["total_tokens"] == 9
    assert payload["runs"][0]["password"] == secret


# LLM: 目标正文目前不在记录模型里；先用字段合同断言避免把 TypeError 当复现。
# 函数用途: 验证 goal 被接受并在持久化之前脱敏。
def test_task_goal_is_supported_and_redacted(tmp_path):
    assert "goal" in TaskCallRecord.__dataclass_fields__
    secret = "dummy-goal-secret"
    inspector = TaskCallInspector(tmp_path)
    row = inspector.upsert_task_call(dict(task_id="goal", goal="password=" + secret))
    assert secret not in row["goal"]
    assert secret not in (tmp_path / "data/telemetry/task_calls/goal.json").read_text()


# LLM: 无显式任务写入时也要从工具账本发现任务，并保留树身份与资产去重。
# 函数用途: 验证只读自动合成、过滤、时间、状态与统计。
def test_auto_task_from_ledger(tmp_path):
    ledger = ToolTelemetryLedger(tmp_path, owner_id="one")
    for i, tool in enumerate(("scripts", "workflows", "skill_search", "schedule")):
        ledger.record_tool_call(dict(call_id=str(i), tool_name=tool, task_id="task_auto_1", run_id="child",
                                     parent_run_id="parent", root_run_id="root", started_at=10+i, ended_at=11+i,
                                     status="error" if i == 3 else "ok", args_summary=dict(script_id="s", workflow_id="w", skill_id="k", job_id="j")))
    inspector = TaskCallInspector(tmp_path, owner_id="one")
    rows = inspector.list_task_calls(status="failed", since=10, until=10)
    assert len(rows) == 1
    detail = inspector.get_task_call_detail("task_auto_1")
    assert (detail["owner_id"], detail["started_at"], detail["ended_at"], detail["duration_ms"]) == ("one", 10, 14, 4000)
    assert detail["root_run_id"] == "root"
    assert len(detail["runs"]) == 1
    assert detail["runs"][0]["parent_run_id"] == "parent"
    assert len(detail["tool_calls"]) == detail["tool_summary"]["total_calls"] == 4
    assert detail["tool_summary"]["tools"]["schedule"]["failed"] == 1
    assert detail["mounted_assets"] == dict(script_ids=["s"], workflow_ids=["w"], skill_ids=["k"], monitor_ids=["j"])
    assert not (tmp_path / "data/telemetry/task_calls").exists()
    assert inspector.get_task_call_detail("missing") is None


# LLM: 明确记录的终态、模型和轮次优先；账本只补新节点、树关系和资产。
# 函数用途: 验证合并去重、只读与 owner 隔离。
def test_persisted_task_merges_live_runs_and_assets(tmp_path):
    inspector = TaskCallInspector(tmp_path, owner_id="one")
    inspector.upsert_task_call(dict(task_id="t1", status="paused", runs=[dict(run_id="root", model_name="model")], mounted_assets={"script_ids": ["old"]}))
    ledger = ToolTelemetryLedger(tmp_path, owner_id="one")
    for i, run in enumerate(("root", "child", "child")):
        ledger.record_tool_call(dict(call_id=str(i), tool_name="scripts", task_id="t1", run_id=run,
                                     root_run_id="root", parent_run_id="root" if run == "child" else "", args_summary={"script_id": "new"}))
    ToolTelemetryLedger(tmp_path, owner_id="other").record_tool_call(dict(call_id="foreign", tool_name="read", task_id="foreign"))
    rows = inspector.list_task_calls()
    assert len(rows) == 1
    assert {r["run_id"] for r in rows[0]["runs"]} == {"root", "child"}
    detail = inspector.get_task_call_detail("t1")
    assert detail["status"] == "paused"
    assert detail["runs"][0]["model_name"] == "model"
    assert detail["mounted_assets"]["script_ids"] == ["old", "new"]
    assert len(detail["runs"]) == 2


# LLM: 故障注入只替换遥测边界，证据仓库和脏标记使用真实实现。
# 函数用途: 验证任意遥测异常不会中断证据登记或写后过期。
@pytest.mark.parametrize("error", [OSError, KeyError, Exception])
def test_telemetry_failure_does_not_break_verification(tmp_path, monkeypatch, error):
    from test_verification_runtime import _agent, _call, _success

    from agent_py_agent.agent.verification import runtime
    from agent_py_agent.agent.verification.repository import (
        VerificationContext,
        VerificationEvidenceRepository,
    )
    agent, project, home = _agent(tmp_path)

    # LLM: 注入不同异常类别，不替换主链的任何行为。
    # 函数用途: 模拟遥测写入失败。
    def fail(*args):
        raise error("telemetry unavailable")

    monkeypatch.setattr(runtime, "_record_tool_telemetry", fail)
    test_call = _call("run_command", {"command": "pytest -q", "working_dir": str(project)})
    result = runtime.record_tool_verification(agent, test_call, _success(test_call, "1 passed", {"process": {"status": "exited", "return_code": 0}}))
    assert result.metadata.get("handler_details", {}).get("verification_evidence", {}).get("status") == "passed"
    write_call = _call("edit_file", {"path": str(project / "app.py")})
    result = runtime.record_tool_verification(agent, write_call, _success(write_call, "edited", {}))
    assert result.metadata["handler_details"]["verification_state"][0]["status"] == "stale"
    assert VerificationEvidenceRepository(home).status(VerificationContext("providers/feishu/users/owner-a", "thread-1", "root-task"), root=project)["status"] == "stale"


# LLM: 公共入口必须在附加验证元数据后仍记遥测，并保留当前代理作用域身份。
# 函数用途: 验证真实公共出口生成可自动发现的任务树。
def test_runtime_telemetry_tree_identity(tmp_path):
    from types import SimpleNamespace

    from test_verification_runtime import _agent, _call, _success

    from agent_py_agent.agent.verification.runtime import record_tool_verification
    agent, project, home = _agent(tmp_path)
    home.mkdir()
    agent._current_run_scope = SimpleNamespace(run_id="child-run", parent_run_id="parent", root_run_id="root")
    tool_call = _call("run_command", {"command": "pytest -q", "working_dir": str(project)})
    result = record_tool_verification(agent, tool_call, _success(tool_call, "1 passed", {"process": {"status": "exited", "return_code": 0}}))
    assert "verification_evidence" in result.metadata["handler_details"]
    row = ToolTelemetryLedger(home).get_tool_call("call-1")
    assert (row["parent_run_id"], row["root_run_id"]) == ("parent", "root")
    detail = TaskCallInspector(home, owner_id="providers/feishu/users/owner-a").get_task_call_detail("root-task")
    assert detail is not None and detail["tool_summary"]["total_calls"] == 1


# LLM: 在临时目录写合法批量夹具，跨过真实账本分页上限；不使用假查询接口。
# 函数用途: 验证超过一万条后新增任务与末页调用不丢失。
def test_auto_tasks_beyond_ledger_page(tmp_path):
    import json

    ledger = ToolTelemetryLedger(tmp_path, owner_id="one")
    sample = ledger.record_tool_call(call()).to_dict()
    path = tmp_path / "data/telemetry/tool_calls.jsonl"
    with path.open("w") as stream:
        for i in range(10001):
            stream.write(json.dumps({**sample, "call_id": str(i), "task_id": "last" if i == 10000 else "t1"}) + "\n")
    inspector = TaskCallInspector(tmp_path, owner_id="one")
    assert {r["task_id"] for r in inspector.list_task_calls()} == {"t1", "last"}
    assert inspector.get_task_call_detail("last")["tool_summary"]["total_calls"] == 1
    assert len(inspector.get_task_call_detail("t1")["tool_calls"]) == 10000


# LLM: 从公共工具出口写账再通过 Inspector 读取，断言资产与字典作用域的最终投影。
# 函数用途: 验证四类资产工具的真实接线。
@pytest.mark.parametrize("tool,key,plural", [("scripts", "script_id", "script_ids"), ("workflows", "workflow_id", "workflow_ids"), ("skill_search", "skill_id", "skill_ids"), ("schedule", "job_id", "monitor_ids")])
def test_runtime_asset_references(tmp_path, tool, key, plural):
    from test_verification_runtime import _agent, _call, _success

    from agent_py_agent.agent.verification.runtime import record_tool_verification
    agent, _, home = _agent(tmp_path)
    home.mkdir()
    agent._current_run_scope = {"root_run_id": "root", "parent_run_id": "parent"}
    tool_call = _call(tool, {key: "asset"})
    record_tool_verification(agent, tool_call, _success(tool_call, "ok", {}))
    detail = TaskCallInspector(home, owner_id="providers/feishu/users/owner-a").get_task_call_detail("root-task")
    assert detail["mounted_assets"][plural] == ["asset"]
    assert detail["runs"][0]["parent_run_id"] == "parent"


# LLM: 账本身份不是文件路径；详情查询不得读取同名越界文件。
# 函数用途: 验证非文件名任务身份仍可从账本自动发现。
@pytest.mark.parametrize("task_id", ["task/a", "任务甲", "task-path:123", "../outside"])
def test_auto_task_opaque_identity(tmp_path, task_id):
    ledger = ToolTelemetryLedger(tmp_path)
    ledger.record_tool_call(dict(call_id="opaque", tool_name="read", task_id=task_id))
    inspector = TaskCallInspector(tmp_path)
    assert inspector.list_task_calls()[0]["task_id"] == task_id
    try:
        detail = inspector.get_task_call_detail(task_id)
    except ValueError:
        detail = None
    assert detail is not None
    assert detail["tool_summary"]["total_calls"] == 1
    assert not (tmp_path / "data/telemetry/task_calls").exists()


# LLM: 明确写入的状态继续优先，空时间和工具推导字段须用当前账本补齐。
# 函数用途: 验证旧观测及空占位不会压住新的调用事实。
@pytest.mark.parametrize("node", [dict(run_id="r", ended_at=0, duration_ms=0, status="paused"), dict(run_id="r", ended_at=1, duration_ms=1, status="succeeded", status_source="tool_calls")])
def test_live_run_refreshes_observed_and_empty_fields(tmp_path, node):
    inspector = TaskCallInspector(tmp_path)
    inspector.upsert_task_call(dict(task_id="t1", runs=[node]))
    ToolTelemetryLedger(tmp_path).record_tool_call(dict(call_id="run", tool_name="read", task_id="t1", run_id="r", started_at=10, ended_at=12, status="error"))
    run = inspector.get_task_call_detail("t1")["runs"][0]
    assert (run["ended_at"], run["duration_ms"]) == (12, 2000)
    assert run["status"] == ("failed" if node.get("status_source") == "tool_calls" else "paused")


# LLM: 一个节点的父根链必须来自与最终 run_id 一致的宿主作用域，不静默跨身份拼接。
# 函数用途: 验证身份来源冲突时选用匹配的完整父链。
def test_runtime_identity_does_not_mix_different_runs():
    from types import SimpleNamespace

    from agent_py_agent.agent.verification.runtime import _telemetry_identity
    context = SimpleNamespace(run_id="other", parent_run_id="wrong", root_run_id="wrong")
    scope = SimpleNamespace(run_id="child", parent_run_id="parent", root_run_id="root")
    identity = _telemetry_identity(SimpleNamespace(_current_run_scope=scope), context, SimpleNamespace(run_id="child"))
    assert identity == dict(run_id="child", parent_run_id="parent", root_run_id="root")


# LLM: 使用真实调用账本和受控时钟，只替换模型响应；上下文快照不含真实提示词或网络调用。
# 函数用途: 构造可从开始驱动到终态的临时Owner请求。
def _model_request(home):
    clock = [100.0]
    agent = SimpleNamespace(home_paths=SimpleNamespace(home_dir=home, owner_id="one"),
                            backend=SimpleNamespace(name="test-backend", model_name="test-model", max_tokens=128),
                            config=SimpleNamespace(), _model_call_ledger=ModelCallLedger(context=ModelCallLedgerContext(now=lambda: clock[0])))
    params = SimpleNamespace(task_id="model-task", run_id="child", request_id="request", context_scope="isolated",
                             task_attributes={"conversation_thread_id": "thread"})
    return SimpleNamespace(agent=agent, params=params, prompt="synthetic-prompt-not-persisted", tool_rounds=0), clock


# LLM: 三种真实终态入口共用同一合成响应，不绕过ledger的首终态规则。
# 函数用途: 结束一次请求以验证自动遥测与重放。
def _end_model_call(ledger, call_id, status):
    if status == "finished":
        call_runtime.record_model_call_finished(ledger, call_id, SimpleNamespace(text="synthetic-response-not-persisted",
            usage=dict(prompt_tokens=10, completion_tokens=3, prompt_tokens_details=dict(cached_tokens=4)), stop_reason="end_turn"))
    elif status == "failed":
        call_runtime.record_model_call_failed(ledger, call_id, RuntimeError("synthetic-error-not-persisted"))
    else:
        call_runtime.record_model_call_timeout(ledger, call_runtime.ModelCallTimeoutFacts(call_id, 2, "wall_clock"))


# LLM: 公开开始与三种终态都必须实际写入Inspector，身份只取同run的完整来源，重复终态不多记轮次。
# 函数用途: 验证模型轮次、四种身份来源、累计Token、耗时和隐私边界。
@pytest.mark.parametrize("status", ["finished", "failed", "timed_out"])
@pytest.mark.parametrize("source", ["params", "attrs", "scope", "current", "split-parent", "split-root"])
def test_model_rounds_auto_lifecycle(tmp_path, status, source):
    request, clock = _model_request(tmp_path)
    identity = dict(run_id="child", parent_run_id="parent", root_run_id="root")
    target = {"params": request.params, "attrs": request.params.task_attributes,
              "scope": SimpleNamespace(), "current": SimpleNamespace()}.get(source, request.params)
    if isinstance(target, dict):
        target.update(identity)
    else:
        vars(target).update(identity)
    if source.startswith("split-"):
        setattr(request.params, source.removeprefix("split-") + "_run_id", "")
        request.params.task_attributes.update(identity)
    request.agent._current_run_scope = target if source == "scope" else SimpleNamespace(run_id="foreign", root_run_id="wrong")
    request.agent._current_run_params = target if source == "current" else None
    ledger, call_id, _ = call_runtime.start_model_call_record(request, context_snapshot=SimpleNamespace(current_tokens=17, to_public_dict=lambda: {}))
    metadata = dict(ledger.records()[0].metadata)
    clock[0] += 2
    _end_model_call(ledger, call_id, status)
    _end_model_call(ledger, call_id, "finished")
    detail = inspector_module.TaskCallInspector(tmp_path, owner_id="one").get_task_call_detail("model-task")
    assert detail is not None, "模型调用没有自动写入任务轮次"
    assert detail["root_run_id"] == "root" and detail["thread_id"] == "thread"
    assert len(detail["rounds"]) == len(detail["runs"]) == 1
    row = detail["rounds"][0]
    assert (row["round_id"], row["run_id"], row["parent_run_id"], row["status"]) == (call_id, "child", "parent", status)
    assert (row["model"], row["backend"], row["purpose"], row["duration_ms"]) == ("test-model", "test-backend", "main", 2000)
    assert detail["token_usage"] == (dict(prompt_tokens=10, completion_tokens=3, cached_tokens=4, total_tokens=13) if status == "finished" else dict(prompt_tokens=17, completion_tokens=0, cached_tokens=0, total_tokens=17))
    assert detail["runs"][0]["parent_run_id"] == "parent" and detail["runs"][0]["root_run_id"] == "root"
    assert ledger.records()[0].metadata == metadata and getattr(ledger, "_telemetry_call_scopes", {}) == {}
    assert "synthetic-" not in (tmp_path / "data/telemetry/task_calls/model-task.json").read_text()


# LLM: 注入遥测入口或写入内部故障，真实调用账本始终保持既有返回与终态。
# 函数用途: 验证可选遥测失败不会破坏模型主链。
@pytest.mark.parametrize("point", ["bind_model_call_telemetry", "record_model_call_telemetry", "append_round"])
@pytest.mark.parametrize("status", ["finished", "failed", "timed_out"])
def test_model_rounds_failure_is_optional(tmp_path, monkeypatch, point, status):
    request, _ = _model_request(tmp_path)
    # LLM: 只替换遥测边界，账本开始及终态仍执行真实实现。
    # 函数用途: 注入可选遥测故障。
    def fail(*args, **kwargs):
        raise OSError("synthetic telemetry failure")
    target = inspector_module.TaskCallInspector if point == "append_round" else call_runtime
    assert callable(getattr(target, point, None)), "模型遥测接口尚未接入"
    monkeypatch.setattr(target, point, fail)
    ledger, call_id, estimate = call_runtime.start_model_call_record(request, context_snapshot=SimpleNamespace(current_tokens=17, to_public_dict=lambda: {}))
    _end_model_call(ledger, call_id, status)
    assert estimate is not None and ledger.records()[0].status == status


# LLM: 别名只在规范字段缺失时使用，已有零值和四字段返回合同不能改变。
# 函数用途: 验证Token归一及仅显式允许的任务自动创建。
def test_model_rounds_aliases_and_atomic_create(tmp_path):
    inspector = inspector_module.TaskCallInspector(tmp_path, owner_id="one")
    assert inspector_module._usage(dict(input_tokens=10, output_tokens=3, cached_input_tokens=4)) == dict(prompt_tokens=10, completion_tokens=3, cached_tokens=4, total_tokens=13)
    assert inspector_module._usage(dict(prompt_tokens=0, input_tokens=10))["prompt_tokens"] == 0
    with pytest.raises(KeyError):
        inspector.append_round("new", {})
    inspector.append_round("new", dict(run_id="child", parent_run_id="parent", root_run_id="root", model="m", started_at=10, prompt_tokens=1), create_if_missing=True)
    inspector.append_round("new", dict(run_id="child", prompt_tokens=2), create_if_missing=True)
    detail = inspector.get_task_call_detail("new")
    assert len(detail["runs"]) == 1 and detail["token_usage"]["prompt_tokens"] == 3
    assert detail["runs"][0]["parent_run_id"] == "parent"


# LLM: 同home其他owner合法，owner筛选先于最后身份选择，统计不物化lifetime样本。
# 函数用途: 验证索引接线、重复身份隔离及精确工具分位。
def test_indexed_telemetry_owner_last_wins_and_exact_stats(tmp_path, monkeypatch):
    from agent_py_agent.agent.observability import telemetry_ledger as module

    one = ToolTelemetryLedger(tmp_path, owner_id='one')
    for index, duration in enumerate((10, 20, 30, 40)):
        one.record_tool_call(call(index, duration_ms=duration,
            status='ok' if index == 0 else 'error', error_code='' if index == 0 else 'E'))
    ToolTelemetryLedger(tmp_path, owner_id='other').record_tool_call(call(3, duration_ms=999))

    # LLM: 原始扫描和Python样本聚合被禁止，查询仍执行真实索引。
    # 函数用途: 暴露漏接线入口。
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy JSONL scan or lifetime aggregation')

    monkeypatch.setattr(module, '_entries', forbidden)
    monkeypatch.setattr(module, '_stats', forbidden)
    assert one.get_tool_call('c3')['duration_ms'] == 40
    assert len(one.query_tool_calls()) == 4
    stats = one.aggregate_tool_stats()['tools']['read']
    assert (stats['calls'], stats['succeeded'], stats['failed']) == (4, 1, 3)
    assert stats['success_rate'] == .25
    assert [stats[k] for k in ('avg_duration_ms', 'p50_duration_ms', 'p95_duration_ms')] == [25, 20, 40]
    assert stats['error_codes'] == {'E': 3}


# LLM: 固定through不因追加扩展，limit和无匹配过滤不能掩盖坏尾行。
# 函数用途: 验证固定分页、warm解码复用及损坏后的结构化未知。
def test_indexed_telemetry_fixed_view_and_corrupt_suffix(tmp_path, monkeypatch):
    ledger = ToolTelemetryLedger(tmp_path)
    assert callable(getattr(ledger, 'query_tool_calls_page', None))
    for index in range(20):
        ledger.record_tool_call(call(index))
    first = ledger.query_tool_calls_page(limit=5)
    assert first['coverage']['status'] == 'complete'
    decoded, original = [], ledger._read_index._decode

    # LLM: 不替换来源检查或SQL，仅旁听真实解码。
    # 函数用途: 计量warm路径的旧行解码。
    def observe(*args, **kwargs):
        decoded.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger._read_index, '_decode', observe)
    assert len(ledger.query_tool_calls_page(limit=5, cursor=5, view=first['view'])['rows']) == 5
    assert ledger.get_tool_call('c19')['call_id'] == 'c19'
    assert ledger.aggregate_tool_stats()['total_calls'] == 20
    assert not decoded
    ledger.record_tool_call(call(20))
    second = ledger.query_tool_calls_page(limit=100, cursor=19, view=first['view'])
    assert [row['call_id'] for row in second['rows']] == ['c19']
    assert second['total_count'] == 20 and len(ledger.query_tool_calls()) == 21
    with (tmp_path / 'data/telemetry/tool_calls.jsonl').open('a') as stream:
        stream.write('broken\n')
    report = ledger.query_tool_calls_page(limit=1, task_id='no-match')
    assert report['coverage']['status'] == 'unavailable' and report['total_count'] is None
    with pytest.raises(ValueError) as caught:
        ledger.get_tool_call('missing')
    assert caught.value.coverage['status'] == 'unavailable'


# LLM: 真首次缺源有效零，实际预算部分推进未知，不得为读取修复规范来源。
# 函数用途: 区分空读和不完整投影。
def test_indexed_telemetry_absence_and_partial_are_distinct(tmp_path):
    ledger = ToolTelemetryLedger(tmp_path)
    assert callable(getattr(ledger, 'query_tool_calls_page', None))
    empty = ledger.query_tool_calls_page()
    assert empty['coverage']['status'] == 'complete' and empty['total_count'] == 0
    assert not (tmp_path / 'cache').exists() and not (tmp_path / 'data').exists()
    ledger.record_tool_call(call(0))
    ledger.record_tool_call(call(1))
    view = ledger._read_index.advance('telemetry', ('data', 'telemetry', 'tool_calls.jsonl'), max_rows=1)
    report = ledger.query_tool_calls_page(view=view)
    assert report['coverage']['status'] == 'partial' and report['total_count'] is None
