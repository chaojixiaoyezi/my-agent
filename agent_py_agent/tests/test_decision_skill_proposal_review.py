# LLM: 真实 S1 提案服务与临时 owner home；只替换决策服务的阶段/决策/采用复核三个边界，不发送模型请求。
# 模块用途: 离线验证自学习 S2 审核顺序点的资格、材料隐私、逐题校验、采用前复核、取消传播与零写入。
"""自学习 S2：待确认 Skill 提案审核顺序点的离线合同。"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.decision_protocol import (
    DecisionAnswer,
    DecisionBinding,
    DecisionResponse,
)
from agent_py_agent.agent.capability import decision_skill_proposal_review as module
from agent_py_agent.agent.capability.skill_proposals import SkillProposalService
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

LESSON = "修改共享状态前先读取当前版本，再用精确版本做比较交换写入。"


# LLM: 经原 runner 候选服务与 S1 服务生成真实待确认提案；来源 run/task 编号刻意可辨，供隐私断言检索。
# 函数用途: 在临时 home 生成 count 条不同经验的待确认提案，返回 home、服务和原列表；goal 可覆盖来源任务目标。
def seeded(tmp_path: Path, count: int, lesson: str = LESSON, goal: str = "修复并发写入问题") -> SimpleNamespace:
    home = ensure_my_agent_home(tmp_path / "home")
    manager = SubAgentManager(tmp_path / "subagents", candidate_service=CandidateService(home.owner_memory_candidates_jsonl))
    service = SkillProposalService(home)
    for index in range(count):
        task = SubAgentTask(id=f"run-secret-{index}", root_id=f"task-secret-{index}", goal=f"{goal}（第 {index} 个）",
                            thought="", plan=[], output_json=f"/artifacts/run-secret-{index}/output.json", updated_at=100.0 + index)
        recorded = manager.memory_candidates.record_result_candidates(task, lessons=[f"{lesson}（场景 {index}）"], findings=[])
        service.propose_from_candidates(recorded)
    return SimpleNamespace(root=tmp_path, home=home, service=service, proposals=service.list())


# LLM: 回答按题号给出选择值；未指定的题默认 normal，shape 可改写成缺题、多答或错类型等不合格回答。
# 函数用途: 把 {题号: 值} 转成原协议回答元组。
def answers_of(values: dict) -> tuple[DecisionAnswer, ...]:
    return tuple(DecisionAnswer(key, "choice", value) for key, value in values.items())


# LLM: 替身的可控项全部显式列出；deadline/revision 为 None 时分别取“一分钟后”和请求自身的候选版本。
# 类用途: 描述一次决策服务替身的阶段结果、模式、回答形状、等待期间动作与采用复核结果。
@dataclass(frozen=True)
class Fake:
    mode: str = "apply"
    stage_error: str = ""
    points: tuple[str, ...] = (module._POINT,)
    revision: str | None = None
    deadline: float | None = None
    current: Callable[..., bool] = lambda _host, _params, _stage, _outcome: True
    during: Callable[[], object] = lambda: None
    shape: Callable[[dict], tuple] = answers_of


# LLM: 只替换 begin_decision_stage/decide/decision_outcome_is_current，替身签名与原服务的显式关键字参数一致，
#   多传或少传参数都会直接报错；结果字段与原 DecisionStage/DecisionOutcome 同名。
# 函数用途: 安装可控的阶段与决策结果并记录每次调用，供零请求、绑定与材料断言。
def install(monkeypatch, answers: dict | None = None, fake: Fake | None = None) -> list:
    calls = []
    fake = fake or Fake()

    def begin(_host, params, *, operation_id, caller_deadline=None, scope="thread", experiment=False):
        calls.append(("stage", {"operation_id": operation_id, "scope": scope, "params": params,
                                "caller_deadline": caller_deadline, "experiment": experiment}))
        return SimpleNamespace(error_code=fake.stage_error, deadline=time.monotonic() + 60,
                               enabled_points=fake.points, run_id=params.run_id)

    def decide(_host, params, _stage, *, point, state, questions, candidates_revision, source_refs=(),
               caller_deadline=None, explicit_retry=False):
        calls.append(("decide", {"point": point, "state": state, "questions": questions, "source_refs": source_refs,
                                 "candidates_revision": candidates_revision}))
        fake.during()
        values = {key: (answers or {}).get(key, "normal") for key in questions}
        binding = DecisionBinding(point, "owner", "op", "policy", fake.revision or candidates_revision)
        response = DecisionResponse(binding, "digest", "configured", "actual", fake.shape(values), b"{}")
        deadline = time.monotonic() + 60 if fake.deadline is None else fake.deadline
        return SimpleNamespace(mode=fake.mode, status="success", may_apply=fake.mode == "apply", response=response,
                               deadline=deadline)

    monkeypatch.setattr(module, "begin_decision_stage", begin)
    monkeypatch.setattr(module, "decide", decide)
    monkeypatch.setattr(module, "decision_outcome_is_current", fake.current)
    return calls


# 函数用途: 以空宿主调用审核顺序点（边界已替换），默认使用原列表。
def run(ctx: SimpleNamespace, proposals=None):
    return module.skill_proposal_review_order(SimpleNamespace(), ctx.service, ctx.proposals if proposals is None else proposals)


# LLM: 只读提案目录、owner skills 目录与候选账本的实际字节，不解析内容。
# 函数用途: 生成审核前后比较用的文件快照。
def trees(ctx: SimpleNamespace) -> dict:
    roots = (ctx.home.owner_skill_proposals_dir, ctx.home.owner_home_dir / "skills", ctx.home.owner_memory_candidates_jsonl.parent)
    return {str(root): {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()} if root.exists() else {}
            for root in roots}


# 函数用途: 取出一次记录里实际外发的 state/questions JSON 文本。
def sent_payload(calls: list) -> str:
    kwargs = next(item for kind, item in calls if kind == "decide")
    return json.dumps({"state": kwargs["state"], "questions": kwargs["questions"]}, ensure_ascii=False)


def ids(proposals) -> list[str]:
    return [item.proposal_id for item in proposals]


@pytest.mark.parametrize("count", [0, 1])
def test_zero_or_one_pending_prepares_nothing(tmp_path, monkeypatch, count):
    ctx = seeded(tmp_path, count)
    calls = install(monkeypatch)
    assert run(ctx) is None and calls == []


def test_unregistered_point_is_a_strict_no_op(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 3)
    calls = install(monkeypatch)
    monkeypatch.setattr(module, "POINT_RUNTIME_SCOPES", {})
    assert run(ctx) is None and calls == []


def test_pending_count_bounds_are_two_to_thirty(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 31)
    calls = install(monkeypatch, {"proposal_30": "review_first"})
    assert run(ctx) is None and calls == []
    ctx.service.reject(ctx.proposals[-1].proposal_id, 1)
    result = run(ctx, ctx.service.list())
    assert [kind for kind, _ in calls] == ["stage", "decide"]
    assert len(next(item for kind, item in calls if kind == "decide")["questions"]) == 30
    assert ids(result.proposals)[0] == ctx.proposals[29].proposal_id


def test_two_pending_is_the_lower_bound(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 2)
    calls = install(monkeypatch, {"proposal_2": "review_first"})
    assert ids(run(ctx).proposals) == ids(ctx.proposals[::-1])
    assert [kind for kind, _ in calls] == ["stage", "decide"]


def test_resolved_proposals_do_not_count_and_keep_their_positions(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 5)
    for proposal in (ctx.proposals[0], ctx.proposals[2]):
        ctx.service.reject(proposal.proposal_id, 1)
    listed = ctx.service.list()
    calls = install(monkeypatch, {"proposal_3": "review_first", "proposal_1": "review_later"})
    result = run(ctx, listed)
    assert ids(result.proposals) == ids([listed[0], listed[4], listed[2], listed[3], listed[1]])
    assert [entry.alias for entry in result.entries] == ["proposal_3", "proposal_2", "proposal_1"]
    assert len(next(item for kind, item in calls if kind == "decide")["questions"]) == 3
    for proposal in (listed[3], listed[4]):
        ctx.service.reject(proposal.proposal_id, 1)
    calls.clear()
    assert run(ctx, ctx.service.list()) is None and calls == []


@pytest.mark.parametrize("fake", [Fake(stage_error="configuration_unavailable"), Fake(points=()), Fake(points=("curator",))])
def test_stage_error_or_point_off_sends_nothing(tmp_path, monkeypatch, fake):
    ctx = seeded(tmp_path, 3)
    monkeypatch.setattr(module, "_material", lambda *_args: pytest.fail("关闭时不准备材料"))
    calls = install(monkeypatch, {"proposal_2": "review_first"}, fake)
    assert run(ctx) is None
    assert [kind for kind, _ in calls] == ["stage"]


def test_stage_and_request_are_bound_to_owner_background_and_exact_pending_set(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 3)
    calls = install(monkeypatch)
    run(ctx)
    run(ctx)
    stages = [item for kind, item in calls if kind == "stage"]
    decide = next(item for kind, item in calls if kind == "decide")
    assert all(stage["scope"] == "owner_background" and stage["operation_id"].startswith("skill_proposal_review:") for stage in stages)
    assert stages[0]["operation_id"] == stages[1]["operation_id"]
    params = [stage["params"] for stage in stages]
    assert all(item.thread_id == "" and item.run_id.startswith("skill_proposal_review:") for item in params)
    assert params[0].run_id != params[1].run_id
    assert decide["point"] == "skill_proposal_review"
    assert decide["source_refs"] == tuple(f"skill_proposal:{item.proposal_id}" for item in ctx.proposals)


def test_observe_requests_but_keeps_original(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 3)
    calls = install(monkeypatch, {"proposal_3": "review_first"}, Fake(mode="observe"))
    assert run(ctx) is None
    assert [kind for kind, _ in calls] == ["stage", "decide"]


def test_apply_groups_first_normal_later_and_uses_host_labels(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 5)
    p = ctx.proposals
    before = trees(ctx)
    install(monkeypatch, {"proposal_1": "review_later", "proposal_2": "review_first", "proposal_3": "possible_duplicate",
                          "proposal_4": "normal", "proposal_5": "review_first"})
    result = run(ctx)
    assert ids(result.proposals) == ids([p[1], p[4], p[3], p[0], p[2]])
    assert [(entry.alias, entry.suggestion, entry.label) for entry in result.entries] == [
        ("proposal_2", "review_first", "建议优先审核"), ("proposal_5", "review_first", "建议优先审核"),
        ("proposal_4", "normal", ""), ("proposal_1", "review_later", "建议稍后"),
        ("proposal_3", "possible_duplicate", "可能与其他提案重复")]
    assert [entry.proposal_id for entry in result.entries] == ids(result.proposals)
    assert result.to_payload() == {"point": "skill_proposal_review", "mode": "apply", "status": "applied", "order": [
        {"alias": entry.alias, "proposal_id": entry.proposal_id, "suggestion": entry.suggestion, "label": entry.label}
        for entry in result.entries]}
    assert trees(ctx) == before and ids(ctx.service.list()) == ids(p)


def test_all_normal_keeps_order_and_has_no_labels(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 3)
    install(monkeypatch)
    result = run(ctx)
    assert ids(result.proposals) == ids(ctx.proposals)
    assert {entry.label for entry in result.entries} == {""}


# 函数用途: 生成各种不合格回答形状（缺题、多答、错题号、逐题错误、错类型、越界值）。
def bad_shape(kind: str):
    def shape(values):
        answers = list(answers_of(values))
        if kind == "missing":
            return tuple(answers[:-1])
        if kind == "duplicate":
            return (*answers, answers[0])
        if kind == "wrong_id":
            return (DecisionAnswer("proposal_99", "choice", "normal"), *answers[1:])
        changes = {"error": {"error_code": "invalid_answer"}, "kind": {"kind": "score"}, "value": {"value": "reject"}}[kind]
        return (replace(answers[0], **changes), *answers[1:])
    return shape


@pytest.mark.parametrize("kind", ["missing", "duplicate", "wrong_id", "error", "kind", "value"])
def test_invalid_answers_keep_original(tmp_path, monkeypatch, kind):
    ctx = seeded(tmp_path, 3)
    calls = install(monkeypatch, {"proposal_2": "review_first"}, Fake(shape=bad_shape(kind)))
    assert run(ctx) is None and [name for name, _ in calls] == ["stage", "decide"]


@pytest.mark.parametrize("value", ["not_needed", "need_data", "abstain"])
def test_each_non_selection_keeps_original(tmp_path, monkeypatch, value):
    ctx = seeded(tmp_path, 3)
    calls = install(monkeypatch, {"proposal_1": value, "proposal_3": "review_first"})
    assert run(ctx) is None and [name for name, _ in calls] == ["stage", "decide"]


@pytest.mark.parametrize("fake", [
    Fake(revision="other-revision"), Fake(current=lambda _host, _params, _stage, _outcome: False), Fake(deadline=0.0),
])
def test_stale_binding_policy_or_deadline_keeps_original(tmp_path, monkeypatch, fake):
    ctx = seeded(tmp_path, 3)
    install(monkeypatch, {"proposal_3": "review_first"}, fake)
    assert run(ctx) is None


# LLM: 只在测试 fake 的等待期间改写临时提案文件，模拟用户或另一窗口的并发变化。
# 函数用途: 按名称返回一种“请求期间提案变化”的改写动作。
def change(ctx: SimpleNamespace, kind: str):
    path = ctx.home.owner_skill_proposals_dir / f"{ctx.proposals[1].proposal_id}.json"

    def rewrite(top: dict, draft: dict) -> None:
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(top)
        record["draft"].update(draft)
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    return {
        "rejected": lambda: ctx.service.reject(ctx.proposals[1].proposal_id, 1),
        "revision": lambda: rewrite({"revision": 2}, {}),
        "sha": lambda: rewrite({}, {"sha256": "f" * 64}),
        "body_same_sha": lambda: rewrite({}, {"body": ctx.proposals[1].draft.body + "\n未经审核的新增内容。"}),
        "new_pending": lambda: _add_pending(ctx),
        "corrupt": lambda: path.write_text("{not json", encoding="utf-8"),
    }[kind]


# 函数用途: 在同一 owner 追加一条新的待确认提案。
def _add_pending(ctx: SimpleNamespace) -> None:
    manager = SubAgentManager(ctx.root / "subagents-extra",
                              candidate_service=CandidateService(ctx.home.owner_memory_candidates_jsonl))
    task = SubAgentTask(id="run-late", root_id="task-late", goal="迟到的任务", thought="", plan=[],
                        output_json="/artifacts/late.json", updated_at=999.0)
    ctx.service.propose_from_candidates(manager.memory_candidates.record_result_candidates(task, lessons=["迟到的经验。"], findings=[]))


@pytest.mark.parametrize("kind", ["rejected", "revision", "sha", "body_same_sha", "new_pending", "corrupt"])
def test_proposal_changed_during_request_keeps_original(tmp_path, monkeypatch, kind):
    ctx = seeded(tmp_path, 3)
    calls = install(monkeypatch, {"proposal_3": "review_first"}, Fake(during=change(ctx, kind)))
    assert run(ctx) is None and [name for name, _ in calls] == ["stage", "decide"]


def test_tampered_draft_is_never_sent(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 3)
    change(ctx, "body_same_sha")()
    calls = install(monkeypatch, {"proposal_3": "review_first"})
    assert run(ctx, ctx.service.list()) is None
    assert [name for name, _ in calls] == ["stage"]


def test_material_is_aliased_redacted_and_carries_only_structured_counts(tmp_path, monkeypatch):
    secret = "sk-" + "A1b2C3d4E5f6G7h8I9j0"
    ctx = seeded(tmp_path, 3, lesson=f"调用接口前核对 password=hunter2hunter2 与 {secret}")
    calls = install(monkeypatch)
    run(ctx)
    text = sent_payload(calls)
    state = next(item for kind, item in calls if kind == "decide")["state"]
    private = [str(tmp_path), secret, "hunter2hunter2", ctx.home.owner_id, "run-secret-", "task-secret-", "memory-candidate-",
               "/artifacts/", "skill_proposals", *ids(ctx.proposals), *(item.target.skill_name for item in ctx.proposals),
               *(item.source.candidate_id for item in ctx.proposals)]
    assert [value for value in private if value and value in text] == []
    assert text.count("<untrusted_tool_result") == 3 and "<redacted>" in text
    assert [row["proposal"] for row in state["proposals"]] == ["proposal_1", "proposal_2", "proposal_3"]
    assert [(row["created_order"], row["source_task_count"], row["source_run_count"]) for row in state["proposals"]] == [
        (1, 1, 1), (2, 1, 1), (3, 1, 1)]
    assert set(state["proposals"][0]) == {"proposal", "created_order", "source_task_count", "source_run_count", "draft"}


def test_questions_are_review_order_only_with_fixed_candidates(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 2)
    calls = install(monkeypatch)
    run(ctx)
    questions = next(item for kind, item in calls if kind == "decide")["questions"]
    assert list(questions) == ["proposal_1", "proposal_2"]
    for alias, question in questions.items():
        assert question["type"] == "choice" and question["instructions"]["proposal"] == alias
        assert "不决定确认或拒绝" in question["instructions"]["question"]
        assert set(question["criteria"]) == {"review_first", "normal", "review_later", "possible_duplicate",
                                             "not_needed", "need_data", "abstain"}
        assert question["criteria"]["need_data"]["required_refs"] == [{"kind": "existing_proposal", "ref": alias}]


def test_lesson_excerpt_matches_s1_template(tmp_path):
    multi = "第一行：先读取当前版本。\n\n## 适用场景\n\n第二行：再比较交换写入。"
    ctx = seeded(tmp_path, 1, lesson=multi)
    body = ctx.proposals[0].draft.body
    assert module._lesson_excerpt(body) == " ".join(f"{multi}（场景 0）".split())
    assert "记忆候选" not in module._lesson_excerpt(body) and "run-secret-0" not in module._lesson_excerpt(body)
    long_ctx = seeded(tmp_path / "long", 1, lesson="很长的经验" * 100)
    excerpt = module._lesson_excerpt(long_ctx.proposals[0].draft.body)
    assert len(excerpt) == module._LESSON_EXCERPT_CHARS and excerpt.endswith("…")
    with pytest.raises(ValueError):
        module._lesson_excerpt("没有模板标题的正文")


def test_review_never_confirms_rejects_or_writes(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 4)
    before = trees(ctx)
    for name in ("confirm", "reject", "propose_from_candidates", "_create_if_absent", "_commit_locked"):
        monkeypatch.setattr(SkillProposalService, name, lambda *_args, **_kwargs: pytest.fail("审核顺序不得写提案"))
    install(monkeypatch, {"proposal_4": "review_first", "proposal_1": "possible_duplicate"})
    assert run(ctx) is not None
    install(monkeypatch, fake=Fake(mode="observe"))
    assert run(ctx) is None
    assert trees(ctx) == before
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert ".confirm(" not in source and ".reject(" not in source and "write_" not in source


@pytest.mark.parametrize("mode", ["apply", "observe"])
def test_host_cancellation_during_request_propagates(tmp_path, monkeypatch, mode):
    ctx = seeded(tmp_path, 3)
    token = CancellationToken()
    install(monkeypatch, fake=Fake(mode=mode, during=lambda: token.cancel("user-stop")))
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        run(ctx)


def test_cancellation_during_policy_check_propagates(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 3)
    token = CancellationToken()
    install(monkeypatch, fake=Fake(current=lambda _host, _params, _stage, _outcome: token.cancel("user-stop") is None))
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        run(ctx)


@pytest.mark.parametrize("error", [InterruptedError("stop"), ToolCancelled("stop")])
def test_interrupt_from_decision_boundary_propagates(tmp_path, monkeypatch, error):
    ctx = seeded(tmp_path, 3)

    def raise_error():
        raise error

    install(monkeypatch, fake=Fake(during=raise_error))
    with pytest.raises(type(error)):
        run(ctx)


def test_optional_error_returns_original_but_never_hides_cancellation(tmp_path, monkeypatch):
    ctx = seeded(tmp_path, 3)
    token = CancellationToken()

    def fail():
        raise RuntimeError("provider down")

    install(monkeypatch, fake=Fake(during=fail))
    assert run(ctx) is None

    def cancel_then_fail():
        token.cancel("user-stop")
        raise RuntimeError("provider down")

    install(monkeypatch, fake=Fake(during=cancel_then_fail))
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        run(ctx)
