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

def _new_id(prefix: str) -> str:
    """生成短 ID。"""

    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _merge_list(left: list[str], right: list[str]) -> list[str]:
    """保持顺序合并两个字符串列表。"""

    merged = list(left)
    for item in right:
        if item not in merged:
            merged.append(item)
    return merged


def _read_json_object(path: Path) -> dict[str, object]:
    """读取 JSON object，缺失或格式不对时返回空对象。"""

    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _apply_paths(task: SubAgentTask, paths: dict[str, object]) -> None:
    """把路径字典写回任务对象。"""

    for key, value in paths.items():
        setattr(task, key, value)


def _apply_missing_paths(task: SubAgentTask, paths: dict[str, object]) -> None:
    """只补齐缺失路径，避免覆盖已有工单位置。"""

    for key, value in paths.items():
        if not getattr(task, key, None):
            setattr(task, key, value)


def _write_if_missing(path: Path, content: str) -> None:
    """只在文件不存在时写入，避免覆盖子代理已产出的内容。"""

    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json_if_missing(path: Path, payload: dict[str, object]) -> None:
    """只在 JSON 文件不存在时写入默认结构。"""

    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


