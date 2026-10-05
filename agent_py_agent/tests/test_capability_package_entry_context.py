# LLM: 验证宿主入口上下文的原安装读取、原 task pin、真实 ActionPolicy 和总预算；仅本地组件，禁止模型联网或脚本执行。
# 模块用途: 检查引用与实际交付页对应，部分正文可准确续读，选择结果不能伪造业务工具历史。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from itertools import permutations
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.capability import package_selection_context as entry_context
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.capability.package_read import PackagePageChecks, read_package_page
from agent_py_agent.agent.capability.package_selection import (
    build_package_selection_material,
    select_capability_packages,
)
from agent_py_agent.agent.capability.package_selection_runtime import _commit_selection
from agent_py_agent.agent.capability.package_selection_scope import PackageSelectionScope
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotError
from agent_py_agent.agent.common.cancellation import CancellationToken, ToolCancelled
from agent_py_agent.agent.conversation.capability_selection_state import TaskCapabilitySelection
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import ApprovalPolicy
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_capability_package import content_bundle


# LLM: 只在 pytest 私有 home 安装合成内容，绑定真实 TaskStore；authority 替身只消费本地取消令牌，不冒充 managed attempt 验收。
# 函数用途: 装配宿主准备所需的原安装快照、工具策略、任务身份和可观察资源读取。
def _fixture(tmp_path, *, bodies=(b"entry-a",), max_chars=10000, bundles=None):
    agent = SimpleAgent(AgentConfig(enable_plugins=True, prompt_files=[], tool_read_max_chars=max_chars), tmp_path / "repo")
    store = PluginInstallStore(resolve_owner_home(agent.home_paths.root))
    installed = []
    for index, bundle in enumerate(_entry_bundles(bodies) if bundles is None else bundles):
        package_id = f"entry-{index}"
        package = inspect_plugin_package(bundle)
        row = store.install(PluginInstallRequest(package, f"install-{index}", 0)).installation
        activation = PluginContentActivation(f"enable-{index}", package_id, row.package_sha256,
                                             row.revision, row.settings_revision)
        installed.append(store.change_activation(PluginActivationRequest(f"enable-{index}", row.revision, activation)).installation)
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "entry-context",
    })
    link = agent.conversation_store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "entry-task", "goal": "核对资料"},
        capability_selection=TaskCapabilitySelection.pending(),
    )
    params = SimpleNamespace(
        task_id=link.task_id, request_id="entry-request", run_id="entry-run", attempt_id="entry-attempt",
        conversation_turn_id="entry-turn", write_boundary=None, allowed_tools=["skill_search"],
        task_attributes={"conversation_task_id": link.task_id, "conversation_thread_id": thread.thread_id},
        cancellation_token=CancellationToken(), tool_ir_history=[UserTurn("原始业务请求")],
    )
    agent._current_run_params = params
    agent.tools.approval_mode_reader = lambda: "ask"
    reads = []
    skills = agent.current_skill_snapshot()

    def observed(package):
        def reader(path):
            reads.append((package.package_id, path))
            return package.read(path)
        return replace(package, reader=reader)

    skills = replace(skills, packages=tuple(observed(package) for package in skills.packages))
    scope = PackageSelectionScope(CapabilityConfig(capability_bundle_max_tokens=10000), skills,
                                  agent.tools.runtime_snapshot(allowed_tools=["skill_search"], run_id=params.run_id))
    authority = SimpleNamespace(check=params.cancellation_token.raise_if_cancelled,
                                task_id=link.task_id, thread_id=thread.thread_id,
                                is_current=lambda current, _marker: current.task_id == link.task_id and current.status == "active")
    return SimpleNamespace(agent=agent, params=params, scope=scope, authority=authority, reads=reads,
                           store=store, installed=installed, link=link)


