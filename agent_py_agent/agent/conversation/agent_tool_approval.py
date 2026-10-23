# LLM: 主代理缓存按原typed callback分前后台；后台scope携准确attempt也不免除LocalRunControl与同claim发布核验。
# LLM: 会话许可只存原Agent级有界缓存；每次以真实canonical owner/run/thread/claim/attempt和有效权限重核scope，重建展示sink不丢许可，旧attempt/插件不得借用。
# LLM: 原审批纯退休未结只同对象传播和核收；普通同码不升级，物理未知优先，不以清理完成签发终态或释放资源。
# LLM: 主/子后台共用原审批账；纯退休只写原permission负向事实、不取op或恢复执行权，忙保native未结；插件/claim/原schema保持。
# 模块用途: 将当次插件确认交给用户，批准、拒绝、取消和失联仍回原调用；C取消仅复用原Store路径，不另建状态源。

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic,
)
from ..common.opaque_id import validate_opaque_id
from ..contracts.tool_approval import (
    ToolApprovalDecision,
    ToolApprovalRequest,
    ToolApprovalWaitOptions,
    approval_deadline,
    plugin_gate_required,
)
from .tool_approval_scope import (
    ToolApprovalScope,
    tool_approval_scope,
    tool_approval_scope_is_current,
)

AGENT_TOOL_APPROVAL_SCHEMA = "subagent_tool_approval.v1"
AGENT_TOOL_APPROVAL_CONSUMER_SCHEMA = "subagent_tool_approval_consumer.v1"
# 子代理工具审批：发现/查询一次审批记录的等待上限 1.5 秒，避免阻塞主链路太久。
AGENT_TOOL_APPROVAL_DISCOVERY_SECONDS = 1.5
# 子代理工具审批：消费者拿到的租约 15 秒，超时未提交就让位，避免卡死的审批占住队列。
AGENT_TOOL_APPROVAL_CONSUMER_LEASE_SECONDS = 15.0
# 子代理工具审批：轮询间隔 0.05 秒（20 次/秒），足够灵敏又不烧 CPU。
AGENT_TOOL_APPROVAL_POLL_SECONDS = 0.05
# 子代理工具审批：同一时刻最多挂起 32 条审批，超出直接拒绝，防止积压。
AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT = 32


# LLM: 句柄冻结原canonical/request/claim及publish局部callback；callback不授权限，原wait线程必须现场重核，finally关闭。
# 类用途: 保存一条主/子后台审批请求的等待句柄。
@dataclass(frozen=True)
class AgentToolApprovalHandle:
    scope: ToolApprovalScope
    path: Path
    request: ToolApprovalRequest
    created_at: float
    scope_valid: Callable[[], bool] = field(repr=False, compare=False)
    decision_input: Callable | None = field(default=None, repr=False, compare=False)
    close_input: Callable | None = field(default=None, repr=False, compare=False)


# LLM: 仅原waiter生产不可变的纯清理结果，不能包含writer/批准callback；request同对象另以规范JSON核漂移，消费须核原native出处。
# 类用途: 保留尚未持久的取消、超时或失活事实，不把它当批准、任务终态或重放许可。
@dataclass(frozen=True)
class ApprovalRetirementObligation:
    decision: ToolApprovalDecision
    path: Path
    scope: ToolApprovalScope
    request: ToolApprovalRequest = field(repr=False)
    input_refs: tuple[tuple[str, str], ...] | None = field(repr=False)
    request_json: str = field(repr=False)


# LLM: 只标原permission退休未确认；没有call_id/任意writer/批准函数，不能当普通cancelled完成，30B须由原Future接收同对象。
# 类用途: 在退休锁忙或落盘未确认时把准确负向事实交给直接调用者，裸wait不承诺已有后继消费者。
class ApprovalRetirementUnconfirmedError(RuntimeError):
    error_code = "APPROVAL_RETIREMENT_UNCONFIRMED"

    # LLM: 只有原producer的native origin绑定此error，手工构造/复制不由只读查询承认为原出处；异常不执行清理或授予权限。
    # 函数用途: 保存同一个原退休义务与固定安全原因，不把私有请求正文放入异常文案。
    def __init__(self, obligation, origin, reason):
        self.obligation = obligation
        self.reason = reason
        self._retirement_origin = origin
        super().__init__("原工具审批退休事实尚未持久确认，原清理义务仍未结")


# LLM: 只捕获原wait实际对象，不保存批准callback或运行Context；PID/Thread与self/error锚点只证明出处，不能重新打开执行scope。
# 类用途: 在一次原等待期间保留退休事实的原生来源，供未来原完成点只读核验。
class _ApprovalRetirementOrigin:
    # LLM: 原handle仅在构造时读取字段，不持有其callback；不读当前run补身份，不新增表或磁盘状态。
    # 函数用途: 冻结原permission身份和请求规范值，并记录本次真实线程及进程。
    def __init__(self, handle):
        self.path, self.scope, self.request = handle.path, handle.scope, handle.request
        self.request_json = _retirement_request_json(handle.request)
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self._origin, self._error, self._fact = self, None, None
        self.input_refs = None
        self.observation = self._observation_origin = _APPROVAL_REQUEST_OBSERVATION.get()
        self.retirement_result = None
        self.callback_closed = False
        self.pre_handler = None
        self.run_receipts = ()
        report = read_json_object_report(handle.path, context="tool_approval.retirement.origin")
        if report.load_error is None and _record_matches(report.payload, handle.scope, handle.request):
            self.input_refs = _retirement_input_refs(report.payload.get("decision_input"))

    # LLM: 仅原wait线程能首次绑定精确error，之后只返回同一对象；不含任意执行函数，闭合批准能力不会删除此原义务。
    # 函数用途: 把明确未结的纯退休结果封入原错误对象，不伪称写入成功或未执行整个业务。
    def unconfirmed(self, decision, reason):
        if self._error is not None:
            if approval_retirement_obligation(self._error) is None:
                raise ValueError("原审批退休出处不可用")
            return ApprovalRetirementResult("unconfirmed", error=self._error)
        if (self._origin is not self or self.pid != os.getpid() or self.thread is not threading.current_thread()
                or self.request_json != _retirement_request_json(self.request)):
            raise ValueError("原审批退休出处不可用")
        if self._error is None:
            fact = ApprovalRetirementObligation(decision, self.path, self.scope, self.request,
                                               self.input_refs, self.request_json)
            self._fact = fact
            self._error = ApprovalRetirementUnconfirmedError(fact, self, reason)
        return ApprovalRetirementResult("unconfirmed", error=self._error)


# LLM: 四状态只区分本次负向提交，不携执行权；prior_decided不代表当前取消路径应返回approved继续工具。
# 类用途: 区分已清理、已有决定、已写退休和仍未确认，避免None吞掉锁忙。
@dataclass(frozen=True)
class ApprovalRetirementResult:
    status: str
    prior_decision: ToolApprovalDecision | None = None
    error: ApprovalRetirementUnconfirmedError | None = field(default=None, repr=False)


# LLM: 只读核原error/origin/request同对象与规范值，copy/fork/任意同码错误不成为清理出处；不运行Context/判活/持门或签发writer。
# 函数用途: 让30B准确识别原异常保留的纯退休事实，本片不冒称已接通后继消费者。
def approval_retirement_obligation(error):
    if type(error) is not ApprovalRetirementUnconfirmedError:
        return None
    origin = error._retirement_origin
    fact = error.obligation
    if (type(origin) is not _ApprovalRetirementOrigin or origin._origin is not origin or origin._error is not error
            or origin.pid != os.getpid() or origin.observation is not origin._observation_origin
            or fact is not origin._fact or type(fact) is not ApprovalRetirementObligation
            or fact.request is not origin.request or fact.scope is not origin.scope or fact.path is not origin.path
            or fact.request_json != origin.request_json or _retirement_request_json(fact.request) != fact.request_json):
        return None
    return fact


