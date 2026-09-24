# LLM: 保留原 CLI 解析与 owner 解析、S1 提案服务、决策设置/模型目录、决策服务、worker、响应解析、冷却表和调用账；
#   唯一替身是 TypesafeDecisionBackend 发送所用的 post_json（prepare 的窗口门与请求头构造照常执行），不联网。
# 模块用途: 离线验证 `my-agent skills proposals list` 在 off/observe/apply 下的真实接线、输出字节、隐私、零写入与设置/菜单入口。
"""自学习 S2：审核顺序点经真实 CLI 与决策服务的组合验证。"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import typesafe_decision
from agent_py_agent.agent.backends.errors import ProviderRecoverableError
from agent_py_agent.agent.conversation import decision_policy
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as settings,
)
from agent_py_agent.agent.settings.decision_settings_schema import decision_field_scopes
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.cli import skill_proposal_commands as commands
from agent_py_agent.cli.parser import main as cli_main
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import host_at, patch
from agent_py_agent.tests.test_decision_skill_proposal_review import ids, seeded, trees
from agent_py_agent.tests.test_tui_decision_menu import (
    Gateway,
    choose,
    open_scope,
    point_index,
    press,
    running,
    visible,
)

POINT = "skill_proposal_review"


# 函数用途: 写只含临时 home 的 CLI 配置文件。
def config_at(tmp_path: Path) -> Path:
    path = tmp_path / "agent_config.yaml"
    path.write_text(f'my_agent_home: "{tmp_path / "home"}"\n', encoding="utf-8")
    return path


# 函数用途: 按 CLI 自己的 owner 解析取得命令上下文（含决策宿主与提案服务）。
def cli_context(config_path: Path):
    return commands._owner_context(SimpleNamespace(config=str(config_path)))


# LLM: 决策宿主与 CLI 同一 owner；只写临时 home 的模型目录与决策设置，保存本身不发请求。
# 函数用途: 按 CLI 口径绑定一个 Jev 模型并设置本点模式，changes 为额外的 owner 字段覆盖。
def configure(config_path: Path, mode: str, changes: dict | None = None) -> object:
    host = cli_context(config_path).decision_host
    key, _ = decision(host)
    patch(host, {"enabled": True, "profile_id": key, f"points.{POINT}.mode": mode, **(changes or {})})
    return host


# LLM: 请求仍经原 prepare/窗口门/请求头与响应解析；返回值按题号给出选择，缺省 normal，概率与候选键一致。
# 函数用途: 记录实际外发载荷，并按 choices 返回合法 TypeSafe 响应；during 可在等待期间制造并发变化或故障。
def install_http(monkeypatch, choices: dict | None = None, during=None) -> list:
    sent = []

    def post(request):
        sent.append(request.payload)
        if during is not None:
            during()
        answers = {}
        for key, question in request.payload["questions"].items():
            value = (choices or {}).get(key, "normal")
            answers[key] = {"type": "choice", "choice": value, "confidence": 0.8,
                            "probabilities": {name: float(name == value) for name in question["criteria"]}}
        return {"model": "fixture-decision", "answers": answers, "usage": {"input_tokens": 41}}

    monkeypatch.setattr(typesafe_decision, "post_json", post)
    return sent


# 函数用途: 经真实 CLI 入口执行一次 list（extra 为附加参数，如 --json），返回退出码与原始标准输出。
def cli(capsys, config_path: Path, extra: tuple[str, ...] = ()) -> tuple[int, str]:
    code = cli_main(["--config", str(config_path), "skills", "proposals", "list", *extra])
    return code, capsys.readouterr().out


# LLM: 按 S1 原实现的字段与中文格式重建期望输出，证明未采用时 --json 与中文输出逐字节不变。
# 函数用途: 生成 S1 版本 list（全部待确认、开关关闭）的 --json 与中文输出。
def s1_outputs(context) -> tuple[str, str]:
    proposals = context.service.list()
    payload = {"command": "skills proposals list", "ok": True, "owner_id": context.owner_id,
               "proposal_dir": str(context.service.directory), "self_learning_enabled": False, "status_filter": "",
               "count": len(proposals), "proposals": [item.to_record() for item in proposals]}
    human = [f"Skill 提案（owner {context.owner_id}，共 {len(proposals)} 条；自学习开关：关闭）"]
    for item in proposals:
        human.append(f"- {item.proposal_id} [待确认] 版本 {item.revision} 目标 {item.target.skill_name} "
                     f"来源任务 {'、'.join(item.source.task_ids)}")
        human.append(f"  {item.draft.description}")
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "\n".join(human) + "\n"


# 函数用途: 记录 CLI 每次解析出的命令上下文，供读取其决策宿主上的原调用账。
def spy_contexts(monkeypatch) -> list:
    contexts = []
    original = commands._owner_context

    def spy(args):
        contexts.append(original(args))
        return contexts[-1]

    monkeypatch.setattr(commands, "_owner_context", spy)
    return contexts


@pytest.mark.parametrize("case", [("off", 3, True), ("apply", 3, False), ("apply", 1, True), ("apply", 0, True)])
def test_off_disabled_or_fewer_than_two_pending_is_byte_identical_without_requests(tmp_path, monkeypatch, capsys, case):
    mode, count, enabled = case
    ctx = seeded(tmp_path, count)
    config_path = config_at(tmp_path)
    configure(config_path, mode, {"enabled": enabled})
    sent = install_http(monkeypatch, {"proposal_2": "review_first"})
    expected_json, expected_human = s1_outputs(cli_context(config_path))
    before = trees(ctx)
    assert cli(capsys, config_path, ("--json",)) == (0, expected_json)
    assert cli(capsys, config_path) == (0, expected_human)
    assert sent == [] and trees(ctx) == before


def test_observe_requests_and_ledgers_but_keeps_output_identical(tmp_path, monkeypatch, capsys):
    ctx = seeded(tmp_path, 3)
    config_path = config_at(tmp_path)
    configure(config_path, "observe")
    sent = install_http(monkeypatch, {"proposal_3": "review_first"})
    expected_json, expected_human = s1_outputs(cli_context(config_path))
    contexts = spy_contexts(monkeypatch)
    before = trees(ctx)
    assert cli(capsys, config_path, ("--json",)) == (0, expected_json)
    assert cli(capsys, config_path) == (0, expected_human)
    assert len(sent) == 2 and trees(ctx) == before
    records = [context.decision_host._model_call_ledger.records() for context in contexts]
    assert [[(row.metadata["purpose"], row.status) for row in rows] for rows in records] == [[("decision", "finished")]] * 2


def test_apply_reorders_display_with_host_labels_and_json_block(tmp_path, monkeypatch, capsys):
    ctx = seeded(tmp_path, 4)
    config_path = config_at(tmp_path)
    configure(config_path, "apply")
    sent = install_http(monkeypatch, {"proposal_1": "review_later", "proposal_2": "possible_duplicate",
                                      "proposal_4": "review_first"})
    before, p = trees(ctx), ctx.proposals
    code, out = cli(capsys, config_path, ("--json",))
    payload = json.loads(out)
    assert code == 0 and [row["proposal_id"] for row in payload["proposals"]] == ids([p[3], p[2], p[0], p[1]])
    assert payload["count"] == 4 and payload["review_order"] == {"point": POINT, "mode": "apply", "status": "applied", "order": [
        {"alias": "proposal_4", "proposal_id": p[3].proposal_id, "suggestion": "review_first", "label": "建议优先审核"},
        {"alias": "proposal_3", "proposal_id": p[2].proposal_id, "suggestion": "normal", "label": ""},
        {"alias": "proposal_1", "proposal_id": p[0].proposal_id, "suggestion": "review_later", "label": "建议稍后"},
        {"alias": "proposal_2", "proposal_id": p[1].proposal_id, "suggestion": "possible_duplicate",
         "label": "可能与其他提案重复"}]}
    code, human = cli(capsys, config_path)
    lines = human.splitlines()
    assert code == 0 and lines[1] == commands._REVIEW_NOTE and len(lines) == 10
    assert lines[2].startswith(f"- {p[3].proposal_id} ") and lines[2].endswith(" 〔建议优先审核〕")
    assert lines[4].startswith(f"- {p[2].proposal_id} ") and "〔" not in lines[4]
    assert lines[6].endswith(" 〔建议稍后〕") and lines[8].endswith(" 〔可能与其他提案重复〕")
    assert len(sent) == 2 and trees(ctx) == before


def test_real_request_is_aliased_redacted_and_carries_no_host_ids(tmp_path, monkeypatch, capsys):
    secret = "sk-" + "Z9y8X7w6V5u4T3s2R1q0"
    ctx = seeded(tmp_path, 3, lesson=f"部署前核对 token={secret} 与 api_key: hunter2hunter2")
    config_path = config_at(tmp_path)
    configure(config_path, "apply")
    sent = install_http(monkeypatch)
    assert cli(capsys, config_path, ("--json",))[0] == 0
    text = json.dumps(sent[0], ensure_ascii=False)
    private = [str(tmp_path), secret, "hunter2hunter2", "run-secret-", "task-secret-", "memory-candidate-", "skill_proposals",
               "/artifacts/", *ids(ctx.proposals), *(item.target.skill_name for item in ctx.proposals)]
    assert [value for value in private if value in text] == []
    assert set(sent[0]) == {"state", "questions", "model"} and "<redacted>" in text
    assert text.count("<untrusted_tool_result") == 3
    assert [row["proposal"] for row in sent[0]["state"]["proposals"]] == ["proposal_1", "proposal_2", "proposal_3"]


def test_proposal_rejected_during_request_keeps_original_output(tmp_path, monkeypatch, capsys):
    ctx = seeded(tmp_path, 3)
    config_path = config_at(tmp_path)
    configure(config_path, "apply")
    target = ctx.proposals[1].proposal_id
    sent = install_http(monkeypatch, {"proposal_3": "review_first"}, during=lambda: ctx.service.reject(target, 1))
    code, out = cli(capsys, config_path, ("--json",))
    payload = json.loads(out)
    assert code == 0 and len(sent) == 1 and "review_order" not in payload
    assert [row["proposal_id"] for row in payload["proposals"]] == ids(ctx.proposals)
    assert [row["status"] for row in payload["proposals"]] == ["pending_confirmation"] * 3


@pytest.mark.parametrize("value", ["need_data", "not_needed", "abstain", "bogus"])
def test_non_selection_or_invalid_real_answer_keeps_original(tmp_path, monkeypatch, capsys, value):
    ctx = seeded(tmp_path, 3)
    config_path = config_at(tmp_path)
    configure(config_path, "apply")
    sent = install_http(monkeypatch, {"proposal_1": value, "proposal_3": "review_first"})
    expected_json, _ = s1_outputs(cli_context(config_path))
    assert cli(capsys, config_path, ("--json",)) == (0, expected_json) and len(sent) == 1


def test_provider_error_then_cooldown_keep_original_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(decision_policy, "_FAILURES", OrderedDict())
    ctx = seeded(tmp_path, 3)
    config_path = config_at(tmp_path)
    configure(config_path, "apply")

    def fail():
        raise ProviderRecoverableError("simulated outage")

    sent = install_http(monkeypatch, {"proposal_3": "review_first"}, during=fail)
    expected_json, _ = s1_outputs(cli_context(config_path))
    before = trees(ctx)
    assert cli(capsys, config_path, ("--json",)) == (0, expected_json)
    assert cli(capsys, config_path, ("--json",)) == (0, expected_json)
    assert len(sent) == 1 and trees(ctx) == before
    assert [row[0] for row in decision_policy._FAILURES.values()] == ["cooldown"]


def test_timeout_keeps_original_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(decision_policy, "_FAILURES", OrderedDict())
    seeded(tmp_path, 3)
    config_path = config_at(tmp_path)
    configure(config_path, "apply", {f"points.{POINT}.timeout_seconds": 0.2})
    sent = install_http(monkeypatch, {"proposal_3": "review_first"}, during=lambda: time.sleep(0.6))
    expected_json, _ = s1_outputs(cli_context(config_path))
    started = time.monotonic()
    assert cli(capsys, config_path, ("--json",)) == (0, expected_json)
    assert len(sent) == 1 and time.monotonic() - started < 0.55


def test_interrupt_during_request_fails_the_command_without_listing(tmp_path, monkeypatch, capsys):
    ctx = seeded(tmp_path, 3)
    config_path = config_at(tmp_path)
    configure(config_path, "apply")

    def stop():
        raise InterruptedError("user stop")

    install_http(monkeypatch, during=stop)
    before = trees(ctx)
    code, out = cli(capsys, config_path, ("--json",))
    payload = json.loads(out)
    assert code == 1 and payload["error_code"] == "SKILL_PROPOSAL_CLI_INTERRUPTEDERROR" and "proposals" not in payload
    assert trees(ctx) == before


def test_thirty_long_proposals_pass_the_real_window_gate(tmp_path, monkeypatch, capsys):
    ctx = seeded(tmp_path, 30, lesson="修改共享状态前先读取当前版本再比较交换写入" * 60, goal="目标" * 400)
    config_path = config_at(tmp_path)
    configure(config_path, "apply")
    sent = install_http(monkeypatch, {"proposal_30": "review_first"})
    code, out = cli(capsys, config_path, ("--json",))
    payload = json.loads(out)
    assert code == 0 and len(sent) == 1 and len(sent[0]["questions"]) == 30
    assert payload["proposals"][0]["proposal_id"] == ctx.proposals[29].proposal_id


def test_config_defaults_are_off_and_point_is_owner_only(tmp_path):
    config = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    defaults = AgentConfig()
    for suffix in ("mode", "timeout_seconds", "profile_id"):
        name = f"decision_{POINT}_{suffix}"
        assert getattr(config, name) == getattr(defaults, name) == ("off" if suffix == "mode" else None)
        assert decision_field_scopes()[f"points.{POINT}.{suffix}"] == ["owner"]
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    with pytest.raises(ModelProfileError, match="作用范围"):
        patch(host, {f"points.{POINT}.mode": "apply"}, thread_id=thread.thread_id, scope="thread")
    view = settings(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    row = view["effective"]["points"][POINT]
    assert (row["mode"], row["effective_mode"], row["runtime_scope"], row["timeout_seconds"]) == (
        "off", "off", "owner_background", 4.0)
    assert view["sources"][f"points.{POINT}.mode"] == f"agent_config.decision_{POINT}_mode"
    assert view["sources"][f"points.{POINT}.timeout_seconds"].startswith("inherit:background_timeout_seconds:")
    patch(host, {"enabled": True, f"points.{POINT}.mode": "apply"})
    assert settings(host, "read", {})["effective"]["points"][POINT]["effective_mode"] == "apply"


def test_tui_owner_menu_lists_point_and_saves_mode(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path)
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui)
            await choose(ui, 6)
            assert "Skill 提案审核顺序（用户长期）" in visible(ui.app)
            await choose(ui, point_index(gateway, POINT))
            await choose(ui, 0)
            await press(ui, b"\x1b[B\x1b[B\r")
            assert settings(gateway.host, "read", {})["overrides"]["owner"][f"points.{POINT}.mode"] == "apply"
    asyncio.run(scenario())


def test_tui_thread_menu_does_not_offer_owner_only_point(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path)
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui, thread=True)
            await choose(ui, 5)
            assert "Skill 提案审核顺序" not in visible(ui.app)
            assert "交付复核焦点" in visible(ui.app)
    asyncio.run(scenario())