# LLM: 这里只调用宿主准备函数；选择引用均来自既有受限快照，不补造工具调用或业务产物。
# 函数用途: 让各用例独立指定总预算或 refs，观察准备的完整结果。
def _prepare(fixture, *, budget=10000, refs=None):
    references = [package.to_ref() for package in fixture.scope.skills.packages] if refs is None else refs
    return entry_context.prepare_package_entry_context(
        fixture.agent, fixture.params, fixture.scope, references,
        authority=fixture.authority, claim_id="entry-claim", max_tokens=budget,
    )


# LLM: 同组顺序用例复用同一合成归档字节；不把 ZIP 创建时间导致的包摘要变化混作投递顺序变化。
# 函数用途: 一次生成每包输入，再在各独立 home 原样安装，只有激活代次允许不同。
def _entry_bundles(bodies):
    return [content_bundle(files={"CAPABILITY.md": body, "methods/private.md": b"PRIVATE-UNREAD"},
                           change=lambda row, name=f"entry-{index}": row.update(plugin_id=name))
            for index, body in enumerate(bodies)]


# LLM: 解析实际对主模型可见的 JSON，不拿 loaded_count 或 pins 代替已展示正文。
# 函数用途: 取回宿主上下文中的完整来源和实际可见页。
def _entries(result):
    return json.loads(result.text.split("\n", 2)[-1])["entries"] if result.text else []


# LLM: 各顺序独立安装产生不同 activation；仅去掉这两个代次字段，其余正文、来源、分页与投递诊断逐字段比较。
# 函数用途: 比较独立 home 的同一包交付，不把完整来源或续页差异藏成只比较 status。
def _stable_receipt(value):
    if isinstance(value, dict):
        return {key: _stable_receipt(item) for key, item in value.items()
                if key not in ("activation_id", "expected_activation_id")}
    if isinstance(value, list):
        return [_stable_receipt(item) for item in value]
    return value


# LLM: 解析真实 RuntimeFacts 正文并按包关联结构化诊断；未投递页的缺席也必须参与比较。
# 函数用途: 取得每包完整可见页和投递形态，不拿 pin 或总数代替正文。
def _delivery_by_package(result):
    rows = {row["package_id"]: _stable_receipt(row) for row in result.entry_status}
    for page in _entries(result):
        rows[page["package_id"]]["page"] = _stable_receipt(page)
    return rows


@pytest.mark.parametrize("case", [
    ((b"A" * 4000, ("甲🙂\\\n" * 1000).encode()), 978),
    ((b"A" * 4000, ("甲🙂\\\n" * 1000).encode(), ('"\\\t<&>Z' * 600).encode()), 1800),
])
def test_heterogeneous_entries_have_identical_delivery_in_every_fresh_home_order(tmp_path, monkeypatch, case):
    bodies, budget = case
    baseline = None
    bundles = _entry_bundles(bodies)
    for index, order in enumerate(permutations(range(len(bodies)))):
        monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / f"home-{index}"))
        fixture = _fixture(tmp_path / f"run-{index}", bundles=bundles)
        tasks = fixture.agent.conversation_store.tasks
        assert tasks.load(fixture.link.task_id).skill_snapshot_refs == ()
        history = deepcopy(fixture.params.tool_ir_history)
        refs = [package.to_ref() for package in fixture.scope.skills.packages]
        result = _prepare(fixture, budget=budget, refs=[refs[position] for position in order])
        assert estimate_tokens(result.text) <= budget
        assert fixture.params.tool_ir_history == history
        assert {ref["package_id"] for ref in tasks.load(fixture.link.task_id).skill_snapshot_refs} == {
            ref["package_id"] for ref in refs}
        assert len(fixture.reads) == len(bodies)
        assert result.loaded_count > 0  # 防止用完全不投递制造空集合“换序一致”。
        delivery = _delivery_by_package(result)
        if baseline is None:
            baseline = delivery
        assert delivery == baseline
        for page in _entries(result):
            source = bodies[int(page["package_id"].split("-")[-1])].decode()
            assert page["body"] == source[:len(page["body"])] and page["offset"] == 0
            assert page["total_chars"] == len(source)
            assert page["continuation"]["offset"] == len(page["body"])
            assert page["continuation"]["max_chars"] == max(1, len(page["body"]))


