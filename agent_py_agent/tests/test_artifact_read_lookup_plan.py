# LLM: c1真实查找与预算入口回归；计数只包装真实函数，不替代权限或正文读取。
# 模块用途: 锁定一次查找、空路径与父目录去重、prepared身份隔离及预算结算。
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.memory_archive.artifact import reader
from agent_py_agent.agent.tooling import artifact as tooling
from agent_py_agent.tests.test_memory_artifact_read import _write_externalized_tool_output
from agent_py_agent.tests.test_tool_ref_scope_stream import write_index


# LLM: prepare是仅宿主Python接口，缺失时在明确断言上失败，不用ImportError冒充红灯。
# 函数用途: 获取准备入口，供新合同测试复用。
def _prepare(request):
    prepare = getattr(reader, "prepare_tool_output_artifact_read", None)
    assert callable(prepare), "缺少请求局部prepare入口"
    return prepare(request)


# LLM: 三类预算分支共走真实工具和正文校验；没有run时不可为无效预算预扫描。
# 函数用途: 验证全读在预算开关及缺run下都只查找一次、枚举一次。
@pytest.mark.parametrize("budget,run_id", [(0, "run-artifact"), (100_000, "run-artifact"), (100_000, "")])
def test_full_read_uses_one_lookup_and_discovery(tmp_path, budget, run_id):
    _write_externalized_tool_output(tmp_path, content="abcdef" * 100)
    tool = tooling.ReadArtifactTool(tmp_path, artifact_read_budget_max_chars=budget)
    with patch.object(reader, "_find_index_record", wraps=reader._find_index_record) as lookup, patch.object(
        reader, "tool_output_index_paths_for_lookup", wraps=reader.tool_output_index_paths_for_lookup
    ) as discover, patch.object(tooling, "estimate_tool_output_artifact_size", wraps=tooling.estimate_tool_output_artifact_size) as estimate:
        result = tool.execute({"artifact_ref": "run-artifact:call-artifact", "run_id": run_id, "max_chars": 0})
    assert result.ok, result.output
    assert json.loads(result.output)["content"] == "abcdef" * 100
    assert lookup.call_count == discover.call_count == 1
    assert estimate.call_count == int(budget > 0 and bool(run_id))
    assert tool.read_budget._pending == {}


# LLM: 有限读取必须先预留，失败不能触发索引查找；prepared不进入公开Schema。
# 函数用途: 验证有限读预算拒绝保持零查找、零正文与零pending。
def test_finite_budget_denial_does_not_scan(tmp_path):
    tool = tooling.ReadArtifactTool(tmp_path, artifact_read_budget_max_chars=2)
    with patch.object(reader, "_find_index_record", side_effect=AssertionError("不应查索引")):
        result = tool.execute({"artifact_ref": "x", "run_id": "r", "max_chars": 3})
    assert not result.ok and result.error_code == "QUOTA_EXCEEDED"
    assert tool.read_budget._pending == {}
    assert "prepared" not in tool.model_spec.input_schema["properties"]


# LLM: 成功/失败/异常均须释放reservation；故障只替代最外层不可用正文入口。
# 函数用途: 验证读取抛错时finally结算，不把失败计入已读额度。
def test_reader_exception_releases_pending_reservation(tmp_path):
    tool = tooling.ReadArtifactTool(tmp_path, artifact_read_budget_max_chars=100)
    with patch.object(tooling, "read_tool_output_artifact", side_effect=RuntimeError("read failed")), pytest.raises(RuntimeError, match="read failed"):
        tool.execute({"artifact_ref": "x", "run_id": "r", "max_chars": 20})
    assert tool.read_budget._pending == tool.read_budget._events_by_run == {}


