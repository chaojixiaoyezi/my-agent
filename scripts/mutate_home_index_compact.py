#!/usr/bin/env python3
"""home_index_compact 的变异验证脚本。

每个变异体锚定一处真实机制，改坏它之后必须让至少一条测试变红；
存活（SURVIVED）说明那条机制没被任何测试钉住。

用法（cwd 必须是工作树根）：
    python3 scripts/mutate_home_index_compact.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "agent_py_agent/agent/user_space/home_index_compact.py"
INDEXES = REPO / "agent_py_agent/agent/user_space/home_indexes.py"
TEST = "agent_py_agent/tests/test_home_index_compact.py"


@dataclass(frozen=True)
class Mutant:
    name: str
    note: str
    old: str
    new: str
    # 变异落在哪个文件；省略表示默认的 home_index_compact.py。
    # 读取侧的键定义在 home_indexes.py，必须显式指过去，否则锚点永远匹配不上。
    path: Path | None = None


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "MH1",
        "保留**第一行**而不是最后一行（C1/C2 行序与陈旧状态复活）",
        "                last_line_for_key[key] = line_index",
        "                last_line_for_key.setdefault(key, line_index)",
    ),
    Mutant(
        "MH2",
        "key_fields 统一退化成 ('owner_id',)（C2：别的文件的 key 用错）",
        "    from .home_indexes import key_fields_for_index_file\n\n    return key_fields_for_index_file(path)",
        '    return ("owner_id",)',
    ),
    Mutant(
        "MH3",
        "非 JSON 对象也当合法 key（JSON 数组/标量行被当成记录保留）",
        """    if not isinstance(record, dict):
        return None""",
        """    if not isinstance(record, dict):
        return tuple(str(record) for _ in key_fields)""",
    ),
    Mutant(
        "MH3b",
        "key 字段缺失时不填空串（key 变成 'None'，与实际行的 key 对不上）",
        """    return tuple(str(record.get(field) or "") for field in key_fields)""",
        """    return tuple(record.get(field) for field in key_fields)""",
    ),
    Mutant(
        "MH4",
        "压缩后不接窗口期追加的尾部字节（并发追加丢失）",
        "            _copy_tail(target, tmp, prefix_len)",
        "            pass",
    ),
    Mutant(
        "MH5",
        "压缩后不记录本次大小（2 倍增长判据失去基准）",
        "    _LAST_COMPACT_BYTES[str(target)] = after_bytes",
        "    pass",
    ),
    Mutant(
        "MH6",
        "忽略冷却（冷却期内反复压缩）",
        "    if last_at and (current - last_at) < COMPACT_COOLDOWN_SECONDS:",
        "    if False:",
    ),
    Mutant(
        "MH7",
        "忽略 2 倍增长判据（刚压完又长一点就再压）",
        "    if size < max(COMPACT_MIN_BYTES, 2 * last_bytes):",
        "    if size < COMPACT_MIN_BYTES:",
    ),
    Mutant(
        "MH8",
        "不做文件身份核对（rebuild 换文件后把陈旧前缀写回）",
        "    if not _still_same_file(target, stat0, prefix_len):\n        _discard(tmp)\n        return IndexCompactResult(target, False, \"identity_changed\", before_bytes=prefix_len, bad_lines=bad_lines)",
        "    if False:\n        _discard(tmp)\n        return IndexCompactResult(target, False, \"identity_changed\", before_bytes=prefix_len, bad_lines=bad_lines)",
    ),
    Mutant(
        "MH10",
        "锁内不再核对身份（锁外核对通过 → 拿锁之间换文件，38 探针重现）",
        "            if not _still_same_file(target, stat0, prefix_len):\n                return None, \"identity_changed\"\n",
        "",
    ),
    Mutant(
        "MH11",
        "键定义不再来自 home_indexes 的注册表（各抄一份会漂移）",
        "    return key_fields_for_index_file(path)",
        '    return {"active_runs.jsonl": ("owner_id", "run_id", "task_id")}.get(\n        Path(path).name, ("owner_id",)\n    )',
    ),
    Mutant(
        "MH12",
        "**读取侧** runs 的键改成带 task_id（读取键变宽→压缩会合并掉读取侧视为不同的行）",
        '            home.global_index_active_runs_jsonl,\n            "run_path",\n            key_fields_for_index_file(home.global_index_active_runs_jsonl),',
        '            home.global_index_active_runs_jsonl,\n            "run_path",\n            ("owner_id", "run_id", "task_id"),',
        path=INDEXES,
    ),
    Mutant(
        "MH9",
        "坏行不计数（坏行被当成正常丢弃，dropped 口径失真）",
        "                bad_lines += 1  # 坏行计数上报，不静默",
        "                pass",
    ),
)


def run_tests() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "--no-header", "-x", "-p", "no:cacheprovider"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-600:]


def main() -> int:
    originals = {path: path.read_text(encoding="utf-8") for path in {TARGET, INDEXES}}
    backups = {path: path.with_suffix(".py.mutbak") for path in originals}
    for path, backup in backups.items():
        shutil.copy2(path, backup)

    baseline_ok, baseline_out = run_tests()
    if not baseline_ok:
        print("BASELINE FAILED —— 先让未变异版本全绿再跑变异")
        print(baseline_out)
        return 2
    print("baseline: PASS\n")

    killed = 0
    survived: list[str] = []
    try:
        for mutant in MUTANTS:
            path = mutant.path or TARGET
            original = originals[path]
            if original.count(mutant.old) != 1:
                print(f"{mutant.name}: ANCHOR MISS (count={original.count(mutant.old)})")
                survived.append(mutant.name + "(anchor-miss)")
                continue
            path.write_text(original.replace(mutant.old, mutant.new), encoding="utf-8")
            ok, out = run_tests()
            path.write_text(original, encoding="utf-8")
            if ok:
                print(f"{mutant.name}: SURVIVED  -- {mutant.note}")
                survived.append(mutant.name)
            else:
                print(f"{mutant.name}: KILLED    -- {mutant.note}")
                killed += 1
    finally:
        for path, backup in backups.items():
            backup.replace(path)

    print(f"\n{killed}/{len(MUTANTS)} KILLED, {len(survived)} survived")
    if survived:
        print("survived:", ", ".join(survived))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
