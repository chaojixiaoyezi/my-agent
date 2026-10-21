# LLM: 全部持久化仅使用tmp_path；只跑本片真实描述符/存储/图入口，不接模型或真实Owner。
# 模块用途: 锁住统一资产协议、原子保存、安全路径、数据源更新、运行计数和拓扑反向引用。
import importlib
import importlib.util
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest


# LLM: 缺包时先以明确契约断言报红，避免把导入异常误当功能反例；无环境写入。
# 函数用途: 为用例加载本片各公共类型，产品未实现时指出缺少的入口。
@pytest.fixture
def api():
    assert importlib.util.find_spec("agent_py_agent.agent.assets") is not None, "尚未实现资产存储包"
    modules = [importlib.import_module(f"agent_py_agent.agent.assets.{name}") for name in
               ("descriptor", "script_store", "workflow_store", "topology")]
    return type("API", (), {key: value for module in modules for key, value in vars(module).items()
                            if not key.startswith("_")})


# LLM: 合成标准资产不含真实地址或凭据；描述符和Store都走公开构造入口。
# 函数用途: 为每类资产提供具有可编辑数据源的统一测试样本。
def asset(api, kind="script", asset_id="collect", **kwargs):
    return api.AssetDescriptor(asset_id, kind, asset_id, data_sources=(api.DataSourceSpec(
        "apps", "logs", {"active_appnames": ["A", "B", "C"]},
    ),), **kwargs)


# LLM: 四类资产必须共用同一JSON契约，恢复不能丢结构或借用可变默认值。
# 函数用途: 验证嵌套描述符完整往返、冻结字段和默认容器隔离。
@pytest.mark.parametrize("kind", ["monitor", "workflow", "script", "skill"])
def test_descriptor_json_roundtrip_and_frozen_defaults(api, kind):
    descriptor = asset(api, kind, access_targets=(api.AccessTarget("logs", "api", "https://example.test", "GET"),),
                       credential_refs=(api.CredentialBindingRef("cred-example", "logs", "bearer"),),
                       io_contract=api.IOContract(("appnames",), ({"type": "json"},)), tags=("ops",))
    payload = descriptor.to_dict()
    assert payload["schema_version"] == "asset_descriptor.v1"
    assert api.AssetDescriptor.from_dict(json.loads(json.dumps(payload))) == descriptor
    with pytest.raises(FrozenInstanceError):
        descriptor.name = "changed"
    other = asset(api)
    other.metadata["label"] = "independent"
    assert descriptor.metadata == {}


# LLM: 秘密键递归过滤，未知普通字段拒绝；凭据引用本身必须保留。
# 函数用途: 证明构造、序列化和恢复都不会持有嵌套明文secret字段。
def test_descriptor_filters_secrets_and_rejects_unknown_fields(api):
    descriptor = asset(api, metadata={"password": "synthetic", "nested": [{"api_key": "synthetic", "label": "keep"}],
                                     "access_token": "synthetic", "clientSecret": "synthetic"})
    assert descriptor.metadata == {"nested": [{"label": "keep"}]}
    payload = descriptor.to_dict()
    payload["api_key"] = "synthetic"
    assert api.AssetDescriptor.from_dict(payload).to_dict() == descriptor.to_dict()
    payload["unexpected"] = True
    with pytest.raises((ValueError, TypeError)):
        api.AssetDescriptor.from_dict(payload)
    payload = descriptor.to_dict()
    payload["schema_version"] = "asset_descriptor.v99"
    with pytest.raises(ValueError):
        api.AssetDescriptor.from_dict(payload)


# LLM: 类型、枚举、负数和非JSON值须在边界拒绝，不能静默转换或污染存储。
# 函数用途: 覆盖严格校验的常见损坏输入。
@pytest.mark.parametrize("change", [{"kind": "plugin"}, {"id": "../escape"}, {"name": 123},
                                    {"metadata": []}, {"tags": "abc"}, {"metadata": {"x": float("nan")}},
                                    {"runtime_summary": {"run_count": -1}}, {"runtime_summary": {"run_count": True}}])
def test_descriptor_rejects_invalid_fields(api, change):
    payload = asset(api).to_dict() | change
    with pytest.raises((ValueError, TypeError)):
        api.AssetDescriptor.from_dict(payload)


# LLM: 评估结果是结构化状态，不接受自由文案当状态；只允许非负样本数。
# 函数用途: 验证候选评估的枚举与计数边界。
def test_candidate_validation(api):
    candidate = api.EvaluatedCandidate("D", "ignored", "2026-10-21T00:00:00Z", "empty", 0)
    assert api.EvaluatedCandidate.from_dict(candidate.to_dict()) == candidate
    for change in ({"decision": "maybe"}, {"sample_count": -1}, {"sample_count": "1"}):
        with pytest.raises((ValueError, TypeError)):
            api.EvaluatedCandidate.from_dict(candidate.to_dict() | change)


# LLM: 只在临时Owner写脚本，JSON元数据和代码文件必须能由全新Store读回。
# 函数用途: 验证CRUD、规范路径、owner隔离和代码安全读口。
def test_script_crud_and_owner_isolation(api, tmp_path):
    store = api.ScriptStore(tmp_path / "one")
    result = store.save_script(asset(api), "print('ok')\n")
    assert result["code"] == "print('ok')\n" and result["entrypoint"] == "main.py"
    assert (tmp_path / "one/scripts/collect/script.json").is_file()
    assert (tmp_path / "one/scripts/collect/main.py").read_text() == result["code"]
    restored = api.ScriptStore(tmp_path / "one").get_script("collect")
    assert restored == result
    assert [row["id"] for row in store.list_scripts()] == ["collect"]
    assert "code" not in store.list_scripts()[0]
    assert api.ScriptStore(tmp_path / "two").get_script("collect") is None
    assert store.delete_script("collect") is True
    assert store.get_script("collect") is None and store.list_scripts() == []
    assert store.delete_script("collect") is False


# LLM: 查询范围按键更新，同一候选的不同评估保留为历史；旧范围和代码不能丢失。
# 函数用途: 验证active_appnames增删、新源评估备忘与嵌套过滤。
def test_script_data_source_patch_and_candidate_upsert(api, tmp_path):
    store = api.ScriptStore(tmp_path)
    store.save_script(asset(api), "pass\n")
    candidate = api.EvaluatedCandidate("D", "ignored", "2026-10-21", "no data", 0)
    store.update_data_sources("collect", "apps", query_scope={"active_appnames": ["A", "C"], "region": "west"}, candidate=candidate)
    result = store.update_data_sources("collect", "apps", query_scope={"active_appnames": ["A", "C", "E"], "region": None},
                                       candidate=api.EvaluatedCandidate("D", "active", "2026-10-22", "now has data", 1))
    source = result["data_sources"][0]
    assert source["query_scope"] == {"active_appnames": ["A", "C", "E"]}
    assert len(source["evaluated_candidates"]) == 2
    assert [item['decision'] for item in source['evaluated_candidates']] == ['ignored', 'active']
    assert store.get_script("collect")["code"] == "pass\n"
    with pytest.raises(KeyError):
        store.update_data_sources("collect", "absent", query_scope={})


# LLM: 原子读改写须由不同Store实例共用锁，不能丢并发运行次数。
# 函数用途: 验证失败连计、成功归零及多线程计数。
def test_script_run_summary_and_concurrent_updates(api, tmp_path):
    store = api.ScriptStore(tmp_path)
    store.save_script(asset(api), "pass")
    store.record_run_summary("collect", "failed", 12, "2026-10-21")
    failed = store.record_run_summary("collect", "failed", 15, "2026-10-21")
    assert failed["runtime_summary"]["consecutive_failures"] == 2
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: api.ScriptStore(tmp_path).record_run_summary("collect", "succeeded", 3, "2026-10-21"), range(8)))
    summary = store.get_script("collect")["runtime_summary"]
    assert summary == {"last_run_at": "2026-10-21", "last_status": "succeeded", "last_duration_ms": 3,
                       "run_count": 10, "consecutive_failures": 0, "last_error": ""}
    with pytest.raises(ValueError):
        store.record_run_summary("collect", "failed", -1, "2026-10-21")