# LLM: 只沿原Future异常链读已存在native义务，限32个对象防环；wrapper不能签收/结算/变成批准来源，外层actual同对象优先。
# 函数用途: 外层清理另报错误时仍保原退休债务，不把新错误翻成审批错误或丢掉原Future。
def find_approval_retirement_error(error):
    pending, visited = [error], set()
    while pending and len(visited) < 32:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in visited:
            continue
        visited.add(id(current))
        if approval_retirement_obligation(current) is not None:
            return current
        pending.extend((current.__context__, current.__cause__))
    return None


# LLM: 仅为原request漂移核验生成规范内存值，不写账或以description等自然语言裁决权限。
# 函数用途: 冻结审批请求的完整结构，防止异常持有期间可变binding被悄悄替换。
def _retirement_request_json(request):
    return json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# LLM: 只保存已观察到的原两refs，未读到不补造；此投影不验证MAC、不授予执行权。
# 函数用途: 冻结原决定输入引用，供未结诊断与未来同一完成点核对。
def _retirement_input_refs(raw):
    if (type(raw) is dict and set(raw) == {"operation_id", "input_digest"}
            and all(type(value) is str and value for value in raw.values())):
        return tuple(sorted(raw.items()))
    return None


# LLM: 只有原Gateway handler创建的词法观察可绑定异常出处；不持有writer/callback，不构造claim、许可或第二状态表。
# 类用途: 记录原请求body仍在当前进程线程有效区间的身份，供外层仅写未知观察。
class _ApprovalRequestObservation:
    # LLM: request只取原claim四元组与原不可变FP，拒绝缺失/类型不明；不复制正文/凭据或查询当前Params。
    # 函数用途: 捕获原handler实际对象，初始化纯内存观察区间。
    def __init__(self, agent, path, request, fingerprint):
        self.agent, self.path = agent, path
        self.identity = tuple(request.get(key) for key in ("id", "execution_attempt_id", "lease_epoch", "lease_owner"))
        self.fingerprint = fingerprint
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.origin, self.active = self, True
        self.pending_error, self.terminal_acceptance = None, None
        self.receiver = False
        self._captured = (agent, path, self.identity, fingerprint, self.pid, self.thread)

    # LLM: 原字段必须仍为构造期同对象；这里只证明原捕获完整，关闭之后也不授写许可。
    # 函数用途: 拒绝原观察对象的字段替换或复制，供只读canonical核收使用。
    def original(self):
        return (self.origin is self and self.pid == os.getpid() and all(actual is value for actual, value in zip(
            (self.agent, self.path, self.identity, self.fingerprint, self.pid, self.thread), self._captured, strict=True)))

    # LLM: 只当前原pointer/PID/Thread可观察；copy_context不延长active，跨线程不得借观察写原request。
    # 函数用途: 检查当前catch仍属于原词法请求区间，不恢复业务执行来源。
    def current(self):
        return (self.original() and self.active
                and self.thread is threading.current_thread() and _APPROVAL_REQUEST_OBSERVATION.get() is self)


_APPROVAL_REQUEST_OBSERVATION = ContextVar("original_approval_request_observation", default=None)


# LLM: 仅原_handle_gateway_request包body/catch；观察不等于执行源，末写仍原T和实际C/W来源，finally令所有副本共同失活。
# 函数用途: 给同请求原审批错误保留只读出处，不在Params已恢复后重挂旧对象。
@contextmanager
def approval_retirement_request_observation(agent, path, request, fingerprint, *, receiver=False):
    current = _APPROVAL_REQUEST_OBSERVATION.get()
    if (type(current) is _ApprovalRequestObservation and current.current() and current.agent is agent
            and current.path == path and current.fingerprint == fingerprint):
        yield current
        return
    observation = _ApprovalRequestObservation(agent, path, request, fingerprint)
    observation.receiver = receiver
    token = _APPROVAL_REQUEST_OBSERVATION.set(observation)
    try:
        yield observation
    finally:
        observation.active = False
        _APPROVAL_REQUEST_OBSERVATION.reset(token)


# LLM: 原error→native origin→原词法观察必须同对象；只能匹配既有claim/FP，不给别的请求、晚副本或同码异常签未知。
# 函数用途: 核当前原catch是否可以为本条退休错误写原请求未知观察。
def approval_retirement_request_matches(error, path, request, fingerprint, *, agent):
    fact = approval_retirement_obligation(error)
    if fact is None:
        return False
    observation = error._retirement_origin.observation
    identity = tuple(request.get(key) for key in ("id", "execution_attempt_id", "lease_epoch", "lease_owner"))
    return (type(observation) is _ApprovalRequestObservation and observation.current() and observation.agent is agent
            and observation.path == path and path.stem == identity[0] and identity == observation.identity
            and all(type(value) is str and value for value in (identity[0], identity[1], identity[3]))
            and type(identity[2]) is int and identity[2] > 0 and request.get("status") == "processing"
            and fingerprint == observation.fingerprint)


# LLM: 摘要只原computed negative结构，不含参数/描述/secret；不是授权摘要，不能用于恢复批准或重放工具。
# 函数用途: 为原execution_outcome附加唯一许可编号与不可变负向结果校验值。
def approval_retirement_diagnostic(error):
    fact = approval_retirement_obligation(error)
    if fact is None:
        return None
    encoded = json.dumps(fact.decision.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"permission_id": fact.decision.permission_id, "negative_digest": hashlib.sha256(encoded.encode()).hexdigest()}


# LLM: 只在原Future核收调用，一次try只清理原negative；原native查询失败拒绝，不能传任意writer/handle或重新打开批准callback。
# 函数用途: 在原等待线程已退出后重试同一个退休义务，仍忙返回原错误而不重新执行业务。
def retry_approval_retirement(error):
    fact = approval_retirement_obligation(error)
    if fact is None:
        raise ValueError("原审批退休义务不可用")
    origin = error._retirement_origin
    if origin.retirement_result is None:
        result = _try_retire_approval_permission(origin, fact.decision, origin)
        if result.status != "unconfirmed":
            origin.retirement_result = result
        return result
    return origin.retirement_result


# LLM: 只读严格原canonical；必须原Future错误native出处、完整claim/FP和同negative观察俱在，不从P3/缺行/类名推定责任已接收。
# 函数用途: 确认原请求、真实run和会话claim已按原链关闭，退休完成后才回收局部Future清理义务。
def approval_retirement_canonical_received(error, claim):
    from ..gateway_parts.io import gateway_request_fingerprint
    from ..gateway_parts.queue_service import GatewayClaim

    if approval_retirement_obligation(error) is None or type(claim) is not GatewayClaim:
        return False
    observation = error._retirement_origin.observation
    expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch, claim.lease_owner)
    if (type(observation) is not _ApprovalRequestObservation or not observation.original()
            or observation.path != claim.path or observation.identity != expected
            or not approval_retirement_runs_closed(error, accepted=True)):
        return False
    accepted = observation.terminal_acceptance
    if accepted is None or accepted[0] is not error:
        return False
    report = read_json_object_report(accepted[1], context="tool_approval.retirement.terminal")
    payload = report.payload
    return (report.load_error is None and _retirement_terminal_matches(payload, observation, accepted[2])
            and gateway_request_fingerprint(payload, claim.request_id) == observation.fingerprint)


# LLM: 原Future核收仅允许一次try；物理设施未知优先，许可退休成功不能冒充物理排空或删除未接收的请求责任。
# 函数用途: 负向清理和原精确终态都已核收，且没有外层/物理未知，才移除原已done Future。
def complete_approval_retirement_future(error, claim):
    from ..gateway_parts.request_errors import request_resource_exit_unconfirmed

    original = find_approval_retirement_error(error)
    if original is None:
        return False
    result = retry_approval_retirement(original)
    return (error is original and result.status != "unconfirmed" and not request_resource_exit_unconfirmed(error)
            and approval_retirement_canonical_received(error, claim))


