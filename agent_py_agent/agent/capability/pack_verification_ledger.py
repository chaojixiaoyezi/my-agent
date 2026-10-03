# LLM: 能力包 v2 块 3 的唯一运行账本：每个 run 一个 JSONL，放在规范任务根（conversation/workspace_paths.canonical_task_root：
#   runs/<日期>/<键>、tasks/<日期>/<名>、audits/<编号>）的 `data/pack_verification/<run>.jsonl`，跟着任务归档的保留期走，文件权限 600。
#   子代理 run 的工作目录在 <任务根>/work/agents/<run> 下，这里往上找到规范任务根再落盘，不会落进模型可写的 work/。
#   data/pack_verification/ 整个目录由宿主托管（H3 声明对模型只读，读照常），块 4 的原件清单和副本也在这里。
#   记录种类：baseline（回合开始时的工作区快照）、result（一次检查程序结果）、closeout（收尾检查跑过）、rework（发过返工提示）。
#   返工次数、结果复用和对外事实都只读这本账；Compact 续跑、进程重启后照样有效，不放在会被重建的 live_archive_state 里。
#   写入失败只让本次核验少一条记录，不打断工具循环。改动同步 test_pack_verification_service.py。
# 模块用途: 保存一次 run 里宿主核验的基线、结果和返工次数，供写后检查、收尾检查和最终事实共用。

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

PACK_VERIFICATION_DIRECTORY = ("data", "pack_verification")
# run_id 可以直接做文件名的写法；其它写法用摘要命名，避免路径注入。
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9_.-]{1,96}\Z")


# LLM: 账本只由宿主写；读到坏行跳过（不猜），追加用 O_APPEND 保证单行原子。
# 类用途: 一个 run 的宿主核验账本。
class PackVerificationLedger:
    # 函数用途: 绑定账本文件路径（不创建文件）。
    def __init__(self, path: Path) -> None:
        self.path = path

    # LLM: 目录 700、文件 600；写失败返回 False，调用方据此不再依赖这条记录（例如返工计数写不进去就不返工）。
    # 函数用途: 追加一条记录。
    def append(self, record: dict) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except (OSError, TypeError, ValueError):
            return False
        return True

    # 函数用途: 读出全部可解析的记录（文件不存在时为空）。
    def records(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    # 函数用途: 返回本 run 第一条基线记录（没有时为 None）。
    def baseline(self) -> dict | None:
        return next((row for row in self.records() if row.get("kind") == "baseline"), None)

    # 函数用途: 返回本 run 某种记录的条数（如 rework、input_rework 是已发过的返工提示次数）。
    def count(self, kind: str) -> int:
        return sum(1 for row in self.records() if row.get("kind") == kind)

    # LLM: written 记录是写工具回执里本回合写出的工作区相对路径（9b 复审应修 2：基线截断时，只有它能证明不在基线里的文件是本回合写的）。
    # 函数用途: 返回本 run 写工具写出过的工作区相对路径集合。
    def written_paths(self) -> frozenset[str]:
        return frozenset(path for row in self.records() if row.get("kind") == "written"
                         for path in row.get("paths") or [] if isinstance(path, str))

    # LLM: 只数 trigger 相同、(包 ID, 检查程序 ID, 目标相对路径) 相同的 result 记录：failed 加一，passed 清零，其它状态（not_run、error，
    #   含写后反馈暂停记录）不加不减。块 8 试点后加，写后检查据此决定还要不要继续给写后反馈。
    # 函数用途: 返回某个检查对象在某种触发下当前连续失败的次数。
    def failure_streak(self, trigger: str, identity: tuple[str, str, str]) -> int:
        streak = 0
        for row in self.records():
            fact = row.get("fact") if row.get("kind") == "result" and row.get("trigger") == trigger else None
            if not isinstance(fact, dict) or (fact.get("package_id"), fact.get("verifier_id"), fact.get("target")) != identity:
                continue
            streak = 0 if fact.get("status") == "passed" else streak + (fact.get("status") == "failed")
        return streak

    # LLM: key 由包摘要、检查程序、目标与各输入的路径和摘要组成；同样的内容只跑一次，收尾时直接复用写后结果。
    # 函数用途: 按复用键找本 run 最近一次的检查结果事实。
    def cached_fact(self, key: str) -> dict | None:
        rows = [row for row in self.records() if row.get("kind") == "result" and row.get("key") == key]
        return rows[-1].get("fact") if rows and isinstance(rows[-1].get("fact"), dict) else None


# LLM: 账本位置只取宿主核验目录和 run_id；任何一个缺失（没有持久任务、不在规范任务根下、纯测试）返回 None，调用方按“不核验”处理。
# 函数用途: 找到当前 run 的核验账本。
def ledger_for_run(pack_root: Path | None, run_id: str) -> PackVerificationLedger | None:
    if pack_root is None or not run_id:
        return None
    name = run_id if _SAFE_RUN_ID.fullmatch(run_id) else hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return PackVerificationLedger(Path(pack_root) / f"{name}.jsonl")


# LLM: 起点是本 run 的任务工作区（current_run_task_workspace_root），连同它的各级上级逐个用 canonical_task_root 判断，第一个是规范
#   任务根的就用它；都不是（例如不在 owner home 的 runs/tasks/audits 下）返回 None，不在别处落盘。只做路径运算，不建目录。
# 函数用途: 返回本 run 的宿主核验目录 <规范任务根>/data/pack_verification。
def pack_verification_root(agent: object, params: object, owner: object) -> Path | None:
    from ..agent_core.run_task_workspace_writer import current_run_task_workspace_root
    from ..conversation.workspace_paths import canonical_task_root

    start = current_run_task_workspace_root(agent, params)
    home = getattr(owner, "home_dir", None)
    if start is None or home is None:
        return None
    for candidate in (Path(start), *Path(start).parents):
        if canonical_task_root(home, candidate) is not None:
            return candidate.joinpath(*PACK_VERIFICATION_DIRECTORY)
    return None