# LLM: 元数据提交前失败不得破坏上一版脚本；故障注入只替换原写入叶，不绕过Store。
# 函数用途: 验证代码与元数据事务异常回滚。
def test_script_save_rolls_back_on_metadata_write_failure(api, tmp_path, monkeypatch):
    module = importlib.import_module("agent_py_agent.agent.assets")
    store = api.ScriptStore(tmp_path)
    store.save_script(asset(api), "old")
    write = module.write_text_atomic_beneath

    # LLM: 只在新元数据第一次提交时失败，回滚仍走原写入器。
    # 函数用途: 构造可重复的保存失败观察点。
    def fail_once(root, parts, content):
        if parts[-1] == "script.json" and '"version": "2"' in content:
            raise OSError("synthetic commit failure")
        return write(root, parts, content)

    monkeypatch.setattr(module, "write_text_atomic_beneath", fail_once)
    with pytest.raises(OSError):
        store.save_script(asset(api, version="2"), "new")
    assert store.get_script("collect")["code"] == "old"
    assert store.get_script("collect")["version"] == "1.0.0"


# LLM: 标识符、代码入口和持久化叶均不允许逃离构造时Owner；不访问真实Owner。
# 函数用途: 验证路径穿越、无效超时及错kind拒绝。
@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "/tmp/escape", ".", "..", "a\\b", ""])
def test_store_rejects_unsafe_ids(api, tmp_path, bad_id):
    for store, read, delete in ((api.ScriptStore(tmp_path), "get_script", "delete_script"),
                                (api.WorkflowStore(tmp_path), "get_workflow", "delete_workflow")):
        with pytest.raises(ValueError):
            getattr(store, read)(bad_id)
        with pytest.raises(ValueError):
            getattr(store, delete)(bad_id)


# LLM: 不让代码路径覆写权威JSON或附带链接；timeout必须为正有限数。
# 函数用途: 锁住脚本保存参数边界。
@pytest.mark.parametrize("options", [{"entrypoint": "../evil.py"}, {"entrypoint": "script.json"}, {"entrypoint": "SCRIPT.JSON"},
                                     {"timeout_seconds": -1}, {"timeout_seconds": True}, {"timeout_seconds": float("inf")}])
def test_script_rejects_invalid_save_options(api, tmp_path, options):
    with pytest.raises((ValueError, TypeError)):
        api.ScriptStore(tmp_path).save_script(asset(api), "pass", **options)


# LLM: 同一owner内伪造的符号链接也不是权限；nofollow拒绝读、写、删除，不触碰链接目标。
# 函数用途: 验证集合目录、资产目录、元数据及代码叶链接都不能跨owner。
@pytest.mark.parametrize("level", ["collection", "asset", "metadata", "code"])
def test_script_rejects_symlink_paths(api, tmp_path, level):
    owner = tmp_path / "owner"
    other = tmp_path / "other"
    other.mkdir()
    marker = other / "marker"
    marker.write_text("keep")
    store = api.ScriptStore(owner)
    if level == "collection":
        (owner / "scripts").symlink_to(other, target_is_directory=True)
    elif level == "asset":
        (owner / "scripts").mkdir()
        (owner / "scripts/collect").symlink_to(other, target_is_directory=True)
    else:
        store.save_script(asset(api), "old")
        leaf = owner / "scripts/collect" / ("script.json" if level == "metadata" else "main.py")
        leaf.unlink()
        leaf.symlink_to(marker)
    for operation in (lambda: store.get_script("collect"), lambda: store.save_script(asset(api), "new"),
                      lambda: store.delete_script("collect")):
        with pytest.raises((OSError, ValueError)):
            operation()
    assert marker.read_text() == "keep"


# LLM: 损坏的权威JSON与错ID不得伪装成缺失，便于调用方发现与修复真实存储问题。
# 函数用途: 验证坏记录显式失败。
def test_store_rejects_corrupt_and_mismatched_metadata(api, tmp_path):
    store = api.ScriptStore(tmp_path)
    store.save_script(asset(api), "pass")
    path = tmp_path / "scripts/collect/script.json"
    path.write_text("{broken")
    with pytest.raises(ValueError):
        store.get_script("collect")
    path.write_text(json.dumps(asset(api, asset_id="other").to_dict() | {"entrypoint": "main.py", "interpreter": "python3", "timeout_seconds": 120}))
    with pytest.raises(ValueError):
        store.get_script("collect")


# LLM: 工作流引用按步骤计算、去重，人工其它类引用保留；不执行脚本或Skill。
# 函数用途: 验证定义往返、说明文件、引用推导、挂载、摘要和删除。
def test_workflow_crud_refs_attach_and_runtime_summary(api, tmp_path):
    store = api.WorkflowStore(tmp_path)
    steps = (api.WorkflowStepSpec("collect", "采集", "deterministic_script", script_id="script-A"),
             api.WorkflowStepSpec("analyze", "分析", "hybrid", script_id="script-A", skill_ids=("skill-A",),
                                  skill_tags=("logs",), inputs_map={"message": "collect.output"}))
    definition = api.WorkflowDefinition(asset(api, "workflow", "daily", refs=api.AssetRefs(monitors=("other",), scripts=("stale",))),
                                        steps, "# 每日分析\n")
    saved = store.save_workflow(definition)
    assert saved.descriptor.refs.scripts == ("script-A",)
    assert saved.descriptor.refs.skills == ("skill-A",)
    assert saved.descriptor.refs.monitors == ("other",)
    assert (tmp_path / "workflows/daily/WORKFLOW.md").read_text() == "# 每日分析\n"
    assert api.WorkflowStore(tmp_path).get_workflow("daily") == saved
    assert store.list_workflows() == [saved]
    attached = store.attach_skill_to_step("daily", "analyze", "skill-B")
    assert attached.steps[1].skill_ids == ("skill-A", "skill-B")
    assert store.attach_skill_to_step("daily", "analyze", "skill-B") == attached
    assert attached.descriptor.refs.skills == ("skill-A", "skill-B")
    with pytest.raises(KeyError):
        store.attach_skill_to_step("daily", "absent", "skill-C")
    updated = store.record_run_summary("daily", "failed", 10, "2026-10-21")
    assert updated.descriptor.runtime_summary.consecutive_failures == 1
    assert store.record_run_summary("daily", "succeeded", 2, "2026-10-21").descriptor.runtime_summary.run_count == 2
    assert store.delete_workflow("daily") is True
    assert store.get_workflow("daily") is None and store.list_workflows() == []
    assert store.delete_workflow("daily") is False


# LLM: 步骤契约只接受三种模式与正超时；重复step_id会破坏定位，须在保存前拒绝。
# 函数用途: 验证工作流结构错误边界。
def test_workflow_validation_and_replacing_empty_body(api, tmp_path):
    with pytest.raises(ValueError):
        api.WorkflowStepSpec("s", "bad", "unknown")
    with pytest.raises(ValueError):
        api.WorkflowStepSpec("s", "bad", "hybrid", timeout_seconds=0)
    step = api.WorkflowStepSpec("s", "analysis", "llm_skill_analysis")
    with pytest.raises(ValueError):
        api.WorkflowDefinition(asset(api, "workflow"), (step, step))
    store = api.WorkflowStore(tmp_path)
    store.save_workflow(api.WorkflowDefinition(asset(api, "workflow"), (step,), "old"))
    store.save_workflow(api.WorkflowDefinition(asset(api, "workflow"), (), ""))
    assert not (tmp_path / "workflows/collect/WORKFLOW.md").exists()
    assert store.get_workflow("collect").descriptor.refs.scripts == ()