# LLM: 只由真实执行器ask消费者在批准重执行前调用；完整request与原native债务同值、原callback已关闭且handler未进，不能从同码或仅负向JSON补事实。
# 函数用途: 捕获当前被审批调用未启动的实际只读证据，不证明整个请求无副作用。
def capture_approval_retirement_call(error, request, execution, *, agent=None, params=None):
    from ..gateway_parts.request_errors import request_resource_exit_unconfirmed

    fact = approval_retirement_obligation(error)
    if (fact is None or request_resource_exit_unconfirmed(error)
            or not error._retirement_origin.callback_closed or error._retirement_origin.thread is not threading.current_thread()
            or _retirement_request_json(request) != fact.request_json or execution.decision.status != "ask"
            or execution.result.handler_executed is not False):
        return False
    identity = (agent, str(getattr(params, "run_id", "") or execution.call.run_id),
                str(getattr(params, "task_id", "") or execution.call.run_id),
                execution.call.attempt_id, request.request_id)
    if (request.binding.get("run_id") != fact.scope.run_id
            or (fact.scope.execution_attempt_id != identity[3]
                and not (agent is None and fact.scope.agent_kind == "main" and not fact.scope.execution_attempt_id))):
        return False
    error._retirement_origin.pre_handler = identity
    return True


# LLM: 只核原ask捕获与实际本栈Params/Agent；不读取其它线程、不重挂Params、不从scope生成新执行源。
# 函数用途: 判断这条清理债务是否确属本次实际运行的未启动调用。
def approval_retirement_run_matches(error, agent, params):
    from ..gateway_parts.request_errors import request_resource_exit_unconfirmed

    fact = approval_retirement_obligation(error)
    if fact is None or request_resource_exit_unconfirmed(error):
        return False
    origin = error._retirement_origin
    expected = (agent, params.run_id, params.task_id, params.attempt_id, params.request_id)
    return (origin.thread is threading.current_thread() and origin.callback_closed
            and origin.pre_handler == expected and origin.pre_handler[0] is agent
            and fact.scope.execution_attempt_id == params.attempt_id)


# LLM: 结算helper是fail-silent，必须用原repo读回本run/current attempt终态、ended及exec锁释放；不写SQL、不猜缺行、不执行旧Context。
# 函数用途: 将真实本栈已完成的运行收口保留为只读核收事实，供原请求终态接收使用。
def record_approval_retirement_run(error, agent, params):
    if not approval_retirement_run_matches(error, agent, params):
        return False
    repo = agent.subagents.runtime_db
    attempt = repo.get_attempt(params.attempt_id)
    run = repo.agent_run_for_run_id(params.run_id)
    if (attempt is None or run is None or attempt["agent_run_id"] != run["agent_run_id"]
            or run["current_attempt_id"] != params.attempt_id or run["status"] not in {"failed", "cancelled"}
            or attempt["status"] != run["status"] or not attempt["ended_at"]
            or run["current_attempt_generation"] != attempt["attempt_generation"]
            or repo.get_agent_run_exec_lock(run["agent_run_id"]) is not None or repo.unsettled_attempt_operations(params.attempt_id)):
        return False
    receipt = (repo, run["agent_run_id"], params.run_id, params.attempt_id, run["status"],
               attempt["ended_at"], attempt["attempt_generation"], run["task_run_id"])
    error._retirement_origin.run_receipts += (receipt,)
    return True


# LLM: 仅原运行catch完成原异常收口后才能走正常失败response；原repo原行还须匹配，不以调用过settle/类型字符串为凭据。
# 函数用途: 只读复核原债务的运行收口，真实设施未知优先不被纯清理掩盖。
def approval_retirement_runs_closed(error, *, accepted=False):
    from ..gateway_parts.request_errors import request_resource_exit_unconfirmed

    fact = approval_retirement_obligation(error)
    if fact is None or request_resource_exit_unconfirmed(error):
        return False
    receipts = error._retirement_origin.run_receipts
    if not receipts:
        return False
    for repo, agent_run_id, run_id, attempt_id, status, ended_at, generation, task_run_id in receipts:
        run, attempt = repo.get_agent_run(agent_run_id), repo.get_attempt(attempt_id)
        if (run is None or attempt is None or run["run_id"] != run_id or attempt["agent_run_id"] != agent_run_id
                or attempt["status"] != status or attempt["ended_at"] != ended_at
                or run["task_run_id"] != task_run_id or attempt["attempt_generation"] != generation or not ended_at
                or repo.unsettled_attempt_operations(attempt_id)):
            return False
        if run["current_attempt_id"] == attempt_id:
            if (run["status"] != status or run["current_attempt_generation"] != generation
                    or repo.get_agent_run_exec_lock(agent_run_id) is not None):
                return False
        else:
            # 仅已真实terminal签收后允许同run更高代；新执行锁不属于已结束旧attempt，不能因此永久保清理Future。
            observation = error._retirement_origin.observation
            current = repo.get_attempt(run["current_attempt_id"])
            if (not accepted or type(observation) is not _ApprovalRequestObservation
                    or observation.terminal_acceptance is None or current is None
                    or current["agent_run_id"] != agent_run_id or current["attempt_generation"] <= generation
                    or run["current_attempt_generation"] != current["attempt_generation"]):
                return False
            lock = repo.get_agent_run_exec_lock(agent_run_id)
            if lock is not None and (lock["attempt_id"] != current["attempt_id"]
                    or lock["attempt_generation"] != current["attempt_generation"] or lock["attempt_id"] == attempt_id):
                return False
    return True


# LLM: 原handler只在同词法receiver/完整claim/实际运行已结时接纯清理债务；不写公开response异常，不授执行或终态许可。
# 函数用途: 让原handler按正常失败或取消返回，原worker继续承担真实归档与会话claim收尾。
def receive_approval_retirement(error, agent, path, request, fingerprint):
    if (not approval_retirement_request_matches(error, path, request, fingerprint, agent=agent)
            or not approval_retirement_runs_closed(error)):
        return False
    observation = error._retirement_origin.observation
    current = read_json_object_report(path, context="tool_approval.retirement.transport_run")
    if current.load_error is not None or not _approval_retirement_transport_run_closed(error, current.payload, agent):
        return False
    if not observation.receiver or observation.pending_error not in (None, error):
        return False
    observation.pending_error = error
    return True


# LLM: 只认同一原canonical terminal/fullclaim/FP及原实际response，不把文件存在、future.done或unknown诊断当业务完成。
# 函数用途: 纯读验证一份失败或取消的原终态是否属于这次真实收口。
def _retirement_terminal_matches(payload, observation, response):
    from ..gateway_parts.io import gateway_request_fingerprint

    identity = tuple(payload.get(key) for key in ("id", "execution_attempt_id", "lease_epoch", "lease_owner"))
    return (payload.get("schema_version") == "gateway_terminal_request.v1" and payload.get("turn_phase") == "closed"
            and payload.get("status") in {"failed", "cancelled", "interrupted"}
            and all(type(a) is type(b) and a == b for a, b in zip(identity, observation.identity, strict=True))
            and gateway_request_fingerprint(payload, identity[0]) == observation.fingerprint
            and payload.get("terminal_response") == response and response.get("id") == identity[0]
            and not response.get("provider_transient_resume") and not response.get("restart_resume"))


