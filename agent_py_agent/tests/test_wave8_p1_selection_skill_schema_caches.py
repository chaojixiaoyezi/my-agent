# LLM: 在临时规范文件上回归缓存收益与原安全合同；不访问真实Owner，不执行Skill脚本。
# 模块用途: 验证位置索引惰性字段、共享Skill失效和Schema隔离缓存。
from __future__ import annotations

import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.backends import tool_schema
from agent_py_agent.agent.capability import skill_service as skills
from agent_py_agent.agent.contracts.tool_input_schema import EXCLUSIVE_ARGUMENT_GROUPS_KEY
from agent_py_agent.agent.conversation import message_selection as selection
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.tooling.models import ToolModelSpec


# LLM: 只创建测试消息文件和轻量存储端口；完整选择与重放仍由生产代码执行。
# 函数用途: 不构建Agent就准备带嵌套元数据的JSONL来源。
def _messages(tmp_path, name="thread", count=3):
    path = tmp_path / f"{name}.jsonl"
    rows = tuple(MessageLogEntry.from_dict({
        "message_id": f"m-{i}", "thread_id": name, "role": "user", "content": "original",
        "metadata": {"nested": {"values": [i]}},
    }) for i in range(count))
    path.write_text("".join(json.dumps(row.to_dict()) + "\n" for row in rows))
    messages = SimpleNamespace(
        storage=SimpleNamespace(message_path=lambda _: path),
        complete_offset_report=lambda _: (path.stat().st_size, []),
    )
    return path, messages, rows


# LLM: 独立缓存字典仅用于测试隔离，不关闭缓存或改变实际容量/安全策略。
# 函数用途: 防止同进程其他测试的已缓存记录影响计数。
@pytest.fixture(autouse=True)
def _isolated_caches(monkeypatch):
    monkeypatch.setattr(selection, "_POSITION_CACHE", selection.OrderedDict())
    for module, names in ((skills, ("_PROCESS_SKILL_DIR_CACHE", "_PROCESS_BUILTIN_ROOT_CACHE")),
                          (tool_schema, ("_STRIPPED_SCHEMA_CACHE",))):
        for name in names:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, {})


# LLM: 32个真实小文件均在原字节预算内，不修改生产容量常量。
# 函数用途: 防止位置缓存重新退化为16线程抖动。
def test_position_cache_retains_32_small_threads(tmp_path):
    indexes = []
    for i in range(32):
        path, _, _ = _messages(tmp_path, f"thread-{i}", count=1)
        indexes.append(selection.message_position_index(path, path.stat().st_size))
    assert len(selection._POSITION_CACHE) == 32
    assert all(selection._POSITION_CACHE[index.identity] is index for index in indexes)
    assert sum(index.size_bytes for index in indexes) < selection._POSITION_CACHE_MAX_BYTES


# LLM: 观测真实deepcopy并修改嵌套字段，缓存投影必须保持原值。
# 函数用途: 验证首字段触发且仅触发一次隔离副本。
def test_selection_row_copies_only_on_first_field_access(tmp_path, monkeypatch):
    path, _, rows = _messages(tmp_path)
    index = selection.message_position_index(path, path.stat().st_size)
    spy = Mock(wraps=copy.deepcopy)
    monkeypatch.setattr(selection.copy, "deepcopy", spy)
    with path.open("rb") as handle:
        proxy = selection._SelectionRow(handle, index.entries[0])
        assert spy.call_count == 0
        proxy.metadata["nested"]["values"].append("private")
        assert spy.call_count == 1
        assert proxy.role == "user" and proxy.content == rows[0].content
        assert spy.call_count == 1
    assert index.entries[0].row.metadata == rows[0].metadata


# LLM: predicate不触碰字段时不应产生副本，仍通过真实选择器完成筛选。
# 函数用途: 锁定短路选择的零深拷贝收益。
def test_short_circuit_predicate_never_deepcopies_light_rows(tmp_path, monkeypatch):
    _, messages, _ = _messages(tmp_path)
    spy = Mock(wraps=copy.deepcopy)
    monkeypatch.setattr(selection.copy, "deepcopy", spy)
    selected = selection.select_message_snapshot(messages, "thread", selector_factory=lambda _: lambda row: False)
    assert len(selected) == 0
    assert spy.call_count == 0