# LLM: 图投影不得写回原描述符；反向引用由实际正向边重算，循环不得造成无限遍历。
# 函数用途: 验证完整链路、反向引用、可达子图和循环。
def test_topology_reverse_refs_rooted_graph_and_cycles(api):
    descriptors = [asset(api, "monitor", "A", refs=api.AssetRefs(workflows=("B",))),
                   asset(api, "workflow", "B", refs=api.AssetRefs(scripts=("C", "D"), skills=("E", "F"))),
                   asset(api, "script", "C", refs=api.AssetRefs(monitors=("A",))),
                   asset(api, "script", "D"), asset(api, "skill", "E"), asset(api, "skill", "F"),
                   asset(api, "monitor", "unrelated")]
    graph = api.build_asset_topology(descriptors, root_key="monitor:A")
    nodes = {row["key"]: row for row in graph["nodes"]}
    assert set(nodes) == {"monitor:A", "workflow:B", "script:C", "script:D", "skill:E", "skill:F"}
    assert nodes["workflow:B"]["used_by"]["monitors"] == ["A"]
    assert nodes["script:C"]["used_by"]["workflows"] == ["B"]
    assert nodes["skill:F"]["used_by"]["workflows"] == ["B"]
    assert {"from": "monitor:A", "to": "workflow:B", "relation": "triggers"} in graph["edges"]
    assert {"from": "workflow:B", "to": "script:C", "relation": "uses_script"} in graph["edges"]
    assert descriptors[1].used_by == api.AssetRefs()
    assert len(api.build_asset_topology(descriptors)["nodes"]) == 7
    assert api.build_asset_topology([]) == {"nodes": [], "edges": [], "cycles": [], "has_cycles": False}
    with pytest.raises(ValueError):
        api.build_asset_topology(descriptors, root_key="monitor:missing")
    with pytest.raises(ValueError):
        api.build_asset_topology(descriptors + [descriptors[0]])


# LLM: 未提供描述符的引用也必须显式出现在图中，不能静默丢掉依赖。
# 函数用途: 验证悬空依赖节点带missing标记。
def test_topology_preserves_missing_reference(api):
    graph = api.build_asset_topology([asset(api, "monitor", "A", refs=api.AssetRefs(workflows=("B",)))])
    missing = next(row for row in graph["nodes"] if row["key"] == "workflow:B")
    assert missing["missing"] is True and missing["used_by"]["monitors"] == ["A"]


# LLM: 子进程只在临时Owner写测试脚本，使用真实进程突然退出模拟无法执行except的提交中断。
# 函数用途: 证明跨代码/元数据保存中断后可由新Store恢复一致旧版本。
@pytest.mark.parametrize("rename", [False, True])
def test_script_recovers_transaction_after_process_exit(api, tmp_path, rename):
    import subprocess
    import sys

    store = api.ScriptStore(tmp_path)
    old_entrypoint = "old.py" if rename else "main.py"
    new_entrypoint = "new.py" if rename else "main.py"
    store.save_script(asset(api), "old", entrypoint=old_entrypoint)
    program = """
import os, sys
from agent_py_agent.agent.assets import _AssetStore
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
original = _AssetStore._put
# LLM: 故障只发生在测试子进程的代码叶替换后，模拟真实进程死亡而不是Python异常。
# 函数用途: 创建回滚钩子无法执行的跨文件提交中断。
def interrupted(self, asset_id, filename, content):
    original(self, asset_id, filename, content)
    if filename == sys.argv[2]:
        os._exit(19)
_AssetStore._put = interrupted
ScriptStore(sys.argv[1]).save_script(AssetDescriptor('collect', 'script', 'collect', version='2'), 'new', entrypoint=sys.argv[2])
"""
    result = subprocess.run([sys.executable, "-c", program, str(tmp_path), new_entrypoint], check=False, capture_output=True)
    assert result.returncode == 19, result.stderr.decode()
    restored = api.ScriptStore(tmp_path).get_script("collect")
    assert restored["version"] == "1.0.0" and restored["code"] == "old"
    assert restored["entrypoint"] == old_entrypoint
    assert not (tmp_path / "scripts/collect/asset-transaction.json").exists()


# LLM: 已有入口仅改变大小写时保持既有规范拼写，避免不同文件系统对别名删除语义不一致。
# 函数用途: 验证大小写别名更新不会丢掉刚写入的代码。
def test_script_entrypoint_case_alias_keeps_existing_name(api, tmp_path):
    store = api.ScriptStore(tmp_path)
    store.save_script(asset(api), "old", entrypoint="main.py")
    result = store.save_script(asset(api), "new", entrypoint="MAIN.py")
    assert result["entrypoint"] == "main.py" and result["code"] == "new"
    assert store.get_script("collect")["code"] == "new"


