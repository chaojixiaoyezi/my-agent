# LLM: 仅用临时 legacy 文件与真实缓存验证五字段类型；坏字段不能借出可变引用或被字符串化，正常摘要仍零深拷贝。
# 模块用途: 覆盖轻摘要的冷读、热缓存和调用方修改隔离，不访问生产任务或启动服务。
from __future__ import annotations

import json
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.persistence import service as module

_TEXT_FIELDS = ("id", "root_id", "status")
_TIME_FIELDS = ("updated_at", "created_at")
_SUMMARY_FIELDS = (*_TEXT_FIELDS, *_TIME_FIELDS)
_BAD_TYPES = [
    pytest.param([], id="list"),
    pytest.param({}, id="dict"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(None, id="null"),
]


# LLM: 真实非托管 manager 只读测试目录；局部时钟只移出粗 mtime 保护期，不伪造 stat 或任务内容。
# 函数用途: 准备可真实命中解析缓存的隔离服务。
def _manager(tmp_path: Path, monkeypatch) -> SubAgentManager:
    manager = SubAgentManager(tmp_path / "runs")
    after_guard = time.time() + module.CACHE_TRUST_AGE_SECONDS + 60
    monkeypatch.setattr(module, "_now", lambda: after_guard)
    return manager


# LLM: 只写 pytest 临时目录的合法 JSON，保持必要 legacy 字段与原 reader，不使用产品旁路。
# 函数用途: 创建摘要和完整读口共同使用的历史任务文件。
def _legacy_task(manager: SubAgentManager, run_id: str, **overrides) -> Path:
    payload = {
        "id": run_id, "root_id": "root", "goal": "摘要隔离", "thought": "只读原任务",
        "plan": [], "status": "RUNNING", "created_at": 1.0, "updated_at": 2.0,
        "attributes": {"nested": [{"items": [1]}]},
    }
    payload.update(overrides)
    path = manager.workspace / run_id / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    return path


# LLM: 热场景经公共完整读口建立真实坏类型缓存；每次只读一条，避免旧排序先比较异型字段。
# 函数用途: 明确区分首次解析与已有缓存命中的摘要路径。
def _prime_cache(manager: SubAgentManager, paths: list[Path], warm_cache: bool) -> None:
    assert manager.persistence._run_cache == {}
    if warm_cache:
        for path in paths:
            report = manager.list_runs_by_ids_report([path.parent.name])
            assert report.load_errors == [] and len(report.runs) == 1
        assert set(manager.persistence._run_cache) == {path.parent.name for path in paths}


# LLM: 合法 JSON 的容器、bool/null 都不是预期摘要标量；坏行不得连坐健康任务或改变磁盘。
# 函数用途: 冷热两路拒绝五字段坏类型，并证明错误列表和正常返回列表只属于本次调用。
@pytest.mark.parametrize("warm_cache", [False, True], ids=["cold", "hot"])
@pytest.mark.parametrize("field_name", _SUMMARY_FIELDS)
@pytest.mark.parametrize("bad_value", _BAD_TYPES)
def test_summary_rejects_non_scalar_field_types(tmp_path, monkeypatch, warm_cache, field_name, bad_value):
    manager = _manager(tmp_path, monkeypatch)
    good = _legacy_task(manager, "good", updated_at=10.0)
    bad = _legacy_task(manager, "bad", **{field_name: bad_value})
    original = bad.read_bytes()
    _prime_cache(manager, [good, bad], warm_cache)
    loader = Mock(wraps=manager.persistence.load)
    monkeypatch.setattr(manager.persistence, "load", loader)
    with monkeypatch.context() as patch:
        copier = Mock(side_effect=AssertionError("summary must not deepcopy tasks"))
        patch.setattr(module.copy, "deepcopy", copier)
        report = manager.list_run_summaries_report()
        copier.assert_not_called()
    assert [item.id for item in report.runs] == ["good"]
    assert len(report.load_errors) == 1
    error = report.load_errors[0]
    assert error["run_id"] == "bad" and error["path"] == str(bad)
    assert error["context"] == "subagents.load" and field_name in error["message"]
    assert loader.call_count == (0 if warm_cache else 2)
    assert bad.read_bytes() == original
    report.runs.clear()
    error["run_id"] = "caller mutation"
    report.load_errors.clear()
    again = manager.list_run_summaries_report()
    assert [item.id for item in again.runs] == ["good"]
    assert [error["run_id"] for error in again.load_errors] == ["bad"]


# LLM: 不可变也不等于类型正确；数字身份/状态和字符串时间不得被 str/float 洗白。
# 函数用途: 对五字段的具体标量类型保留严格约束，而不只排除容器。
@pytest.mark.parametrize("warm_cache", [False, True], ids=["cold", "hot"])
@pytest.mark.parametrize("field_name,bad_value", [
    *(pytest.param(name, value, id=f"{name}-{type(value).__name__}")
      for name in _TEXT_FIELDS for value in (7, 1.5)),
    *(pytest.param(name, "2.0", id=f"{name}-numeric-string") for name in _TIME_FIELDS),
])
def test_summary_rejects_wrong_immutable_scalar_types(tmp_path, monkeypatch, warm_cache, field_name, bad_value):
    manager = _manager(tmp_path, monkeypatch)
    path = _legacy_task(manager, "bad", **{field_name: bad_value})
    _prime_cache(manager, [path], warm_cache)
    report = manager.list_run_summaries_report()
    assert report.runs == []
    assert len(report.load_errors) == 1
    assert report.load_errors[0]["run_id"] == "bad"
    assert field_name in report.load_errors[0]["message"]


# LLM: 指数溢出仍是合法 JSON 数字，解析后的无穷浮点不能作为排序时间；不用非法 NaN 文本。
# 函数用途: 验证摘要仅接受有限时间，异常行保持结构化报告。
@pytest.mark.parametrize("warm_cache", [False, True], ids=["cold", "hot"])
@pytest.mark.parametrize("field_name", _TIME_FIELDS)
@pytest.mark.parametrize("literal", ["1e400", "-1e400"])
def test_summary_rejects_nonfinite_json_times(tmp_path, monkeypatch, warm_cache, field_name, literal):
    manager = _manager(tmp_path, monkeypatch)
    path = _legacy_task(manager, "bad", **{field_name: "overflow"})
    path.write_text(path.read_text(encoding="utf-8").replace('"overflow"', literal), encoding="utf-8")
    _prime_cache(manager, [path], warm_cache)
    report = manager.list_run_summaries_report()
    assert report.runs == []
    assert len(report.load_errors) == 1
    assert field_name in report.load_errors[0]["message"]


# LLM: 身份沿原 opaque ID 规则拒绝而不清洗；root 空串是合法旧缺省，状态不新增枚举或别名。
# 函数用途: 防止空身份、路径式身份或空状态被当成可信摘要。
@pytest.mark.parametrize("warm_cache", [False, True], ids=["cold", "hot"])
@pytest.mark.parametrize("field_name,bad_value", [
    ("id", ""), ("id", "../escape"), ("id", " with-space"),
    ("root_id", "../escape"), ("root_id", "with space"),
    ("status", ""), ("status", "   "),
])
def test_summary_rejects_invalid_identity_or_empty_status(tmp_path, monkeypatch, warm_cache, field_name, bad_value):
    manager = _manager(tmp_path, monkeypatch)
    path = _legacy_task(manager, "bad", **{field_name: bad_value})
    _prime_cache(manager, [path], warm_cache)
    report = manager.list_run_summaries_report()
    assert report.runs == []
    assert len(report.load_errors) == 1
    assert field_name in report.load_errors[0]["message"]


# LLM: 若摘要误借出 list/dict，原地修改后完整读口仍须等于磁盘；此测试不靠 frozen 存在性冒充隔离。
# 函数用途: 动态重现五字段的可变引用逃逸，并守住完整读口原有的深副本合同。
@pytest.mark.parametrize("warm_cache", [False, True], ids=["cold", "hot"])
@pytest.mark.parametrize("field_name", _SUMMARY_FIELDS)
@pytest.mark.parametrize("bad_value", [pytest.param(["original"], id="list"), pytest.param({"original": 1}, id="dict")])
def test_caller_cannot_mutate_cached_bad_fields_through_summary(tmp_path, monkeypatch, warm_cache, field_name, bad_value):
    manager = _manager(tmp_path, monkeypatch)
    path = _legacy_task(manager, "bad", **{field_name: bad_value})
    expected = json.loads(path.read_text(encoding="utf-8"))[field_name]
    original = path.read_bytes()
    _prime_cache(manager, [path], warm_cache)
    report = manager.list_run_summaries_report()
    # 原实现有泄漏时真实尝试原地修改，修复后没有可借出的坏摘要。
    for summary in report.runs:
        value = getattr(summary, field_name)
        if isinstance(value, list):
            value.append("caller mutation")
        elif isinstance(value, dict):
            value["caller mutation"] = True
    full = manager.list_runs_by_ids_report(["bad"])
    assert full.load_errors == []
    assert getattr(full.runs[0], field_name) == expected
    assert path.read_bytes() == original
    assert report.runs == []
    assert len(report.load_errors) == 1
    # 坏类型兼容仍由原完整读取器负责；调用方改其副本也不能污染私有缓存。
    getattr(full.runs[0], field_name).clear()
    again = manager.list_runs_by_ids_report(["bad"])
    assert getattr(again.runs[0], field_name) == expected


# LLM: 合法字符串与有限 int/float 共享不可变值；冷/热摘要均零 deepcopy，返回容器和完整任务仍独立。
# 函数用途: 守住正常排序、旧空根、非别名化状态、冻结字段和调用方隔离。
@pytest.mark.parametrize("warm_cache", [False, True], ids=["cold", "hot"])
def test_normal_summaries_are_copy_free_frozen_and_caller_isolated(tmp_path, monkeypatch, warm_cache):
    manager = _manager(tmp_path, monkeypatch)
    paths = [
        _legacy_task(manager, "first", root_id="", created_at=1, updated_at=0),
        _legacy_task(manager, "second", created_at=2.0, updated_at=5.0),
        _legacy_task(manager, "third", status="COMPLETED", created_at=3, updated_at=0),
    ]
    _prime_cache(manager, paths, warm_cache)
    loader = Mock(wraps=manager.persistence.load)
    monkeypatch.setattr(manager.persistence, "load", loader)
    with monkeypatch.context() as patch:
        copier = Mock(side_effect=AssertionError("normal summary must not deepcopy tasks"))
        patch.setattr(module.copy, "deepcopy", copier)
        report = manager.list_run_summaries_report()
        assert report.load_errors == []
        assert [item.id for item in report.runs] == ["second", "third", "first"]
        expected = list(report.runs)
        assert expected[1].status == "COMPLETED" and expected[2].root_id == ""
        assert type(expected[0].updated_at) is float and type(expected[2].created_at) is int
        for field_name in _SUMMARY_FIELDS:
            with pytest.raises(FrozenInstanceError):
                setattr(expected[0], field_name, ["caller mutation"])
        report.runs.clear()
        report.load_errors.append({"caller": ["mutation"]})
        assert manager.list_run_summaries() == expected
        copier.assert_not_called()
    assert loader.call_count == (0 if warm_cache else 3)
    full = manager.list_runs_report()
    assert full.load_errors == []
    full.runs[0].attributes["nested"][0]["items"].append("caller mutation")
    full.runs[0].status = "caller mutation"
    assert manager.list_run_summaries() == expected
    assert manager.list_runs_report().runs[0].attributes["nested"][0]["items"] == [1]