# LLM: 分别观测冷扫描与热前缀验证，factory后和final全hash都不能省掉。
# 函数用途: 锁定无interrupt的已有摘要复用与完全一致的选择结果。
@pytest.mark.parametrize("warm", [False, True])
def test_no_interrupt_reuses_verified_index_before_factory(tmp_path, monkeypatch, warm):
    path, messages, rows = _messages(tmp_path)
    if warm:
        selection.message_position_index(path, path.stat().st_size)
    spy = Mock(wraps=selection._prefix_hasher)
    monkeypatch.setattr(selection, "_prefix_hasher", spy)
    calls_at_factory = []

    # LLM: 观察真实hash调用位置，不替代其结果或削弱回调后的完整检查。
    # 函数用途: 锁定无取消回调时索引摘要已足够覆盖进入选择器之前的验证。
    def factory(positions):
        calls_at_factory.append(spy.call_count)
        return lambda row: positions[row.message_id] % 2 == 0

    snapshot = selection.select_message_snapshot(messages, "thread", selector_factory=factory)
    assert calls_at_factory == [int(warm)]
    assert spy.call_count == int(warm) + 2  # factory后与返回前仍各做一次全hash
    assert tuple(snapshot) == rows[::2]


# LLM: 三种回调边界与三种真实来源修改组合均必须失败关闭。
# 函数用途: 防止性能缓存掩盖截断、同长度改写或inode替换。
@pytest.mark.parametrize("phase", ["factory", "predicate", "interrupt"])
@pytest.mark.parametrize("mutation", ["rewrite", "truncate", "replace"])
def test_callbacks_cannot_rebind_or_tamper_source(tmp_path, phase, mutation):
    path, messages, _ = _messages(tmp_path)
    original = path.read_bytes()
    mutated = []

    # LLM: 仅修改测试来源，包含同长度改写和相同字节的新inode；不靠尾锚放行。
    # 函数用途: 在不同回调边界模拟来源变化。
    def mutate():
        if mutated:
            return False
        mutated.append(True)
        if mutation == "replace":
            other = path.with_suffix(".new")
            other.write_bytes(original)
            other.replace(path)
        else:
            path.write_bytes(original[:-1] if mutation == "truncate" else original.replace(b"original", b"tampered", 1))
        return False

    # LLM: factory与predicate各在指定阶段调用一次修改端口，不伪造校验结果。
    # 函数用途: 把同一来源变化安排到不同回调阶段。
    def factory(_):
        if phase == "factory":
            mutate()
        return lambda row: not mutate() if phase == "predicate" else True

    with pytest.raises(DataCorruptionError):
        selection.select_message_snapshot(messages, "thread", selector_factory=factory,
            interrupt_check=mutate if phase == "interrupt" else None)


# LLM: 只创建临时方法和不执行的脚本；真实guard负责判断修改后的脚本。
# 函数用途: 准备所有来源可复用的安全Skill。
def _skill(root, description="original"):
    path = root / "method" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: method\ndescription: {description}\ntags: [one, two]\n---\n正文\n")
    helper = path.parent / "scripts" / "helper.sh"
    helper.parent.mkdir()
    helper.write_text("echo safe\n")
    return path, helper


# LLM: 每个服务保留独立Owner策略；只把builtin/shared路径指向共同测试目录。
# 函数用途: 模拟多个Owner共享只读叶子但不共享enabled策略。
def _service(tmp_path, owner="a", disabled=()):
    policy = SimpleNamespace(owner_id=owner, skills_enabled=True,
        enabled_skill_sources=("workspace", "owner", "shared", "builtin"),
        disabled_skills=disabled, enabled_shared_skills=())
    home = SimpleNamespace(owner_home_dir=tmp_path / owner,
        shared_skills_dir=tmp_path / "shared", shared_builtin_dir=tmp_path / "builtin")
    return skills.SkillsService(home_paths=home, workspace_root=tmp_path / owner / "workspace",
        policy_provider=lambda: policy)


# LLM: 发现后真实替换正文，解析必须继续使用manifest文本而非二读后的新卡片。
# 函数用途: 复现并锁定Skill正文hash与元数据的一致性。
def test_manifest_hash_and_card_use_the_same_frozen_text(tmp_path, monkeypatch):
    path, _ = _skill(tmp_path / "owner")
    root = skills.SkillRoot(tmp_path / "owner", "owner")
    read = Mock(wraps=skills.read_skill_text)
    monkeypatch.setattr(skills, "read_skill_text", read)
    item, error = skills._manifest_item(root, root.path.resolve(), path)
    assert error is None and read.call_count == 1
    path.write_text(path.read_text().replace("original", "tampered"))
    entry, error = skills._entry_from_manifest(item, _service(tmp_path).policy_provider())
    assert error is None
    assert entry.description == "original"
    assert entry.content_sha256 == skills.skill_content_sha256(item.text)
    assert read.call_count == 1


