# LLM: 守卫默认值及校验唯一归 DeploymentPolicy；此读口只从宿主 JSON 冻结 typed 快照，旧 YAML/缓存/吞错分支已结束。
# 模块用途: 为现有运行调用方提供同一部署策略的只读访问与准确来源，不创建第二份配置权威。
from __future__ import annotations

import time
from pathlib import Path

from ..user_space.home_layout import MyAgentHomePaths, home_paths
from .deployment_policy import (
    DeploymentPolicyError,
    RuntimeGuardPolicy,
    deployment_policy_path,
    load_deployment_policy,
)


# LLM: 路径参数仅核对宿主唯一来源，不能从任意 JSON 的父目录反推 host；不接受 owner/task/run 临时覆盖。
# 函数用途: 从受信宿主策略生成隔离的运行守卫快照，损坏配置直接报错。
def runtime_guard_policy(path: Path | str | None = None, *,
                         trusted_host_paths: MyAgentHomePaths | None = None) -> RuntimeGuardPolicy:
    paths = trusted_host_paths if trusted_host_paths is not None else home_paths()
    canonical = deployment_policy_path(paths)
    if path is not None and Path(path).expanduser().resolve() != canonical.resolve():
        raise DeploymentPolicyError("deployment_policy_source_invalid")
    deployment = load_deployment_policy(trusted_host_paths=paths)
    policy = deployment.runtime_guards
    configured = set(deployment.to_dict().get("runtime_guards", {}))
    object.__setattr__(policy, "_source_path", str(canonical))
    object.__setattr__(policy, "_loaded_at", time.time())
    object.__setattr__(policy, "_configured_fields", tuple(sorted(configured)))
    return policy


# LLM: 值字典从 frozen typed 对象按需生成，不缓存或维护一份可变策略。
# 函数用途: 给原审计及序列消费者返回独立的守卫值投影。
def runtime_guard_data(path: Path | str | None = None, *, policy: RuntimeGuardPolicy | None = None) -> dict[str, object]:
    return _effective_policy(path=path, policy=policy).values


# LLM: 真实字段严格取 typed 值；代码内部退役键仅沿原默认，不接文件中的未知键。
# 函数用途: 保留整数读口，统一消费宿主冻结的守卫快照。
def runtime_guard_int(key: str, default: int = 0, *, path: Path | str | None = None,
                      policy: RuntimeGuardPolicy | None = None) -> int:
    return _effective_policy(path=path, policy=policy).int_value(key, default)


# LLM: 只接受已校验的真实 bool，不把外部字符串猜成授权开关。
# 函数用途: 保留布尔读口，统一消费宿主冻结的守卫快照。
def runtime_guard_bool(key: str, default: bool = False, *, path: Path | str | None = None,
                       policy: RuntimeGuardPolicy | None = None) -> bool:
    return _effective_policy(path=path, policy=policy).bool_value(key, default)


# LLM: 元组已在 typed 构造校验；显式空序列不能被默认序列替代。
# 函数用途: 为原重试调用方读取冻结的有限数字序列。
def runtime_guard_float_tuple(key: str, default: tuple[float, ...], *, path: Path | str | None = None,
                              policy: RuntimeGuardPolicy | None = None) -> tuple[float, ...]:
    return _effective_policy(path=path, policy=policy).float_tuple_value(key, default)


# LLM: 已冻结快照直接复用；没有快照才读唯一宿主策略，错误不得被吞成默认。
# 函数用途: 统一原各类守卫读取入口。
def _effective_policy(*, path: Path | str | None = None, policy: RuntimeGuardPolicy | None = None) -> RuntimeGuardPolicy:
    if policy is not None:
        if not isinstance(policy, RuntimeGuardPolicy):
            raise DeploymentPolicyError()
        return policy
    return runtime_guard_policy(path)


__all__ = ["RuntimeGuardPolicy", "runtime_guard_bool", "runtime_guard_data", "runtime_guard_float_tuple",
           "runtime_guard_int", "runtime_guard_policy"]
