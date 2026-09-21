# LLM: 资源访问授权和任务执行归属分别冻结；这里只整理可信身份，不查询记录、发信号或推断缺失任务。
# 模块用途: 为进程工具和交互终端提供无副作用的身份合同，避免把可见范围误用成资源停止范围。
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# LLM: 相等比较才授予模型访问；不增加路径祖先匹配，空 owner 或 conversation 必须拒绝。
# 类用途: 保存后台进程可被哪个用户、哪个会话查询和操作的不可变范围。
@dataclass(frozen=True)
class ProcessAccessScope:
    owner_id: str = ""
    conversation_id: str = ""
    owner_home: str = ""

    # LLM: 模型可见访问必须同时具有 owner 和 conversation；执行归属不能补齐这里的缺值。
    # 函数用途: 判断共享 Gateway 中的进程查询是否具有必要的访问身份。
    def is_bound(self) -> bool:
        return bool(self.owner_id and self.conversation_id)


# LLM: 只读 executor 注入的 run_scope；Full Access 不抹去 owner home。会话选择保留原
# session/root task/root run/run 顺序，但这些访问回退值不能充当执行归属；联测 shell 与 process_session。
# 函数用途: 规范后台工具的用户和会话范围，用于权限比较及选择原持久目录，不改变文件或状态。
def process_access_scope(
    run_scope: object,
    owner_scope_root: object = "",
) -> ProcessAccessScope:
    scope = run_scope if isinstance(run_scope, dict) else {}
    owner_home = ""
    owner_root = scope.get("owner_home") or owner_scope_root
    if owner_root:
        owner_home = str(Path(str(owner_root)).expanduser().resolve(strict=False))
    owner_id = str(scope.get("owner_id") or "").strip()
    if not owner_id and owner_home:
        owner_id = f"path:{owner_home}"
    conversation_id = next(
        (
            str(scope.get(key) or "").strip()
            for key in ("session_id", "root_task_id", "root_run_id", "run_id")
            if str(scope.get(key) or "").strip()
        ),
        "",
    )
    return ProcessAccessScope(
        owner_id=owner_id,
        conversation_id=conversation_id,
        owner_home=owner_home,
    )


# LLM: 执行归属只读取宿主明确字段；root_task_id 不与权限 task_id、访问 conversation_id 或目录指纹互换。
# 类用途: 把资源绑定到真实用户、会话、持久任务和执行轮，允许精确控制且不扩大工具访问权限。
@dataclass(frozen=True)
class ProcessExecutionScope:
    owner_home: str = ""
    thread_id: str = ""
    root_task_id: str = ""
    run_id: str = ""
    attempt_id: str = ""

    # LLM: Full Access 只取消路径墙，不改变宿主地址；返回独立快照，原字典后续变化不能重绑资源。
    # 函数用途: 启动时复制可信执行身份，缺失的任务、会话或轮次保持为空，不猜测补齐。
    @classmethod
    def from_run_scope(cls, run_scope: object, owner_home: object) -> ProcessExecutionScope:
        scope = run_scope if isinstance(run_scope, dict) else {}
        return cls(
            owner_home=process_access_scope(scope, owner_home).owner_home,
            thread_id=str(scope.get("session_id") or ""),
            root_task_id=str(scope.get("root_task_id") or ""),
            run_id=str(scope.get("run_id") or ""),
            attempt_id=str(scope.get("attempt_id") or ""),
        )

    # LLM: 停止目标必须有规范 owner 加 thread/root task 或 run；全部已提供维度同时精确匹配。
    # 函数用途: 从启动预留和已登记资源中选择本次停止的归属，空目标不能成为跨任务通配符。
    def matches(self, target: ProcessExecutionScope) -> bool:
        if not target.owner_home or not (
            (target.thread_id and target.root_task_id) or target.run_id
        ):
            return False
        return self.owner_home == target.owner_home and all(
            not expected or actual == expected
            for actual, expected in (
                (self.thread_id, target.thread_id),
                (self.root_task_id, target.root_task_id),
                (self.run_id, target.run_id),
                (self.attempt_id, target.attempt_id),
            )
        )