def test_original_installation_reader_and_visible_entries_have_exactly_matching_pins(tmp_path):
    fixture = _fixture(tmp_path, bodies=(b"entry-a", "第二个入口🙂\r\n".encode()))
    history = deepcopy(fixture.params.tool_ir_history)
    result = _prepare(fixture)
    pages = _entries(result)
    assert result.loaded_count == 2 and result.warning_codes == ()
    assert [page["body"] for page in pages] == ["entry-a", "第二个入口🙂\r\n"]
    pins = fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs
    assert [ref["package_id"] for ref in pins] == [page["package_id"] for page in pages]
    assert [page["source_ref"] for page in pages] == [
        {**package.to_ref(), "resource_path": package.entry_document,
         "resource_sha256": package.resolve().sha256} for package in fixture.scope.skills.packages
    ]
    assert all(len(page["source_ref"]) == 9 and not page["has_more"] for page in pages)
    assert fixture.reads == [("entry-0", "CAPABILITY.md"), ("entry-1", "CAPABILITY.md")]
    assert estimate_tokens(result.text) <= 10000 and fixture.params.tool_ir_history == history


@pytest.mark.parametrize("body", [
    "甲🙂\r\n乙" * 2000,
    '"\\\t\n\r\b\f\x00\x1f<&>尾🙂' * 2000,
    "A" * 20000,
])
def test_total_budget_includes_json_receipts_and_partial_page_continues_without_skipping(tmp_path, body):
    fixture = _fixture(tmp_path, bodies=(body.encode(),))
    result = _prepare(fixture, budget=1000)
    assert result.loaded_count == 1 and estimate_tokens(result.text) <= 1000
    assert "CAPABILITY_SELECTION_ENTRY_PARTIAL" in result.warning_codes
    page, = _entries(result)
    assert page["body"] and body.startswith(page["body"]) and page["has_more"]
    assert page["continuation"]["offset"] == len(page["body"])
    chunks = [page["body"]]
    while page["has_more"]:
        arguments = dict(page["continuation"])
        assert arguments.pop("action") == "get"
        arguments["checks"] = PackagePageChecks(
            expected_package_sha256=arguments.pop("expected_package_sha256"),
            expected_activation_id=arguments.pop("expected_activation_id"),
        )
        page = read_package_page(fixture.agent, fixture.scope.skills, **arguments)
        chunks.append(page["body"])
    assert "".join(chunks).encode() == body.encode()



# LLM: 三分法是本次改动的核心合同，三条分别对应“准入不过”“读不出”“读得出但塞不下”。
# 函数用途: 三条判据各一条用例，钉住 pin 与读成功绑定、与投递解耦。
@pytest.mark.parametrize("decision", ["ask", "deny"])
def test_access_denied_selected_package_is_never_pinned(tmp_path, decision):
    # 判据①：准入不通过（ask/deny/越权）不 pin，也不读页。
    fixture = _fixture(tmp_path)
    runtime = fixture.scope.tools.runtime("skill_search")
    if decision == "ask":
        runtime = replace(runtime, runtime_policy=replace(runtime.runtime_policy, approval_policy=ApprovalPolicy("always")))
    else:
        runtime = replace(runtime, exposure=replace(runtime.exposure, model_visible=False))
    fixture.scope = replace(fixture.scope, tools=replace(fixture.scope.tools, runtimes=(runtime,), snapshot_hash=""))
    result = _prepare(fixture)
    assert fixture.reads == []
    assert [row["status"] for row in result.entry_status] == ["not_delivered"]
    assert result.entry_status[0]["reason"] == "not_authorized"
    assert fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs == ()


