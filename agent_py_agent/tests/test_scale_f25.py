"""W10: 写工具反馈只能访问当前事实或有界locator。"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from agent_py_agent.agent.memory_archive.runtime_fact_source import (
    RuntimeFactSourceRequest,
    write_runtime_fact_source,
)
from agent_py_agent.agent.run_intent import latest_run_intent_report, reference_write_feedback
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions


# LLM: 通过原事实writer生成最新意图，旧目录仅为历史规模输入。
# 函数用途: 建立真实参考目录、目标路径和原始运行事实。
def _facts(root, count):
    reference = root / "reference"
    reference.mkdir(parents=True)
    facts = root / "memory_archive/runtime_facts"
    facts.mkdir(parents=True)
    for i in range(count):
        child = facts / f"old-{i}"
        child.mkdir()
        (child / "task.json").write_text("{}", encoding="utf-8")
    write_runtime_fact_source(RuntimeFactSourceRequest(root=root, request_id="request-current", run_id="run-current",
        user_prompt=f"参考 {reference} 制作结果", delivery_contract={"artifacts":[{"path":"output/final.txt"}]}))
    return reference


# LLM: 与executor._handler_run_scope同形；turn_id是单次模型调用编号，不等于宿主请求ID或事实目录名。
# 函数用途: 给直接execute提供真实形状的宿主内部身份。
def _run_scope(run_id="run-current"):
    return {"owner_id": "", "owner_home": "", "run_id": run_id, "task_id": "", "attempt_id": "attempt-current",
            "turn_id": f"{run_id}:model:1:after-tools:0:{uuid4().hex}"}


# LLM: 透明包裹scandir和原read_text，计量包含真实写工具的所有目录项和文件读取。
# 函数用途: 证明历史增长不进入反馈的目录/文件访问预算，有无宿主身份都一样有界。
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("count", [1000, 10000])
def test_real_write_feedback_directory_and_file_reads_are_bounded(tmp_path, monkeypatch, count, scoped):
    reference = _facts(tmp_path, count)
    entries, reads = [], []
    original_scan, original_read = os.scandir, Path.read_text

    class Scan:
        def __init__(self, *args):
            self.iterator = original_scan(*args)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def __iter__(self):
            return self
        def __next__(self):
            value = next(self.iterator)
            entries.append(value.name)
            return value
        def close(self):
            self.iterator.close()

    def read(path, *args, **kwargs):
        reads.append(str(path))
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", Scan)
    monkeypatch.setattr(Path, "read_text", read)
    before = len(os.listdir("/dev/fd"))
    tool = WriteFileTool(tmp_path)
    params = {"path": str(reference / "note.txt"), "content": "本轮内容"}
    if scoped:
        params["__run_scope"] = _run_scope()
    result = tool.execute(params)
    assert result.ok
    feedback = result.result_envelope["soft_feedback"]
    assert feedback["blocking"] is False and feedback["desired_outputs"] == ["output/final.txt"]
    assert feedback["reference_roots"] == [str(reference)]
    assert (reference / "note.txt").read_text(encoding="utf-8") == "本轮内容"
    assert len(os.listdir("/dev/fd")) - before == 0
    print("intent_cost", {"history":count, "directory_entries":len(entries), "files":len(reads)})
    assert len(entries) <= 16 and len(reads) <= 16


# LLM: 无locator的热miss不能同步遍历历史；既无事实也不能虚构输出位置。
# 函数用途: 披露unknown并守住无全量回落边界。
def test_missing_locator_is_explicit_unknown_without_enumeration(tmp_path, monkeypatch):
    facts = tmp_path / "memory_archive/runtime_facts"
    facts.mkdir(parents=True)
    (facts / "old").mkdir()
    (facts / "old/task.json").write_text("{}", encoding="utf-8")
    calls = []
    original = os.scandir
    def scan(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(os, "scandir", scan)
    feedback = reference_write_feedback(workspace_root=tmp_path, target=tmp_path / "result.txt")
    assert feedback.get("run_intent_load_errors"), "缺失locator必须是明确unknown，不是无意图"
    # 有宿主身份时缺失locator仍由原locator读口披露unknown，不被当作"本轮没有意图"。
    scoped = reference_write_feedback(workspace_root=tmp_path, target=tmp_path / "result.txt", run_id="run-current")
    assert [error["error_code"] for error in scoped["run_intent_load_errors"]] == ["RUN_INTENT_LOCATOR_MISSING"]
    assert not calls


# LLM: 通过工具的宿主内部字段传准确身份，不给模型新增输入字段；另一个最新run不能污染当前反馈。
# 函数用途: 验证当前run优先并保持owner/task-local范围。
def test_current_run_identity_wins_over_newer_owner_intent(tmp_path):
    reference = _facts(tmp_path, 0)
    other_reference = tmp_path / "other-reference"
    other_reference.mkdir()
    write_runtime_fact_source(RuntimeFactSourceRequest(root=tmp_path, request_id="request-other", run_id="run-other",
        user_prompt=f"参考 {other_reference} 制作结果", delivery_contract={"artifacts":[{"path":"output/other.txt"}]}))
    tool = WriteFileTool(tmp_path, options=WriteFileToolOptions(runtime_fact_roots=[tmp_path]))
    scope = _run_scope()
    assert scope["turn_id"] != "request-current"
    result = tool.execute({"path":str(reference / "note.txt"), "content":"当前内容", "__run_scope":scope})
    assert result.ok and result.result_envelope["soft_feedback"]["desired_outputs"] == ["output/final.txt"]
    assert "run_intent_load_errors" not in result.result_envelope["soft_feedback"]
    assert "__run_scope" in tool.runtime_policy.input_policy.internal_parameters
    assert "__run_scope" not in tool.model_spec.input_schema["properties"]


# LLM: 用Event卡在事实已发布但locator未发布的窗口；读者只能得到一份完整旧或新意图。
# 函数用途: 验证原子事实写和locator更新交错不混合字段。
def test_fact_publication_racing_feedback_is_old_or_new(tmp_path, monkeypatch):
    _facts(tmp_path, 0)
    from agent_py_agent.agent.memory_archive import runtime_fact_source as module
    original = module._write_json_atomic
    ready, release = threading.Event(), threading.Event()
    failures = []
    def write(path, payload):
        original(path, payload)
        if path.name == "task.json":
            ready.set()
            assert release.wait(5)
    monkeypatch.setattr(module, "_write_json_atomic", write)
    old = latest_run_intent_report(tmp_path)[0]
    def update():
        try:
            write_runtime_fact_source(RuntimeFactSourceRequest(root=tmp_path, request_id="request-next", run_id="run-next",
                user_prompt=f"参考 {tmp_path / 'reference'}", delivery_contract={"artifacts":[{"path":"output/next.txt"}]}))
        except Exception as exc:
            failures.append(exc)
    thread = threading.Thread(target=update)
    thread.start()
    assert ready.wait(5)
    during = latest_run_intent_report(tmp_path)[0]
    release.set()
    thread.join(5)
    new = latest_run_intent_report(tmp_path)[0]
    assert not failures and not thread.is_alive() and old != new
    assert during in (old, new)


# LLM: 原executor执行真实write_file，只有宿主ToolCall携带run/turn；模型参数不含内部身份。
# 函数用途: 防止只测直接execute而漏掉运行策略到当前请求反馈的真实接线。
def test_executor_injects_current_identity_before_real_write(tmp_path):
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
    from agent_py_agent.agent.tooling.models import ToolRuntime, ToolRuntimeSnapshot
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall

    reference = _facts(tmp_path, 0)
    other = tmp_path / "other-reference"
    other.mkdir()
    write_runtime_fact_source(RuntimeFactSourceRequest(root=tmp_path, request_id="request-other", run_id="run-other",
        user_prompt=f"参考 {other}", delivery_contract={"artifacts":[{"path":"output/other.txt"}]}))
    tool = WriteFileTool(tmp_path)
    runtime = ToolRuntime(tool.model_spec, tool.runtime_policy, tool)
    snapshot = ToolRuntimeSnapshot(run_id="run-current", runtimes=(runtime,),
        available_tool_names=frozenset({"write_file"}), unavailable_tools=(), allowed_tools=None)
    # 宿主请求ID(事实目录名request-current)与单次模型调用turn_id不同，按真实generation格式构造。
    call = ToolCall(call_id="write-current", tool_name="write_file",
        arguments={"path":str(reference / "result.txt"), "content":"本轮写入"}, source_protocol="native",
        schema_hash=tool.model_spec.schema_hash, run_id="run-current",
        turn_id=f"run-current:model:1:after-tools:0:{uuid4().hex}", attempt_id="attempt-current")
    execution = ToolExecutor().execute(ToolExecutorRequest(call=call, runtime_snapshot=snapshot,
        workspace_root=tmp_path, workspace_roots=(tmp_path,), approval_mode="auto", operation_store_required=False))
    assert execution.result.ok and execution.result.handler_executed, execution.result
    assert (reference / "result.txt").read_text(encoding="utf-8") == "本轮写入"
    assert "output/final.txt" in execution.result.output, execution.result.output
    assert "output/other.txt" not in execution.result.output
    assert "__run_scope" not in tool.model_spec.input_schema["properties"]


# LLM: 原事实根的直接子项仍只能是事实目录；定位页及其锁不能改变只读消费者的目录契约。
# 函数用途: 防止新增查询元数据让真实CLI归档读取把普通文件当成事实目录。
def test_locator_preserves_fact_only_root_layout(tmp_path):
    _facts(tmp_path, 0)
    facts = tmp_path / "memory_archive/runtime_facts"
    entries = list(facts.iterdir())
    assert {entry.name for entry in entries} == {"request-current"}
    assert all(entry.is_dir() and (entry / "task.json").is_file() for entry in entries)
    desired = latest_run_intent_report(tmp_path)[0]["desired_outputs"]
    assert desired == {"source_status": "recorded", "items": ["output/final.txt"]}


# LLM: 普通提示没有desired_outputs时，当前运行"未记录意图"不是账本读取失败；较新的另一运行意图也不能借用。
# 函数用途: 真实事实writer、真实写工具与executor同形身份下，写入输出与锚点一样不带反馈。
@pytest.mark.parametrize("other_run", [False, True])
def test_plain_prompt_without_desired_outputs_has_no_feedback(tmp_path, other_run):
    owner, workspace = tmp_path / "owner", tmp_path / "workspace"
    reference = workspace / "reference"
    reference.mkdir(parents=True)
    write_runtime_fact_source(RuntimeFactSourceRequest(root=owner, request_id="request-current", run_id="run-current",
        user_prompt=f"整理一下 {reference} 里的笔记"))
    if other_run:
        write_runtime_fact_source(RuntimeFactSourceRequest(root=owner, request_id="request-other", run_id="run-other",
            user_prompt=f"参考 {reference} 制作结果", delivery_contract={"artifacts":[{"path":"output/other.txt"}]}))
    tool = WriteFileTool(workspace, options=WriteFileToolOptions(runtime_fact_roots=[owner]))
    before = len(os.listdir("/dev/fd"))
    result = tool.execute({"path":str(reference / "note.txt"), "content":"普通写入", "__run_scope":_run_scope()})
    assert result.ok, result.output
    assert "soft_feedback" not in result.result_envelope, result.result_envelope.get("soft_feedback")
    assert "软提醒" not in result.output
    assert (reference / "note.txt").read_text(encoding="utf-8") == "普通写入"
    assert len(os.listdir("/dev/fd")) - before == 0
    assert reference_write_feedback(workspace_root=workspace, target=reference / "note.txt", fact_roots=[owner],
                                    run_id="run-current") == {}


# LLM: 当前运行记录了desired_outputs时，按身份得到的提示与锚点(无身份locator读口)对同一份事实完全一致。
# 函数用途: 守住身份修正后既有提示形状不变，写到目标产物本身仍然没有提示。
@pytest.mark.parametrize("target_kind", ["reference", "desired"])
def test_current_run_desired_output_hint_is_unchanged(tmp_path, target_kind):
    reference = _facts(tmp_path, 0)
    target = reference / "draft.txt" if target_kind == "reference" else tmp_path / "output" / "final.txt"
    anchor = reference_write_feedback(workspace_root=tmp_path, target=target)
    result = WriteFileTool(tmp_path).execute({"path":str(target), "content":"产物内容", "__run_scope":_run_scope()})
    assert result.ok, result.output
    if target_kind == "desired":
        assert anchor == {} and "soft_feedback" not in result.result_envelope
        return
    assert anchor == {"severity":"soft", "blocking":False, "message":(
        "软提醒：这个写入路径看起来位于本轮参考目录内，不是明确的目标产物路径。"
        "如果你正在写最终产物，请写到 desired_outputs：output/final.txt"),
        "desired_outputs":["output/final.txt"], "reference_roots":[str(reference)]}
    assert result.result_envelope["soft_feedback"] == anchor
    assert anchor["message"] in result.output


# LLM: 身份命中的事实文件本身损坏是真实读取失败，仍须披露unknown，不能被"本轮无意图"吞掉。
# 函数用途: 守住删除RUN_INTENT_NOT_FOUND后真实候选读取错误照常返回。
def test_current_run_unreadable_fact_is_still_reported(tmp_path):
    reference = _facts(tmp_path, 0)
    (tmp_path / "memory_archive/runtime_facts/request-current/task.json").write_text("{broken", encoding="utf-8")
    result = WriteFileTool(tmp_path).execute({"path":str(reference / "note.txt"), "content":"内容",
                                              "__run_scope":_run_scope()})
    assert result.ok, result.output
    errors = result.result_envelope["soft_feedback"]["run_intent_load_errors"]
    assert [Path(error["path"]).parent.name for error in errors] == ["request-current"]