# LLM: 使用真实UTF-8字节超限输入，提交前必须拒绝，旧版本仍能读回。
# 函数用途: 防止写入大于安全读取预算的代码或JSON后才返回失败。
@pytest.mark.parametrize("field", ["code", "metadata"])
def test_script_rejects_oversize_before_overwriting(api, tmp_path, field):
    store = api.ScriptStore(tmp_path)
    store.save_script(asset(api), "old")
    large = "界" * ((16 * 1024 * 1024) // 3 + 1)
    descriptor = asset(api, metadata={"note": large}) if field == "metadata" else asset(api)
    with pytest.raises(ValueError):
        store.save_script(descriptor, large if field == "code" else "new")
    assert store.get_script("collect")["code"] == "old"


# LLM: condition是数据声明，允许执行器已有的结构化前序输出比较，不在Store里求值。
# 函数用途: 验证结构化条件及空字符串默认经过存储往返不丢失。
def test_workflow_structured_condition_roundtrip(api, tmp_path):
    condition = {"step_id": "collect", "key": "flag", "equals": True}
    step = api.WorkflowStepSpec("analyze", "分析", "llm_skill_analysis", condition=condition)
    store = api.WorkflowStore(tmp_path)
    store.save_workflow(api.WorkflowDefinition(asset(api, "workflow"), (step,)))
    assert store.get_workflow("collect").steps[0].condition == condition
    assert api.WorkflowStepSpec("s", "step", "hybrid").condition == ""


# LLM: 回滚再失败不能删除恢复材料或把混合状态返回为成功；下一次锁内读取重试同一undo。
# 函数用途: 验证中断恢复的恢复失败仍可由新Store完成。
def test_script_keeps_journal_when_rollback_itself_fails(api, tmp_path, monkeypatch):
    module = importlib.import_module("agent_py_agent.agent.assets")
    store = api.ScriptStore(tmp_path)
    store.save_script(asset(api), "old")
    original = module.write_text_atomic_beneath

    # LLM: 只在这次保存及随后首次代码回滚注入错误，不改原权限或持久路径。
    # 函数用途: 造出需要下次读取继续恢复的失败现场。
    def failing(root, parts, content):
        if parts[-1] == "script.json" and '"version": "2"' in content or parts[-1] == "main.py" and content == "old":
            raise OSError("synthetic rollback failure")
        return original(root, parts, content)

    monkeypatch.setattr(module, "write_text_atomic_beneath", failing)
    with pytest.raises(OSError):
        store.save_script(asset(api, version="2"), "new")
    assert (tmp_path / "scripts/collect/asset-transaction.json").is_file()
    monkeypatch.setattr(module, "write_text_atomic_beneath", original)
    assert api.ScriptStore(tmp_path).get_script("collect")["code"] == "old"
    assert not (tmp_path / "scripts/collect/asset-transaction.json").exists()


# LLM: 环枚举须覆盖每条简单有向环，旋转不重复，根过滤不改变原nodes/edges排序。
# 函数用途: 复现多节点环、自环和同一强连通分量内多个独立环缺失。
def test_topology_reports_all_directed_cycles_stably(api):
    specs = [('A', ('A', 'B', 'C')), ('B', ('A', 'C')), ('C', ('A',)), ('Z', ())]
    descriptors = [asset(api, 'workflow', name, refs=api.AssetRefs(workflows=targets)) for name, targets in specs]
    graph = api.build_asset_topology(descriptors)
    assert graph.get('has_cycles') is True
    assert graph['cycles'] == [
        ['workflow:A', 'workflow:A'], ['workflow:A', 'workflow:B', 'workflow:A'],
        ['workflow:A', 'workflow:B', 'workflow:C', 'workflow:A'], ['workflow:A', 'workflow:C', 'workflow:A']]
    assert {node['key'] for node in graph['nodes'] if node['in_cycle']} == {'workflow:A', 'workflow:B', 'workflow:C'}
    assert graph == api.build_asset_topology(reversed(descriptors))
    selected = api.build_asset_topology(descriptors, root_key='workflow:Z')
    assert selected['cycles'] == [] and selected['has_cycles'] is False
    assert selected['nodes'][0]['in_cycle'] is False
    assert [row['key'] for row in graph['nodes']] == sorted(row['key'] for row in graph['nodes'])


# LLM: 深链不得依赖Python递归深度；缺失引用是叶节点，不能被标成环。
# 函数用途: 校验无环深图和空图结果的明确标记。
def test_topology_acyclic_deep_chain(api):
    descriptors = [asset(api, 'workflow', f'w{i}', refs=api.AssetRefs(workflows=(f'w{i+1}',))) for i in range(1050)]
    graph = api.build_asset_topology(descriptors)
    assert graph.get('cycles') == [] and graph['has_cycles'] is False
    assert not any(node['in_cycle'] for node in graph['nodes'])
    assert api.build_asset_topology([]) == {'nodes': [], 'edges': [], 'cycles': [], 'has_cycles': False}


# LLM: 候选记录以全部五字段去重而不是candidate_id覆盖，重复提交同一观察才幂等。
# 函数用途: 复现候选再次评估和普通保存时历史被清空的问题。
def test_script_preserves_full_candidate_history_across_update_and_save(api, tmp_path):
    from dataclasses import replace
    store = api.ScriptStore(tmp_path)
    store.save_script(asset(api), 'old')
    first = api.EvaluatedCandidate('D', 'ignored', '2026-10-21T01:00:00Z', 'static', 3)
    second = api.EvaluatedCandidate('D', 'active', '2026-10-21T02:00:00Z', 'useful', 4)
    store.update_data_sources('collect', 'apps', candidate=first)
    store.update_data_sources('collect', 'apps', candidate=second)
    store.update_data_sources('collect', 'apps', candidate=second)
    expected = [first.to_dict(), second.to_dict()]
    assert store.get_script('collect')['data_sources'][0]['evaluated_candidates'] == expected
    fresh = asset(api, version='2')
    store.save_script(fresh, 'new')
    assert store.get_script('collect')['data_sources'][0]['evaluated_candidates'] == expected
    third = api.EvaluatedCandidate('D', 'active', '2026-10-21T02:00:00Z', 'more samples', 5)
    source = replace(fresh.data_sources[0], evaluated_candidates=(second, third))
    saved = store.save_script(replace(fresh, data_sources=(source,)), 'third')
    assert saved['data_sources'][0]['evaluated_candidates'] == [*expected, third.to_dict()]
    assert api.ScriptStore(tmp_path).get_script('collect') == saved


# LLM: 空摘要代表调用方没有新运行观察，不应把现有计数归零；显式非空摘要仍以新值为准。
# 函数用途: 复现普通脚本更新抹掉运行统计的问题。
def test_script_save_inherits_only_empty_runtime_summary(api, tmp_path):
    from dataclasses import replace
    store = api.ScriptStore(tmp_path)
    store.save_script(asset(api), 'old')
    old = store.record_run_summary('collect', 'failed', 2, '2026-10-21')
    assert store.save_script(asset(api), 'new')['runtime_summary'] == old['runtime_summary']
    summary = api.RuntimeSummary(run_count=8, last_status='succeeded')
    assert store.save_script(replace(asset(api), runtime_summary=summary), 'newer')['runtime_summary'] == summary.to_dict()


# LLM: 数字预算不是凭据；普通元数据值里的命令、URL与HTTP头仍沿统一脱敏口。
# 函数用途: 复现token子串误删以及秘密藏在值中未脱敏的问题。
def test_clean_json_keeps_token_metrics_and_redacts_retained_strings(api):
    budgets = dict.fromkeys(('tokens', 'max_tokens', 'input_tokens', 'output_tokens', 'total_tokens',
                             'token_count', 'prompt_tokens', 'completion_tokens'), 100)
    budgets['max_tokens'] = 100.5
    payload = {**budgets, 'notes': ['--password=synthetic', 'https://user:synthetic@example.test/a',
                                   'Cookie: session=synthetic; other=hidden'],
               'tokenizer': 'ordinary', 'password_hint': 'use vault', 'clientSecret': 'synthetic',
               'auth_token': 'synthetic', 'bearer': 'synthetic', 'token': 'synthetic'}
    safe = api.clean_json(payload)
    assert {key: safe.get(key) for key in budgets} == budgets
    assert safe['tokenizer'] == 'ordinary' and safe['password_hint'] == 'use vault'
    assert 'synthetic' not in json.dumps(safe) and 'hidden' not in json.dumps(safe)
    assert payload['notes'][0] == '--password=synthetic'


# LLM: 硬拒绝必须发生在原始构造和JSON恢复两处，不能先打码再把非法凭据当有效引用。
# 函数用途: 复现引用各文本字段携带原始秘密时被接受的问题。
@pytest.mark.parametrize('field', ['credential_ref', 'alias', 'authentication', 'status'])
@pytest.mark.parametrize('raw', ['sk-' + 'x' * 24, 'Bearer synthetic', 'https://user:synthetic@example.test',
                                 'api_key=synthetic', 'token=synthetic', '<redacted>', '[REDACTED]'])
def test_credential_binding_rejects_raw_values(api, field, raw):
    values = {'credential_ref': 'cred-safe', field: raw}
    with pytest.raises(ValueError, match='CredentialBindingRef must not contain raw secret values'):
        api.CredentialBindingRef(**values)
    with pytest.raises(ValueError, match='CredentialBindingRef must not contain raw secret values'):
        api.CredentialBindingRef.from_dict(values)


# LLM: endpoint仅为不带秘密的访问说明，不能把敏感URL打码后静默登记成可执行配置。
# 函数用途: 复现URL用户密码与查询凭据在访问目标中被接受的问题。
@pytest.mark.parametrize('endpoint', ['https://user:synthetic@example.test/a',
    'https://example.test?a=1&api_key=synthetic', 'https://example.test?access_token=synthetic',
    'https://example.test?password=synthetic', 'https://example.test?secret=synthetic'])
def test_access_target_rejects_raw_endpoint_credentials(api, endpoint):
    values = {'target_id': 'api', 'type': 'http', 'endpoint': endpoint}
    with pytest.raises(ValueError, match='AccessTarget endpoint must not contain raw credentials'):
        api.AccessTarget(**values)
    with pytest.raises(ValueError, match='AccessTarget endpoint must not contain raw credentials'):
        api.AccessTarget.from_dict(values)


# LLM: 批量和单条更新使用同一五字段去重规则；删除源是显式变更，不能因保历史把源复活。
# 函数用途: 验证批量候选幂等、字段差异保留及源删除边界。
def test_candidate_batch_history_dedup_and_removed_source(api, tmp_path):
    from dataclasses import replace
    store = api.ScriptStore(tmp_path)
    store.save_script(asset(api), 'old')
    first = api.EvaluatedCandidate('D', 'ignored', '2026-10-21', 'empty', 1)
    observations = [first, replace(first, decision='pending'), replace(first, evaluated_at='2026-10-22'),
                    replace(first, reason='different'), replace(first, sample_count=2)]
    saved = store.update_data_sources('collect', 'apps', evaluated_candidates=[*observations, first])
    assert saved['data_sources'][0]['evaluated_candidates'] == [item.to_dict() for item in observations]
    store.save_script(replace(asset(api), data_sources=()), 'removed')
    assert store.get_script('collect')['data_sources'] == []
    fresh = store.save_script(asset(api), 'recreated')
    assert fresh['data_sources'][0]['evaluated_candidates'] == []


# LLM: 统一密钥形状识别不能被短前缀替换；locale和不透明引用名不是实际密钥。
# 函数用途: 复现独审指出的sk-普通文本误拒，保留长密钥拒绝用例。
def test_short_sk_prefix_is_not_a_raw_secret(api):
    endpoint = 'https://example.test/sk-SK/docs'
    assert api.AccessTarget('docs', 'http', endpoint).endpoint == endpoint
    assert api.CredentialBindingRef('vault:team/sk-prod').credential_ref == 'vault:team/sk-prod'


# LLM: 直接测资产权威层，不让引擎子类的额外检查掩盖存储契约缺口；无IO。
# 函数用途: 验证非法错误策略在构造和JSON恢复时均被拒绝。
@pytest.mark.parametrize('policy', ['ignore', '', 'FAIL'])
def test_asset_step_rejects_unsupported_on_error(api, policy):
    values = {'step_id': 's', 'name': 'step', 'mode': 'hybrid', 'on_error': policy}
    with pytest.raises(ValueError, match=f'Unsupported on_error policy: {policy}'):
        api.WorkflowStepSpec(**values)
    with pytest.raises(ValueError, match=f'Unsupported on_error policy: {policy}'):
        api.WorkflowStepSpec.from_dict(values)


# LLM: 两个公开构造入口继续接受同一完整策略集合，资产层是新增权威；无IO。
# 函数用途: 验证三个合法策略及默认策略仍可正常往返。
@pytest.mark.parametrize('policy', ['escalate_to_agent', 'fail', 'skip'])
def test_asset_step_supported_on_error_policies(api, policy):
    assert getattr(api, 'WORKFLOW_ON_ERROR_POLICIES', None) == ('escalate_to_agent', 'fail', 'skip')
    models = importlib.import_module('agent_py_agent.agent.workflows.models')
    assert api.WORKFLOW_ON_ERROR_POLICIES == models.WORKFLOW_ON_ERROR_POLICIES
    for step_type in (api.WorkflowStepSpec, models.WorkflowStepSpec):
        step = step_type('s', 'step', 'hybrid', on_error=policy)
        assert step_type.from_dict(step.to_dict()) == step
        assert step_type('s', 'step', 'hybrid').on_error == 'escalate_to_agent'


# LLM: 老JSON没有错误字段时兼容空串，新增字符串仍必须走统一脱敏；不使用真实凭据。
# 函数用途: 验证运行摘要的旧格式恢复、严格字段类型及秘密清洗。
def test_runtime_summary_legacy_default_and_error_redaction(api):
    legacy = api.RuntimeSummary.from_dict({'last_status': 'failed', 'consecutive_failures': 1})
    assert legacy.to_dict().get('last_error') == ''
    raw = 'Bearer synthetic-summary-secret timeout'
    summary = api.RuntimeSummary(last_error=raw)
    assert summary.last_error == api.clean_json(raw) and 'synthetic-summary-secret' not in summary.last_error
    assert api.RuntimeSummary.from_dict(summary.to_dict()) == summary
    assert api.RuntimeSummary.from_dict({'last_error': raw}) == summary
    with pytest.raises(ValueError):
        api.RuntimeSummary.from_dict({'last_error': 12})


# LLM: 失败状态必须累计并继承已知错误；省略时间生成UTC，显式空时间不被替换；无IO。
# 函数用途: 验证所有失败状态的默认时间、错误脱敏和旧错误保留。
@pytest.mark.parametrize('status', ['failed', 'error', 'timeout', 'timed_out'])
def test_next_run_summary_failure_error_and_default_utc(api, status):
    module = importlib.import_module('agent_py_agent.agent.assets')
    assert 'last_error' in inspect.signature(module.next_run_summary).parameters
    previous = api.RuntimeSummary(run_count=3, consecutive_failures=1, last_error='old detail')
    before = datetime.now(timezone.utc)
    raw = 'Bearer synthetic-summary-secret timeout'
    current = module.next_run_summary(previous, status, 12.5, last_error=raw)
    observed = datetime.fromisoformat(current.last_run_at)
    assert observed.utcoffset() == timedelta(0) and before <= observed <= datetime.now(timezone.utc)
    assert current.last_status == status and current.last_duration_ms == 12.5
    assert current.run_count == 4 and current.consecutive_failures == 2
    assert current.last_error == api.clean_json(raw) and 'synthetic-summary-secret' not in current.last_error
    inherited = module.next_run_summary(current, status, 1, '', last_error='')
    assert inherited.last_run_at == '' and inherited.last_error == current.last_error
    assert inherited.run_count == 5 and inherited.consecutive_failures == 3


# LLM: 非失败状态默认清除历史错误，也允许保留调用方显式说明；原摘要不能被修改。
# 函数用途: 验证成功及其它非失败状态的归零和错误覆盖规则。
@pytest.mark.parametrize('status', ['succeeded', 'skipped', 'paused'])
def test_next_run_summary_nonfailure_clears_error(api, status):
    module = importlib.import_module('agent_py_agent.agent.assets')
    assert 'last_error' in inspect.signature(module.next_run_summary).parameters
    previous = api.RuntimeSummary(run_count=2, consecutive_failures=2, last_error='old failure')
    cleared = module.next_run_summary(previous, status, 5, '2026-10-21')
    assert cleared.to_dict() == {'last_run_at': '2026-10-21', 'last_status': status, 'last_duration_ms': 5,
                                 'run_count': 3, 'consecutive_failures': 0, 'last_error': ''}
    supplied = module.next_run_summary(previous, status, 5, last_error='--password=synthetic-summary-secret')
    assert supplied.last_error == api.clean_json('--password=synthetic-summary-secret')
    assert previous.last_error == 'old failure' and previous.consecutive_failures == 2


# LLM: 两类Store均使用真实临时资产和磁盘JSON，不以返回对象冒充持久化；无真实Owner写入。
# 函数用途: 验证错误落盘脱敏、失败继承、成功清空及新Store读回。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_stores_persist_last_error_and_clear_after_success(api, tmp_path, kind):
    store = api.ScriptStore(tmp_path) if kind == 'script' else api.WorkflowStore(tmp_path)
    parameters = inspect.signature(store.record_run_summary).parameters
    assert 'last_error' in parameters, 'Store尚未支持last_error'
    assert parameters['last_error'].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters['run_at'].default is None and parameters['last_error'].default == ''
    if kind == 'script':
        store.save_script(asset(api), 'pass')
    else:
        store.save_workflow(api.WorkflowDefinition(asset(api, 'workflow'), ()))
    path = tmp_path / f'{kind}s/collect/{kind}.json'
    store.record_run_summary('collect', status='failed', duration_ms=12.5, last_error='Bearer <redacted> timeout')
    first = json.loads(path.read_text())['runtime_summary']
    assert first['last_error'] == api.clean_json('Bearer <redacted> timeout') and first['last_error']
    raw = 'Bearer synthetic-summary-secret timeout'
    store.record_run_summary('collect', 'error', 10, '2026-10-21', last_error=raw)
    assert 'synthetic-summary-secret' not in path.read_text()
    failed = json.loads(path.read_text())['runtime_summary']
    assert failed['last_error'] == api.clean_json(raw) and failed['consecutive_failures'] == 2
    store.record_run_summary('collect', 'timeout', 8)
    inherited = json.loads(path.read_text())['runtime_summary']
    assert inherited['last_error'] == failed['last_error'] and inherited['consecutive_failures'] == 3
    store.record_run_summary('collect', status='succeeded', duration_ms=5.0)
    final = json.loads(path.read_text())['runtime_summary']
    assert final['consecutive_failures'] == 0 and final['last_error'] == '' and final['run_count'] == 4
    fresh = type(store)(tmp_path)
    restored = fresh.get_script('collect')['runtime_summary'] if kind == 'script' else fresh.get_workflow('collect').descriptor.runtime_summary.to_dict()
    assert restored == final


# LLM: 真实Runner按签名探测调用摘要入口，只执行临时脚本且关闭内核沙箱，不宣称验证平台隔离。
# 函数用途: 验证执行器失败原因确实写入Store，随后真实成功运行清除旧错误。
def test_script_runner_persists_last_error_through_signature_probe(api, tmp_path):
    from agent_py_agent.agent.workflows import ScriptRunner
    store = api.ScriptStore(tmp_path)
    store.save_script(asset(api), 'raise SystemExit(7)')
    runner = ScriptRunner(tmp_path, use_sandbox=False)
    result = runner.run_script('collect')
    assert not result.ok and result.error_code == 'SCRIPT_EXIT_NONZERO'
    summary = api.ScriptStore(tmp_path).get_script('collect')['runtime_summary']
    assert summary.get('last_error') == result.error_code and summary['consecutive_failures'] == 1
    store.save_script(asset(api), 'print("{}")')
    assert runner.run_script('collect').ok
    final = api.ScriptStore(tmp_path).get_script('collect')['runtime_summary']
    assert final['last_error'] == '' and final['consecutive_failures'] == 0 and final['run_count'] == 2


# LLM: 测试工厂仅接临时根，缺少新增入口时以契约断言报红，不把TypeError冒充功能反例。
# 函数用途: 为两类存储统一构造Owner或管理员测试实例。
def scoped_store(api, home, kind, *, admin=False, **options):
    store_type = api.ScriptStore if kind == 'script' else api.WorkflowStore
    if admin:
        assert callable(getattr(store_type, 'for_global_admin', None)), '尚未实现全局管理员存储入口'
        return store_type.for_global_admin(home, **options)
    assert set(options) <= inspect.signature(store_type).parameters.keys(), '尚未实现分层存储构造参数'
    return store_type(home, **options)


# LLM: 测试资产仅含无网络JSON输出或空操作步骤；保存始终走真实公开Store入口。
# 函数用途: 为脚本和工作流生成同形的层级遮蔽样本。
def save_scoped_asset(api, store, kind, asset_id='shared', version='global'):
    descriptor = asset(api, kind, asset_id, version=version)
    if kind == 'script':
        return store.save_script(descriptor, 'print("{}")')
    return store.save_workflow(api.WorkflowDefinition(descriptor, (api.WorkflowStepSpec('s', 'step', 'hybrid'),)))


# LLM: 只统一返回对象形状，不重新读取磁盘或改动生产契约。
# 函数用途: 抽取两类记录的标准描述符供断言复用。
def scoped_descriptor(record):
    return record if isinstance(record, dict) else record.descriptor.to_dict()


# LLM: 只观察pytest临时全局集合，包含字节、mtime和权限；读回相同字节不能掩盖chmod或重复写入。
# 函数用途: 核对普通读取与更新尝试完全没有改写全局集合。
def collection_snapshot(path):
    if not path.exists():
        return None
    return {str(item.relative_to(path)): (item.stat().st_mode, item.stat().st_mtime_ns,
            item.read_bytes() if item.is_file() else None) for item in (path, *path.rglob('*'))}


# LLM: is_admin必须是宿主传入的精确True，不接受真假等价值；拒绝时不能创建目标根。
# 函数用途: 验证管理员入口拒绝普通身份且不会先创建全局目录。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
@pytest.mark.parametrize('flag', [False, None, 0, 1, 'true'])
def test_global_admin_rejects_non_true_authority(api, tmp_path, kind, flag):
    root = tmp_path / 'not-created'
    store_type = api.ScriptStore if kind == 'script' else api.WorkflowStore
    assert callable(getattr(store_type, 'for_global_admin', None))
    with pytest.raises(PermissionError, match='仅管理员可修改全局脚本或工作流'):
        store_type.for_global_admin(root, is_admin=flag)
    assert not root.exists()


# LLM: 本地与飞书Owner布局均自动寻找owners父根；用户写删只影响覆盖副本，不暴露其它Owner。
# 函数用途: 验证自动兜底、作用域标记、稳定合并、同ID遮蔽及删除后恢复全局视图。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
@pytest.mark.parametrize('relative', ['owners/local/main', 'owners/providers/feishu/users/u1', 'owners/legacy'])
def test_global_auto_fallback_owner_override_and_delete(api, tmp_path, kind, relative):
    global_store = scoped_store(api, tmp_path, kind)
    asset_id = 'global_ping' if kind == 'script' else 'global_wf'
    save_scoped_asset(api, global_store, kind, asset_id)
    save_scoped_asset(api, global_store, kind, 'alpha')
    user = scoped_store(api, tmp_path / relative, kind)
    get, listing, delete = getattr(user, f'get_{kind}'), getattr(user, f'list_{kind}s'), getattr(user, f'delete_{kind}')
    shared = get(asset_id)
    assert shared is not None, 'Owner尚不能读取默认全局资产'
    assert scoped_descriptor(shared)['metadata']['asset_scope'] == 'global'
    assert [scoped_descriptor(row)['id'] for row in listing()] == ['alpha', asset_id]
    before = collection_snapshot(tmp_path / f'{kind}s')
    save_scoped_asset(api, user, kind, asset_id, 'owner')
    assert scoped_descriptor(get(asset_id))['version'] == 'owner'
    assert scoped_descriptor(get(asset_id))['metadata'].get('asset_scope', 'owner') == 'owner'
    assert [scoped_descriptor(row)['version'] for row in listing()] == ['global', 'owner']
    other = scoped_store(api, tmp_path / 'owners/local/other', kind)
    assert scoped_descriptor(getattr(other, f'get_{kind}')(asset_id))['version'] == 'global'
    assert delete(asset_id) is True and delete(asset_id) is False
    assert scoped_descriptor(get(asset_id))['version'] == 'global'
    assert collection_snapshot(tmp_path / f'{kind}s') == before
    assert scoped_descriptor(getattr(global_store, f'get_{kind}')(asset_id))['version'] == 'global'


# LLM: 普通更新必须使用本Owner必需记录；运行摘要遇共享资产只能返回只读投影，不触碰全局锁权限或字节。
# 函数用途: 验证全局资产的普通删除、源更新、技能挂载和摘要都不跨界写入。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_global_readonly_update_and_summary_do_not_write(api, tmp_path, kind):
    publisher = scoped_store(api, tmp_path, kind)
    save_scoped_asset(api, publisher, kind)
    collection = tmp_path / f'{kind}s'
    (collection / '.assets.lock').chmod(0o644)
    before = collection_snapshot(collection)
    user = scoped_store(api, tmp_path / 'owners/providers/feishu/users/u1', kind)
    shared = getattr(user, f'get_{kind}')('shared')
    assert shared is not None, '缺少全局只读读取'
    assert getattr(user, f'delete_{kind}')('shared') is False
    with pytest.raises(KeyError):
        if kind == 'script':
            user.update_data_sources('shared', 'apps', query_scope={'new': True})
        else:
            user.attach_skill_to_step('shared', 's', 'skill-new')
    returned = user.record_run_summary('shared', 'failed', 12, last_error='synthetic failure')
    assert scoped_descriptor(returned)['runtime_summary']['run_count'] == 0
    assert scoped_descriptor(returned)['metadata']['asset_scope'] == 'global'
    assert getattr(user, f'list_{kind}s')()
    assert collection_snapshot(collection) == before
    with pytest.raises(KeyError):
        user.record_run_summary('absent', 'failed', 1)


# LLM: 全局写入口依赖可信宿主身份布尔值，不自行认证；管理员的原事务、源更新和挂载能力仍可用。
# 函数用途: 验证显式管理员可以完整创建、更新、记摘要和删除全局资产。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_global_admin_can_publish_update_and_delete(api, tmp_path, kind):
    admin = scoped_store(api, tmp_path, kind, admin=True, is_admin=True)
    save_scoped_asset(api, admin, kind, 'shared', '1')
    save_scoped_asset(api, admin, kind, 'shared', '2')
    if kind == 'script':
        admin.update_data_sources('shared', 'apps', query_scope={'region': 'west'})
    else:
        admin.attach_skill_to_step('shared', 's', 'skill-new')
    admin.record_run_summary('shared', 'failed', 12, last_error='synthetic failure')
    user = scoped_store(api, tmp_path / 'owners/local/main', kind)
    descriptor = scoped_descriptor(getattr(user, f'get_{kind}')('shared'))
    assert descriptor['version'] == '2' and descriptor['runtime_summary']['run_count'] == 1
    assert descriptor['metadata']['asset_scope'] == 'global'
    if kind == 'script':
        assert descriptor['data_sources'][0]['query_scope']['region'] == 'west'
    else:
        assert descriptor['refs']['skills'] == ['skill-new']
    assert getattr(admin, f'delete_{kind}')('shared') is True
    assert getattr(user, f'get_{kind}')('shared') is None


# LLM: 默认全局先于显式extra，同ID只读取首个来源；缺失、重复和Owner自身根不创建或递归查询。
# 函数用途: 验证额外只读根的稳定优先级、去重和被遮蔽坏记录不干扰有效列表。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_global_extra_roots_order_dedup_and_shadowed_corruption(api, tmp_path, kind):
    default, first, second, missing = (tmp_path / name for name in ('default', 'first', 'second', 'absent'))
    for root, version in ((default, 'default'), (first, 'first'), (second, 'second')):
        save_scoped_asset(api, scoped_store(api, root, kind), kind, version=version)
    save_scoped_asset(api, scoped_store(api, first, kind), kind, 'alpha')
    (second / f'{kind}s/shared/{kind}.json').write_text('{broken')
    home = default / 'owners/local/main'
    user = scoped_store(api, home, kind, extra_roots=(missing, first, first, second, home), is_admin=False)
    assert scoped_descriptor(getattr(user, f'get_{kind}')('shared'))['version'] == 'default'
    assert [scoped_descriptor(row)['id'] for row in getattr(user, f'list_{kind}s')()] == ['alpha', 'shared']
    assert getattr(scoped_store(api, default, kind), f'delete_{kind}')('shared') is True
    assert scoped_descriptor(getattr(user, f'get_{kind}')('shared'))['version'] == 'first'
    assert [scoped_descriptor(row)['id'] for row in getattr(user, f'list_{kind}s')()] == ['alpha', 'shared']
    arbitrary = scoped_store(api, tmp_path / 'arbitrary-owner', kind, extra_roots=(first,))
    assert scoped_descriptor(getattr(arbitrary, f'get_{kind}')('shared'))['version'] == 'first'
    assert not missing.exists()


# LLM: 默认全局不存在或为空时不创建集合、锁或候选根；本Owner原读写语义不变。
# 函数用途: 验证空全局零侵入及Owner普通资产的完整读回。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
@pytest.mark.parametrize('empty_collection', [False, True])
def test_global_absent_or_empty_is_noninvasive(api, tmp_path, kind, empty_collection):
    collection, missing = tmp_path / f'{kind}s', tmp_path / 'missing-extra'
    if empty_collection:
        collection.mkdir()
    before = collection_snapshot(collection)
    user = scoped_store(api, tmp_path / 'owners/local/main', kind, extra_roots=(missing,))
    assert getattr(user, f'get_{kind}')('absent') is None
    assert getattr(user, f'list_{kind}s')() == []
    save_scoped_asset(api, user, kind, version='owner')
    assert scoped_descriptor(getattr(user, f'get_{kind}')('shared'))['version'] == 'owner'
    assert collection_snapshot(collection) == before and not missing.exists()


# LLM: 全局读口必须与Owner同样逐段nofollow；拒绝链接时不得触碰哨兵或返回伪装缺失。
# 函数用途: 验证全局集合、资产目录和元数据叶符号链接拒绝。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
@pytest.mark.parametrize('level', ['collection', 'asset', 'metadata'])
def test_global_symlink_paths_are_rejected(api, tmp_path, kind, level):
    publisher = scoped_store(api, tmp_path, kind)
    save_scoped_asset(api, publisher, kind)
    collection = tmp_path / f'{kind}s'
    leaf = collection if level == 'collection' else collection / 'shared' if level == 'asset' else collection / f'shared/{kind}.json'
    target = tmp_path / 'linked-target'
    leaf.rename(target)
    leaf.symlink_to(target, target_is_directory=level != 'metadata')
    before = collection_snapshot(target)
    user = scoped_store(api, tmp_path / 'owners/local/main', kind)
    for read in (lambda: getattr(user, f'get_{kind}')('shared'), lambda: getattr(user, f'list_{kind}s')()):
        with pytest.raises((OSError, ValueError)):
            read()
    assert collection_snapshot(target) == before


# LLM: 真实硬链接创建若被权限拒绝，只注入nlink=2核验原拒绝分支，不冒称真实硬链接验证；符号链接使用实物。
# 函数用途: 验证全局代码链接拒绝，硬链接夹具不可建时保留精确元数据分支覆盖。
@pytest.mark.parametrize('link_kind', ['symlink', 'hardlink'])
def test_global_script_code_rejects_links(api, tmp_path, monkeypatch, record_property, link_kind):
    import os

    from agent_py_agent.agent.common import nofollow_fs
    publisher = scoped_store(api, tmp_path, 'script')
    save_scoped_asset(api, publisher, 'script')
    path, target = tmp_path / 'scripts/shared/main.py', tmp_path / 'code-target'
    path.rename(target)
    if link_kind == 'symlink':
        path.symlink_to(target)
    else:
        try:
            os.link(target, path)
            record_property('hardlink_observation', 'native')
        except PermissionError as error:
            record_property('hardlink_observation', f'metadata_injection_after_errno_{error.errno}')
            path.write_bytes(target.read_bytes())
            original_stat = os.stat

            # LLM: 只给目标代码的nofollow预检注入nlink=2，其它真实stat及所有生产拒绝逻辑保持不变。
            # 函数用途: 在无法创建硬链接的运行中核验相同的链接数拒绝条件。
            def linked_metadata(name, *args, **kwargs):
                metadata = original_stat(name, *args, **kwargs)
                if name == 'main.py' and kwargs.get('follow_symlinks') is False:
                    values = list(metadata)
                    values[3] = 2
                    return os.stat_result(values)
                return metadata

            monkeypatch.setattr(nofollow_fs.os, 'stat', linked_metadata)
    user = scoped_store(api, tmp_path / 'owners/local/main', 'script')
    with pytest.raises((OSError, ValueError)):
        user.get_script('shared')
    assert target.read_text() == 'print("{}")'


# LLM: 根本身也必须nofollow，不因是宿主显式extra就接受符号链接；没有额外写权。
# 函数用途: 验证额外全局根不能通过链接绕开根校验。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_global_extra_root_symlink_is_rejected(api, tmp_path, kind):
    root, link = tmp_path / 'root', tmp_path / 'linked-root'
    save_scoped_asset(api, scoped_store(api, root, kind), kind)
    link.symlink_to(root, target_is_directory=True)
    before = collection_snapshot(root)
    user = scoped_store(api, tmp_path / 'owner', kind, extra_roots=(link,))
    with pytest.raises((OSError, ValueError)):
        getattr(user, f'get_{kind}')('shared')
    assert collection_snapshot(root) == before


# LLM: 只读兜底不能执行undo恢复；未提交事务显式失败且留给管理员修复，不把混合版本当成功。
# 函数用途: 验证全局事务挂起时读取不修改任何字节，管理员仍能沿原机制恢复。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_global_pending_transaction_requires_admin_recovery(api, tmp_path, kind):
    publisher = scoped_store(api, tmp_path, kind)
    save_scoped_asset(api, publisher, kind)
    collection = tmp_path / f'{kind}s'
    metadata = collection / f'shared/{kind}.json'
    journal = metadata.with_name('asset-transaction.json')
    journal.write_text(json.dumps({'schema_version': 'asset_transaction.v1', 'previous': {metadata.name: metadata.read_text()}}))
    before = collection_snapshot(collection)
    user = scoped_store(api, tmp_path / 'owners/local/main', kind)
    for read in (lambda: getattr(user, f'get_{kind}')('shared'), lambda: getattr(user, f'list_{kind}s')()):
        with pytest.raises(ValueError, match='全局.*事务'):
            read()
    assert collection_snapshot(collection) == before and journal.is_file()
    admin = scoped_store(api, tmp_path, kind, admin=True)
    assert getattr(admin, f'get_{kind}')('shared') is not None
    assert not journal.exists() and getattr(user, f'get_{kind}')('shared') is not None


# LLM: 本Owner坏记录不能伪装成缺失而回退全局；低优先级坏记录不能覆盖高优先级有效副本。
# 函数用途: 验证Owner损坏明确失败和Owner遮蔽全局坏JSON的边界。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_global_fallback_does_not_hide_owner_corruption(api, tmp_path, kind):
    save_scoped_asset(api, scoped_store(api, tmp_path, kind), kind)
    home = tmp_path / 'owners/local/main'
    user = scoped_store(api, home, kind)
    save_scoped_asset(api, user, kind, version='owner')
    global_path, owner_path = (root / f'{kind}s/shared/{kind}.json' for root in (tmp_path, home))
    original = owner_path.read_text()
    owner_path.write_text('{broken')
    with pytest.raises(ValueError):
        getattr(user, f'get_{kind}')('shared')
    owner_path.write_text(original)
    global_path.write_text('{broken')
    assert scoped_descriptor(getattr(user, f'get_{kind}')('shared'))['version'] == 'owner'
    assert [scoped_descriptor(row)['version'] for row in getattr(user, f'list_{kind}s')()] == ['owner']


# LLM: 只执行临时发布的无网络脚本；关闭平台沙箱，仅验证全局加载、Owner冻结执行及摘要不越权。
# 函数用途: 验证真实Runner运行全局脚本后仍不修改共享元数据或代码。
def test_global_script_runner_reads_without_global_summary_write(api, tmp_path):
    from agent_py_agent.agent.workflows import ScriptRunner
    save_scoped_asset(api, scoped_store(api, tmp_path, 'script'), 'script')
    before = collection_snapshot(tmp_path / 'scripts')
    home = tmp_path / 'owners/local/main'
    user = scoped_store(api, home, 'script')
    assert user.get_script('shared') is not None, '缺少全局读取入口'
    result = ScriptRunner(home, use_sandbox=False).run_script('shared')
    assert result.ok and result.output_data == {}
    assert collection_snapshot(tmp_path / 'scripts') == before
    assert not (home / 'scripts/shared').exists()


# LLM: 真实替换临时全局锁叶模拟等待后改名及首次发布竞态；不得把失效锁下的读取当一致快照。
# 函数用途: 验证全局锁缺失后出生或读取前后换身份时明确拒绝本次结果。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
@pytest.mark.parametrize('change', ['born_during_read', 'replaced_after_lock', 'replaced_during_read'])
def test_global_lock_conflicts_fail_closed(api, tmp_path, monkeypatch, kind, change):
    module = importlib.import_module('agent_py_agent.agent.assets')
    save_scoped_asset(api, scoped_store(api, tmp_path, kind), kind)
    lock = tmp_path / f'{kind}s/.assets.lock'
    user = scoped_store(api, tmp_path / 'owners/local/main', kind)
    if change == 'born_during_read':
        lock.unlink()
    original_read, original_flock = module._AssetStore._read, module._flock_descriptor

    # LLM: 仅替换测试共享锁的真实inode，保留数据及其它锁不变；复现不能依赖随机sleep。
    # 函数用途: 制造可确定观测到的全局锁身份变化。
    def replace_lock():
        replacement = lock.with_name('.replacement-lock')
        replacement.write_text('')
        replacement.replace(lock)

    # LLM: 只拦全局只读视图的元数据读取，在结果返回前模拟新锁或替换，不替代生产解码。
    # 函数用途: 稳定复现未持锁读取或持锁期间的锁叶变化。
    def read_during_change(store, asset_id):
        record = original_read(store, asset_id)
        if getattr(store, '_read_only', False) and change != 'replaced_after_lock':
            replace_lock()
        return record

    # LLM: 只拦只读锁的已取得时刻，模拟等待期间名字已换，原flock仍真实执行。
    # 函数用途: 验证拿到旧锁FD后必须重新核对路径身份。
    def lock_after_change(descriptor):
        original_flock(descriptor)
        if change == 'replaced_after_lock':
            replace_lock()

    monkeypatch.setattr(module._AssetStore, '_read', read_during_change)
    monkeypatch.setattr(module, '_flock_descriptor', lock_after_change)
    with pytest.raises(BlockingIOError, match='全局.*锁'):
        getattr(user, f'get_{kind}')('shared')


# LLM: 读取抛错也必须核锁；锁未变保留真实事务错误，锁出生或换身份则提供重试信号，不执行恢复写。
# 函数用途: 复现独审发现的异常出口跳过锁复核问题。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
@pytest.mark.parametrize('change', ['stable', 'born_during_read', 'replaced_during_read'])
def test_global_lock_change_is_checked_when_read_raises(api, tmp_path, monkeypatch, kind, change):
    module = importlib.import_module('agent_py_agent.agent.assets')
    save_scoped_asset(api, scoped_store(api, tmp_path, kind), kind)
    collection = tmp_path / f'{kind}s'
    lock, journal = collection / '.assets.lock', collection / 'shared/asset-transaction.json'
    if change == 'born_during_read':
        lock.unlink()
    original_read = module._AssetStore._read

    # LLM: 准确在全局元数据读取前模拟锁及undo出生，随后仍走原_read抛错，不伪造异常类型。
    # 函数用途: 触发需要后置锁复核区分的正在发布与残留事务。
    def read_with_pending_transaction(store, asset_id):
        if getattr(store, '_read_only', False):
            if change != 'stable':
                replacement = lock.with_name('.new-lock')
                replacement.write_text('')
                replacement.replace(lock)
            journal.write_text('pending')
        return original_read(store, asset_id)

    monkeypatch.setattr(module._AssetStore, '_read', read_with_pending_transaction)
    user = scoped_store(api, tmp_path / 'owners/local/main', kind)
    expected = ValueError if change == 'stable' else BlockingIOError
    with pytest.raises(expected, match='全局.*(?:锁|事务)'):
        getattr(user, f'get_{kind}')('shared')
    assert journal.read_text() == 'pending'