# LLM: 用原文件parser作元数据合同对照，包含false/0而非只测常见字符串。
# 函数用途: 保证单次文本解析不悄悄改变现有卡片字段。
@pytest.mark.parametrize("category", ["false", "0", "null", '""', "research/sub"])
def test_frozen_text_parser_preserves_original_card_fields(tmp_path, category):
    from agent_py_agent.agent.capability.skills import parse_skill_file

    path, _ = _skill(tmp_path / "private")
    path.write_text(path.read_text().replace("tags:", f"category: {category}\ntags:"))
    root = skills.SkillRoot(tmp_path / "private", "owner")
    item, error = skills._manifest_item(root, root.path.resolve(), path)
    assert error is None
    original = parse_skill_file(path, source="owner", require_frontmatter=True)
    assert skills._card_from_manifest_text(item) == original


# LLM: 两个Owner共用公开方法字节和guard，禁用策略必须分别计算。
# 函数用途: 同时验证进程复用和Owner启停隔离。
@pytest.mark.parametrize("source", ["builtin", "shared"])
def test_public_cache_reuses_manifest_parse_and_guard_but_not_owner_policy(tmp_path, monkeypatch, source):
    _skill(tmp_path / source)
    read, guard = Mock(wraps=skills.read_skill_text), Mock(wraps=skills.evaluate_skill_guard_gate)
    monkeypatch.setattr(skills, "read_skill_text", read)
    monkeypatch.setattr(skills, "evaluate_skill_guard_gate", guard)
    a, b = _service(tmp_path), _service(tmp_path, "b", disabled=("method",))
    first, second = a.snapshot_for(), b.snapshot_for()
    assert first.resolve("method").description == "original"
    assert second.resolve("method") is None
    assert read.call_count == guard.call_count == 1
    item_a = next(item for item in a._manifest_cache[2] if item.root.source == source)
    item_b = next(item for item in b._manifest_cache[2] if item.root.source == source)
    assert item_a is item_b


# LLM: 不改SKILL.md，使用真实guard验证普通、隐藏和新脚本都参与失效。
# 函数用途: 防止仅按正文stat缓存安全判定。
@pytest.mark.parametrize("source", ["builtin", "shared"])
@pytest.mark.parametrize("resource", ["scripts/helper.sh", ".hidden/deep.sh", "scripts/new.sh"])
def test_script_change_invalidates_guard_with_unchanged_skill_md(tmp_path, monkeypatch, source, resource):
    path, helper = _skill(tmp_path / source)
    target = path.parent / resource
    if resource.startswith(".hidden"):
        target.parent.mkdir(exist_ok=True)
        target.write_text("echo safe\n")
    service = _service(tmp_path)
    guard = Mock(wraps=skills.evaluate_skill_guard_gate)
    monkeypatch.setattr(skills, "evaluate_skill_guard_gate", guard)
    assert service.snapshot_for().resolve("method") is not None
    before = path.stat()
    target.parent.mkdir(exist_ok=True)
    if target == helper:
        stamp = target.stat()
        target.write_text("eval('x')\n")  # 与原脚本同长度；mtime复原后ctime仍须使缓存失效
        os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    else:
        target.write_text("eval('x')\n")
    snapshot = service.snapshot_for()
    assert guard.call_count == 2
    assert (path.stat().st_mtime_ns, path.stat().st_size) == (before.st_mtime_ns, before.st_size)
    if source == "shared":
        assert snapshot.resolve("method") is None
        assert any(error.code == "SKILL_GUARD_DENIED" for error in snapshot.errors)


# LLM: 签名命中省遍历但不省后代变更检测，tmp builtin必须与正式包根同样正确。
# 函数用途: 验证根目录清单缓存不依赖根mtime猜测子文件不变。
def test_builtin_signature_cache_observes_nested_change_without_rglob_on_hit(tmp_path, monkeypatch):
    path, _ = _skill(tmp_path / "builtin")
    root = skills.SkillRoot(tmp_path / "builtin", "builtin")
    before = skills._roots_signature((root,))
    rglob = Mock(wraps=Path.rglob)
    # 用原始方法包装描述符，保留真实路径遍历行为。
    original = Path.rglob
    monkeypatch.setattr(Path, "rglob", lambda self, *a, **kw: rglob(self, *a, **kw))
    rglob.side_effect = original
    assert skills._roots_signature((root,)) == before
    assert rglob.call_count == 0
    path.write_text(path.read_text().replace("original", "modified"))
    assert skills._roots_signature((root,)) != before