# LLM: 必须原worker的同receiver/actualsource区间，在原finish之后核真实canonical、run/exec锁与准确claim结束；只记内存核收槽，不改账/清锁/P3。
# 函数用途: 为原Future签收同一清理债务，业务完成和permission清理仍各守唯一权威。
def accept_approval_retirement_terminal(observation, response, store):
    error = observation.pending_error
    if (error is None or not observation.current() or not observation.receiver
            or error._retirement_origin.observation is not observation or not approval_retirement_runs_closed(error)):
        return False
    canonical = observation.path.parent.parent / "terminal" / observation.path.name
    report = read_json_object_report(canonical, context="tool_approval.retirement.accept")
    if report.load_error is not None or not _retirement_terminal_matches(report.payload, observation, response):
        return False
    binding = report.payload.get("conversation_claim")
    if (store is None or type(binding) is not dict or binding.get("schema_version") != "gateway_conversation_claim.v1"
            or binding.get("request_id") != observation.identity[0]
            or binding.get("task_id") != "gateway:" + observation.identity[0]
            or binding.get("thread_id") != error.obligation.scope.thread_id):
        return False
    claim, load_error = store.claims.load_report(binding["thread_id"])
    if (load_error is not None or claim.get("thread_id") != binding["thread_id"]
            or claim.get("task_id") != binding["task_id"] or claim.get("status") not in {"failed", "cancelled", "finished"}
            or not claim.get("claim_id") or not claim.get("finished_at")):
        return False
    observation.terminal_acceptance = (error, canonical, dict(response), dict(claim))
    return True


# LLM: Host原resolver已核完整MAC请求与真实ask/handler未入；这里仅比原binding和native捕获，不给普通同码/外run债务补归属。
# 函数用途: 判断原HostCommand能否用require_no_tool_operations的既有CAS收口未启动调用。
def approval_retirement_host_matches(error, binding):
    from ..gateway_parts.request_errors import request_resource_exit_unconfirmed

    if approval_retirement_obligation(error) is None or request_resource_exit_unconfirmed(error):
        return False
    origin = error._retirement_origin
    expected = (None, binding.run_id, binding.run_id, binding.attempt_id, binding.request.request_id)
    return (origin.thread is threading.current_thread() and origin.callback_closed and origin.pre_handler == expected
            and error.obligation.request.binding.get("operation_id") == binding.request.operation_id)


# LLM: 只读原无tool-operation CAS后的实际run/attempt/exec锁，不把ExitStack结束或调用过settle当成功，不改权限/行。
# 函数用途: 为确实已结清的原HostCommand保存清理债务对应的运行证据。
def record_approval_retirement_host(error, repo, binding):
    if not approval_retirement_host_matches(error, binding):
        return False
    run, attempt = repo.get_agent_run(binding.agent_run_id), repo.get_attempt(binding.attempt_id)
    if (run is None or attempt is None or run["run_id"] != binding.run_id
            or run["current_attempt_id"] != binding.attempt_id or attempt["agent_run_id"] != binding.agent_run_id
            or run["status"] != "failed" or attempt["status"] != "failed" or not attempt["ended_at"]
            or repo.get_agent_run_exec_lock(binding.agent_run_id) is not None
            or repo.unsettled_attempt_operations(binding.attempt_id)):
        return False
    error._retirement_origin.run_receipts += ((repo, binding.agent_run_id, binding.run_id, binding.attempt_id,
                                              run["status"], attempt["ended_at"], attempt["attempt_generation"], run["task_run_id"]),)
    return True


# LLM: 只重核当前原transport绑定的运行事实；子代理已结不能替父Agent/别的RuntimeDB attempt结账，缺真实ask runtime绑定保未结。
# 函数用途: 确认Gateway自身运行也在原清理债务的真实收口中，避免借child终态结束父请求。
def _approval_retirement_transport_run_closed(error, payload, agent):
    if not approval_retirement_runs_closed(error):
        return False
    authority = payload.get("runtime_authority")
    if authority is None:
        return payload.get("kind") == "background"
    if type(authority) is not dict or authority.get("schema_version") != "gateway_runtime_authority.v1":
        return False
    return any(repo is agent.subagents.runtime_db and agent_run_id == authority.get("agent_run_id")
               and run_id == authority.get("run_id") and attempt_id == authority.get("attempt_id")
               for repo, agent_run_id, run_id, attempt_id, _status, _ended, _generation, _task_run in error._retirement_origin.run_receipts)


# LLM: 主/子共用精确审批入口；后台默认不可交互，前台须由宿主显式启用，群禁止剩余人工审批；缓存不跨 claim/attempt。
# 类用途: 在已有权限内自动续跑，剩余确认只在交互前台发布原会话审批账。
# LLM: sink只负责审批运输，approved_session归原Agent有界缓存；每次凭当前canonical执行/权限生成scope，不能从展示对象保存授权事实。
# 类用途: 给主/子后台回合提供同一审批入口，重建展示对象时保留准确会话许可。
class AgentToolApprovalSinkMixin:
    agent: object
    thread_id: str
    task_id: str
    approval_interactive: bool = False

    # LLM: 插件禁缓存/provider；会话许可复用原Agent缓存，scope仅由当前canonical执行与权限生成；后台/群剩余确认仍拒，typed未知原样传播。
    # 函数用途: 自动续跑已有权限内操作，交互前台才上送精确确认。
    def request_permission(
        self,
        request_value: Mapping[str, object],
        *,
        cancellation_token: object | None = None,
    ) -> dict[str, object]:
        try:
            from ..user_space.approval_mode import autonomous_tool_decision

            request = ToolApprovalRequest.from_mapping(request_value)
            forced = plugin_gate_required(request)
            if _cancelled(cancellation_token):
                return ToolApprovalDecision(request.permission_id, "cancelled").to_dict()
            if time.time() >= approval_deadline(request):
                return ToolApprovalDecision(request.permission_id, "denied", reason_code="APPROVAL_TIMEOUT").to_dict()
            scope = _request_scope(self.agent, self.task_id, self.thread_id, request)
            from ..gateway_parts.approval_session import agent_tool_approval_session_cache

            session_key = "" if forced else _approval_session_key(request)
            session_scope = "" if forced else _agent_approval_session_scope(self.agent, scope)
            cache = agent_tool_approval_session_cache(self.agent) if session_scope else None
            if session_key and cache is not None and cache.is_approved(session_scope, session_key):
                return ToolApprovalDecision(
                    request.permission_id,
                    "approved",
                ).to_dict()
            from ..user_space.operation_grants import (
                owner_operation_granted,
                record_owner_operation_grant,
            )

            grant_key = "" if forced else str(request.binding.get("grant_key") or "").strip()
            if grant_key and owner_operation_granted(getattr(self.agent, "home_paths", None), grant_key):
                # 用户已对这类操作长期允许：不再发布审批，直接以 approved 放行本次精确调用。
                return ToolApprovalDecision(request.permission_id, "approved").to_dict()
            automatic = autonomous_tool_decision(self.agent, request) if not forced else None
            if automatic is not None:
                return automatic.to_dict()
            home = getattr(self.agent, "home_paths", None)
            if not getattr(self, "approval_interactive", False) or getattr(home, "owner_kind", "") == "group":
                return ToolApprovalDecision(request.permission_id, "denied", reason_code="APPROVAL_NONINTERACTIVE").to_dict()
            handle = publish_agent_tool_approval(
                self.agent,
                run_id=self.task_id,
                thread_id=self.thread_id,
                request_value=request,
            )
            decision = wait_for_agent_tool_approval(
                handle,
                cancellation_token=cancellation_token,
                options=ToolApprovalWaitOptions(mode_decision_provider=partial(autonomous_tool_decision, self.agent)),
            )
            if decision.decision == "approved_session" and session_key and cache is not None:
                # 等待期间发生权限/attempt变化时不保存旧许可；用户决定仍交原消费链复核。
                if _agent_approval_session_scope(self.agent, scope) == session_scope:
                    cache.approve(session_scope, session_key)
            if decision.decision == "approved_owner" and grant_key:
                record_owner_operation_grant(self.agent.home_paths, grant_key, source="agent_tool_approval")
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as error:
            from ..gateway_parts.private_approval_control import (
                raise_private_approval_transport_error,
            )
            raise_private_approval_transport_error(error)
            permission_id = str(request_value.get("permission_id") or "").strip()
            if not permission_id:
                raise
            decision = ToolApprovalDecision(permission_id, "unavailable")
        return decision.to_dict()


