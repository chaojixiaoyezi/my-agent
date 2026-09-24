# LLM: 核心与插件共用的纯写入合同，只依赖标准库、PathAccessPolicy 与读取上下文的同一序列化原语；不得导入 Agent、Store 或执行器。
#   裁决必须与宿主 tooling/write_boundary.validate_write_boundary 一致且只可能更严（写入根之外一律拒绝）；
#   一致性由 test_workspace_write_context 的逐项比对强制。本文件会被逐字节复制进插件 SDK，只能用同包相对导入。
# 模块用途: 运输宿主冻结的工作区写入范围，并在插件写每个具体路径前做同一套检查。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .path_access_policy import PathAccessDecision, PathAccessPolicy
from .workspace_read_context import _absolute_path, _path_list, _policy_payload, _read_policy

WORKSPACE_WRITE_EXTENSION = "my-agent/workspace-write-context"
WORKSPACE_WRITE_VERSION = "1"
_INTERNAL_OUTPUT_JSON_NAME = "output.json"


# LLM: 全部字段来自宿主结构化写入边界；空 write_roots 表示拒绝全部写入。协议不是 OS 沙箱。
# 类用途: 保存一次调用可写的根、禁止根、锁定文件与内部结果文件约束。
@dataclass(frozen=True)
class WorkspaceWriteContext:
    cwd: Path
    write_roots: tuple[Path, ...]
    forbidden_roots: tuple[Path, ...]
    locked_files: tuple[Path, ...]
    output_json: Path | None
    product_roots: tuple[Path, ...]
    path_policy: PathAccessPolicy

    # LLM: 只输出独立协议值，不暴露宿主对象。
    # 函数用途: 为本次 MCP 请求生成可序列化的写入上下文。
    def to_payload(self) -> dict[str, object]:
        return {
            "version": WORKSPACE_WRITE_VERSION, "cwd": str(self.cwd),
            "write_roots": [str(root) for root in self.write_roots],
            "forbidden_roots": [str(root) for root in self.forbidden_roots],
            "locked_files": [str(path) for path in self.locked_files],
            "output_json": str(self.output_json) if self.output_json is not None else None,
            "product_roots": [str(root) for root in self.product_roots],
            "path_policy": _policy_payload(self.path_policy),
        }

    # LLM: 严格字段集合与版本；写入根必须是规范绝对路径，不从插件环境补根或放宽。
    # 函数用途: 在隔离插件中读回宿主冻结的写入上下文，畸形值明确失败。
    @classmethod
    def from_payload(cls, value: object) -> WorkspaceWriteContext:
        fields = {"version", "cwd", "write_roots", "forbidden_roots", "locked_files", "output_json",
                  "product_roots", "path_policy"}
        if not isinstance(value, dict) or set(value) != fields or value["version"] != WORKSPACE_WRITE_VERSION:
            raise ValueError("工作区写入上下文协议无效")
        output_json = _absolute_path(value["output_json"]) if value["output_json"] is not None else None
        return cls(_absolute_path(value["cwd"]), _path_list(value["write_roots"]), _path_list(value["forbidden_roots"]),
                   _path_list(value["locked_files"]), output_json, _path_list(value["product_roots"]),
                   _read_policy(value["path_policy"]))

    # LLM: 顺序与宿主一致：路径策略 → 写入根 → 内部结果文件 → 禁止根（更具体的写入根优先，同层禁止胜出）→ 锁定文件。
    # 函数用途: 判断一个目标路径是否允许写入，不创建或打开文件。
    def check(self, path: str | Path) -> PathAccessDecision:
        try:
            target = (self.cwd / Path(path)).resolve(strict=False)
        except (OSError, RuntimeError):
            return PathAccessDecision(False, "PATH_RESOLUTION_FAILED", "路径解析失败。")
        decision = self.path_policy.check(target)
        if not decision.allowed:
            return decision
        matching = [root for root in self.write_roots if target.is_relative_to(root)]
        if not matching:
            return PathAccessDecision(False, "PATH_WRITE_SCOPE_BLOCKED", "目标不在本次工作区写入范围内。")
        if (self.output_json is not None and target != self.output_json and target.name == _INTERNAL_OUTPUT_JSON_NAME
                and any(target.is_relative_to(root) for root in self.product_roots)):
            return PathAccessDecision(False, "PATH_WRITE_SCOPE_BLOCKED", "不能在产物目录写内部结果文件。")
        specificity = max(len(root.parts) for root in matching)
        for root in self.forbidden_roots:
            if target.is_relative_to(root) and len(root.parts) >= specificity:
                return PathAccessDecision(False, "PATH_WRITE_SCOPE_BLOCKED", "目标位于禁止写入的目录。")
        if any(target == locked or target.is_relative_to(locked) for locked in self.locked_files):
            return PathAccessDecision(False, "PATH_WRITE_SCOPE_BLOCKED", "目标文件已被锁定。")
        return PathAccessDecision(True)

    # LLM: 只返回包含目标的最具体写入根和相对段，供 nofollow 写入原语逐级打开；调用前必须已通过 check。
    # 函数用途: 为安全写入找到锚点根与相对路径。
    def anchor(self, path: str | Path) -> tuple[Path, tuple[str, ...]]:
        target = (self.cwd / Path(path)).resolve(strict=False)
        roots = sorted((root for root in self.write_roots if target.is_relative_to(root)),
                       key=lambda root: len(root.parts), reverse=True)
        if not roots:
            raise ValueError("目标不在写入范围内")
        return roots[0], target.relative_to(roots[0]).parts