# LLM: 显式刷新不能被进程缓存吞掉，观察原guard的实际调用。
# 函数用途: 保留force与clear的重读安全检查语义。
def test_force_reload_and_clear_cache_rescan_public_skill(tmp_path, monkeypatch):
    _skill(tmp_path / "shared")
    service = _service(tmp_path)
    guard = Mock(wraps=skills.evaluate_skill_guard_gate)
    monkeypatch.setattr(skills, "evaluate_skill_guard_gate", guard)
    assert service.snapshot_for().resolve("method")
    assert service.snapshot_for(force_reload=True).resolve("method")
    service.clear_cache()
    assert service.snapshot_for().resolve("method")
    assert guard.call_count == 3


# LLM: 多线程真实服务并行发现，公开manifest对象应一致，Owner快照仍各自建立。
# 函数用途: 检查冷启动并发不会留下多份公开正文副本。
def test_public_manifest_is_shared_under_parallel_owner_scans(tmp_path):
    _skill(tmp_path / "builtin")
    services = [_service(tmp_path, f"owner-{i}") for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(lambda service: service.snapshot_for(), services))
    assert all(snapshot.resolve("method").description == "original" for snapshot in snapshots)
    items = [next(item for item in service._manifest_cache[2] if item.root.source == "builtin") for service in services]
    assert all(item is items[0] for item in items)


# LLM: 静态独审提出的枚举/stat窗口须用实际文件追加重现，不能只断言缓存键形式。
# 函数用途: 防止builtin缓存永久漏掉扫描期间加入的文件。
def test_builtin_scan_does_not_permanently_miss_file_added_during_enumeration(tmp_path, monkeypatch):
    root = tmp_path / "builtin"
    directory = root / "method"
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    original = skills._path_stat_signature
    created = []

    # LLM: 在目录首次stat之前插入真实文件，复现先rglob后stat可能把缺项清单永久缓存的窗口。
    # 函数用途: 模拟扫描期间的追加，而不是伪造stat或目录清单。
    def add_before_directory_stat(candidate):
        if candidate == directory and not created:
            created.append(True)
            path.write_text("---\nname: method\ndescription: added\n---\nbody\n")
        return original(candidate)

    monkeypatch.setattr(skills, "_path_stat_signature", add_before_directory_stat)
    skill_root = skills.SkillRoot(root, "builtin")
    skills._roots_signature((skill_root,))
    items, errors = skills._discover_root(skill_root)
    assert created and not errors
    assert [item.path for item in items] == [path]


# LLM: 两个本根内合法别名解析到同目录不同文件，不能错误共用一张冻结卡片。
# 函数用途: 验证目录缓存身份还绑定准确正文路径。
def test_public_directory_key_distinguishes_two_resolved_skill_files(tmp_path):
    root = tmp_path / "shared"
    target = root / "target"
    target.mkdir(parents=True)
    for name in ("alpha", "beta"):
        path = target / f"{name}.md"
        path.write_text(f"---\nname: {name}\ndescription: {name}\n---\n{name}\n")
        alias = root / name / "SKILL.md"
        alias.parent.mkdir()
        alias.symlink_to(path)
    snapshot = _service(tmp_path).snapshot_for()
    assert {entry.name for entry in snapshot.enabled_entries()} == {"alpha", "beta"}
    assert snapshot.resolve("alpha").path != snapshot.resolve("beta").path


# LLM: 生成含嵌套宿主扩展的真实ToolModelSpec，不使用对象id当缓存身份。
# 函数用途: 检查不同工具同内容复用、不同内容隔离与嵌套剥离。
def _spec(name="one", description="field"):
    return ToolModelSpec(name=name, description="工具", input_schema={
        "type": "object", "properties": {"arg": {"type": "object",
            "properties": {"x": {"type": "string", "description": description}, "y": {"type": "string"}},
            EXCLUSIVE_ARGUMENT_GROUPS_KEY: [["x", "y"]]}},
    })


