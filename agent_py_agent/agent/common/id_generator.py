"""B.1: 七类框架 ID 统一生成入口。

run_id / task_id / task_run_id / agent_run_id / attempt_id / session_id /
delegation_id 一律经 new_id(kind) 生成 opaque ID：prefix 由本模块的
kind→prefix 映射决定，调用方不再手拼前缀，杜绝"有的地方框架生成、
有的地方手拼"的漂移。

prefix 一经发布即不可改（存量记录、跨 run 引用、测试断言都依赖它）：
- run_id → subagent：历史命名，存量 task.json 记录沿用，改名会造成
  新旧 run id 形态分裂；
- attempt_id → attempt：lifecycle_runner_attempts 已在用，沿用；
- task_id / task_run_id / agent_run_id / delegation_id → R1 runtime.db
  新实体，前缀全新发布；
- session_id → session：框架派生会话（如 base.py 的 session-<run_id>）
  与随机铸造都从本映射取前缀。

生成形态与既有 _new_id 一致（opaque、可排序、低碰撞），且天然满足
B.2（无路径段/无斜杠/长度受限），可直接进 validate_opaque_id。
"""

from __future__ import annotations

import time
import uuid

#: B.1 框架 ID → 前缀。prefix 一经发布不可改。
#: R3 扩展（同一不变量：实体 ID 由框架生成，不手拼）：
#: contract_id → contract（AcceptanceContract），validator_operation_id → vop
#: （ValidatorOperation，A.8 每操作追到 attempt_id）。
ID_KIND_PREFIXES: dict[str, str] = {
    "run_id": "subagent",        # 既有 subagent run id
    "task_id": "task",           # R1: conversation task 权威记录
    "task_run_id": "taskrun",    # R1: TaskRun 实体
    "agent_run_id": "agentrun",  # R1: AgentRun 实体
    "attempt_id": "attempt",     # 既有 runner attempt id
    "session_id": "session",     # 会话 id（派生或铸造）
    "delegation_id": "delegation",  # R1: parent→child 委托链
    "contract_id": "contract",   # R3: AcceptanceContract（I.2 冻结契约）
    "validator_operation_id": "vop",  # R3: ValidatorOperation（A.8）
}

#: 业务记录 id（route/evpkt/finding/...）不走本入口，保持 utils._new_id。
__all__ = ["ID_KIND_PREFIXES", "UnknownIdKindError", "new_id"]


class UnknownIdKindError(ValueError):
    """请求的 ID 种类不在 B.1 七类内。"""


def new_id(kind: str) -> str:
    """按 B.1 种类生成 opaque 框架 ID。

    kind 必须落在 ID_KIND_PREFIXES 七类内；未知种类直接抛错，防止调用方
    绕过映射手造形态（B.1 只认框架生成）。
    """
    prefix = ID_KIND_PREFIXES.get(str(kind or "").strip())
    if prefix is None:
        raise UnknownIdKindError(
            f"未知 ID 种类: {kind!r}; B.1 七类 = {sorted(ID_KIND_PREFIXES)}"
        )
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