def test_unreadable_selected_package_is_never_pinned(tmp_path):
    # 判据②：准入通过但读不出有效页（内容被篡改）不 pin。
    fixture = _fixture(tmp_path, bodies=(b"broken", b"working"))
    first, second = fixture.scope.skills.packages
    first = replace(first, reader=lambda _path: b"tampered")
    fixture.scope = replace(fixture.scope, skills=replace(fixture.scope.skills, packages=(first, second)))
    result = _prepare(fixture)
    pins = fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs
    assert [ref["package_id"] for ref in pins] == ["entry-1"]
    assert {row["package_id"]: row["status"] for row in result.entry_status} == {
        "entry-0": "not_delivered", "entry-1": "full"}


@pytest.mark.parametrize("body", [b"entry-" + b"x" * 3000, b""])
def test_readable_but_unaffordable_entry_is_pinned_and_marked_not_delivered(tmp_path, body):
    # 判据③：读得出有效页、只是总预算放不下正文 → 仍然 pin（这正是 B05 的根因）。
    fixture = _fixture(tmp_path, bodies=(body,))
    header = estimate_tokens(entry_context._render([]))
    # 总预算只够放包头：连最小骨架都放不下 → 该包 not_delivered，但读成功已经固定引用。
    result = _prepare(fixture, budget=header + 1)
    assert result.loaded_count == 0 and result.text == ""
    assert [row["status"] for row in result.entry_status] == ["not_delivered"]
    assert result.entry_status[0]["reason"] == "budget_exhausted"
    assert fixture.reads == [("entry-0", "CAPABILITY.md")]
    pins = fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs
    assert [ref["package_id"] for ref in pins] == ["entry-0"]



