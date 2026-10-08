# LLM: 包装产品原生成层，并临时改隔离用户能力配置开关后还原；原用量与选包保留，回答在工具循环消费前替换，零模型工具执行。
# 模块用途: 在单进程中测量 A/B/C 的真实 Gateway 首请求，可使用离线脚本化后端校准工具。
from __future__ import annotations

import json
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_py_agent.agent.agent_core import _tool_loop_service
from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
from agent_py_agent.agent.backends.base import (
    BaseBackend,
    ModelResponse,
    ProviderToolCapability,
    _utc_now_iso,
)
from agent_py_agent.agent.backends.gateway_helpers import _emit_provider_attempt
from agent_py_agent.agent.capability import package_selection, router
from agent_py_agent.agent.capability.runtime_config_reload import (
    capability_config_for_agent,
    capability_config_path_for,
)
from agent_py_agent.agent.common.nofollow_fs import write_bytes_atomic_beneath
from agent_py_agent.agent.contracts.model_call_ledger import model_call_purpose
from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    _process_gateway_requests,
    gateway_paths,
    submit_gateway_ask,
)
from agent_py_agent.agent.settings.config_io import set_simple_yaml_raw
from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id
from scripts.eval.pack_pick_results import judge, judge_packages, usage_fields


# LLM: 每例显式冻结题目、组别和重复编号，不从模型回答推断样本身份。
# 类用途: 传递一例测量的结构化参数。
@dataclass(frozen=True)
class BenchCase:
    question: dict
    arm: str
    repeat: int
    profile: str = ""
    fake: bool = False


# LLM: 开关按原用户路径临时写文件，使原现读入口生效；预算仍沿原缓存覆盖，不热刷新，正常/异常都恢复文件和内存。
# 函数用途: A/B 关闭、C 开启隔离选包，B 仅换采用行；不写产品源码、随包默认或真实 owner home。
@contextmanager
def arm_context(agent: object, arm: str):
    if arm not in {"A", "B", "C"}:
        raise ValueError("未知实验组")
    path = _selection_config_path(agent)
    config = capability_config_for_agent(agent)
    if config is None:
        raise ValueError("隔离能力配置不可读")
    with ExitStack() as stack:
        stack.enter_context(_selection_switch_file(path, arm == "C"))
        stack.enter_context(patch.object(agent, "_capability_config_runtime_snapshot",
                                        SimpleNamespace(config=replace(config, enable_capability_package_selection=arm == "C")), create=True))
        if arm == "B":
            stack.enter_context(patch.object(router, "_CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS", _stronger_rule()))
        yield


# LLM: 复用测量器的真实 home/别名/祖先保护；用户路径沿原显式优先级，解析后必须仍在隔离 owner 根内，先查边界再读配置。
# 函数用途: 给实验臂确定唯一可写的用户能力配置，不猜 agent_config 所在目录，也不写随包模板。
def _selection_config_path(agent: object):
    from scripts.eval.pack_pick_bench import safe_home

    root = getattr(agent, "root", None)
    if root is None:
        raise ValueError("实验臂必须绑定隔离 owner 根目录")
    root = safe_home(root)
    path = capability_config_path_for(agent).expanduser().resolve()
    if root not in path.parents:
        raise ValueError("用户能力配置不能越出隔离 owner 根目录")
    return path


# LLM: 原标量写入器只改一个布尔键；finally 复用 no-follow 原子字节写回，保留原权限（包括只读）及 BOM/CRLF，不热刷新预算。
# 函数用途: 临时设置隔离用户开关后原子恢复原字节/权限，或删除新文件；有写盘副作用，不触发安装、授权或模型工具。
@contextmanager
def _selection_switch_file(path, enabled: bool):
    original = path.read_bytes() if path.exists() else None
    original_mode = stat.S_IMODE(path.stat().st_mode) if original is not None else 0o600
    try:
        if original is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600)
        set_simple_yaml_raw(path, "enable_capability_package_selection", "true" if enabled else "false")
        yield
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            write_bytes_atomic_beneath(path.parent, (path.name,), original, file_mode=original_mode)
            # 原子写的创建权限仍受 umask 约束，替换后恢复精确的存量权限位。
            path.chmod(original_mode)


# LLM: 只替换明确的采用行，产品规则形状变了则拒绝，不悄悄把其它规则改成新版。
# 函数用途: 生成任务指定的 B 组采用说法。
def _stronger_rule() -> str:
    lines = router._CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS.splitlines()
    positions = [index for index, line in enumerate(lines) if line.startswith("- 采用：")]
    if len(positions) != 1:
        raise ValueError("产品采用规则无法唯一定位")
    lines[positions[0]] = "- 采用：用户的需求和某个能力包沾边，就先读它的入口看一眼，确认不相关再照常处理；用户点名能力包时必须先读入口。"
    return "\n".join(lines)


