"""运行模式显式枚举（3.txt G4 补 1：managed run 不得自动降级）。

背景：权威库挂载失败（owner_home_dir 空 / DB 打不开 / 纯投影环境）时，
旧实现把 runtime_db 置 None 并在各入口「没找到 DB 就跳过权威链」——
同一代码路径既服务托管运行也服务本地投影，旁路是隐式的。

修复：执行模式由框架显式传入，挂载点与权威入口按模式决策：

- MANAGED：必须完整权威链。无 runtime.db 或挂载失败 → 构造即抛
  （fail-closed，不延迟到调用点、不静默降级）。
- LOCAL_UNMANAGED：本地非托管运行（纯文件层/投影），允许无权威库，
  create_run 只写投影。这是显式选择，不是「没找到 DB」的兜底。

未显式传入时按 owner_home_dir 推断（MANAGED if home else
LOCAL_UNMANAGED），保持存量调用行为不变。
"""

from __future__ import annotations

import enum


class ExecutionMode(str, enum.Enum):
    """权威链执行模式（字符串值便于配置/日志/持久化）。"""

    MANAGED = "managed"
    LOCAL_UNMANAGED = "local_unmanaged"


def resolve_execution_mode(
    explicit: ExecutionMode | str | None,
    *,
    has_home_dir: bool,
) -> ExecutionMode:
    """显式模式优先；未指定按有无 owner home 推断。

    非法字符串 → ValueError（fail-closed：拼错模式名不得静默落到
    推断/投影路径）。
    """
    if explicit is not None:
        if isinstance(explicit, ExecutionMode):
            return explicit
        text = str(explicit).strip().lower()
        if not text:
            # 空字符串 = 构造默认值（未显式指定）→ 按 home 推断。
            return ExecutionMode.MANAGED if has_home_dir else ExecutionMode.LOCAL_UNMANAGED
        try:
            return ExecutionMode(text)
        except ValueError:
            raise ValueError(
                f"非法 execution_mode: {explicit!r}（允许 {[m.value for m in ExecutionMode]}）"
            ) from None
    return ExecutionMode.MANAGED if has_home_dir else ExecutionMode.LOCAL_UNMANAGED


def expects_managed_authority(manager: object) -> bool:
    """权威入口统一判断（base.py/authorization_gate 共用）：是否必须权威库。

    MANAGED（显式或按 home 推断）→ True：repo 缺失即拒绝（fail-closed）；
    LOCAL_UNMANAGED → False：允许纯投影。未显式化的 manager（老路径
    兜底）按有无 owner_home_dir 判断，行为与 G4 补前一致。
    """
    mode = getattr(manager, "execution_mode", None)
    if mode is not None:
        # 值比较（==，str 枚举按值判等）：字符串手设（"managed"）与枚举
        # 成员等价判定，防绕过 _attach_runtime_db 直接设字符串时误判为
        # 非托管（fail-open）。未知模式值 → 按 MANAGED 处理（fail-closed：
        # 无法证明非托管 = 要权威）。
        return mode != ExecutionMode.LOCAL_UNMANAGED
    return bool(str(getattr(manager, "owner_home_dir", "") or "").strip())
