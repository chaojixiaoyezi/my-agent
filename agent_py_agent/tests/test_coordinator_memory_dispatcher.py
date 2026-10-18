# LLM: 只用原真实HTTP dispatcher和领域资源，不替handle_control/source/holder/权限；初始化fixture先种原候选，非真实任务补产物。
# 模块用途: 验证冷C候选七动作在原endpoint、完成账、事件与普通/群身份下的实际接线；不代Persona或原生Worker验收。
import pytest

from agent_py_agent.agent.gateway_parts import event_points
from agent_py_agent.agent.gateway_parts.control_operation_service import (
    read_gateway_control_operation,
)
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)
from agent_py_agent.tests.test_coordinator_agents_domain import submit
from agent_py_agent.tests.test_coordinator_control_forward import cold
from agent_py_agent.tests.test_memory_preview_transaction import read


# LLM: 仅读取本测试真实dispatcher异常；原调用次数、对象与抛出保持，不替HTTP/source判断。
# 函数用途: 在测试失败时保留可定位调用栈，临时fixture只有合成数据。
@pytest.fixture(autouse=True)
def observe_dispatch_errors(monkeypatch):
    import traceback

    from agent_py_agent.agent.gateway_parts import coordinator_owner_management as module
    original = module.execute_coordinator_owner_management

    def observed(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except BaseException:
            traceback.print_exc()
            raise

    monkeypatch.setattr(module, 'execute_coordinator_owner_management', observed)


# LLM: 仅测试准备阶段选择同owner的原CandidateService；群的完整home来自原resolver，不构造业务Agent或源。
# 函数用途: 为普通和群实际HTTP测试准备同格式候选，随后全部动作都由被测endpoint执行。
def seed_for_owner(case, as_group):
    owner = OwnerIdentity.provider_group("feishu", "oc_coldagents") if as_group else case.owner
    if as_group:
        home = home_paths_with_owner(
            home_paths(case.server.control_plane.home_root),
            ensure_owner_home(case.server.control_plane.home_root, owner),
        )
        candidates = CandidateService(home.owner_memory_candidates_jsonl)
    else:
        candidates = case.seeded.memory_candidates
    row = candidates.observe(
        CandidateObservation(
            candidate_type="project",
            content="本项目统一记录UTC时间",
            subject_key="dispatcher.project",
            scope=MemoryScope("personal", "personal"),
            origin="reviewed",
            promotion_target="long_term",
        )
    )
    return owner, candidates, candidates.transition(row, "pending_review", reviewer="alice")


@pytest.mark.parametrize(
    "cold,as_group", [("local", False), ("feishu", False), ("feishu", True)], indirect=["cold"]
)
@pytest.mark.parametrize(
    "action,expected,candidate_status",
    [
        ("confirm", "completed", "promoted"),
        ("reject", "rejected", "rejected"),
        ("cancel", "cancelled", "pending_review"),
    ],
)
def test_original_http_seven_actions_receipt_identity_and_single_event(
    cold, monkeypatch, as_group, action, expected, candidate_status  # noqa: F811
):
    owner, candidates, row = seed_for_owner(cold, as_group)
    events, original = [], event_points.command_completed_from_resources

    def observe(config, home, receipt, **kwargs):
        events.append((home.owner_id, receipt.operation_id, receipt.state))
        return original(config, home, receipt, **kwargs)

    monkeypatch.setattr(event_points, "command_completed_from_resources", observe)
    status, listed = submit(cold, "/memory candidates", "list-original", as_group=as_group)
    assert status == 200 and listed["ok"] and listed["details"]["count"] == 1, listed
    status, shown = submit(
        cold, "/memory candidates show " + row.candidate_id, "show-original", as_group=as_group
    )
    assert status == 200 and shown["ok"] and shown["details"]["value"]["content"] == row.content, (
        shown
    )
    status, preview = submit(
        cold,
        "/memory candidates preview " + row.candidate_id,
        "preview-original",
        as_group=as_group,
    )
    assert status == 200 and preview["ok"] and preview["details"]["state"] == "preview", preview
    command = preview["details"][action + "_command"]
    status, done = submit(cold, command, "action-original", as_group=as_group)
    assert status == 200 and done["ok"] and done["details"]["state"] == expected, done
    assert candidates.get(row.candidate_id).status == candidate_status
    durable = read_gateway_control_operation(
        cold.server.paths, done["operation_id"], read_only=True
    )
    assert durable.state == "completed" and durable.dispatch is None
    assert (durable.owner_provider, durable.owner_kind, durable.owner_id) == (
        owner.provider,
        owner.owner_kind,
        owner.owner_id,
    )
    assert len(events) == 4
    assert submit(cold, command, "action-original", as_group=as_group) == (status, done)
    assert len(events) == 4
    original_raw = read(cold, preview["details"]["nonce"])[1]
    status, late = submit(cold, command, "action-later", as_group=as_group)
    assert status == 200 and late["details"]["state"] == expected, late
    status, receipt = submit(
        cold, preview["details"]["receipt_command"], "receipt-original", as_group=as_group
    )
    assert status == 200 and receipt["details"]["state"] == expected, receipt
    assert read(cold, preview["details"]["nonce"])[1] == original_raw
    assert candidates.get(row.candidate_id).status == candidate_status
    assert len(events) == 6 and len({event[1] for event in events}) == 6
    assert all(
        event[0] == durable.result["details"].get("owner", event[0]) and event[2] == "completed"
        for event in events
    )
    entries = cold.server.coordinator_memory_resources._entries
    assert len(entries) == 1 and all(entry.borrowers == 0 for entry in entries.values())
    domain = next(iter(entries.values())).future.result()
    records = domain.long_term.all()
    assert len(records) == (1 if action == "confirm" else 0)
    if records:
        assert records[0].content == row.content
    if as_group:
        assert not cold.seeded.memory_candidates.list()
    assert cold.server.agent is None and cold.constructions == []


@pytest.mark.parametrize("cold", ["feishu"], indirect=True)
def test_actual_group_nonce_cannot_be_confirmed_by_another_actor(cold):  # noqa: F811
    _owner, candidates, row = seed_for_owner(cold, True)
    status, preview = submit(
        cold, "/memory candidates preview " + row.candidate_id, "preview-owner", as_group=True
    )
    assert status == 200 and preview["ok"], preview
    raw = read(cold, preview["details"]["nonce"])[1]
    status, denied = submit(
        cold, preview["details"]["confirm_command"], "other-actor", as_group=True, actor="bob"
    )
    assert (
        status == 200
        and not denied["ok"]
        and denied["error_code"] == "MEMORY_PREVIEW_BINDING_MISMATCH"
    ), denied
    assert read(cold, preview["details"]["nonce"])[1] == raw
    assert candidates.get(row.candidate_id).status == "pending_review"
    assert cold.server.agent is None and cold.constructions == []
