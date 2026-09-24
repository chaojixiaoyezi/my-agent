# LLM: 实验授权是原 decision_settings 内的单一信封；这里只验证结构，不推断用户许可、建立预算或发送请求。
# v2 必带 input_bound_policy，记录用户接受的输入上界口径；v1 旧信封仍可读取/撤销，但运行时永不据其发送。
# 模块用途: 为宿主授权保存和运行时复读提供相同的严格字段合同，普通配置 patch 不消费本模块。
from __future__ import annotations

import math
from copy import deepcopy

from .decision_settings_schema import POINT_RUNTIME_SCOPES, positive_seconds
from .model_provider_schema import ModelProfileError

EXPERIMENT_SCHEMA_V1 = "decision_experiment_authorization.v1"
EXPERIMENT_SCHEMA = "decision_experiment_authorization.v2"
# 用户于 2026-09-24 接受的经验输入上界口径；代码只实现这一种可发送口径，其它值保留可读但不能发送。
EMPIRICAL_INPUT_BOUND_POLICY = "empirical:jev_wire_bytes.v1"
_BINDING_FIELDS = ("owner_ref", "thread_id", "task_id", "run_id", "attempt_id", "request_id", "ledger_id")
_SOURCE_FIELDS = ("owner_id", "actor_id", "channel", "thread_id", "request_id", "operation_id")
_V1_FIELDS = frozenset({"schema", "authorization_id", "status", "scope", "binding", "source", "points", "operations",
                        "duration_seconds", "issued_at", "expires_at", "max_http_requests", "max_input_tokens",
                        "settings_revision"})


# LLM: 身份只校验显式字符串，不修补空值、不解析自然语言或从路径猜归属；授权仍须由宿主已鉴权入口授予。
# 函数用途: 拒绝缺失、超长或含控制分隔符的实验身份。
def experiment_identifier(value: object) -> str:
    try:
        valid = type(value) is str and bool(value) and value == value.strip() and len(value.encode("utf-8")) <= 1024 and "\x00" not in value
    except UnicodeError:
        valid = False
    if not valid:
        raise ModelProfileError("实验身份字段无效。")
    return value


# LLM: RuntimeDB 为未晋升会话任务的主轮登记 root task=run_id（_bind_main_agent_authority 同一规则）；
# 实验授权、准入与预留共用此投影，避免空 task 让身份字段无效或让不同入口各自猜测。
# 函数用途: 返回实验身份使用的任务编号：有会话任务用原任务，否则用同一运行编号。
def experiment_task_id(task_id: str, run_id: str) -> str:
    return task_id or run_id


# LLM: 上限必须显式有限正整数；布尔值、零和超出标准持久整数范围的值不能被当作无限预算。
# 函数用途: 校验实验 HTTP 次数和完整输入 token 的最大值。
def experiment_positive_count(value: object) -> int:
    if type(value) is not int or not 0 < value <= (2**63 - 1):
        raise ModelProfileError("实验次数和输入 token 上限必须是有限正整数。")
    return value


# LLM: 第一片仅观察同一 thread 的已登记点，不支持后台范围、配置应用或权限扩大；列表不作 owner/thread 合并。
# 函数用途: 验证宿主显式授权的接入点清单，拒绝重复、空值及跨范围点。
def experiment_points(value: object) -> list[str]:
    if (type(value) is not list or not value or len(value) > len(POINT_RUNTIME_SCOPES)
            or any(type(point) is not str or POINT_RUNTIME_SCOPES.get(point) != "thread" for point in value)
            or len(value) != len(set(value))):
        raise ModelProfileError("实验需要非空、无重复的会话接入点清单。")
    return list(value)


# LLM: None 是没有授权；版本/字段/来源/固定到期完整读取，不把损坏或旧状态别名当成可执行许可。
# v1 按原字段集原样读回（无上界口径即不可发送）；v2 额外要求合法的 input_bound_policy 标识。
# 函数用途: 复制合法信封，保留宿主来源与准确任务身份，不修改任何配置或用量。
def validate_experiment_authorization(value: object) -> dict | None:
    if value is None:
        return None
    fields = {EXPERIMENT_SCHEMA_V1: _V1_FIELDS, EXPERIMENT_SCHEMA: _V1_FIELDS | {"input_bound_policy"}}
    schema = value.get("schema") if type(value) is dict else None
    if type(schema) is not str or schema not in fields or set(value) != fields[schema]:
        raise ModelProfileError("实验授权结构或版本无效。")
    if schema == EXPERIMENT_SCHEMA:
        experiment_identifier(value["input_bound_policy"])
    if type(value["status"]) is not str or value["status"] not in {"active", "revoked"} or value["scope"] != "thread" or value["operations"] != ["observe"]:
        raise ModelProfileError("实验授权状态、范围或操作无效。")
    experiment_identifier(value["authorization_id"])
    for key, required in (("binding", _BINDING_FIELDS), ("source", _SOURCE_FIELDS)):
        if type(value[key]) is not dict or set(value[key]) != set(required):
            raise ModelProfileError("实验授权缺少完整可信身份或来源。")
        for item in value[key].values():
            experiment_identifier(item)
    if value["source"]["thread_id"] != value["binding"]["thread_id"]:
        raise ModelProfileError("实验授权来源与任务会话不符。")
    revision = value["settings_revision"]
    if type(revision) is not dict or set(revision) != {"owner", "thread"} or any(type(v) is not int or v < 0 for v in revision.values()):
        raise ModelProfileError("实验授权需要完整设置版本。")
    experiment_points(value["points"])
    experiment_positive_count(value["max_http_requests"])
    experiment_positive_count(value["max_input_tokens"])
    duration = positive_seconds(value["duration_seconds"])
    try:
        valid_time = all(type(value[key]) in (int, float) and math.isfinite(value[key]) and value[key] > 0 for key in ("issued_at", "expires_at"))
    except OverflowError:
        valid_time = False
    if not valid_time or value["expires_at"] <= value["issued_at"] or not math.isclose(value["expires_at"] - value["issued_at"], duration, abs_tol=1e-6):
        raise ModelProfileError("实验授权需要固定、有限且一致的截止时间。")
    return deepcopy(value)