# LLM: 保存的只有投影工具身份与选择 refs 的包编号；真实 response 只在栈内保留，不能执行或写入业务产物。
# 类用途: 包装一次主生成和一次宿主选择，阻止下一轮业务模型请求。
class FirstCallTap:
    # LLM: 每例独立状态，原函数提前冻结以避免 patch 后自递归。
    # 函数用途: 初始化单例观察器。
    def __init__(self):
        self.calls = []
        self.selected = None
        self.selection_error = ""
        self.selection_warnings = []
        self.request_id = ""
        self.generate = _tool_loop_service.generate_model_response
        self.parse_selection = package_selection._selected_references

    # LLM: 原生成做正常输入/后端/账本；仅关闭输出流观察以免工具参数正文落盘，替换后响应不带任何工具或思考。
    # 函数用途: 捕获第一步工具意图，交给循环固定终答，第二次请求直接报错。
    def primary(self, request):
        if self.calls:
            raise RuntimeError("BENCH_SECOND_PRIMARY_REQUEST")
        self.request_id = request.params.request_id
        self.selection_state(request)
        quiet = replace(request, params=replace(request.params, effective_on_chunk=None))
        response = self.generate(quiet)
        self.calls.append(judge([], response.tool_use_blocks))
        return ModelResponse(text="测量结束；未执行模型工具。", backend=response.backend)

    # LLM: 包选择函数完全原样运行，只包装它的原严格解析接缝；不替宿主补 refs、读取、pin 或选择状态。
    # 函数用途: 记录原解析真正通过的包编号，返回未经修改的原结果。
    def selection(self, material, response):
        refs, error = self.parse_selection(material, response)
        self.selected = [dict(ref)["package_id"] for ref in refs]
        self.selection_error = error
        return refs, error

    # LLM: 第一轮生成发生在原选择收口之后；只读同一 task 的原回执，以包含入口预算/失败诊断，不能读别的最新任务。
    # 函数用途: 将宿主真实准备告警与未调用失败补入最小观察。
    def selection_state(self, request):
        task_id = (request.params.task_attributes or {}).get("conversation_task_id") or request.params.task_id
        task = request.agent.conversation_store.tasks.load(task_id) if task_id else None
        marker = task.capability_selection if task else None
        if marker is not None and marker.request_id == self.request_id:
            self.selection_warnings = list(marker.warning_codes)
            if marker.outcome == "failed" and not self.selection_error:
                self.selection_error = "CAPABILITY_SELECTION_FAILED"


# LLM: 只用于测量器自测，按结构化 fixture 的 positive_packs 给脚本化回答；不解析 query，不用于召回评估。
# 类用途: 假传输仍通过原主/辅助模型账、工具协议及提示装配。
class FixtureBackend(BaseBackend):
    name = "anthropic_compatible"
    model_name = "pack-pick-fixture"
    context_window_tokens = 200000
    max_tokens = 65536

    # LLM: 只保存测试预先确定的包编号，不访问真实凭据或网络。
    # 函数用途: 指定此例的脚本化返回。
    def __init__(self, positive: list[str]):
        self.positive = positive[:1]
        calls = [{"id": "fixture-get", "name": "skill_search", "input": {"action": "get", "package_id": name}} for name in self.positive]
        # 标准库 Mock 接受原 BaseBackend 的完整传输签名，不新增 **kwargs 服务接口或影子循环。
        self.generate = Mock(return_value=ModelResponse("", self.name, tool_use_blocks=calls,
                             usage={"input_tokens": 41, "output_tokens": 7, "cache_read_input_tokens": 13, "cache_write_input_tokens": 0}))

    # LLM: 假后端只声明自测的工具能力，不能复用为真实供应商的能力证明。
    # 函数用途: 让原启动探针通过离线 native 合同，不调用网络。
    def probe_tool_capability(self):
        return ProviderToolCapability(provider=self.name, endpoint="local://pack-pick-fixture", model=self.model_name,
                                      stream=False, native_supported=True, evidence="fixture_declares_native", observed_at=_utc_now_iso())

    # LLM: 用选择 schema 限定合法 fake ID，仍经真实 selected_ids 解析，不直接注入准备结果。
    # 函数用途: 返回脚本化选择，用不同用量验证主/辅助观察点没有混账。
    def generate_structured(self, prompt, *, response_schema, messages=None):
        _emit_provider_attempt({"attempt_id": "fixture-select", "status": "finished", "http_status": 200})
        ids = response_schema["properties"]["selected_ids"]["items"]["enum"]
        selected = [f"capability:{name}" for name in self.positive if f"capability:{name}" in ids]
        return ModelResponse(json.dumps({"selected_ids": selected}), self.name,
                             usage={"input_tokens": 17, "output_tokens": 9, "cache_read_input_tokens": 0, "cache_write_input_tokens": 0})