# LLM: 发布验证canonical和绝对截止，并仅绑定本线程等待callback；IM普通失败保原TUI，actual typed未知不吞，不复制RunParams。
# 函数用途: 原子创建一条有效交互审批记录，可选私人卡引用同一记录与截止。
def publish_agent_tool_approval(
    agent: object,
    *,
    run_id: str,
    thread_id: str,
    request_value: ToolApprovalRequest | Mapping[str, object],
) -> AgentToolApprovalHandle:
    request = _approval_request(request_value)
    scope = _request_scope(agent, run_id, thread_id, request)
    path = _approval_path(agent, scope, request.permission_id)
    created_at = request.requested_at or time.time()
    from ..user_space.approval_mode import deployment_approval_config
    timeout = deployment_approval_config(agent.home_paths).tool_approval_timeout_seconds
    deadline = min(request.expires_at or created_at + timeout, created_at + timeout)
    if time.time() >= deadline:
        raise ValueError("tool approval expired before publication")
    payload = {**_pending_payload(scope, request, created_at), "expires_at": deadline}
    with locked_json_path(_transition_path(path)):
        report = read_json_object_report(path, context="subagent.tool_approval.publish")
        if report.load_error is not None:
            raise OSError("subagent approval record is unreadable")
        if report.payload and not _same_pending_record(report.payload, payload):
            raise ValueError("subagent approval identity conflict")
        if not report.payload:
            write_json_file_atomic(path, payload)
    handle = AgentToolApprovalHandle(
        scope, path, request, created_at,
        partial(tool_approval_scope_is_current, agent, scope),
    )
    from dataclasses import replace

    from ..gateway_parts.private_approval_control import approval_input_callbacks
    from .im_tool_approval import publish_im_tool_approval
    consume, close = approval_input_callbacks(agent, handle)
    handle = replace(handle, decision_input=consume, close_input=close)

    try:
        publish_im_tool_approval(agent, handle)
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as error:
        from ..gateway_parts.private_approval_control import raise_private_approval_transport_error
        raise_private_approval_transport_error(error)
        pass  # IM 不可用仍保留原 TUI/Web 消费路径，不伪造已送达或用户批准。
    return handle


# LLM: 原wait先捕获纯退休出处；取消/期限/scope仍优先，finally仅关闭批准能力，未结对象交直接调用者，不宣称30B已接。
# 函数用途: 等待同一原审批，异步输入由原执行线程重核应用，不把RunParams传到HTTP控制线程。
def wait_for_agent_tool_approval(handle, *, cancellation_token=None, options=None):
    origin = None
    try:
        origin = _ApprovalRetirementOrigin(handle)
        return _wait_for_agent_tool_approval(handle, cancellation_token=cancellation_token, options=options,
                                             retirement_origin=origin)
    finally:
        if handle.close_input is not None:
            handle.close_input()
        if origin is not None:
            origin.callback_closed = True


# LLM: 保原轮询/取消/期限/发现顺序；批准需全部原活动门，纯退休不拿op/不授执行；同一origin贯穿未结出口。
# 函数用途: 执行原等待循环，带input的退休只记录原返回结果而不覆写既有决定。
def _wait_for_agent_tool_approval(
    handle: AgentToolApprovalHandle,
    *,
    cancellation_token: object | None = None,
    options: ToolApprovalWaitOptions | None = None,
    retirement_origin: _ApprovalRetirementOrigin | None = None,
) -> ToolApprovalDecision:
    retirement_origin = retirement_origin or _ApprovalRetirementOrigin(handle)
    forced = plugin_gate_required(handle.request)
    options = options or ToolApprovalWaitOptions()
    poll_seconds = options.poll_seconds if options.poll_seconds is not None else AGENT_TOOL_APPROVAL_POLL_SECONDS
    discovery_seconds = options.discovery_seconds if options.discovery_seconds is not None else AGENT_TOOL_APPROVAL_DISCOVERY_SECONDS
    consumer_lease_seconds = options.consumer_lease_seconds if options.consumer_lease_seconds is not None else AGENT_TOOL_APPROVAL_CONSUMER_LEASE_SECONDS
    mode_decision_provider = options.mode_decision_provider
    interval = max(0.01, float(poll_seconds or AGENT_TOOL_APPROVAL_POLL_SECONDS))
    while True:
        if _cancelled(cancellation_token):
            return _retire_approval_wait(handle, ToolApprovalDecision(handle.request.permission_id, "cancelled"), origin=retirement_origin)
        if not handle.scope_valid():
            return _retire_approval_wait(handle, ToolApprovalDecision(handle.request.permission_id, "unavailable", reason_code="APPROVAL_UNAVAILABLE"), origin=retirement_origin)
        report = read_json_object_report(handle.path, context="subagent.tool_approval.wait")
        if report.load_error is not None or not report.payload:
            return ToolApprovalDecision(handle.request.permission_id, "unavailable")
        now = time.time()
        try:
            deadline = approval_deadline(handle.request, report.payload.get("expires_at"))
        except (TypeError, ValueError):
            return _retire_approval_wait(handle, ToolApprovalDecision(handle.request.permission_id, "unavailable", reason_code="APPROVAL_UNAVAILABLE"), origin=retirement_origin)
        if now >= deadline:
            return _retire_approval_wait(handle, ToolApprovalDecision(handle.request.permission_id, "denied", reason_code="APPROVAL_TIMEOUT"), origin=retirement_origin)
        decision = handle.decision_input() if handle.decision_input is not None else None
        decision = decision or _record_decision(handle, report.payload)
        if decision is not None:
            _remove_exact_record(handle)
            return decision
        if not forced and mode_decision_provider is not None:
            decision = mode_decision_provider(handle.request)
            if decision is not None:
                _remove_exact_record(handle)
                return decision
        now = time.time()
        lease_seen_at = _consumer_seen_at(handle.path.parent, handle.scope.root_task_id)
        from .im_tool_approval import im_approval_wait_live

        lease_fresh = now - lease_seen_at <= max(1.0, float(consumer_lease_seconds)) or im_approval_wait_live(report.payload, now)
        if not lease_fresh and now - handle.created_at >= max(0.0, float(discovery_seconds)):
            transport = report.payload.get("im_control") or {}
            reason = "APPROVAL_TRANSPORT_FAILED" if transport.get("delivery_status") == "failed" else "APPROVAL_UNAVAILABLE"
            return _retire_approval_wait(handle, ToolApprovalDecision(handle.request.permission_id, "unavailable", reason_code=reason), origin=retirement_origin)
        time.sleep(interval)


# LLM: 纯退休只取原permission→JSON try锁，不取op/Root/执行门；已有决定不覆写，未结必须准确typed而不能普通cancelled后丢义务。
# 函数用途: 即时保存原取消/超时/失活结果，或把原负向事实交给直接调用者而不恢复批准能力。
def _retire_approval_wait(handle, decision, *, origin=None):
    if (decision.permission_id != handle.request.permission_id
            or not (decision.decision in {"cancelled", "unavailable"}
                    or (decision.decision == "denied" and decision.reason_code == "APPROVAL_TIMEOUT"))):
        raise ValueError("审批退休只能保存原负向结果")
    result = _try_retire_approval_permission(handle, decision, origin or _ApprovalRetirementOrigin(handle))
    if result.status == "unconfirmed":
        raise result.error
    return decision


