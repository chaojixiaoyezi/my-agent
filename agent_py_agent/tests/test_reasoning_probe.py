"""智能程度自动检测（2026-09-27）：宿主用已保存的凭据检测当前模型是否真的按推理强度档位调节思考。

全部用假传输（真实 OpenAI 兼容后端，只替换 request_json），零网络。锁定：
- 判定规则用 2026-09-26 实测数据回放标定：DeepSeek 官方接口判支持，OpenCode 中转、MiniMax M2.7、噪声大的完整主代理上下文判不支持；
  阈值边界、拒绝字段、请求失败、没有用量、全为 0 各有结论。
- /effort 设成 auto 以外的档位且模型未声明、解析为不支持时自动检测一次；出站请求确实按 low / max / 不带字段各 3 次；
  确认支持后经参数中心写进本人档案并记账，/effort revert 能撤销；开关关、auto 档、显式声明 none、已知服务商都不自动检测。
- /effort probe 手动检测；进行中只跑一个；部署默认模型只有管理员触发时写全局配置；凭据不出现在回执、记录与账本里。
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import execute_gateway_conversation_control
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings import reasoning_probe as probe
from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)
from agent_py_agent.agent.settings.parameter_changes import (
    ChangeOrigin,
    ProfileFieldChange,
    ledger_path,
    profile_change_history,
    revert_profile_change,
    set_profile_field,
)
from agent_py_agent.agent.settings.reasoning_probe_judge import (
    ProbeSample,
    judge_reasoning_samples,
    output_token_count,
    reasoning_token_count,
)
from agent_py_agent.cli.chat_parts.control_runtime import _command_text
from agent_py_agent.tests.test_gateway_conversation_control import _command, _scope
from agent_py_agent.tests.test_model_profiles import Host, add

_UNKNOWN = "https://relay.example.test/v1"
_KEY = "sk-FAKE-PROBE-KEY-7788"


# 函数用途: 带推理 token 的 OpenAI Chat 用量。
def _reasoning(count: int) -> dict:
    return {"completion_tokens": count + 12, "completion_tokens_details": {"reasoning_tokens": count}}


# 函数用途: 只有输出 token 的用量（中转接口常见）。
def _output(count: int) -> dict:
    return {"completion_tokens": count}


# 2026-09-26 实测（docs/design/REASONING_EFFORT.md 第 2 节），同一道题。每组按记录值循环回放。
# DeepSeek 官方 OpenAI 兼容（v4-flash）推理 token：不带参数 1925、low 919、max 1887（各测一次）。
_DEEPSEEK = {"low": [919], "max": [1887], "": [1925]}
# OpenCode Go 中转（deepseek-v4-flash）输出 token：不带参数约 1100–1200，low 2234 / 2664；max 未测，
# 字段不生效时与不带参数同分布，取默认组的记录值。
_OPENCODE = {"low": [2234, 2664], "max": [1100, 1200], "": [1100, 1200]}
# MiniMax M2.7（Anthropic 兼容）输出 token：默认 5620，限制思考后仍 4714，output_config.effort 不生效。
_MINIMAX = {"low": [5620, 4714], "max": [4714, 5620], "": [5620]}
# 完整主代理上下文里的 DeepSeek 输出 token（TESTS.md 同日真实验收）：low 4 次 1059–2185（均值约 1536），
# max 4 次 1494–5392（均值约 2847），auto 3 次 1234–5192；取最小、均值（auto 取中点）、最大。
_AGENT_CONTEXT = {"low": [1059, 1536, 2185], "max": [1494, 2847, 5392], "": [1234, 3213, 5192]}


# 函数用途: 按回放数据生成三组各 3 次的样本（usage 形状由 make 决定）。
def _samples(replay: dict, make=_reasoning) -> list[ProbeSample]:
    rows = []
    for level, values in replay.items():
        rows += [ProbeSample(level, reasoning_token_count(make(values[i % len(values)])),
                             output_token_count(make(values[i % len(values)]))) for i in range(3)]
    return rows


class _HttpError(Exception):
    # 函数用途: 带上游状态码的失败。
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status


class _Wire:
    """按档位回放 usage 的假传输：记录每次出站载荷与后端配置，不联网。"""

    # 函数用途: 准备回放数据与按档位注入的失败。
    def __init__(self, replay: dict, make=_reasoning, fail: dict | None = None) -> None:
        self.replay, self.make, self.fail = replay, make, fail or {}
        self.payloads: list[dict] = []
        self.configs: list[object] = []
        self.counts = dict.fromkeys(replay, 0)

    # 函数用途: 让检测模块建出的真实后端改用本假传输。
    def install(self, monkeypatch) -> _Wire:
        real = probe.get_backend

        def factory(name, config):
            self.configs.append(config)
            backend = real(name, config)
            monkeypatch.setattr(backend, "request_json", self.request_json)
            return backend

        monkeypatch.setattr(probe, "get_backend", factory)
        return self

    # 函数用途: 记录载荷，按档位回放 usage 或抛出注入的失败。
    def request_json(self, path, payload, headers):
        self.payloads.append(dict(payload))
        level = str(payload.get("reasoning_effort") or "")
        if level in self.fail:
            raise self.fail[level]
        values = self.replay[level]
        count = values[self.counts[level] % len(values)]
        self.counts[level] += 1
        return {"choices": [{"message": {"content": "64"}, "finish_reason": "stop"}], "usage": self.make(count)}


# 函数用途: 建一个带私有模型档案的 Agent，档案接口是未核对的中转地址（未声明控制方式时解析为不支持）。
def _agent(tmp_path: Path, *, base: str = _UNKNOWN, auto_probe: bool = True, **extra):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
                                    gateway_per_user_owner_scoping=False, stream_enabled=False,
                                    reasoning_control_auto_probe=auto_probe), tmp_path / "ws")
    profile_id = str(uuid4())
    result = execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": {
        "model_backend": "openai_compatible", "model_name": "relay-model", "api_base": base, "api_key": _KEY,
        "model_context_window_tokens": 128000, **extra}})
    assert result["ok"]
    execute_model_profile_operation(agent, "set_default", {"profile_id": profile_id})
    return agent, profile_id


# 函数用途: 以飞书会话身份执行一条 /effort。
def _run(agent, text: str):
    return execute_gateway_conversation_control(agent, gateway_paths(agent), _command(text), _scope())


# 函数用途: 取出 /effort 所在会话的线程编号。
def _run_thread(agent) -> str:
    _run(agent, "/effort")
    return agent.conversation_store.threads.resolve(channel="feishu", channel_conversation_id="c-1",
                                                    channel_user_id="u-1").thread_id


# 函数用途: 读出档案里声明的思考控制方式（未声明为 None）。
def _declared(agent, profile_id: str):
    return read_model_profiles(model_profiles_path(agent.home_paths))["profiles"][profile_id].get("reasoning_control")


# 函数用途: 检测记录、档案账本与档案文件的全部文本（检查有没有泄露凭据）。
def _stored_text(agent) -> str:
    paths = [probe.probe_records_path(agent.home_paths), model_profiles_path(agent.home_paths).with_suffix(".changes.jsonl")]
    return "".join(path.read_text(encoding="utf-8") for path in paths if path.exists())


def test_judge_is_calibrated_on_the_2026_09_26_replays():
    deepseek = judge_reasoning_samples(_samples(_DEEPSEEK))
    assert (deepseek.verdict, deepseek.reason, deepseek.measure) == ("supported", "max_above_low", "reasoning_tokens")
    assert (deepseek.low_median, deepseek.max_median, deepseek.default_median) == (919, 1887, 1925)
    opencode = judge_reasoning_samples(_samples(_OPENCODE, _output))
    assert (opencode.verdict, opencode.reason, opencode.measure) == ("unsupported", "no_difference", "output_tokens")
    assert judge_reasoning_samples(_samples(_MINIMAX, _output)).verdict == "unsupported"
    # 完整主代理上下文里 max 均值约为 low 的 1.9 倍，但两组有重叠：三次抽样分不清，所以检测只用独立短题
    context = judge_reasoning_samples(_samples(_AGENT_CONTEXT, _output))
    assert (context.verdict, context.low_median, context.max_median) == ("unsupported", 1536, 2847)


@pytest.mark.parametrize("low, high, expected", [
    ([400, 400, 400], [600, 600, 600], "supported"),      # 正好 1.5 倍、正好多 200、不重叠
    ([500, 500, 500], [720, 720, 720], "unsupported"),    # 多 220 但只有 1.44 倍
    ([300, 300, 300], [499, 499, 499], "unsupported"),    # 1.66 倍但只多 199
    ([400, 400, 900], [1000, 1000, 1000], "supported"),   # 不重叠：max 最少的一次仍多于 low 最多的一次
    ([400, 400, 1000], [1000, 1000, 1000], "unsupported"),  # 相等即重叠
    ([0, 0, 0], [800, 800, 800], "supported"),           # low 档直接不思考
])
def test_judge_thresholds(low, high, expected):
    samples = [ProbeSample("low", value) for value in low] + [ProbeSample("max", value) for value in high]
    samples += [ProbeSample("", 700)] * 3
    assert judge_reasoning_samples(samples).verdict == expected


def test_judge_edge_cases():
    rejected = [ProbeSample(level, status_code=400, error_type="ProviderError") for level in ("low", "max") for _ in range(3)]
    assert judge_reasoning_samples(rejected + [ProbeSample("", 900)] * 3).reason == "field_rejected"
    failed = [ProbeSample("low", 500)] * 2 + [ProbeSample("low", status_code=500, error_type="ProviderError")]
    incomplete = judge_reasoning_samples(failed + [ProbeSample("max", 900)] * 3 + [ProbeSample("", 900)] * 3)
    assert (incomplete.verdict, incomplete.reason) == ("inconclusive", "incomplete")
    assert judge_reasoning_samples([ProbeSample(level) for level in ("low", "max", "") for _ in range(3)]).reason == "no_usage"
    everything_failed = [ProbeSample(level, status_code=400, error_type="ProviderError") for level in ("low", "max", "")
                         for _ in range(3)]
    assert judge_reasoning_samples(everything_failed).reason == "incomplete"  # 不带字段的也失败，就不是“拒绝字段”
    zeros = judge_reasoning_samples([ProbeSample(level, 0) for level in ("low", "max", "") for _ in range(3)])
    assert (zeros.verdict, zeros.reason) == ("unsupported", "no_reasoning")
    # 推理 token 只要有一个成功样本缺，就整体改比输出 token，不混用两种计量
    mixed = [ProbeSample("low", 100, 400)] * 3 + [ProbeSample("max", None, 2000)] * 3 + [ProbeSample("", 100, 900)] * 3
    assert judge_reasoning_samples(mixed).measure == "output_tokens"


def test_token_counts_read_only_structured_usage_fields():
    assert reasoning_token_count(_reasoning(42)) == 42
    assert reasoning_token_count({"output_tokens_details": {"reasoning_tokens": 7}}) == 7
    assert reasoning_token_count({"completion_tokens_details": {"reasoning_tokens": True}}) is None
    assert reasoning_token_count({"completion_tokens_details": {"reasoning_tokens": -1}}) is None
    assert reasoning_token_count(None) is None and output_token_count({"output_tokens": 9}) == 9
    assert output_token_count({"completion_tokens": "9"}) is None


def test_effort_set_auto_probes_once_writes_the_profile_and_can_be_reverted(tmp_path, monkeypatch):
    agent, profile_id = _agent(tmp_path)
    wire = _Wire(_DEEPSEEK).install(monkeypatch)
    monkeypatch.setattr(probe, "_spawn", lambda target: target())
    first = _run(agent, "/effort high")
    assert first.ok and "当前模型还没检测过是否支持调节智能程度，已在后台开始检测" in first.message
    # 出站请求确实按档位发字段：low / max / 不带字段交替各 3 次，只带固定题目
    assert [payload.get("reasoning_effort") for payload in wire.payloads] == ["low", "max", None] * 3
    assert all(payload["messages"][-1]["content"] == probe.PROBE_PROMPT for payload in wire.payloads)
    assert all(config.model_reasoning_control == "effort" for config in wire.configs)
    assert _declared(agent, profile_id) == "effort"
    change_id = profile_change_history(agent.home_paths)[0]["id"]
    view = _run(agent, "/effort")
    assert "支持按档位调节——推理 token 中位数：“最高”档 1887，“低”档 919，不带参数 1925" in view.message
    assert f"下一轮对话起生效（修改编号 {change_id[:8]}，撤销：/effort revert {change_id[:8]}）" in view.message
    assert "按推理强度档位发送：高" in view.message
    reverted = _run(agent, f"/effort revert {change_id[:8]}")
    assert reverted.ok and "已撤销：模型 relay-model 的思考控制恢复为“自动”" in reverted.message
    assert _declared(agent, profile_id) is None and "不支持调节智能程度" in reverted.message  # 回执已是撤销后的效果
    again = _run(agent, "/effort max")
    assert len(wire.payloads) == 9 and "已在后台开始检测" not in again.message  # 已有记录，不再自动检测
    history = profile_change_history(agent.home_paths)
    assert [row["action"] for row in history] == ["revert", "set"] and history[0]["reverts"] == change_id
    assert history[1]["actor"] == "reasoning_probe" and history[1]["target"]["profile_id"] == profile_id
    assert _KEY not in "".join(result.message for result in (first, view, reverted, again)) + _stored_text(agent)


@pytest.mark.parametrize("case", ["switch_off", "auto_level", "declared_none", "known_host"])
def test_auto_probe_needs_every_condition(tmp_path, monkeypatch, case):
    base = "https://api.deepseek.com/v1" if case == "known_host" else _UNKNOWN
    extra = {"reasoning_control": "none"} if case == "declared_none" else {}
    agent, _profile_id = _agent(tmp_path, base=base, auto_probe=case != "switch_off", **extra)
    wire = _Wire(_DEEPSEEK).install(monkeypatch)
    monkeypatch.setattr(probe, "_spawn", lambda target: target())
    result = _run(agent, "/effort auto" if case == "auto_level" else "/effort high")
    assert result.ok and "开始检测" not in result.message
    assert wire.payloads == [] and not probe.probe_records_path(agent.home_paths).exists()


@pytest.mark.parametrize("case", [
    (_OPENCODE, _output, {}, "不支持调节——“低”和“最高”两档的输出 token 中位数分别是 2234 和 1100，没有明显差别"
                             "（该接口不单独报告推理 token，按输出 token 判定）"),
    (_DEEPSEEK, _reasoning, {"low": _HttpError(500)}, "没有得出结论——有请求失败"),
    (_DEEPSEEK, _reasoning, {"low": _HttpError(400), "max": _HttpError(400)}, "服务商拒绝了推理强度参数"),
])
def test_manual_probe_records_unsupported_or_inconclusive_without_touching_the_profile(tmp_path, monkeypatch, case):
    replay, make, fail, expected = case
    agent, profile_id = _agent(tmp_path, reasoning_control="none")  # 手动检测不受显式声明限制
    _Wire(replay, make, fail).install(monkeypatch)
    monkeypatch.setattr(probe, "_spawn", lambda target: target())
    started = _run(agent, "/effort probe")
    view = _run(agent, "/effort")
    assert started.ok and started.message.count("已在后台开始检测") == 1 and expected in view.message
    assert _declared(agent, profile_id) == "none" and profile_change_history(agent.home_paths) == []
    assert _KEY not in view.message + _stored_text(agent)


def test_manual_probe_skips_needless_writes_for_known_hosts(tmp_path, monkeypatch):
    _Wire(_DEEPSEEK).install(monkeypatch)
    monkeypatch.setattr(probe, "_spawn", lambda target: target())
    known, profile_id = _agent(tmp_path / "known", base="https://api.deepseek.com/v1")  # 已知表已按档位发送
    _run(known, "/effort probe")
    view = _run(known, "/effort")
    assert "支持按档位调节" in view.message and "档案里已经是“按推理强度档位发送”，无需修改" in view.message
    assert _declared(known, profile_id) is None and profile_change_history(known.home_paths) == []


def test_shared_models_are_only_recorded_and_records_of_profiles_coexist(tmp_path, monkeypatch):
    agent, first = _agent(tmp_path)
    _Wire(_DEEPSEEK).install(monkeypatch)
    monkeypatch.setattr(probe, "_spawn", lambda target: target())
    config = selected_model_config(agent, profile_id=first)
    job = probe._ProbeJob(agent, probe.ProbeTarget("shared:" + str(uuid4()), config), "manual", True, "shared-key")
    applied = probe._apply_supported(job)
    assert applied == {"ok": False, "kind": "shared", "code": "SHARED_PROFILE"}
    assert "管理员共享的模型，本次结果只做记录" in probe._applied_text(applied)
    _run(agent, "/effort probe")
    second = str(uuid4())
    execute_model_profile_operation(agent, "add", {"profile_id": second, "profile": {
        "model_backend": "openai_compatible", "model_name": "second-model", "api_base": _UNKNOWN, "api_key": _KEY,
        "model_context_window_tokens": 64000}})
    execute_model_profile_operation(agent, "select", {"profile_id": second}, thread_id=_run_thread(agent))
    _run(agent, "/effort probe")
    records = json.loads(probe.probe_records_path(agent.home_paths).read_text(encoding="utf-8"))["profiles"]
    assert set(records) == {first, second}  # 检测第二个模型不覆盖第一个的记录


def test_probe_in_progress_is_shown_once_and_stale_or_changed_records_expire(tmp_path, monkeypatch):
    agent, profile_id = _agent(tmp_path)
    _Wire(_DEEPSEEK).install(monkeypatch)
    jobs = []
    monkeypatch.setattr(probe, "_spawn", jobs.append)
    first, second = _run(agent, "/effort probe"), _run(agent, "/effort probe")
    running = _run(agent, "/effort")
    assert "已在后台开始检测" in first.message and "正在进行中" in second.message and len(jobs) == 1
    assert "智能程度检测进行中：已完成 0/9 次请求" in running.message
    path = probe.probe_records_path(agent.home_paths)
    first_started = json.loads(path.read_text(encoding="utf-8"))["profiles"][profile_id]["started_at"]
    jobs[0]()  # 后台线程跑完：记录变成结论，登记撤销，之后可以再检测
    assert "支持按档位调节" in _run(agent, "/effort").message
    target = probe.ProbeTarget(profile_id, selected_model_config(agent, profile_id=profile_id))
    renamed = probe.ProbeTarget(profile_id, selected_model_config(agent, profile_id=profile_id).__class__(
        model_backend="openai_compatible", api_base=_UNKNOWN, model_name="other-model"))
    assert probe.current_record(agent, target) is not None and probe.current_record(agent, renamed) is None  # 模型换了
    assert "已在后台开始检测" in _run(agent, "/effort probe").message
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["profiles"][profile_id]["started_at"] > first_started  # 新检测重新计时
    data["profiles"][profile_id]["started_at"] -= 7200  # 进程中断留下的 running 记录超时即失效
    path.write_text(json.dumps(data), encoding="utf-8")
    assert probe.current_record(agent, target) is None


def test_probe_that_cannot_record_does_not_start_and_can_be_retried(tmp_path, monkeypatch):
    agent, _profile_id = _agent(tmp_path)
    _Wire(_DEEPSEEK).install(monkeypatch)
    jobs = []
    monkeypatch.setattr(probe, "_spawn", jobs.append)
    real_save = probe._save_record

    def broken(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(probe, "_save_record", broken)
    assert "这次没有开始检测" in _run(agent, "/effort probe").message and jobs == []
    monkeypatch.setattr(probe, "_save_record", real_save)
    assert "已在后台开始检测" in _run(agent, "/effort probe").message and len(jobs) == 1  # 登记已撤销，可以重试


@pytest.mark.parametrize("admin", [True, False])
def test_default_model_is_written_to_global_config_only_by_admin(tmp_path, monkeypatch, admin):
    user_config = tmp_path / "desktop.yaml"
    user_config.write_text('agent_name: "myagent"\n', encoding="utf-8")
    agent = SimpleAgent(AgentConfig(model_backend="openai_compatible", model_name="relay-model", api_base=_UNKNOWN,
                                    api_key=_KEY, my_agent_home=str(tmp_path / "home"), prompt_files=[],
                                    gateway_per_user_owner_scoping=False, stream_enabled=False,
                                    config_path=str(user_config)), tmp_path / "ws")
    _Wire(_DEEPSEEK).install(monkeypatch)
    monkeypatch.setattr(probe, "_spawn", lambda target: target())
    monkeypatch.setattr("agent_py_agent.agent.user_space.approval_mode.is_permission_admin", lambda _home: admin)
    _run(agent, "/effort high")
    view = _run(agent, "/effort")
    written = 'model_reasoning_control: "effort"' in user_config.read_text(encoding="utf-8")
    assert written is admin
    if admin:
        change_id = json.loads(ledger_path(user_config).read_text(encoding="utf-8").splitlines()[-1])["id"]
        assert f"重启 Gateway 后生效（修改编号 {change_id[:8]}，撤销：/settings revert {change_id[:8]}）" in view.message
    else:
        assert "请管理员执行 /settings set model_reasoning_control effort" in view.message
    assert _KEY not in view.message + _stored_text(agent)


def test_effort_probe_and_revert_parse_and_round_trip_through_the_tui():
    probe_command = parse_conversation_control("/effort probe")
    revert_command = parse_conversation_control("/effort revert ABC123def")
    assert (probe_command.operation, probe_command.valid) == ("probe", True)
    assert (revert_command.operation, revert_command.value, revert_command.valid) == ("revert", "abc123def", True)
    for text in ("/effort revert ab12", "/effort revert", "/effort revert abc-123", "/effort revert abc123 more", "/effort turbo"):
        assert not parse_conversation_control(text).valid, text
    assert _command_text(revert_command) == "/effort revert abc123def" and _command_text(probe_command) == "/effort probe"


def test_profile_field_changes_are_whitelisted_validated_and_owner_scoped(tmp_path):
    agent, other = Host(tmp_path, "alice"), Host(tmp_path, "bob")  # 同一配置目录下的两个用户
    profile_id, _ = add(agent, reasoning_control="none")
    add(other)
    origin = ChangeOrigin("test")
    assert set_profile_field(agent, ProfileFieldChange(profile_id, "model_name", "x"), origin=origin)["code"] == "PROFILE_FIELD_UNKNOWN"
    missing = ProfileFieldChange(str(uuid4()), "reasoning_control", "effort")
    assert set_profile_field(agent, missing, origin=origin)["code"] == "PROFILE_NOT_FOUND"
    invalid = ProfileFieldChange(profile_id, "reasoning_control", "turbo")
    assert set_profile_field(agent, invalid, origin=origin)["code"] == "PROFILE_WRITE_FAILED"
    assert profile_change_history(agent.home_paths) == []  # 写失败不记账
    done = set_profile_field(agent, ProfileFieldChange(profile_id, "reasoning_control", "budget"), origin=origin)
    assert done["ok"] and done["previous"] == "none" and _declared(agent, profile_id) == "budget"
    assert revert_profile_change(other, done["change_id"], origin=origin)["code"] == "CHANGE_NOT_FOUND"
    assert revert_profile_change(agent, done["change_id"][:5], origin=origin)["code"] == "CHANGE_NOT_FOUND"
    assert revert_profile_change(agent, done["change_id"][:6], origin=origin)["ok"] and _declared(agent, profile_id) == "none"
    ledger = model_profiles_path(agent.home_paths).with_suffix(".changes.jsonl")
    with ledger.open("a", encoding="utf-8") as handle:  # 只有脱敏值的记录不能回滚
        handle.write(json.dumps({"id": "5ec7e7ab0000", "key": "reasoning_control", "masked": True, "previous": "***",
                                 "target": {"kind": "model_profile", "profile_id": profile_id}}) + "\n")
    assert revert_profile_change(agent, "5ec7e7ab", origin=origin)["code"] == "CHANGE_MASKED"


def test_auto_probe_switch_defaults_on_and_normalizes():
    assert AgentConfig().reasoning_control_auto_probe is True
    shipped = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    assert shipped.reasoning_control_auto_probe is True
    normalized, warnings = normalize_agent_config({"reasoning_control_auto_probe": "maybe"})
    assert normalized["reasoning_control_auto_probe"] is True and any("reasoning_control_auto_probe" in w for w in warnings)
    assert normalize_agent_config({"reasoning_control_auto_probe": "false"})[0]["reasoning_control_auto_probe"] is False


class _ResponsesWire(_Wire):
    """Responses 形状的假传输：按 reasoning.effort 回放推理 token（09-30 起 Responses 也按档位发送、可以检测）。"""

    # 函数用途: 记录载荷，按服务商档位回放 Responses 用量。
    def request_json(self, path, payload, headers):
        self.payloads.append(dict(payload))
        level = str((payload.get("reasoning") or {}).get("effort") or "")
        values = self.replay[level]
        count = values[self.counts[level] % len(values)]
        self.counts[level] += 1
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "64"}]}],
                "usage": {"output_tokens": count + 12, "output_tokens_details": {"reasoning_tokens": count}}}


def test_responses_models_are_auto_probed_and_confirmed(tmp_path, monkeypatch):
    # 未声明档位时“最高”按通用 high 发送；推理 token 低档 300、高档 1400（差距超过判定门槛 200），判定支持并写入 effort。
    wire = _ResponsesWire({"low": [300], "high": [1400], "": [900]}).install(monkeypatch)
    monkeypatch.setattr(probe, "_spawn", lambda target: target())
    agent, profile_id = _agent(tmp_path, model_backend="openai_responses")
    assert "已在后台开始检测" in _run(agent, "/effort high").message
    assert {str((row.get("reasoning") or {}).get("effort") or "") for row in wire.payloads} == {"low", "high", ""}
    assert len(wire.payloads) == 9 and _declared(agent, profile_id) == "effort"
    assert "支持按档位调节" in _run(agent, "/effort").message


def test_detection_requests_carry_output_cap_and_receipt_is_fact_based(tmp_path, monkeypatch):
    # C7：检测请求显式带输出上限（与档案上限取小后就是真实发送值）；回执的耗时/token 由次数 × 每次上限算出，
    # 不再写死“约需 1～5 分钟 / 少量 token”。用小上限区分“确实传了上限”与“沿档案默认”。
    agent, _profile_id = _agent(tmp_path)
    wire = _Wire(_DEEPSEEK).install(monkeypatch)
    monkeypatch.setattr(probe, "_spawn", lambda target: target())
    monkeypatch.setattr(probe, "PROBE_MAX_OUTPUT_TOKENS", 500)
    started = _run(agent, "/effort high")
    assert started.ok and "每次输出上限 500 token" in started.message
    assert "最多约 4500 token" in started.message  # 9 次 × 500
    assert "长输出模型可能更久" in started.message
    assert "约需 1～5 分钟" not in started.message and "会额外消耗少量 token" not in started.message
    assert all(payload.get("max_tokens") == 500 for payload in wire.payloads)  # min(档案 32000, 上限 500)


def test_judge_still_supports_when_max_output_hits_the_cap():
    # C7：输出被上限截断（max 档顶到上限）时，只要 low 档明显更低，判定仍应判“支持”，上限不能破坏判定。
    cap = probe.PROBE_MAX_OUTPUT_TOKENS
    samples = [ProbeSample("low", 400)] * 3 + [ProbeSample("max", cap)] * 3 + [ProbeSample("", 900)] * 3
    verdict = judge_reasoning_samples(samples)
    assert (verdict.verdict, verdict.reason) == ("supported", "max_above_low")


def test_effort_receipt_uses_effective_output_cap_when_config_is_lower(tmp_path, monkeypatch):
    # C7 加固：回执按实际生效的上限 min(探测常量, 档案输出上限) 算。窗口 128000 的档案输出
    # 上限是 32000（低于常量 40000），回执写 32000 和 288000，不再按常量虚报。
    agent, _profile_id = _agent(tmp_path)
    wire = _Wire(_DEEPSEEK).install(monkeypatch)
    monkeypatch.setattr(probe, "_spawn", lambda target: target())
    started = _run(agent, "/effort high")
    assert started.ok
    assert "每次输出上限 32000 token，最多约 288000 token" in started.message
    assert "40000" not in started.message
    assert all(payload.get("max_tokens") == 32000 for payload in wire.payloads)
