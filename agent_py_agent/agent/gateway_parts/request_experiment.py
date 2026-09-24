# LLM: 这是 /experiment 的唯一产品授权入口：只读取入口冻结在排队请求里的 system_task，在主轮已发布 run/attempt 后、
# 首次模型调用前，于同一精确回合转换锁内写 granting→调用 E1 原语→写 granted/rejected；回执存在即不再授权。
# 失败只通知用户、从不阻断业务回合；模型没有任何工具路径能进入这里（user_config 只读/撤销）。
# 模块用途: 把用户显式的实验命令转换成绑定本请求 owner/thread/task/run/attempt 与当前原账代次的有界授权。
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..conversation.control_commands import decision_experiment_task
from ..runtime_db.host_commands import HostCommandIdentity
from ..settings.decision_experiment_schema import EMPIRICAL_INPUT_BOUND_POLICY
from ..settings.decision_settings_schema import (
    DecisionSettingsAccessError,
    DecisionSettingsConflict,
)
from ..settings.model_provider_schema import ModelProfileError
from .io import update_json_file_atomic

EXPERIMENT_GRANT_KEY = "experiment_grant"
_GRANT_SCHEMA = "gateway_decision_experiment_grant.v1"
_LOGGER = logging.getLogger(__name__)


# LLM: writer 提供精确请求路径/编号/执行代次与内存请求；agent/params 是已绑定 run/attempt 的主轮参数，不能换成新消息身份。
# 类用途: 为一条排队请求执行最多一次的实验授权，并把结构化回执写回原请求记录。
@dataclass(frozen=True)
class GatewayExperimentGrant:
    writer: object
    agent: object
    params: object
    command: dict

    # LLM: 精确回合关闭或停止时原转换抛 InterruptedError，此时不写任何回执；其它异常只记日志，业务回合照常继续。
    # 函数用途: 在原回合锁内完成一次授权并通知用户，返回回执或 None。
    def run(self) -> dict | None:
        from .request_binding import GatewayActiveTurnTransition

        writer = self.writer
        transition = GatewayActiveTurnTransition(writer.request_path, writer.request_id, writer.execution_attempt_id)
        try:
            receipt = transition("experiment_grant", self._grant_once)
        except InterruptedError:
            return None
        except Exception:  # noqa: BLE001 授权失败不能阻断用户业务回合
            _LOGGER.warning("决策实验授权未完成；本轮业务继续，不会发送实验请求。")
            return None
        self._notify(receipt)
        return receipt

    # LLM: 调用方持有 T；先以原 JSON 锁写 granting，已存在任何回执（含崩溃遗留的 granting）即放弃，重放永不再授权。
    # 函数用途: 预留一次授权、调用原语并写回 granted 或 rejected 回执。
    def _grant_once(self) -> dict | None:
        if not self._write(self._receipt("granting"), create=True):
            return None
        try:
            report = self._authorize()
            receipt = self._receipt("granted", authorization_id=report["experiment_authorization"]["authorization_id"])
        except Exception as exc:  # noqa: BLE001 结构化拒绝码只按异常类型给出，不解析文案
            receipt = self._receipt("rejected", code=_rejection_code(exc))
        self._write(receipt, create=False)
        return receipt

    # LLM: 来源身份全部来自入口已鉴权写入的 user_id/metadata.channel 与 Agent 的 owner；阈值参数只取冻结 system_task。
    # 函数用途: 读取当前设置版本后调用 E1 授权原语，要求实验能力已开启。
    def _authorize(self) -> dict:
        from ..conversation.decision_service import _identity
        from ..settings.decision_experiment import authorize_decision_experiment
        from ..settings.decision_settings import execute_decision_settings_operation

        _owner, thread_id, _run, _task = _identity(self.agent, self.params)
        view = execute_decision_settings_operation(self.agent, "read", {"scope": "thread"}, thread_id=thread_id)
        if not view["effective"]["enabled"] or not view["effective"]["experiment_enabled"]:
            raise _GrantRejected("experiment_disabled")
        request = self.writer.request if isinstance(self.writer.request, dict) else {}
        metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
        try:
            source = HostCommandIdentity(str(self.agent.home_paths.owner_id), str(request.get("user_id") or ""),
                                         str(metadata.get("channel") or ""), thread_id, self.writer.request_id)
        except ValueError as exc:
            raise _GrantRejected("source_identity_invalid") from exc
        command = self.command
        return authorize_decision_experiment(
            self.agent, self.params, source=source, expected_revision=view["revision"], points=[command["point"]],
            duration_seconds=command["duration_seconds"], max_http_requests=command["max_http_requests"],
            max_input_tokens=command["max_input_tokens"], input_bound_policy=EMPIRICAL_INPUT_BOUND_POLICY)

    # LLM: 回执只含身份、状态与固定代码/授权编号，不含任务正文、密钥或设置内容。
    # 函数用途: 生成本请求的实验授权回执。
    def _receipt(self, status: str, **fields: str) -> dict:
        return {"schema": _GRANT_SCHEMA, "request_id": self.writer.request_id,
                "execution_attempt_id": self.writer.execution_attempt_id,
                "run_id": str(getattr(self.params, "run_id", "") or ""),
                "attempt_id": str(getattr(self.params, "attempt_id", "") or ""), "status": status, **fields}

    # LLM: create=True 只在没有回执时写入；否则只替换同一次 granting 回执。内存请求与文件保持相同。
    # 函数用途: 用原子 JSON 更新写回执，返回本次是否真正写入。
    def _write(self, receipt: dict, *, create: bool) -> bool:
        written = False

        # LLM: 在原 JSON 锁内比较既有回执，不能覆盖其它状态或其它代次的授权记录。
        # 函数用途: 按预留或收尾规则决定是否替换请求中的实验回执。
        def update(current: dict) -> dict:
            nonlocal written
            previous = current.get(EXPERIMENT_GRANT_KEY)
            granting = isinstance(previous, dict) and previous.get("status") == "granting"
            if (previous is not None) if create else not granting:
                return current
            written = True
            return {**current, EXPERIMENT_GRANT_KEY: receipt}

        update_json_file_atomic(self.writer.request_path, update, require_existing=True)
        if written and isinstance(self.writer.request, dict):
            self.writer.request[EXPERIMENT_GRANT_KEY] = dict(receipt)
        return written

    # LLM: 只通过本轮原 on_chunk 发一行进度文本，不进入模型历史；显示失败不影响授权或业务。
    # 函数用途: 告诉用户实验授权已建立或被拒绝及原因，重放时没有新回执则不重复提示。
    def _notify(self, receipt: dict | None) -> None:
        on_chunk = getattr(self.params, "on_chunk", None)
        if receipt is None or not callable(on_chunk):
            return
        if receipt["status"] == "granted":
            command = self.command
            text = (f"\n[决策实验] 已授权本轮只观察 {command['point']}：{command['duration_seconds']} 秒内最多 "
                    f"{command['max_http_requests']} 次请求、{command['max_input_tokens']} 输入 token；"
                    "输入上界按经验估计（empirical:jev_wire_bytes.v1），不是供应商保证。\n")
        else:
            text = f"\n[决策实验] 未建立授权（{receipt.get('code', '')}）；本轮任务照常执行，不发送实验请求。\n"
        try:
            on_chunk(text)
        except Exception:  # noqa: BLE001 展示失败不改变授权事实
            _LOGGER.warning("决策实验授权提示未能显示。")