# 函数用途: 只放得下骨架时，续页不能把下一次读取压成 1 个字（模型照做会逐字读、白耗调用）；3a 挑入 selfix3 时补。
def test_skeleton_continuation_does_not_force_one_char_reads(tmp_path):
    fixture = _fixture(tmp_path, bodies=(("入口正文" * 600).encode(),))
    package = fixture.scope.skills.packages[0]
    page = read_package_page(fixture.agent, fixture.scope.skills, package.package_id)
    full = estimate_tokens(json.dumps(page, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + ",")
    fitted = (entry_context._fit_entry_page(fixture.scope.skills, page, allowance) for allowance in range(full))
    skeletons = [row for row in fitted if row is not None and row["body"] == ""]
    assert skeletons, "前提：应存在只放得下骨架的份额"
    for row in skeletons:
        assert row["has_more"] is True
        assert row["continuation"]["offset"] == page["offset"]
        assert "max_chars" not in row["continuation"]

def test_two_selected_packages_share_budget_and_are_both_pinned_in_either_order(tmp_path):
    # 新合同：预算在选中包之间均分，且结果与 refs 顺序无关；两个包都固定。
    fixture = _fixture(tmp_path, bodies=(b"A" * 4000, b"B" * 4000))
    refs = [package.to_ref() for package in fixture.scope.skills.packages]
    budget = 900
    forward = _prepare(fixture, budget=budget, refs=refs)
    backward = _prepare(fixture, budget=budget, refs=list(reversed(refs)))
    # 两种顺序都固定了两个包（pin 跟“读成功”走；pin 列表按引用去重）。
    pins = fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs
    assert [ref["package_id"] for ref in pins] == ["entry-0", "entry-1"]
    assert [row["package_id"] for row in forward.entry_status] == ["entry-0", "entry-1"]
    assert [row["package_id"] for row in backward.entry_status] == ["entry-1", "entry-0"]
    assert estimate_tokens(forward.text) <= budget and estimate_tokens(backward.text) <= budget
    # 两边顺序得到相同的 status 集合 —— 与 refs 顺序无关。
    forward_status = {row["package_id"]: row["status"] for row in forward.entry_status}
    backward_status = {row["package_id"]: row["status"] for row in backward.entry_status}
    assert forward_status == backward_status


# LLM: 边界用例：份额恰好等于“包头 + k”、均分有余数、单包与旧行为一致。
# 函数用途: 钉住 _entry_share 的整除与余数行为，保证份额只跟包数/总预算有关。
def test_entry_share_is_equal_split_and_order_independent():
    header = estimate_tokens(entry_context._render([]))
    # 单包：可用预算全给这一个包。
    assert entry_context._entry_share(header + 700, 1) == 700
    # 两个包：均分，余数丢弃（不留半份）；与包的身份/顺序无关。
    assert entry_context._entry_share(header + 700, 2) == 350
    assert entry_context._entry_share(header + 701, 2) == 350
    # 预算不足包头：份额为 0。
    assert entry_context._entry_share(header - 1, 2) == 0


@pytest.mark.parametrize("body", [b"entry-a", b""])
def test_single_package_with_ample_budget_delivers_full_entry(tmp_path, body):
    # 单包且预算充足：与旧行为一致，整页投递、status=full、pin 一个。
    fixture = _fixture(tmp_path, bodies=(body,))
    result = _prepare(fixture)
    assert result.loaded_count == 1
    assert [row["status"] for row in result.entry_status] == ["full"]
    assert result.warning_codes == ()
    assert [page["body"] for page in _entries(result)] == [body.decode()]
    pins = fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs
    assert [ref["package_id"] for ref in pins] == ["entry-0"]


@pytest.mark.parametrize("refs,budget", [([], 0), ([], 10000)])
def test_no_selected_refs_has_no_reader_pin_or_history_side_effect(tmp_path, refs, budget):
    # 没有选中任何包：不读、不 pin、不写历史（这条与新判据无关，是空集合基线）。
    fixture = _fixture(tmp_path)
    before = fixture.agent.conversation_store.tasks.load(fixture.link.task_id)
    history = deepcopy(fixture.params.tool_ir_history)
    result = _prepare(fixture, budget=budget, refs=refs)
    assert result.text == "" and result.loaded_count == 0 and fixture.reads == []
    assert fixture.agent.conversation_store.tasks.load(fixture.link.task_id) == before
    assert fixture.params.tool_ir_history == history


def test_zero_budget_still_reads_and_pins_selected_package(tmp_path):
    # 判据③的极端：预算为 0 但仍有选中包——读成功就固定；正文放不下记 not_delivered。
    fixture = _fixture(tmp_path)
    result = _prepare(fixture, budget=0)
    assert result.loaded_count == 0 and result.text == ""
    assert [row["status"] for row in result.entry_status] == ["not_delivered"]
    assert fixture.reads == [("entry-0", "CAPABILITY.md")]
    pins = fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs
    assert [ref["package_id"] for ref in pins] == ["entry-0"]


def test_empty_entry_has_complete_receipt_without_fake_continuation(tmp_path):
    fixture = _fixture(tmp_path, bodies=(b"",))
    result = _prepare(fixture)
    page, = _entries(result)
    assert result.loaded_count == 1 and page["body"] == "" and page["total_chars"] == 0
    assert not page["has_more"] and "continuation" not in page
    assert len(fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs) == 1


@pytest.mark.parametrize("decision", ["ask", "deny"])
def test_real_policy_ask_or_deny_never_reads_or_pins_entry(tmp_path, decision):
    fixture = _fixture(tmp_path)
    runtime = fixture.scope.tools.runtime("skill_search")
    if decision == "ask":
        runtime = replace(runtime, runtime_policy=replace(runtime.runtime_policy, approval_policy=ApprovalPolicy("always")))
    else:
        runtime = replace(runtime, exposure=replace(runtime.exposure, model_visible=False))
    tools = replace(fixture.scope.tools, runtimes=(runtime,), snapshot_hash="")
    fixture.scope = replace(fixture.scope, tools=tools)
    result = _prepare(fixture)
    assert result.loaded_count == 0 and result.text == ""
    assert result.warning_codes == ("CAPABILITY_SELECTION_ENTRY_NOT_AUTHORIZED",)
    assert fixture.reads == []
    assert fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs == ()


def test_bad_resource_only_warns_and_other_valid_entry_is_still_delivered(tmp_path):
    fixture = _fixture(tmp_path, bodies=(b"broken", b"working"))
    first, second = fixture.scope.skills.packages
    first = replace(first, reader=lambda _path: b"tampered")
    fixture.scope = replace(fixture.scope, skills=replace(fixture.scope.skills, packages=(first, second)))
    result = _prepare(fixture)
    assert result.loaded_count == 1 and _entries(result)[0]["body"] == "working"
    assert result.warning_codes == ("CAPABILITY_SELECTION_ENTRY_UNAVAILABLE",)
    pins = fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs
    assert [ref["package_id"] for ref in pins] == ["entry-1"]


@pytest.mark.parametrize("error", [ToolCancelled, InterruptedError])
@pytest.mark.parametrize("stage", ["authority", "read"])
def test_cancellation_and_interruption_are_not_optional_read_warnings(tmp_path, error, stage):
    fixture = _fixture(tmp_path)

    def stop(*_args):
        raise error("stop before entry delivery")

    if stage == "authority":
        fixture.authority.check = stop
    else:
        package = fixture.scope.skills.packages[0]
        fixture.scope = replace(fixture.scope, skills=replace(fixture.scope.skills,
                                packages=(replace(package, reader=stop),)))
    with pytest.raises(error):
        _prepare(fixture)
    assert fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs == ()


def test_revoked_or_out_of_scope_entry_cannot_be_rendered_from_frozen_metadata(tmp_path):
    fixture = _fixture(tmp_path, bodies=(b"revoked", b"out-of-scope"))
    active = fixture.installed[0]
    fixture.store.change_activation(PluginActivationRequest("revoke", active.revision,
                                                            replace(active.activation, phase="revoked")))
    refs = [package.to_ref() for package in fixture.scope.skills.packages]
    fixture.scope = replace(fixture.scope, skills=replace(fixture.scope.skills,
                            packages=(fixture.scope.skills.packages[0],)))
    result = _prepare(fixture, refs=refs)
    assert result.text == "" and result.loaded_count == 0
    assert result.warning_codes == ("CAPABILITY_SELECTION_ENTRY_UNAVAILABLE",)
    assert fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs == ()


def test_revocation_during_original_archive_read_produces_warning_without_pin(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_package

    fixture = _fixture(tmp_path)
    active = fixture.installed[0]
    original = plugin_package.read_plugin_member

    def revoke_after_bytes(*args, **kwargs):
        content = original(*args, **kwargs)
        fixture.store.change_activation(PluginActivationRequest("revoke-during-read", active.revision,
                                                                replace(active.activation, phase="revoked")))
        return content

    monkeypatch.setattr(plugin_package, "read_plugin_member", revoke_after_bytes)
    result = _prepare(fixture)
    assert not result.text and result.loaded_count == 0
    assert result.warning_codes == ("CAPABILITY_SELECTION_ENTRY_UNAVAILABLE",)
    assert fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs == ()


def test_revocation_after_successful_read_preserves_history_but_never_revives_old_ref(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path)
    active = fixture.installed[0]
    original = entry_context.read_package_page

    def revoke_after_successful_page(*args, **kwargs):
        page = original(*args, **kwargs)
        fixture.store.change_activation(PluginActivationRequest("revoke-after-read", active.revision,
                                                                replace(active.activation, phase="revoked")))
        return page

    monkeypatch.setattr(entry_context, "read_package_page", revoke_after_successful_page)
    result = _prepare(fixture)
    assert result.loaded_count == 1 and _entries(result)[0]["body"] == "entry-a"
    pins = fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs
    assert [ref["activation_id"] for ref in pins] == [active.activation_id]
    with pytest.raises(SkillSnapshotError):
        read_package_page(fixture.agent, fixture.scope.skills, "entry-0")
    assert fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs == pins


def test_cancellation_during_page_fitting_keeps_read_success_pin_but_delivers_nothing(tmp_path, monkeypatch):
    # pin 时机定稿后：pin 与“读成功”绑定。取消发生在读成功之后、裁剪投递之前时，
    # 取消照常上抛，正文不投递；但这一包已经读成功，引用归属按原语义保留。
    fixture = _fixture(tmp_path, bodies=(b"entry-" + b"x" * 3000,))
    original = entry_context.json.dumps
    encoded = []
    history = deepcopy(fixture.params.tool_ir_history)

    def cancel_during_encoding(value, *args, **kwargs):
        text = original(value, *args, **kwargs)
        if isinstance(value, dict) and value.get("package_id") == "entry-0" and "body" in value:
            encoded.append(value["package_id"])
            fixture.params.cancellation_token.cancel("stop during page encoding")
        return text

    monkeypatch.setattr(entry_context.json, "dumps", cancel_during_encoding)
    # 预算只给一点：整页放不下，必然走到 _fit_entry_page 裁剪（注意用 _prepare 的 budget 参数，不是 scope 配置）。
    with pytest.raises(ToolCancelled):
        _prepare(fixture, budget=200)
    assert encoded and fixture.params.tool_ir_history == history
    # 取消发生在读成功之后：pin 已按“读成功即固定”落下（判据③），但这一页不投递、异常上抛。
    pins = fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs
    assert [ref["package_id"] for ref in pins] == ["entry-0"]


def test_cancellation_before_read_does_not_pin(tmp_path):
    # 判据②：取消发生在读之前（没读成功）→ 不 pin，照常上抛。
    fixture = _fixture(tmp_path)

    def stop(*_args):
        raise ToolCancelled("stop before read")

    fixture.authority.check = stop
    with pytest.raises(ToolCancelled):
        _prepare(fixture)
    assert fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs == ()


def test_failed_pin_cannot_deliver_the_already_fitted_page(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path)

    def failed_pin(**_kwargs):
        raise OSError("task pin failed")

    monkeypatch.setattr(fixture.agent.conversation_store.tasks, "pin_skill_reference", failed_pin)
    result = _prepare(fixture)
    assert result.loaded_count == 0 and not result.text
    assert result.warning_codes == ("CAPABILITY_SELECTION_ENTRY_UNAVAILABLE",)
    assert fixture.agent.conversation_store.tasks.load(fixture.link.task_id).skill_snapshot_refs == ()


def test_selector_response_stays_out_of_business_history_and_host_entries_have_own_source(tmp_path):
    fixture = _fixture(tmp_path, bodies=(b"original entry",))
    raw_selector = '{"selected_ids":["capability:entry-0"]}'
    fixture.agent.backend = SimpleNamespace(generate_structured=lambda *_args, **_kwargs: ModelResponse(raw_selector, "fake"))
    material = build_package_selection_material("核对资料", fixture.scope.skills.packages)
    history = deepcopy(fixture.params.tool_ir_history)
    selected = select_capability_packages(fixture.agent, material)
    assert selected.outcome == "selected" and fixture.params.tool_ir_history == history
    tasks = fixture.agent.conversation_store.tasks
    claim = tasks.claim_capability_selection(
        task_id=fixture.link.task_id, thread_id=fixture.link.thread_id,
        request_id=fixture.params.request_id, run_id=fixture.params.run_id, attempt_id=fixture.params.attempt_id,
        candidate_digest=material.candidate_digest, model_binding_digest="b" * 64,
        execution_is_current=fixture.authority.is_current,
    )
    _commit_selection(fixture.agent, fixture.params, fixture.scope, fixture.authority, claim, selected, 10000)
    assert fixture.params.tool_ir_history[:1] == history
    additions = fixture.params.tool_ir_history[1:]
    assert all(isinstance(item, RuntimeFactsTurn) for item in additions)
    assert {item.source for item in additions} <= {"capability_package_entries", "capability_package_selection_warning"}
    entry, = [item for item in additions if item.source == "capability_package_entries"]
    assert all(raw_selector not in item.text for item in additions)
    assert "original entry" in entry.text
    assert tasks.load(fixture.link.task_id).capability_selection.status == "finished"