# LLM: 原scope/request完整核对后才清理/写同一permission，锁忙不等待；本函数不读op或当前run，不从负向事实授工具权。
# 函数用途: 在唯一原审批行里区分四种退休结果，JSON落盘未确认时保原精确义务。
def _try_retire_approval_permission(handle, decision, origin):
    stage = "permission_transition_busy"
    try:
        with locked_json_path(_transition_path(handle.path), blocking=False):
            report = read_json_object_report(handle.path, context="tool_approval.retirement")
            if report.load_error is not None or not _record_matches(report.payload, handle.scope, handle.request):
                return origin.unconfirmed(decision, "permission_identity_unavailable")
            row = report.payload
            prior = _decision_from_payload(row)
            if prior is not None and prior.permission_id != decision.permission_id:
                return origin.unconfirmed(decision, "permission_decision_unavailable")
            if prior is not None:
                return ApprovalRetirementResult("prior_decided", prior_decision=prior)
            if row.get("status") != "pending":
                return origin.unconfirmed(decision, "permission_decision_unavailable")
            raw = row.get("decision_input")
            refs = _retirement_input_refs(raw)
            if origin._fact is not None and origin._fact.input_refs is not None and refs != origin._fact.input_refs:
                return origin.unconfirmed(decision, "permission_input_changed")
            if origin._fact is None:
                origin.input_refs = refs
            stage = "permission_json_busy"
            if raw is None:
                return _try_remove_retired_permission(handle, row, decision, origin)
            if refs is None:
                return origin.unconfirmed(decision, "permission_input_unavailable")
            write_json_file_atomic(handle.path, {**row, "status": "decided", "decision": decision.to_dict(),
                                                "decision_origin": "waiter_retirement"}, blocking=False)
            return ApprovalRetirementResult("committed")
    except BlockingIOError:
        return origin.unconfirmed(decision, stage)
    except OSError:
        return origin.unconfirmed(decision, "permission_persistence_unconfirmed")


# LLM: 调用者持原permission，JSON也try且复读原整行；只删除同scope/request无input项，不以删除失败/缺行推断批准或退休成功。
# 函数用途: 让没有异步输入的原审批及时退出，同时不等待文件锁或丢掉并发已经写入的决定。
def _try_remove_retired_permission(handle, expected, decision, origin):
    with locked_json_path(handle.path, blocking=False):
        report = read_json_object_report(handle.path, context="tool_approval.retirement.remove")
        if report.load_error is not None or report.payload != expected:
            return origin.unconfirmed(decision, "permission_identity_changed")
        handle.path.unlink()
    return ApprovalRetirementResult("no_input")


# LLM: 接收方续租只声明一个 root 的交互能力，不是用户批准，不能决定任何挂起调用。
# 函数用途: 记录当前会话的 TUI/Web 正在接收主/子后台审批请求。
def renew_agent_tool_approval_consumer(
    agent: object,
    *,
    root_task_id: str,
    seen_at: float | None = None,
) -> Path:
    root = validate_opaque_id(root_task_id, kind="root_task_id")
    directory = _approval_root(agent, root)
    target = directory / ".consumer.json"
    write_json_file_atomic(
        target,
        {
            "schema_version": AGENT_TOOL_APPROVAL_CONSUMER_SCHEMA,
            "root_task_id": root,
            "seen_at": max(0.0, float(seen_at if seen_at is not None else time.time())),
        },
    )
    return target


# LLM: 有界列表只投影 owner 的一个 root 目录；每行重验原始请求及当前归属，返回公开字段与展示标签。
# 函数用途: 列出当前主任务树里等待用户处理的主/子后台工具审批。
def list_pending_agent_tool_approvals(
    agent: object,
    *,
    root_task_id: str,
    limit: int = AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT,
) -> list[dict[str, object]]:
    root = validate_opaque_id(root_task_id, kind="root_task_id")
    directory = _approval_root(agent, root)
    rows: list[dict[str, object]] = []
    paths = [
        path
        for path in sorted(directory.glob("*.json"))
        if not path.name.startswith(".")
    ]
    for path in paths[: max(1, int(limit or 1))]:
        report = read_json_object_report(path, context="subagent.tool_approval.list")
        public = _public_pending_record(agent, root, report.payload)
        if public is not None:
            rows.append(public)
    rows.sort(key=lambda item: (float(item.get("requested_at") or 0.0), str(item["run_id"])))
    return rows[: max(1, int(limit or 1))]


# LLM: 同一原scope/完整请求/claim/attempt决定只能在原permission锁内提交；可选C门在锁外，record_guard只在原锁内末核版本/授权。
# 函数用途: 将用户决定写回原审批行；异步私卡等待方也复用同一个锁内实现。
def resolve_agent_tool_approval(agent, *, run_id, request_value, decision_value,
                                interaction_actor=None, commit_transition=None, blocking=True, record_guard=None):
    scope = tool_approval_scope(agent, run_id)
    request, decision = _approval_request(request_value), _approval_decision(decision_value)
    selected = validate_opaque_id(run_id, kind="run_id")
    if request.binding.get("run_id") != selected or decision.permission_id != request.permission_id:
        raise ValueError("subagent approval decision binding mismatch")
    path = _approval_path(agent, scope, request.permission_id)

    # LLM: public仅拥有原permission锁；调用方已持锁时必须用同一inner，不能递归取非重入文件锁。
    # 函数用途: 在原锁内完成唯一审批CAS与审计。
    def decide():
        with locked_json_path(_transition_path(path), **({} if blocking else {"blocking": False})):
            return _resolve_agent_tool_approval_locked(agent, path, scope, request, decision,
                interaction_actor=interaction_actor, blocking=blocking, record_guard=record_guard)
    return decide() if commit_transition is None else commit_transition(decide)


# LLM: 必须已持原permission transition，外层执行门/op锁先取；仅原真实scope现场校验；可选record_guard不替代原首决检查。
# 函数用途: 共用同步与原waiter的审批写入主体，保首决、期限、点击审计和完整请求比较。
def _resolve_agent_tool_approval_locked(agent, path, scope, request, decision, *, interaction_actor=None, blocking=True, record_guard=None):
    if not tool_approval_scope_is_current(agent, scope):
        raise FileNotFoundError("tool approval execution is no longer active")
    report = read_json_object_report(path, context="subagent.tool_approval.resolve")
    if report.load_error is not None or not _record_matches(report.payload, scope, request):
        raise FileNotFoundError("pending subagent approval not found")
    if record_guard is not None:
        record_guard(report.payload)
    if time.time() >= approval_deadline(request, report.payload.get("expires_at")):
        raise FileNotFoundError("tool approval expired")
    prior = _decision_from_payload(report.payload)
    if prior is not None and (prior.decision != decision.decision or prior.feedback != decision.feedback):
        raise ValueError("subagent approval already resolved differently")
    payload = dict(report.payload)
    payload.update(status="decided", decision=decision.to_dict())
    if prior is None and interaction_actor is not None:
        from ..user_space.interaction_actor_audit import record_interaction_actor
        payload['decision_by'] = record_interaction_actor(agent, interaction_actor, decision=decision.decision,
            actor=interaction_actor['actor_id'], **({} if blocking else {'blocking': False}))
    write_json_file_atomic(path, payload, **({} if blocking else {"blocking": False}))
    return {"ok": True, "run_id": scope.run_id, "permission_id": request.permission_id, "decision": decision.decision,
            **({'clicked_by': payload['decision_by']} if payload.get('decision_by') else {})}


# LLM: 读取原pending行并核完整scope/claim/attempt，绝不登记consumer或延长lease；不作为公开API投影。
# 函数用途: 给宿主交互适配器提供精确原审批版本材料。
def read_pending_approval_record(agent, *, run_id, request_value):
    scope = tool_approval_scope(agent, run_id)
    request = _approval_request(request_value)
    report = read_json_object_report(_approval_path(agent, scope, request.permission_id),
                                    context="subagent.tool_approval.plugin_read")
    if report.load_error is not None:
        raise OSError("approval record unavailable")
    if (not scope.active or not _record_matches(report.payload, scope, request)
            or report.payload.get("status") != "pending"
            or time.time() >= approval_deadline(request, report.payload.get("expires_at"))):
        raise FileNotFoundError("pending approval no longer current")
    return report.payload


