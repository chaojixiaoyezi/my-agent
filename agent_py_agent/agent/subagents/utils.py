# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: small subagent file/id helpers with no business orchestration.

Human version:
这里放真正通用的小工具：生成 ID、合并列表、读取 JSON、只在文件缺失时写默认文件。
如果函数开始有业务含义，就应该搬回对应业务模块。
"""

import json
import time
import uuid
from pathlib import Path

from .models import SubAgentTask


# LLM: _new_id 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 构建id所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _new_id(prefix: str) -> str:
    """生成短 ID。"""

    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


# LLM: _merge_list 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新mergelist对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def _merge_list(left: list[str], right: list[str]) -> list[str]:
    """保持顺序合并两个字符串列表。"""

    merged = list(left)
    for item in right:
        if item not in merged:
            merged.append(item)
    return merged


# LLM: _read_json_object tolerates historical JSON-string-wrapped objects while still returning only dicts.
# 函数用途: 读取子代理 JSON 事实源，兼容旧的双层编码文件；读不到对象时返回空对象，不引入写入副作用。
def _read_json_object(path: Path) -> dict[str, object]:
    """读取 JSON object，缺失或格式不对时返回空对象。"""

    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, str):
            obj = json.loads(obj)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


# LLM: _apply_paths 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新路径对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def _apply_paths(task: SubAgentTask, paths: dict[str, object]) -> None:
    """把路径字典写回任务对象。"""

    for key, value in paths.items():
        setattr(task, key, value)


# LLM: _apply_missing_paths 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新missing路径对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def _apply_missing_paths(task: SubAgentTask, paths: dict[str, object]) -> None:
    """只补齐缺失路径，避免覆盖已有工单位置。"""

    for key, value in paths.items():
        if not getattr(task, key, None):
            setattr(task, key, value)


# LLM: _write_if_missing 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 写入ifmissing的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
def _write_if_missing(path: Path, content: str) -> None:
    """只在文件不存在时写入，避免覆盖子代理已产出的内容。"""

    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# LLM: _write_json_if_missing 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 写入JSONifmissing的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
def _write_json_if_missing(path: Path, payload: dict[str, object]) -> None:
    """只在 JSON 文件不存在时写入默认结构。"""

    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
