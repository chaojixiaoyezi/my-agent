from __future__ import annotations

"""把父代理派工声明的 output_files 播种成子代理 progress 账本的 expected_outputs。

让 expected_outputs_gate(声明驱动产物对账门)在子代理 closeout 时,能核对"父代理点名
要的产物是否真落地"——治真机实锤的"子代理标 DONE 但用户要的文件没产出"(r1c:任务要求
写 ip_top10.txt,子代理只写了过程日志就被判完成)。

纪律(与既有"声明驱动、绝不解析自然语言"裁决对齐):
- 只取结构化声明字段(output_files/output_refs)经 output_alignment 过滤的真实文件路径,
  绝不从 goal 自然语言里猜路径;
- 只在账本尚无 expected_outputs 声明时播种,不覆盖子代理自己/已有的声明;
- 零声明零影响:父代理没传 output_files 的任务完全不受影响;
- expected_outputs_gate 命中缺失走 repair(非终态 CONTINUE),是"打回补产物"不是硬卡死。
"""

from pathlib import Path

from ...subagents.services.output_alignment import (
    anchored_output_refs,
    looks_like_output_path,
)
from ...task_progress import read_task_progress, write_task_progress


def seed_declared_expected_outputs(agent: object, run_id: str) -> bool:
    """有 output_files 声明且账本尚无 expected_outputs 时,播种成对账条目。返回是否播种。"""
    root = _owner_home_root(agent)
    if root is None or not run_id:
        return False
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return False
    if read_task_progress(root, run_id).get("expected_outputs"):
        return False  # 尊重子代理自己/已有声明,不覆盖
    patterns = _seed_patterns(task)
    if not patterns:
        return False
    write_task_progress(
        root,
        run_id,
        {
            "expected_outputs": [
                {"pattern": pattern, "min_count": 1, "note": "declared via parent output_files"}
                for pattern in patterns
            ]
        },
    )
    return True


def _seed_patterns(task: object) -> list[str]:
    seen: dict[str, None] = {}
    try:
        anchored_refs = anchored_output_refs(task).anchored_refs
    except Exception:
        return []
    for ref in anchored_refs:
        text = str(ref or "").strip()
        if not text or not looks_like_output_path(text):
            continue
        if ".." in Path(text.replace("\\", "/")).parts:
            continue
        seen.setdefault(text, None)
    return list(seen)


def _owner_home_root(agent: object) -> Path | None:
    home_paths = getattr(agent, "home_paths", None)
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if owner_home:
        return Path(owner_home).expanduser().resolve(strict=False)
    root = getattr(agent, "root", None)
    return Path(root).expanduser().resolve(strict=False) if root else None


__all__ = ["seed_declared_expected_outputs"]
