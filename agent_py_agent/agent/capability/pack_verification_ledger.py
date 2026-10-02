# LLM: 能力包 v2 块 3 的唯一运行账本：每个 run 一个 JSONL，放在本任务的宿主存储根 `data/pack_verification/<run>.jsonl`
#   （和产物登记 data/artifacts/registry.jsonl 同一个任务根，跟着任务归档的保留期走），文件权限 600。
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

LEDGER_DIRECTORY = ("data", "pack_verification")
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

    # 函数用途: 返回本 run 已发过的返工提示次数。
    def rework_count(self) -> int:
        return sum(1 for row in self.records() if row.get("kind") == "rework")

    # LLM: key 由包摘要、检查程序、目标与各输入的路径和摘要组成；同样的内容只跑一次，收尾时直接复用写后结果。
    # 函数用途: 按复用键找本 run 最近一次的检查结果事实。
    def cached_fact(self, key: str) -> dict | None:
        rows = [row for row in self.records() if row.get("kind") == "result" and row.get("key") == key]
        return rows[-1].get("fact") if rows and isinstance(rows[-1].get("fact"), dict) else None


# LLM: 账本位置只取本任务的宿主存储根和 run_id；任何一个缺失（没有持久任务、纯测试）返回 None，调用方按“不核验”处理。
# 函数用途: 找到当前 run 的核验账本。
def ledger_for_run(task_root: Path | None, run_id: str) -> PackVerificationLedger | None:
    if task_root is None or not run_id:
        return None
    name = run_id if _SAFE_RUN_ID.fullmatch(run_id) else hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return PackVerificationLedger(Path(task_root).joinpath(*LEDGER_DIRECTORY, f"{name}.jsonl"))
