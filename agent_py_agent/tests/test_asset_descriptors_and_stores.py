# LLM: 全部持久化仅使用tmp_path；只跑本片真实描述符/存储/图入口，不接模型或真实Owner。
# 模块用途: 锁住统一资产协议、原子保存、安全路径、数据源更新、运行计数和拓扑反向引用。
import importlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

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
                       "run_count": 10, "consecutive_failures": 0}
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
