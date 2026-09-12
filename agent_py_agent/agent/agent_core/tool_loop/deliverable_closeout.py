# LLM: 本模块是"声明了交付物就必须真的落盘"的收口门，作用域严格限定在子代理 run
#   (context_scope=task_local/control_plane)。判定只用结构化事实：宿主/父代理写进
#   task_attributes 的交付清单字符串 + 文件系统 is_file() 事实；不读 goal 文字，
#   不从正文猜产物路径，也不对主代理的用户会话生效。
#   与 EXEC-38(2026-08-16 owner 拍板)的关系：那次撤销的是"推断式产物门"——对任何任务
#   都猜一个交付目录，任务千奇百怪时必然误伤。这里恢复的是它的严格子集：只有**宿主自己
#   声明过**的交付清单才校验，且有界（超限放行，交给如实报告），主代理会话永不生效。
# 模块用途: 阻止子代理在"父代理点名要写的文件一个都没落盘"时直接收口成 DONE。
from __future__ import annotations

from pathlib import Path

# 声明式交付清单的权威字段：这些名字只出现在宿主写下的结构化 attributes/账本里，
# 模型参数无法伪造（artifact_refs 故意不在列表内——它是宿主自己的内部产物引用，
# 运行期本来就不存在，纳入会造成误判）。
_DELIVERABLE_KEYS = ("required_file_refs", "declared_output_refs", "output_files", "output_refs")
_CHILD_SCOPES = frozenset({"task_local", "control_plane"})
_STATE_KEY = "deliverable_closeout_repairs"
_MAX_DELIVERABLE_REPAIRS = 2
_MISSING_PREVIEW_LIMIT = 5


# LLM: 只读取结构化清单；相对路径或无法解析的值按"没有可核对的事实"丢弃，绝不猜。
# 函数用途: 返回本 run 声明过的交付物绝对路径（去重、保持声明顺序）。
def declared_deliverables(params: object) -> list[str]:
    sources = [
        getattr(params, "task_attributes", None),
        getattr(params, "live_archive_state", None),
    ]
    refs: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in _DELIVERABLE_KEYS:
            raw = source.get(key)
            for item in raw if isinstance(raw, (list, tuple)) else ():
                text = str(item or "").strip()
                if not text or text in refs:
                    continue
                try:
                    path = Path(text).expanduser()
                except (OSError, RuntimeError, ValueError):
                    continue
                if not path.is_absolute():
                    continue
                refs.append(str(path.resolve(strict=False)))
    return refs


# LLM: 交付门只认"文件系统事实"：is_file() 为假即视为缺产物；已存在同名目录也算存在，
#   避免把"要求产出目录"的任务误判成缺产物。
# 函数用途: 返回声明清单中尚未落盘的路径。
def missing_deliverables(params: object) -> tuple[list[str], list[str]]:
    declared = declared_deliverables(params)
    if not declared:
        return [], []
    missing: list[str] = []
    for raw in declared:
        try:
            if not Path(raw).exists():
                missing.append(raw)
        except OSError:
            missing.append(raw)
    return declared, missing


# LLM: 有界收口门的唯一入口。返回 None 表示允许收口；返回文本表示本轮否决收口，
#   调用方必须把它作为一条宿主指令回灌并 continue 一轮。次数记在 live_archive_state
#   上（每 run 一份），超限后放行——绝不把"必须产出"变成新的死循环。
# 函数用途: 判断这次 plain final 是否属于"声明了交付物却一个都没落盘"，并登记一次阻断。
def deliverable_closeout_block(params: object) -> str | None:
    scope = str(getattr(params, "context_scope", "") or "").strip().lower()
    if scope not in _CHILD_SCOPES:
        return None
    declared, missing = missing_deliverables(params)
    if not declared or not missing:
        return None
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return None
    attempts = int(state.get(_STATE_KEY) or 0)
    if attempts >= _MAX_DELIVERABLE_REPAIRS:
        return None
    state[_STATE_KEY] = attempts + 1
    preview = "、".join(missing[:_MISSING_PREVIEW_LIMIT])
    suffix = "" if len(missing) <= _MISSING_PREVIEW_LIMIT else f" 等 {len(missing)} 个"
    return (
        "[deliverable-closeout-blocked]\n"
        f"本轮被判定为不能收口：宿主声明的交付物里还有 {len(missing)} 个不在磁盘上：{preview}{suffix}。\n"
        "这不是格式问题，也不是让你复述计划：请立刻用写文件工具把缺的产物真正写到声明路径"
        "（大文件用 write_file 覆盖首块 + mode=\"append\" 追加，写完后读回确认），"
        "或者在确实做不到时走结构化上抛（capability_request / 如实标记未完成与原因），"
        "不要用\"即将写入/下一步再做\"的中间汇报收口。\n"
        f"（本次是第 {attempts + 1}/{_MAX_DELIVERABLE_REPAIRS} 次阻断，超限后会按如实报告收口。）"
    )


__all__ = [
    "declared_deliverables",
    "deliverable_closeout_block",
    "missing_deliverables",
]
