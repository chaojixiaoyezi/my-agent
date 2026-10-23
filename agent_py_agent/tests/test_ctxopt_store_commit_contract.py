# LLM: 通过临时Owner的真实Store/Tool观察提交与读回边界；故障只注入已完成_write之后，不改变事务实现。
# 模块用途: 守住提交后错误分类、原子Skill绑定和唯一工作流定义合同。
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool


# LLM: 提供完整领域合同，不含外部端点或凭据；id/kind明确匹配目标资产。
# 函数用途: 创建用于脚本和工作流工具的最小合法描述符。
def descriptor(kind="script", asset_id="demo"):
    return {"id": asset_id, "kind": kind, "name": "演示", "access_targets": [],
            "credential_refs": [], "data_sources": [], "io_contract": {}}


# LLM: 输入均从值对象公开入口恢复，不绕过步骤校验或Owner锁。
# 函数用途: 构造单步工作流以检验存储层绑定和引用派生。
def definition(mode="llm_skill_analysis", skills=()):
    return WorkflowDefinition.from_dict({"descriptor": descriptor("workflow"), "steps": [
        {"step_id": "analyze", "name": "分析", "mode": mode, "skill_ids": list(skills)}]})


# LLM: 原_write真实完成后才打开故障，保留异常因果链；不伪造磁盘提交成功。
# 函数用途: 仅阻断提交后的_get读回，供脚本和工作流共同回归。
def break_post_commit_readback(monkeypatch, store):
    write, get, state = store._write, store._get, {"committed": False}

    # LLM: 原事务异常照常抛出，只有正常返回才标记已提交。
    # 函数用途: 记录真实写入完成这一观察点。
    def observed_write(*args, **kwargs):
        result = write(*args, **kwargs)
        state["committed"] = True
        return result

    # LLM: 提交前读取完全沿用真实实现，避免把预检错误误当提交后故障。
    # 函数用途: 为本次提交后的读回注入确定的ValueError。
    def faulted_get(*args, **kwargs):
        if state["committed"]:
            raise ValueError("injected readback failure")
        return get(*args, **kwargs)

    monkeypatch.setattr(store, "_write", observed_write)
    monkeypatch.setattr(store, "_get", faulted_get)
    return get, state


# LLM: 参数合法且资产确实落盘后必须unknown，不允许not_started诱发重试；普通预检另测。
# 函数用途: 验证两个工具对已提交读回失败给出同一结构化合同。
@pytest.mark.parametrize("kind", ["script", "workflow"])
def test_post_commit_tool_outcome_is_unknown(tmp_path, monkeypatch, kind):
    store = ScriptStore(tmp_path) if kind == "script" else WorkflowStore(tmp_path)
    tool = ScriptsTool(tmp_path, store=store) if kind == "script" else WorkflowsTool(tmp_path, store=store)
    get, state = break_post_commit_readback(monkeypatch, store)
    params = ({"action": "register", "script_id": "demo", "descriptor": descriptor(), "code": 'print("{}")'}
              if kind == "script" else {"action": "save", "workflow_id": "demo", "definition": definition().to_dict()})
    result = tool.execute(params)
    assert state["committed"]
    monkeypatch.setattr(store, "_get", get)
    assert get("demo") is not None
    assert not result.ok
    # 宿主保留原错误码；未登记taxonomy的业务码规范为UNKNOWN_ERROR。
    assert result.error_code == "UNKNOWN_ERROR"
    assert result.reported_error_code == "TOOL_POST_COMMIT_READBACK_FAILED"
    assert result.effect_outcome == "unknown"
    # 宿主阶段枚举只认execution；业务回执仍必须保留准确阶段。
    assert result.failure_stage == "execution"
    data = json.loads(result.output)
    assert data["failure_stage"] == "post_commit_readback"
    assert data["committed"] is True


# LLM: 预检失败不能触发Store，也不能被已提交异常分类覆盖。
# 函数用途: 验证坏参数仍明确报告未开始且无资产产生。
@pytest.mark.parametrize("tool_cls,key,action", [(ScriptsTool, "script_id", "register"),
                                                (WorkflowsTool, "workflow_id", "save")])
def test_preflight_stays_not_started(tmp_path, tool_cls, key, action):
    result = tool_cls(tmp_path).execute({"action": action, key: "demo"})
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.effect_outcome == "not_started"
    assert not (tmp_path / ("scripts" if key == "script_id" else "workflows")).exists()


# LLM: 绑定模式必须在Store锁内验证，不能只依赖工具wrapper；失败保持原定义逐值一致。
# 函数用途: 禁止直接Store调用给确定性步骤挂Skill。
def test_store_binding_rejects_deterministic_step(tmp_path):
    store = WorkflowStore(tmp_path)
    original = store.save_workflow(definition("deterministic_script"))
    with pytest.raises(ValueError):
        store.attach_skill_to_step("demo", "analyze", "audit")
    assert store.get_workflow("demo") == original


# LLM: detach与attach共用Store锁和引用派生，不能用工具层独立读改写代替。
# 函数用途: 验证解绑命令存在且精确更新目标步骤和refs。
def test_store_detach_is_authoritative(tmp_path):
    store = WorkflowStore(tmp_path)
    store.save_workflow(definition(skills=("audit", "keep")))
    detach = getattr(store, "detach_skill_from_step", None)
    assert callable(detach), "Store缺少原子解绑命令"
    saved = detach("demo", "analyze", "audit")
    assert saved.steps[0].skill_ids == ("keep",)
    assert saved.descriptor.refs.skills == ("keep",)


# LLM: 定义构造和派生refs各只有一份权威，稳定引用留在步骤、裸名称留在AssetRefs。
# 函数用途: 验证from_any往返并清除用户提交的陈旧引用。
def test_definition_from_any_and_canonical_refs(tmp_path):
    restore = getattr(WorkflowDefinition, "from_any", None)
    assert callable(restore), "缺少唯一WorkflowDefinition.from_any"
    raw = definition().to_dict()
    raw["steps"][0]["skill_ids"] = ["owner:audit", "builtin:audit"]
    raw["descriptor"]["refs"] = {"scripts": ["stale"], "skills": ["stale"]}
    value = restore(raw)
    saved = WorkflowStore(tmp_path).save_workflow(value)
    assert saved.steps[0].skill_ids == ("owner:audit", "builtin:audit")
    assert saved.descriptor.refs.skills == ("audit",)
    assert saved.descriptor.refs.scripts == ()
    assert restore(value) == value


# LLM: 同一Owner两次原子绑定必须保留已有步骤及描述符，不修改传入值对象。
# 函数用途: 核对Store挂载的幂等和非目标字段保持合同。
def test_store_bind_preserves_definition(tmp_path):
    store = WorkflowStore(tmp_path)
    initial = definition(skills=("keep",))
    original = replace(initial, descriptor=AssetDescriptor.from_dict(initial.descriptor.to_dict()))
    store.save_workflow(original)
    first = store.attach_skill_to_step("demo", "analyze", "audit")
    second = store.attach_skill_to_step("demo", "analyze", "audit")
    assert first == second
    assert first.steps[0].skill_ids == ("keep", "audit")
    assert original.steps[0].skill_ids == ("keep",)