# LLM: 旧Agent入口只取原ConversationStore，路径公式由同一资源leaf维护；不从工作区或正文替代审批根。
# 函数用途: 保留主/子调用方的原审批路径入口。
def _approval_path(agent: object, scope: ToolApprovalScope, permission_id: str) -> Path:
    return _approval_path_at_root(_approval_root(agent, scope.root_task_id), scope, permission_id)


# LLM: Store仅定位原账，不证明scope活动或批准权限；C取消必须仍核原MAC、记录和同生命周期提交。
# 函数用途: 从真实owner Store计算唯一审批记录路径，避免冷C构造假Agent。
def approval_path_for_store(store: object, scope: ToolApprovalScope, permission_id: str) -> Path:
    return _approval_path_at_root(_approval_root_for_store(store, scope.root_task_id), scope, permission_id)


# LLM: Agent虚调用缝与Store入口共用这一个permission哈希公式；根只定位原账，不授当前执行权。
# 函数用途: 在已选原审批根下生成同一条安全记录路径。
def _approval_path_at_root(root: Path, scope: ToolApprovalScope, permission_id: str) -> Path:
    digest = hashlib.sha256(str(permission_id or "").encode("utf-8")).hexdigest()[:24]
    return root / f"{scope.run_id}.{digest}.json"


# LLM: 原Agent路径入口保留，ConversationStore仍是Gateway与主/子runner唯一owner审批根。
# 函数用途: 返回某棵主任务树审批账本的目录。
def _approval_root(agent: object, root_task_id: str) -> Path:
    return _approval_root_for_store(getattr(agent, "conversation_store", None), root_task_id)


# LLM: 只消费原Store.storage.root；资源路径不授权决定，也不初始化owner、会话或索引。
# 函数用途: 在同一位置组装主/子审批的原持久目录。
def _approval_root_for_store(store: object, root_task_id: str) -> Path:
    root = getattr(getattr(store, "storage", None), "root", None)
    if root is None:
        raise RuntimeError("subagent approval requires ConversationStore")
    return Path(root) / "subagent_tool_approvals" / root_task_id


# LLM: 发布和缓存使用同一归属校验；明确 thread/run 与 canonical 状态冲突时拒绝，不能猜测或代换身份。
# 函数用途: 核对当前 sink 是否有资格为这一次调用发起审批。
def _request_scope(agent: object, run_id: str, thread_id: str, request: ToolApprovalRequest) -> ToolApprovalScope:
    scope = tool_approval_scope(agent, run_id)
    if not scope.active or scope.thread_id != thread_id or request.binding.get("run_id") != scope.run_id:
        raise ValueError("tool approval execution scope mismatch")
    return scope


# LLM: 原审批记录冻结 main claim/child canonical attempt；旧 schema/路径保留，额外执行身份不构成批准，联测恢复换轮。
# 函数用途: 构造一条尚未决定的审批记录。
def _pending_payload(
    scope: ToolApprovalScope,
    request: ToolApprovalRequest,
    created_at: float,
) -> dict[str, object]:
    return {
        "schema_version": AGENT_TOOL_APPROVAL_SCHEMA,
        "status": "pending",
        "root_task_id": scope.root_task_id,
        "run_id": scope.run_id,
        "thread_id": scope.thread_id,
        "request": request.to_dict(),
        "created_at": created_at,
        **({"claim_id": scope.claim_id} if scope.claim_id else {}),
        **({"execution_attempt_id": scope.execution_attempt_id} if scope.execution_attempt_id else {}),
    }


# LLM: 投影重新验证归属和 main claim，隐藏终态/旧轮/坏账；代理类型与名称只用于展示，不能授权。
# 函数用途: 将仍属于当前执行的等待记录转换成主/子代理共用的 TUI/Web 行。
def _public_pending_record(
    agent: object,
    root_task_id: str,
    payload: object,
) -> dict[str, object] | None:
    if not isinstance(payload, Mapping) or str(payload.get("status") or "") != "pending":
        return None
    try:
        run_id = validate_opaque_id(str(payload.get("run_id") or ""), kind="run_id")
        request = _approval_request(payload.get("request") if isinstance(payload.get("request"), Mapping) else {})
        scope = tool_approval_scope(agent, run_id)
        deadline = approval_deadline(request, payload.get("expires_at"))
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        return None
    if time.time() >= deadline:
        return None
    if not scope.active or scope.root_task_id != root_task_id or not _record_matches(
        payload, scope, request,
    ):
        return None
    return {
        "run_id": run_id,
        "agent_name": scope.agent_name,
        "agent_kind": scope.agent_kind,
        "request": request.to_dict(),
        "requested_at": request.requested_at,
    }


# LLM: schema/root/run/request/main claim/child attempt 全相等；缺失活动轮身份的旧记录失败关闭，不能靠同 permission ID 批准。
# 函数用途: 核验等待记录是否精确对应原调用及原执行租约。
def _record_matches(
    payload: object,
    scope: ToolApprovalScope,
    request: ToolApprovalRequest,
) -> bool:
    if not isinstance(payload, Mapping):
        return False
    raw_request = payload.get("request")
    try:
        stored = _approval_request(raw_request if isinstance(raw_request, Mapping) else {})
    except (TypeError, ValueError):
        return False
    return bool(
        payload.get("schema_version") == AGENT_TOOL_APPROVAL_SCHEMA
        and str(payload.get("root_task_id") or "") == scope.root_task_id
        and str(payload.get("run_id") or "") == scope.run_id
        and str(payload.get("thread_id") or "") == scope.thread_id
        and str(payload.get("claim_id") or "") == scope.claim_id
        and str(payload.get("execution_attempt_id") or "") == scope.execution_attempt_id
        and stored == request
    )


# LLM: Idempotent publish compares the full pending identity while ignoring only
# the atomically-added decision field, which cannot exist on a pending record.
# 函数用途: 判断磁盘上的等待记录是否就是本次重放请求。
def _same_pending_record(current: object, expected: Mapping[str, object]) -> bool:
    return bool(
        isinstance(current, Mapping)
        and str(current.get("status") or "") == "pending"
        and all(current.get(key) == value for key, value in expected.items())
    )


# LLM: 决定必须匹配句柄冻结的完整归属和请求，才能唤醒原主/子调用；不是当前作用域的记录保持不可用。
# 函数用途: 从当前账本行提取已核验决定，尚未决定时返回 None。
def _record_decision(
    handle: AgentToolApprovalHandle,
    payload: object,
) -> ToolApprovalDecision | None:
    if not _record_matches(
        payload,
        handle.scope,
        handle.request,
    ):
        return ToolApprovalDecision(handle.request.permission_id, "unavailable")
    decision = _decision_from_payload(payload)
    if decision is not None and decision.permission_id != handle.request.permission_id:
        return ToolApprovalDecision(handle.request.permission_id, "unavailable")
    return decision


# LLM: Decision recovery accepts only the closed contract enum and matching
# permission id; malformed data fails closed as unavailable.
# 函数用途: 从账本记录恢复一条已决定结果。
def _decision_from_payload(payload: object) -> ToolApprovalDecision | None:
    if not isinstance(payload, Mapping) or str(payload.get("status") or "") != "decided":
        return None
    raw = payload.get("decision")
    if not isinstance(raw, Mapping):
        return None
    try:
        return ToolApprovalDecision.from_mapping(raw)
    except (TypeError, ValueError):
        return None