# LLM: 仅内部使用的结构化拒绝；code 为固定值，不会被当作通用异常文案解析。
# 类用途: 表示授权前置条件不满足，携带写入回执的拒绝代码。
class _GrantRejected(Exception):
    # LLM: code 来自本模块固定分支。
    # 函数用途: 保存拒绝代码。
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# LLM: 只按异常类型映射固定代码；冲突/访问/配置错误的子类顺序在前，未知异常统一 grant_failed。
# 函数用途: 把授权失败转换成写入回执与提示的结构化代码。
def _rejection_code(exc: Exception) -> str:
    from ..backends.decision_protocol import DecisionInputError
    from ..contracts.model_call_budget import ModelCallBudgetError

    if isinstance(exc, _GrantRejected):
        return exc.code
    if isinstance(exc, ModelCallBudgetError):
        return exc.reason
    for kind, code in ((DecisionSettingsConflict, "settings_conflict"), (DecisionSettingsAccessError, "access_denied"),
                       (DecisionInputError, "invalid_identity"), (ModelProfileError, "invalid_request")):
        if isinstance(exc, kind):
            return code
    return "grant_failed"


# LLM: 普通请求（无 decision_experiment system_task）直接返回；writer 必须是本请求的 GatewayTaskBindingWriter。
# 函数用途: 供 GatewayTaskBindingWriter 在主轮绑定后调用的实验授权入口。
def grant_request_decision_experiment(writer: object, agent: object, params: object) -> dict | None:
    request = getattr(writer, "request", None)
    command = decision_experiment_task(request.get("system_task") if isinstance(request, dict) else None)
    if command is None:
        return None
    return GatewayExperimentGrant(writer, agent, params, command).run()


__all__ = ["EXPERIMENT_GRANT_KEY", "GatewayExperimentGrant", "grant_request_decision_experiment"]
