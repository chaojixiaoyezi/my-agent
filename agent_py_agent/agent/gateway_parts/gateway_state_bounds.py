# LLM: gateway_state.json 的读写字节上限与收尾引用投影（GW-18）。排空/交接裁决先按内存完整行做完，
#   state 只存紧凑投影：每个 (reason, purpose) 一行 count + 规范化摘要，不存逐条明细；完整行留在
#   重启完成 marker（交接证明）与 gateway_run_cleanup 事件里。
# 模块用途: 保证 Gateway 自己写下的状态文件永远能被下一次启动的读者读回，并给出可与完整证明对账的摘要。
from __future__ import annotations

import hashlib
import json

from ..common.json_io import serialize_private_json_no_newline

# gateway_state.json 写者提交前强制的字节上限；原 16 KiB 的 state 读者同步放宽到此值，超限报 retirement_source_oversized。
GATEWAY_STATE_MAX_BYTES = 1024 * 1024
# 只投影这些收尾列表字段（含 cleanup_result/restart_drain 等嵌套结果里的同名字段），其它字段原样保留。
_PROJECTED_LIST_KEYS = frozenset({
    "alive_threads", "background_blockers", "unresolved_operation_refs", "continuation_refs",
    "executing_tools", "drain_blockers",
})
# 嵌套收尾结果的最大递归层数（cleanup_result 里还可能再套一层 cleanup_result）。
_NESTED_DEPTH_COUNT = 4
# 已是汇总的行（如 gateway_admission 前台汇总）只含这些字段且带正整数 count，不是逐条明细，原样保留。
_AGGREGATE_ROW_KEYS = frozenset({"reason", "ref", "purpose", "count"})


# LLM: 投影行与 foreground 汇总行自带 count；普通引用行与字符串计 1。只用于展示条数，不参与裁决。
# 函数用途: 统计一组（可能已投影的）收尾引用实际代表的条数。
def ref_count(rows: object) -> int:
    if not isinstance(rows, list):
        return 0
    return sum(max(1, int(row.get("count") or 1)) if isinstance(row, dict) else 1 for row in rows)


# LLM: 紧凑行 = 本模块的投影行（带 digest）或上游已汇总的 count 行；两者都不含逐条明细。
# 函数用途: 判断一行是否已是 state 可直接保存的紧凑行。
def _is_compact_row(row: object) -> bool:
    if not isinstance(row, dict) or type(row.get("count")) is not int or row["count"] < 1:
        return False
    return "digest" in row or set(row) <= _AGGREGATE_ROW_KEYS


def _is_projected(rows: list) -> bool:
    return all(_is_compact_row(row) for row in rows)


# LLM: 规范化 = 每行按 sort_keys 的紧凑 JSON，行文本排序后再哈希，与行顺序无关；完整证明可按同一规则复算对账。
# 函数用途: 计算一组完整引用行的规范化摘要。
def refs_digest(rows: list) -> str:
    encoded = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
                     for row in rows)
    return "sha256:" + hashlib.sha256("\n".join(encoded).encode("utf-8")).hexdigest()


# LLM: 空列表原样（非空/空的裁决语义不变）；全是紧凑行的列表原样（幂等）。其余按 (reason, purpose) 分组只留
#   count 与摘要；组内只有一条上游汇总行（如 gateway_admission 的 count 行）时原样保留；同组只有一个相同 ref 时
#   保留该 ref 便于识别，不留逐条样本。字符串列表（如 alive_threads 的循环名）按名字分组。
# 函数用途: 把逐条收尾引用投影成每原因一行的 count + 规范化摘要。
def project_refs(rows: object) -> object:
    if not isinstance(rows, list) or not rows or _is_projected(rows):
        return rows
    groups: dict[tuple[str, str], list] = {}
    for row in rows:
        if isinstance(row, dict):
            key = (str(row.get("reason") or "unlabeled"), str(row.get("purpose") or ""))
        else:
            key = (str(row), "")
        groups.setdefault(key, []).append(row)
    projected = []
    for (reason, purpose), members in groups.items():
        if len(members) == 1 and _is_compact_row(members[0]):
            projected.append(members[0])
            continue
        entry: dict[str, object] = {"reason": reason, "count": ref_count(members), "digest": refs_digest(members)}
        if purpose:
            entry["purpose"] = purpose
        refs = {row.get("ref") for row in members if isinstance(row, dict)}
        if len(refs) == 1 and None not in refs and all(isinstance(row, dict) for row in members):
            entry["ref"] = next(iter(refs))
        projected.append(entry)
    return projected


# LLM: 只认 _PROJECTED_LIST_KEYS 的列表，递归进嵌套字典（有界层数）；返回新字典，不修改调用方对象。
# 函数用途: 得到一个状态/收尾结果的紧凑 state 投影。
def project_gateway_state_lists(payload: dict, *, _depth: int = 0) -> dict:
    projected = {}
    for key, value in payload.items():
        if key in _PROJECTED_LIST_KEYS and isinstance(value, list):
            projected[key] = project_refs(value)
        elif isinstance(value, dict) and _depth < _NESTED_DEPTH_COUNT:
            projected[key] = project_gateway_state_lists(value, _depth=_depth + 1)
        else:
            projected[key] = value
    return projected


# LLM: 与 gateway_parts.io.update_json_file_atomic 落盘同一序列化，计量的就是读者将读到的字节数。
# 函数用途: 计算一个状态字典写盘后的字节数。
def gateway_state_bytes(state: dict) -> int:
    return len(serialize_private_json_no_newline(state, sort_keys=True).encode("utf-8"))


# LLM: 投影后仍超上限返回 None，由写者拒写并保留原（可读）文件；不截断其它业务字段冒充成功。
# 函数用途: 得到一份保证能被读者在 GATEWAY_STATE_MAX_BYTES 内读回的状态投影。
def fit_gateway_state(state: dict, *, max_bytes: int = GATEWAY_STATE_MAX_BYTES) -> dict | None:
    projected = project_gateway_state_lists(state)
    return projected if gateway_state_bytes(projected) <= max_bytes else None


__all__ = [
    "GATEWAY_STATE_MAX_BYTES",
    "fit_gateway_state",
    "gateway_state_bytes",
    "project_gateway_state_lists",
    "project_refs",
    "ref_count",
    "refs_digest",
]