# LLM: Lease read validates exact schema/root and returns zero for missing,
# corrupt, or cross-root files; zero can only shorten waiting, never approve.
# 函数用途: 读取最近一次交互界面领取审批请求的时间。
def _consumer_seen_at(directory: Path, root_task_id: str) -> float:
    report = read_json_object_report(directory / ".consumer.json", context="subagent.tool_approval.consumer")
    payload = report.payload
    if (
        report.load_error is not None
        or payload.get("schema_version") != AGENT_TOOL_APPROVAL_CONSUMER_SCHEMA
        or str(payload.get("root_task_id") or "") != root_task_id
    ):
        return 0.0
    try:
        return max(0.0, float(payload.get("seen_at") or 0.0))
    except (TypeError, ValueError):
        return 0.0


# LLM: 原permission锁内只删同scope/request且无input的原临时行；带input必须留决定事实给同receipt对账，不靠absence推断。
# 函数用途: 消费、取消或无人接收后移除这一条精确临时审批记录。
def _remove_exact_record(handle: AgentToolApprovalHandle) -> None:
    with locked_json_path(_transition_path(handle.path)):
        report = read_json_object_report(handle.path, context="subagent.tool_approval.remove")
        if (_record_matches(report.payload, handle.scope, handle.request)
                and not report.payload.get("decision_input")):
            try:
                handle.path.unlink()
            except OSError:
                return


# LLM: One sibling lock serializes publish, decision, and cleanup for the exact
# hashed permission path without blocking unrelated child requests.
# 函数用途: 返回审批记录对应的跨进程事务锁文件。
def _transition_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.transition")


# LLM: 取消只读结构化令牌字段/方法；Goal paused、异常正文和用户普通文字不能取消或批准工具。
# 函数用途: 判断主/子后台等待审批期间是否收到当前回合的真实取消信号。
def _cancelled(token: object | None) -> bool:
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    return bool(checker()) if callable(checker) else bool(getattr(token, "cancelled", False))


# LLM: 只读真实owner、canonical child/currentattempt或main claim及当前ToolRegistry；不读sink attrs、模型参数或恢复JSON。缺失/漂移拒缓存，不退成宽scope。
# 函数用途: 生成同一准确执行和有效权限下的有界会话缓存作用域，供展示sink重建后安全复用。
def _agent_approval_session_scope(agent, scope):
    from ..gateway_parts.approval_session import tool_approval_session_scope
    from ..tooling.registry import ToolRegistry
    from ..user_space.approval_mode import read_permission_state

    try:
        home, tools, config = agent.home_paths, agent.tools, agent.config
        owner_id = str(home.owner_id or "")
        if (not owner_id or not isinstance(tools, ToolRegistry)
                or not tool_approval_scope_is_current(agent, scope)):
            return ""
        permissions = {"owner_scope": tools.owner_scope_root, "workspace_roots": tuple(map(str, tools.workspace_roots)),
                       "disabled_tools": sorted(tools.disabled_tool_names), "permission": read_permission_state(home)}
        if scope.agent_kind == "subagent":
            task = agent.subagents.load(scope.run_id)
            if (task.owner != owner_id or task.agent_thread_id != scope.thread_id
                    or not scope.execution_attempt_id or task.runner_active_attempt_id != scope.execution_attempt_id):
                return ""
            context = agent.subagents.runner_context.build_execution_context(scope.run_id)
            if context.run_id != scope.run_id:
                return ""
            permissions.update(tools=sorted(context.allowed_tools), skills=sorted(context.allowed_skills),
                               boundary=context.write_boundary)
            cwd = str(context.write_boundary.get("execution_cwd") or "")
        else:
            projection = _main_approval_session_projection(agent, scope)
            if projection is None:
                return ""
            boundary, execution = projection
            permissions.update(boundary=boundary, execution=execution)
            cwd = str(boundary.get("execution_cwd") or tools.workspace_root)
        if not cwd or not tool_approval_scope_is_current(agent, scope):
            return ""
        base = tool_approval_session_scope(owner_id=owner_id, thread_id=scope.thread_id,
            conversation_id=scope.root_task_id, cwd=cwd, access_mode=config.access_mode,
            path_access_mode=config.path_access_mode)
        if not base:
            return ""
        payload = (base, scope.run_id, scope.claim_id, scope.execution_attempt_id, permissions)
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return "agent-approval-session:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except (AttributeError, FileNotFoundError, OSError, TypeError, ValueError):
        return ""


# LLM: main按实际typed callback区分前后台，不以scope有attempt代替前台来源；后台仍核LocalRunControl/原claim，JSON只核对不能签权。
# 函数用途: 只读核实主代理当前权限的出处；来源缺失时关闭会话缓存，保留原准确单次审批。
def _main_approval_session_projection(agent, scope):
    from ..agent_core.runtime.loop_models import RunParams
    from ..agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
    from ..gateway_parts.request_binding import GatewayTaskBindingWriter
    from .local_run_control import LocalRunControl
    from .tool_approval_scope import _foreground_runtime_is_current

    current = getattr(agent, "_current_run_params", None)
    if not isinstance(current, RunParams) or current.task_id != scope.root_task_id:
        return None
    store = agent.conversation_store
    thread = store.threads.load(scope.thread_id)
    if thread is None:
        return None
    run = agent.subagents.runtime_db.agent_run_for_run_id(current.run_id)
    if run is None:
        return None
    execution = {"task_id": current.task_id, "run_id": current.run_id,
                 "attempt_id": current.attempt_id, "agent_run_id": run["agent_run_id"]}
    if not _foreground_runtime_is_current(agent, thread, execution):
        return None
    control = current.conversation_task_binding_callback
    if isinstance(control, GatewayTaskBindingWriter):
        if not scope.execution_attempt_id or scope.execution_attempt_id != current.attempt_id:
            return None
    else:
        if (not isinstance(control, LocalRunControl) or control.runtime_authority() != execution
                or control.request_id != current.request_id or not LocalRunControl.__call__(control, None)):
            return None
        if scope.execution_attempt_id and scope.execution_attempt_id != current.attempt_id:
            return None
        claim = store.claims.load(scope.thread_id)
        authority = claim.get("runtime_authority")
        if (not isinstance(authority, dict) or authority.get("schema_version") != "conversation_claim_execution.v1"
                or authority.get("claim_id") != scope.claim_id
                or authority.get("invocation_run_id") != current.request_id
                or any(authority.get(key) != value for key, value in execution.items())):
            return None
    return write_boundary_with_runtime_ledger(agent, current), execution


# LLM: 缓存沿前台的精确工具名与参数哈希；原Agent缓存另以canonical执行和权限scope隔离，不能跨执行或参数扩权。
# 函数用途: 生成本次执行内“始终允许同一调用”的参数缓存键。
def _approval_session_key(request: ToolApprovalRequest) -> str:
    tool_name = str(request.binding.get("tool_name") or "").strip()
    args_hash = str(request.binding.get("args_hash") or "").strip()
    return f"{tool_name}:{args_hash}" if tool_name and args_hash else ""


# LLM: Request/decision conversion always re-enters shared contract validation.
# 函数用途: 规范跨进程审批请求对象。
def _approval_request(value: ToolApprovalRequest | Mapping[str, object]) -> ToolApprovalRequest:
    return value if isinstance(value, ToolApprovalRequest) else ToolApprovalRequest.from_mapping(value)


# LLM: Request/decision conversion always re-enters shared contract validation.
# 函数用途: 规范跨进程审批决定对象。
def _approval_decision(value: ToolApprovalDecision | Mapping[str, object]) -> ToolApprovalDecision:
    return value if isinstance(value, ToolApprovalDecision) else ToolApprovalDecision.from_mapping(value)


__all__ = [
    "AGENT_TOOL_APPROVAL_CONSUMER_LEASE_SECONDS",
    "AgentToolApprovalHandle",
    "AgentToolApprovalSinkMixin",
    "list_pending_agent_tool_approvals",
    "publish_agent_tool_approval",
    "renew_agent_tool_approval_consumer",
    "resolve_agent_tool_approval",
    "wait_for_agent_tool_approval",
]
