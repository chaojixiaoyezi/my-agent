from __future__ import annotations

"""单一权威 workspace 资源 scope resolver（seq 245 P5）。

ActionPolicy（审计记录）、concurrency（调度投影）、operation-store（写锁）
共用同一解析结果，绝不各算一套：只要调用方持有 workspace_root 上下文，
scope 一律归一化到 canonical physical root（"workspace:{path}"）；
无物理根上下文（纯 policy 层，无法归一化）时回落旧文本投影，保持既有
调用面兼容。锁的排他语义只由 operation-store 在 executor 层执行——
executor 层必有 workspace_root。
"""

import json
from pathlib import Path

from .models import resource_scopes_for_runtime_policy
from .registry_workspace import effective_registry_cwd


def authoritative_workspace_scopes(
    *,
    workspace_root: Path | None,
    write_boundary: dict | None,
    policy,
    arguments: dict,
    additional_scopes: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """把运行期资源参数投影为权威锁 scope。

    - declared：static_scopes 原样透传（声明式 scope 字符串）。
    - from_arguments：parameter_names 命中的参数值 → canonical physical root
      归一化锁 "workspace:{path}"。相对路径以 workspace_root 为基准 resolve
      （别名 out/../out/x 与 symlink 最终归一化到同一物理根 → 同一 scope）；
      绝对路径 canonicalize 后照锁（参数本身即真实写根，锁不能跳过）；
      list 参数逐项锁；参数缺失 → 锁 effective_registry_cwd（handler 的缺省
      写根——run_command 缺省 working_dir 补绝对路径是 0 锁实锤）。
    - none / 未声明参数 / 非路径类型值（结构信号缺失）：不产生锁 scope。
    - workspace_root 缺失（纯 policy 层无物理根上下文）：回落旧文本投影
      "name:value"（此时无锁可比，仅调度/审计投影）。
    """
    mode = str(getattr(policy.resource_scopes, "mode", "") or "").strip().lower()
    if mode == "declared":
        static = getattr(policy.resource_scopes, "static_scopes", ()) or ()
        return tuple(
            str(scope) for scope in static if str(scope or "").strip()
        )
    if mode != "from_arguments":
        return ()
    names = list(getattr(policy.resource_scopes, "parameter_names", ()) or ())
    if not names:
        return ()
    if workspace_root is None:
        return resource_scopes_for_runtime_policy(
            policy,
            arguments,
            additional_scopes=additional_scopes,
        )
    scopes: list[str] = []
    root = Path(workspace_root)
    cwd = effective_registry_cwd(root, write_boundary or {}).resolve()
    kinds = getattr(policy.resource_scopes, "parameter_kinds", None) or {}
    domains = getattr(policy.resource_scopes, "resource_domains", None) or {}
    for name in names:
        value = arguments.get(name)
        # seq 248 #6：logical 型参数（session_id/artifact_ref 等）不是物理路径，
        # 投影 "logical:{name}:{value}" 文本 scope——绝不 resolve 成 workspace
        # 假写根（把逻辑 ID 当路径锁会锁错根且互不冲突）。
        # seq 261 #1：缺省/空值不产 scope（共享 "logical:{name}:null" 会让无关
        # 调用互撞）；list 逐元素投影去重（整体 json.dumps 成一条 scope 时，
        # [r1,r2] 与 [r2,r3] 交集为 0，共同目标 r2 不互斥——语义错误）。
        # seq 266 #1：先 strip 再构造（首尾空格是同一目标，handler 会归一），
        # 并按资源域映射（run_id/run_ids → agent_run）锁「同一资源」而非
        # 「参数名+原值」——resource_domains 缺省时域 = 参数名，行为不变。
        if str(kinds.get(name) or "path").lower() == "logical":
            if value is None:
                continue
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not isinstance(item, str):
                    continue
                text = str(item).strip()
                if not text:
                    continue
                domain = domains.get(name) or name
                scopes.append(f"logical:{domain}:{text}")
            continue
        if value is None:
            scopes.append(f"workspace:{cwd}")
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if not isinstance(item, str) or not str(item).strip():
                continue
            if Path(item).is_absolute():
                path = Path(item).resolve()
            else:
                path = (root / item).resolve(strict=False)
            scopes.append(f"workspace:{path}")
    scopes.extend(
        str(scope) for scope in additional_scopes if str(scope or "").strip()
    )
    return tuple(dict.fromkeys(scopes))
