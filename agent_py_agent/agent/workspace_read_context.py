# LLM: 这是核心与插件共用的纯读取合同，只依赖标准库和唯一 PathAccessPolicy；不得导入 Agent、Store 或执行器。
# 模块用途: 运输冻结的工作区读取权限，并在读取每个具体路径时检查原权限和当前范围。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .path_access_policy import PathAccessDecision, PathAccessPolicy

WORKSPACE_READ_EXTENSION = "my-agent/workspace-read-context"
WORKSPACE_READ_VERSION = "1"


# LLM: 所有字段来自宿主结构化事实；空 read_roots 明确拒绝全部读取，协议不是 OS 沙箱。
# 类用途: 保存一次调用的 cwd 和读取上界，避免共享插件串用另一个会话的目录。
@dataclass(frozen=True)
class WorkspaceReadContext:
    cwd: Path
    read_roots: tuple[Path, ...]
    path_policy: PathAccessPolicy
    granted_external_roots: tuple[Path, ...]
    external_policy: PathAccessPolicy

    # LLM: 仅返回独立的协议值，不暴露宿主对象或共享可变列表；arguments 与安装设置由原执行链管理。
    # 函数用途: 为本次 MCP 请求生成可序列化的只读上下文。
    def to_payload(self) -> dict[str, object]:
        return {
            "version": WORKSPACE_READ_VERSION, "cwd": str(self.cwd),
            "read_roots": [str(root) for root in self.read_roots],
            "path_policy": _policy_payload(self.path_policy),
            "granted_external_roots": [str(root) for root in self.granted_external_roots],
            "external_policy": _policy_payload(self.external_policy),
        }

    # LLM: 恢复仅接受当前完整协议；不调用 from_values，不从插件环境补根或将空权限变成 cwd。
    # 函数用途: 在隔离插件中读回宿主冻结的上下文，畸形或过宽范围明确失败。
    @classmethod
    def from_payload(cls, value: object) -> WorkspaceReadContext:
        fields = {"version", "cwd", "read_roots", "path_policy", "granted_external_roots", "external_policy"}
        if not isinstance(value, dict) or set(value) != fields or value["version"] != WORKSPACE_READ_VERSION:
            raise ValueError("工作区读取上下文协议无效")
        cwd = _absolute_path(value["cwd"])
        roots, external = _path_list(value["read_roots"]), _path_list(value["granted_external_roots"])
        policy, external_policy = _read_policy(value["path_policy"]), _read_policy(value["external_policy"])
        if (any(not root.is_relative_to(cwd) for root in roots)
                or any(not any(root.is_relative_to(allowed) for allowed in roots) for root in external)
                or external_policy.owner_scope_root is not None or external_policy.mode != policy.mode):
            raise ValueError("工作区读取上下文范围无效")
        return cls(cwd, roots, policy, external, external_policy)

    # LLM: 每个实际目标都须先解析再验证；授权根不能替代危险路径/owner 裁决，符号链接按真实目标判断。
    # 函数用途: 对文件或目录子项执行同一读取范围检查，不打开文件。
    def check(self, path: Path) -> PathAccessDecision:
        try:
            target = (self.cwd / path).resolve(strict=False)
        except (OSError, RuntimeError):
            return PathAccessDecision(False, "PATH_RESOLUTION_FAILED", "路径解析失败。")
        if not any(target.is_relative_to(root) for root in self.read_roots):
            return PathAccessDecision(False, "PATH_READ_SCOPE_BLOCKED", "目标不在本次工作区读取范围内。")
        return self.path_policy.check_with_external_roots(
            target, self.granted_external_roots, external_policy=self.external_policy,
        )


# LLM: 策略只包含已冻结字段；序列化不能重新读环境或计算另一份权限。
# 函数用途: 将唯一策略转换成协议值。
def _policy_payload(policy: PathAccessPolicy) -> dict[str, object]:
    return {
        "mode": policy.mode, "dangerous_roots": [str(root) for root in policy.dangerous_roots],
        "owner_scope_root": str(policy.owner_scope_root) if policy.owner_scope_root is not None else None,
        "agent_home_root": str(policy.agent_home_root) if policy.agent_home_root is not None else None,
    }


# LLM: 这里只校验冻结协议而不做权限归一化，避免独立 Python 的 HOME 改变宿主决定。
# 函数用途: 恢复完整路径策略；缺字段、未知模式及相对根拒绝。
def _read_policy(value: object) -> PathAccessPolicy:
    fields = {"mode", "dangerous_roots", "owner_scope_root", "agent_home_root"}
    if not isinstance(value, dict) or set(value) != fields or value["mode"] not in ("normal", "full"):
        raise ValueError("路径策略协议无效")
    return PathAccessPolicy(
        mode=value["mode"], dangerous_roots=_path_list(value["dangerous_roots"]),
        owner_scope_root=_absolute_path(value["owner_scope_root"]) if value["owner_scope_root"] is not None else None,
        agent_home_root=_absolute_path(value["agent_home_root"]) if value["agent_home_root"] is not None else None,
    )


# LLM: 绝对根必须来自宿主，禁止在接收进程展开变量、~ 或 .. 后重新解释协议。
# 函数用途: 校验单个规范绝对路径。
def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("上下文路径无效")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError("上下文路径必须为规范绝对路径")
    return path


# LLM: 空列表保留 deny-all 含义；不接受字符串冒充序列，不丢弃坏项以掩盖协议错误。
# 函数用途: 读取互不重复的规范路径列表。
def _path_list(value: object) -> tuple[Path, ...]:
    if not isinstance(value, list):
        raise ValueError("上下文路径列表无效")
    result = tuple(_absolute_path(item) for item in value)
    if len(result) != len(set(result)):
        raise ValueError("上下文路径重复")
    return result