# LLM: 同内容跨工具命中、不同内容不命中；返回值与canonical/缓存均无可变别名。
# 函数用途: 验证按hash复用剥离结果而不共享可变字典。
def test_schema_cache_is_content_addressed_and_returns_isolated_copies(monkeypatch):
    spec = _spec()
    strip = Mock(wraps=tool_schema._strip_host_schema_extensions)
    monkeypatch.setattr(tool_schema, "_strip_host_schema_extensions", strip)
    first = tool_schema.tool_model_spec_to_input_schema(spec)
    expected = copy.deepcopy(first)
    first["properties"]["arg"]["properties"]["x"]["description"] = "mutated"
    second = tool_schema.tool_model_spec_to_input_schema(_spec("another"))
    assert second == expected and first != second
    assert strip.call_count == 1
    assert any(key[0] == spec.schema_hash for key in tool_schema._STRIPPED_SCHEMA_CACHE)
    assert EXCLUSIVE_ARGUMENT_GROUPS_KEY not in second["properties"]["arg"]
    assert EXCLUSIVE_ARGUMENT_GROUPS_KEY in spec.input_schema["properties"]["arg"]
    assert tool_schema.tool_model_spec_to_input_schema(_spec(description="new")) != second
    assert strip.call_count == 2


# LLM: 同hash但属性插入顺序不同的Schema不得互相污染；命中后还须更新LRU顺序。
# 函数用途: 防止sort_keys=True的schema_hash吞掉属性键顺序确定性。
def test_schema_cache_preserves_property_insertion_order_and_lru():
    spec_ab = ToolModelSpec(
        name="ab",
        description="工具",
        input_schema={
            "type": "object",
            "properties": {
                "first": {"type": "string"},
                "second": {"type": "string"},
            },
        },
    )
    spec_ba = ToolModelSpec(
        name="ba",
        description="工具",
        input_schema={
            "type": "object",
            "properties": {
                "second": {"type": "string"},
                "first": {"type": "string"},
            },
        },
    )
    assert spec_ab.schema_hash == spec_ba.schema_hash
    out_ab = tool_schema.tool_model_spec_to_input_schema(spec_ab)
    out_ba = tool_schema.tool_model_spec_to_input_schema(spec_ba)
    assert list(out_ab["properties"].keys()) == ["first", "second"]
    assert list(out_ba["properties"].keys()) == ["second", "first"]


# LLM: 先填缓存再改canonical，仍须由原完整hash断言拒绝。
# 函数用途: 防止缓存命中绕过快照完整性检查。
def test_warm_schema_cache_still_rejects_in_place_tampering():
    spec = _spec()
    tool_schema.tool_model_spec_to_input_schema(spec)
    spec.input_schema["properties"]["arg"]["properties"]["x"]["type"] = "integer"
    with pytest.raises(ValueError, match="mutated after snapshot"):
        tool_schema.tool_model_spec_to_input_schema(spec)


# LLM: 独审提出的assert/复制窗口不得把B内容发表到A的hash下。
# 函数用途: 锁定一次竞态不能污染进程中其它正常工具。
def test_schema_change_after_assert_cannot_poison_other_specs(monkeypatch):
    victim = _spec()
    original = ToolModelSpec.assert_schema_hash

    # LLM: 精确模拟另一线程在assert返回后改变canonical的窗口；其它spec仍由原断言验证。
    # 函数用途: 保证进程缓存不能在旧hash下发表不同内容。
    def mutate_after_assert(spec):
        original(spec)
        if spec is victim:
            spec.input_schema["properties"]["arg"]["properties"]["x"]["type"] = "integer"

    monkeypatch.setattr(ToolModelSpec, "assert_schema_hash", mutate_after_assert)
    with pytest.raises(ValueError, match="mutated"):
        tool_schema.tool_model_spec_to_input_schema(victim)
    clean = tool_schema.tool_model_spec_to_input_schema(_spec("clean"))
    assert clean["properties"]["arg"]["properties"]["x"]["type"] == "string"


# LLM: 真实并发渲染超过1024种不同Schema，既不能串内容也不能无界增长。
# 函数用途: 验证缓存容量和并发发表隔离。
def test_schema_cache_is_bounded_and_thread_safe():
    specs = [_spec(str(i), description=str(i)) for i in range(1030)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        rendered = list(pool.map(tool_schema.tool_model_spec_to_input_schema, specs))
    assert all(schema["properties"]["arg"]["properties"]["x"]["description"] == str(i)
               for i, schema in enumerate(rendered))
    assert len(tool_schema._STRIPPED_SCHEMA_CACHE) <= 1024