# LLM: 同一prepared只复用元数据，后续正文仍读盘和重算hash；offset/模式不改变身份。
# 函数用途: 验证估算和读取复用同一冻结计划且记录防御性复制。
def test_prepared_is_frozen_detached_and_reused_for_estimate_and_read(tmp_path):
    path = _write_externalized_tool_output(tmp_path, content="中文正文abcdef" * 10)
    request = reader.ReadToolOutputArtifactRequest(tmp_path, str(path))
    original = reader._find_index_record(tmp_path, str(path), request)
    with patch.object(reader, "_find_index_record", return_value=original) as lookup:
        plan = _prepare(request)
        original["path"] = ""
        original["size_bytes"] = 1
        assert reader.estimate_tool_output_artifact_size(request, prepared=plan) == len(("中文正文abcdef" * 10).encode())
        result = reader.read_tool_output_artifact(replace(request, max_chars=4), prepared=plan)
    assert lookup.call_count == 1 and result["content"] == "中文正文"
    assert plan.record is not original
    with pytest.raises(FrozenInstanceError):
        plan.artifact_ref = "other"
    assert plan.index_paths == (path.parent / "index.jsonl",)


# LLM: root/ref与全部宿主scope不同时不能借旧命中；重新查找后仍执行current_run硬门。
# 函数用途: 验证准备计划不能跨身份或owner复用。
@pytest.mark.parametrize("field,value", [("artifact_ref", "missing"), ("run_id", "other"), ("task_id", "other"), ("request_id", "other"), ("scope_mode", "current_run"), ("root", "other-owner")])
@pytest.mark.parametrize("entry", ["read", "estimate"])
def test_prepared_mismatch_is_reprepared(tmp_path, field, value, entry):
    _write_externalized_tool_output(tmp_path, content="plan-content")
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "run-artifact:call-artifact")
    plan = _prepare(request)
    changed = replace(request, **{field: tmp_path / value if field == "root" else value})
    call = reader.read_tool_output_artifact if entry == "read" else reader.estimate_tool_output_artifact_size
    with patch.object(reader, "_find_index_record", wraps=reader._find_index_record) as lookup:
        call(changed, prepared=plan)
    assert lookup.call_count == 1


# LLM: 每行匹配一次，异常不提前吞掉；对重复basename不去重，以保留旧拒绝语义。
# 函数用途: 验证流式候选保留原命中结果但不二次解析候选路径。
def test_candidate_match_is_computed_only_once(tmp_path):
    rows = [{"call_id": "hit", "path": ""}, {"call_id": "other", "path": ""}]
    write_index(tmp_path, b"\n".join(json.dumps(row).encode() for row in rows))
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "hit")
    with patch.object(reader, "_record_matches_ref", wraps=reader._record_matches_ref) as match:
        assert reader._find_index_record(tmp_path, "hit", request) == rows[0]
    assert match.call_count == len(rows)


# LLM: 空path和规范父目录缓存只属于本次查询，不能存到全局或缓存授权结果。
# 函数用途: 用确定性系统调用计数验证空path与普通叶路径去重，并复查下一查询重新解析。
def test_empty_paths_and_clean_parents_are_resolved_once_per_lookup(tmp_path):
    directory = tmp_path / "normal"
    directory.mkdir()
    rows = [{"call_id": "noise", "path": ""}] * 60
    rows += [{"call_id": "noise", "path": str(directory / f"noise-{n}.json")} for n in range(40)]
    rows.append({"call_id": "hit", "path": ""})
    write_index(tmp_path, b"\n".join(json.dumps(row).encode() for row in rows))
    original = Path.resolve
    empty = []
    # LLM: 包装真实resolver只计空路径调用，不替换路径计算和错误行为。
    # 函数用途: 记录本查询的空路径解析次数。
    def resolve(path, *args, **kwargs):
        if str(path) == ".":
            empty.append(path)
        return original(path, *args, **kwargs)
    with patch.object(Path, "resolve", resolve), patch.object(reader.os.path, "realpath", wraps=reader.os.path.realpath) as realpath:
        for _ in range(2):
            assert reader._find_index_record(tmp_path, "hit", reader.ReadToolOutputArtifactRequest(tmp_path, "hit")) == rows[-1]
    assert len(empty) <= 2
    assert sum(str(c.args[0]) == str(directory) for c in realpath.call_args_list) == 2
    assert not any("noise-" in str(c.args[0]) for c in realpath.call_args_list)