# LLM: 每例独立新会话走 submit/process 的原入口；不启动 Gateway 服务，必须无旧队列，真实工具表不裁剪。
# 函数用途: 提交和处理一个首句，返回选择与首次主请求的最小观察行。
def run_case(agent: object, case: BenchCase) -> list[dict]:
    import uuid

    session = "pack-pick-" + uuid.uuid4().hex
    thread = agent.conversation_store.threads.get_or_create({"canonical_user_id": "local/main", "channel": "chat",
                "channel_conversation_id": session, "channel_user_id": "local-agent"})
    if case.profile:
        thread_model_profile_id(agent, thread.thread_id, select=case.profile)
    paths = gateway_paths(agent)
    _prepare_queue(paths)
    tap = FirstCallTap()
    with ExitStack() as stack:
        stack.enter_context(arm_context(agent, case.arm))
        stack.enter_context(patch.object(_tool_loop_service, "generate_model_response", tap.primary))
        stack.enter_context(patch.object(package_selection, "_selected_references", tap.selection))
        if case.fake:
            stack.enter_context(patch.object(agent, "backend", FixtureBackend(case.question["positive_packs"])))
        request_id, _, response_path = submit_gateway_ask(paths, params=GatewayAskParams(
            prompt=case.question["query"], save=False, chat_session_id=session, agent=agent))
        _process_gateway_requests(agent, paths)
    response = json.loads(response_path.read_text(encoding="utf-8"))
    if response.get("tool_calls"):
        raise RuntimeError("BENCH_TOOL_EXECUTED")
    rows = _case_rows(agent, case, tap, (request_id, thread.thread_id))
    ok = bool(response.get("ok")) and len(tap.calls) == 1
    return [dict(row, request_ok=ok, request_error=str(response.get("error_code") or ("primary_missing" if not ok else "")))
            for row in rows]


# LLM: 不能把不属于这次测量的积压消息发给模型；只建产品已有的队列目录。
# 函数用途: 准备本进程文件队列，并拒绝旧未处理请求。
def _prepare_queue(paths: object) -> None:
    for path in (paths.inbox, paths.processing):
        if path.exists() and any(path.glob("*.json")):
            raise ValueError("隔离目录还有未处理请求，拒绝混跑")
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)


# LLM: 原 ModelCallLedger 按 request_id 过滤，持久线程用量账必须存在；每个物理调用单列，估算缺报不补零。
# 函数用途: 合并样本身份、选择/工具意图和实际用量，不复制模型正文。
def _case_rows(agent, case: BenchCase, tap: FirstCallTap, identity: tuple) -> list[dict]:
    request_id, thread_id = identity
    events, errors = agent.conversation_store.model_usage.events_report(thread_id)
    if errors:
        raise RuntimeError("BENCH_USAGE_LEDGER_MISSING")
    records = [row for row in model_call_ledger(agent).records() if row.request_id == request_id]
    if records and not events:
        raise RuntimeError("BENCH_USAGE_LEDGER_MISSING")
    rows = [_record_row(case, tap, identity, record) for record in records]
    required = {"main", "selection"} if case.arm == "C" else {"main"}
    missing = required - {row["phase"] for row in rows}
    return rows + [_observation_row(case, tap, identity, phase) for phase in sorted(missing)]


# LLM: 未建物理调用不能伪造 token/耗时；额外观察行显式标明非调用，保留未触发/请求失败的样本分母。
# 函数用途: 补齐没有调用的主请求或 C 选择观察点。
def _observation_row(case, tap, identity, phase) -> dict:
    from scripts.eval.pack_pick_results import observation_fields

    return {**_sample_fields(case, tap, identity), "phase": phase, "record_kind": "observation", **observation_fields()}


# LLM: 样本身份只来自输入与真实请求返回值，诊断只存结构化错误码，不复制正文。
# 函数用途: 生成主请求和选择共用的最小样本字段。
def _sample_fields(case, tap, identity) -> dict:
    return {"arm": case.arm, "repeat": case.repeat, "id": case.question["id"],
            "lang": case.question["lang"], "query_domain": case.question["query_domain"],
            "positive_packs": case.question["positive_packs"], "request_id": identity[0], "thread_id": identity[1],
            "selected_packs": tap.selected or [], "selection_error": tap.selection_error,
            "selection_warnings": tap.selection_warnings, "fake": case.fake}


# LLM: phase 只读账本 purpose，不能按请求正文猜；selection 的选中与主 get 意图各自判定。
# 函数用途: 生成一次物理调用的 JSONL 行。
def _record_row(case, tap, identity, record) -> dict:
    phase = "selection" if record.metadata.get("purpose") == "capability_selection" else model_call_purpose(record)
    facts = judge(case.question["positive_packs"], [])
    if phase == "main" and record.status == "finished" and tap.calls:
        facts = {**tap.calls[0], **judge_packages(case.question["positive_packs"], tap.calls[0]["opened_packs"])}
    selected = tap.selected or []
    if phase == "selection" and not tap.selection_error and record.status == "finished":
        facts.update(judge_packages(case.question["positive_packs"], selected))
    return {**_sample_fields(case, tap, identity), "phase": phase, "record_kind": "model_call", **facts, **usage_fields(record)}
