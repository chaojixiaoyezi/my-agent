# LLM: 选模型询问节奏（J4）：points.model_selection.cadence=structure_change 时只在结构变化才问决策模型；结构 = 压缩代数 +
#   候选目录版本 + 当前冻结的模型档案，从没成功问过（新会话）也算变化。指纹只在成功拿到回答后记；every_turn（默认）不读不写指纹文件。
#   指纹文件在 owner 决策数据目录（model_selection_structure.json），最多 SELECTION_STRUCTURE_THREADS_COUNT 个会话；读不出按没问过处理。
#   只读结构化字段，不读消息正文；改动须同步 gateway_model_observation 与 test_decision_selection_cadence.py。
# 模块用途: 让选模型可以设成“只在新会话、压缩之后、模型目录或当前模型变化时才问”，省掉结构没变时每轮都问的调用。
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)

SELECTION_CADENCE_EVERY_TURN = "every_turn"
SELECTION_CADENCE_STRUCTURE_CHANGE = "structure_change"
# 到达诊断的原因码：开了“只在结构变化时问”，且结构没变，所以这一轮没问。
STRUCTURE_UNCHANGED = "structure_unchanged"
# 指纹文件最多保留的会话数：超过时淘汰最久没更新的，文件大小有界。
SELECTION_STRUCTURE_THREADS_COUNT = 500
_FILENAME = "model_selection_structure.json"
_SCHEMA = "model_selection_structure.v1"


# LLM: 与 decision_point_mode_from_read 同一分层：配置默认 → owner 覆盖 → 本会话覆盖（选模型是会话范围点位）；值经共用校验。
# 函数用途: 读出选模型这一轮生效的询问节奏。
def selection_cadence_from_read(config: object, owner_settings: dict, thread: object) -> str:
    from ..settings.decision_settings_schema import (
        validate_decision_field,
        validate_decision_settings,
    )
    from ..settings.defaults import default_config_value

    path, attr = "points.model_selection.cadence", "decision_model_selection_cadence"
    value = validate_decision_field(path, getattr(config, attr, default_config_value(attr)))
    layers = [validate_decision_settings(owner_settings)["overrides"]]
    if thread is not None:
        layers.append(validate_decision_settings(thread.decision_settings)["overrides"])
    for layer in layers:
        value = layer.get(path, value)
    return value


# LLM: 只取结构化字段；candidates_revision 是候选公开摘要的哈希，profile_id 是本请求冻结的当前模型档案。
# 函数用途: 生成一次选模型的结构指纹。
def selection_structure(thread: object, candidates_revision: str, profile_id: str) -> dict:
    return {"compact_generation": int(getattr(thread, "compact_generation", 0) or 0),
            "candidates_revision": str(candidates_revision or ""), "profile_id": str(profile_id or "")}


# LLM: 只读；没有规范路径、文件读不出、没有这个会话的记录都返回 False（照常询问）。
# 函数用途: 判断本会话这一轮的结构与上次成功询问时是否完全相同。
def selection_structure_unchanged(home_paths: object, thread_id: str, structure: dict) -> bool:
    path = _structure_path(home_paths)
    if path is None or not thread_id:
        return False
    report = read_json_object_report(path, context="decision_selection_cadence.read")
    threads = report.payload.get("threads") if isinstance(report.payload.get("threads"), dict) else {}
    entry = threads.get(thread_id)
    return isinstance(entry, dict) and entry.get("structure") == structure


# LLM: 有副作用：在同一把文件锁里读-改-写指纹文件（0600 不强制，与结果日志同目录同权限）；坏文件按空表重写。
#   超过上限时按 updated_at 淘汰最旧的会话。写失败只吞掉（指纹只用于少问，写不进去最多下一轮再问一次）。
# 函数用途: 记下本会话这次成功询问时的结构指纹。
def remember_selection_structure(home_paths: object, thread_id: str, structure: dict) -> None:
    path = _structure_path(home_paths)
    if path is None or not thread_id:
        return
    try:
        with locked_json_path(path):
            report = read_json_object_report(path, context="decision_selection_cadence.write")
            threads = _updated_threads(report.payload, thread_id, structure)
            write_json_file_atomic_unlocked(path, {"schema": _SCHEMA, "threads": threads})
    except OSError:
        return


# LLM: 纯函数：坏条目丢弃，写入本会话的新指纹，超过上限按 updated_at 淘汰最旧的。
# 函数用途: 算出写回指纹文件的会话表。
def _updated_threads(payload: dict, thread_id: str, structure: dict) -> dict:
    threads = payload.get("threads") if isinstance(payload.get("threads"), dict) else {}
    threads = {key: value for key, value in threads.items() if isinstance(value, dict)}
    threads[thread_id] = {"structure": dict(structure), "updated_at": round(time.time(), 3)}
    ordered = sorted(threads.items(), key=lambda item: float(item[1].get("updated_at") or 0))
    return dict(ordered[-SELECTION_STRUCTURE_THREADS_COUNT:])


# LLM: 冻结值对象；只在 structure_change 且结构变了、真的要问的那一轮创建。record_if_success 只在拿到供应商回答
#   （status=success 且有 response）时写指纹；observe 转后台时同步结果是 deferred，由 wrap 包住的后台回调在完成时再记。
# 类用途: 一次选模型询问对应的结构指纹，问成功后负责记下它。
@dataclass(frozen=True)
class SelectionStructureMemo:
    home_paths: object
    thread_id: str
    structure: dict

    # LLM: 只认 status=success 且带供应商响应的结果；deferred、超时、失败都不记，下一轮照常再问。
    # 函数用途: 这次询问成功拿到回答时记下结构指纹（写文件副作用），否则什么都不做。
    def record_if_success(self, outcome: object) -> None:
        if getattr(outcome, "status", "") == "success" and getattr(outcome, "response", None) is not None:
            remember_selection_structure(self.home_paths, self.thread_id, self.structure)

    # LLM: 返回的函数先记指纹再调原回调；原回调的异常处理不变（它自己吞异常），记指纹本身不抛 OSError。
    # 函数用途: 把后台完成回调包一层，完成时顺带记下结构指纹。
    def wrap(self, callback: Callable[[object], None]) -> Callable[[object], None]:
        # LLM: 后台 worker 线程里调用；先记指纹（不抛 OSError），再交原回调补记请求标记。
        # 函数用途: 后台完成时先记指纹再补记原请求。
        def recorded(outcome: object) -> None:
            self.record_if_success(outcome)
            callback(outcome)
        return recorded


# LLM: 只认宿主 HomePaths 的真实 Path 字段（替身/mock 属性不算，免得写出垃圾文件）。
# 函数用途: 返回 owner 决策数据目录下的指纹文件路径，没有规范路径时返回 None。
def _structure_path(home_paths: object) -> Path | None:
    outcomes = getattr(home_paths, "owner_decision_outcomes_jsonl", None)
    return outcomes.with_name(_FILENAME) if isinstance(outcomes, Path) else None


__all__ = [
    "SELECTION_CADENCE_EVERY_TURN",
    "SELECTION_CADENCE_STRUCTURE_CHANGE",
    "SELECTION_STRUCTURE_THREADS_COUNT",
    "STRUCTURE_UNCHANGED",
    "SelectionStructureMemo",
    "remember_selection_structure",
    "selection_cadence_from_read",
    "selection_structure",
    "selection_structure_unchanged",
]
