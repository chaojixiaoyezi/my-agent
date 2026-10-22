# LLM: 使用台账只保存身份与计数，不存方法正文；独立于学习开关，所有异常只能软降级。
# 模块用途: 统一记录 owner 的人工检索、读取、宿主注入与采用事实。
from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..common.nofollow_fs import (
    open_directory_beneath,
    read_text_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)
from ..user_space.owner_quota import OwnerQuotaChange
from .skill_learning_store import SkillLearningStore

USAGE_EVENTS = ("manual_search", "manual_get", "host_injected", "adopted")
MAX_USAGE_LEDGER_EVENTS = 2000
KEEP_USAGE_LEDGER_EVENTS = 1000
_CONTEXT_DEFAULTS = {"request_id": "", "thread_id": "", "task_id": "", "chars": 0}


# LLM: 关键字兼容且上下文白名单固定；与批量口共用原state事务，Agent上下文在进锁前冻结，所有异常只warning。
# 函数用途: 记录一次使用事实，不改变业务调用的结果。
def record_skill_usage_event(agent_or_store, *, event, skill_id, source, kind="skill", **context) -> None:
    try:
        row = _usage_row(agent_or_store, event, skill_id, source, kind, **context)
        store = _usage_store(agent_or_store)
        _record_usage_rows(store, [row])
    except Exception:  # noqa: BLE001 - 附带统计不能改变原工具/宿主的成功或失败。
        logging.getLogger(__name__).warning("SKILL_USAGE_RECORD_UNAVAILABLE")


# LLM: 整批先校验再拿原owner配额→state锁；空批次不创建目录，任何统计故障只warning，不写半批合法事件。
# 函数用途: 为一次搜索的全部命中只追加并保存一次使用台账。
def record_skill_usage_events(store: SkillLearningStore, events: Sequence[dict[str, object]]) -> None:
    try:
        rows = [_usage_row(store, **event) for event in events]
        if rows:
            _record_usage_rows(_usage_store(store), rows)
    except Exception:  # noqa: BLE001 - 附带批量统计不能改变原工具结果。
        logging.getLogger(__name__).warning("SKILL_USAGE_RECORD_UNAVAILABLE")


# LLM: 仅接收本地已校验事件；同一锁内恢复水位、连续编号、联合核额并保存一次，沿原双文件故障恢复。
# 函数用途: 让单条与批量记账共用同一个持久事务。
def _record_usage_rows(store, events: list[dict]) -> None:
    _check_usage_directory(store)
    with store.state_lock() as admission:
        rows = _ledger_rows(store)
        summary = _read_summary(store, rows)
        for row in events:
            row["sequence"] = summary["last_sequence"] + 1
            _add_event(summary, row)
            rows.append(row)
            if len(rows) > MAX_USAGE_LEDGER_EVENTS:
                rows = rows[-KEEP_USAGE_LEDGER_EVENTS:]
        _save_usage(store, admission, rows, summary)


# LLM: 只读恢复已写明细尚未写汇总的事件，不创建目录或保存恢复状态；故障不影响报告主入口。
# 函数用途: 读取 owner 使用汇总，失败时返回空计数。
def load_skill_usage_summary(store_or_home) -> dict:
    try:
        store = _usage_store(store_or_home)
        summary = _read_summary(store)
        _prune_daily(summary)
        return summary
    except Exception:  # noqa: BLE001 - 报告必须能在统计文件不可读时继续。
        logging.getLogger(__name__).warning("SKILL_USAGE_SUMMARY_UNAVAILABLE")
        return _empty_summary()


# LLM: Agent 优先取当前 owner 的 home_paths；不能用 cwd、全局 home 或学习开关决定归属。
# 函数用途: 兼容真实 Agent、Store、HomePaths 和 owner_home 路径，不创建新存储权威。
def _usage_store(value) -> SkillLearningStore:
    if isinstance(value, SkillLearningStore):
        return value
    home = getattr(value, "home_paths", value)
    if hasattr(home, "owner_skill_learning_dir") and hasattr(home, "owner_home_dir"):
        return SkillLearningStore.for_home(home)
    root = Path(getattr(home, "owner_home_dir", home))
    return SkillLearningStore(root / "data" / "skill_learning", root / "skills")


# LLM: 只接收固定事件/种类与结构化标识，未知上下文键拒绝；不把 query、正文或异常文本抄到账本。
# 函数用途: 生成一行有 UTC 日期和有界关联标识的使用记录。
def _usage_row(agent, event, skill_id, source, kind="skill", **context) -> dict:
    if event not in USAGE_EVENTS or kind not in {"skill", "capability_package"} or not skill_id:
        raise ValueError("invalid usage identity")
    if set(context) - set(_CONTEXT_DEFAULTS):
        raise ValueError("invalid usage context")
    params = getattr(agent, "_current_run_params", None)
    attrs = getattr(params, "task_attributes", None) or {}
    values = {key: context.get(key, getattr(params, key, default)) for key, default in _CONTEXT_DEFAULTS.items()}
    if "thread_id" not in context:
        values["thread_id"] = attrs.get("conversation_thread_id", "")
    chars = values.pop("chars")
    if type(chars) is not int or chars < 0:
        raise ValueError("invalid usage chars")
    at = datetime.now(timezone.utc).isoformat()
    return {"at": at, "date": at[:10], "event": event, "skill_id": str(skill_id)[:512],
            "kind": kind, "source": str(source)[:128], "chars": chars,
            **{key: str(value or "")[:512] for key, value in values.items()}}


# LLM: 文件路径固定在原 Store 目录内，读写统一 no-follow；坏 JSON 不按空账覆盖。
# 函数用途: 安全读取一份可选统计文件。
def _read_usage_text(store, name: str) -> str | None:
    root = store.skills_root.parent
    parts = (store.directory / name).relative_to(root).parts
    return read_text_beneath(root, parts)


# LLM: 原 Store 锁会以最近祖先为锚，先从 owner 根核受管父链，避免既有目录链接把锁文件建到其它 owner。
# 函数用途: 在进入原配额/状态锁前拒绝不安全目录；正常首次缺失仍交原 Store 创建。
def _check_usage_directory(store) -> None:
    root = store.skills_root.parent
    try:
        descriptor = open_directory_beneath(root, store.directory.relative_to(root).parts)
    except FileNotFoundError:
        return
    os.close(descriptor)


# LLM: JSONL 边界仅物理 LF，Unicode NEL/分隔符仍是合法标识内容；损坏行拒绝而非静默丢账。
# 函数用途: 读取完整有界事件账本，供追加或缺失汇总时重建。
def _ledger_rows(store) -> list[dict]:
    text = _read_usage_text(store, "usage_ledger.jsonl") or ""
    rows = [json.loads(line) for line in text.split("\n") if line.strip()]
    if any(not isinstance(row, dict) or row.get("event") not in USAGE_EVENTS for row in rows):
        raise ValueError("invalid usage ledger")
    return rows


# LLM: 四类事实分别计数；有效使用只算正文读取与实际宿主投递，不把检索/采用混进去。
# 函数用途: 返回新的空计数，避免跨 owner 共享可变对象。
def _empty_counts() -> dict:
    return {**dict.fromkeys(USAGE_EVENTS, 0), "effective_uses": 0}


# LLM: 日期映射与生命周期计数附带同一事件水位，明细裁剪不能重置累计总数；构造不落盘。
# 函数用途: 创建一个 owner 的空使用汇总。
def _empty_summary() -> dict:
    return {"totals": _empty_counts(), "by_skill": {}, "daily_history": {}, "last_sequence": 0}


# LLM: 生命周期计数取原汇总；先合并旧by_skill前缀键再补水位后明细，迁移不改总数、水位或日期，不在只读口落盘。
# 函数用途: 加载汇总并检查基本结构，拒绝损坏状态被新事件覆盖。
def _read_summary(store, rows: list | None = None) -> dict:
    text = _read_usage_text(store, "usage_summary.json")
    summary = json.loads(text) if text is not None else _empty_summary()
    if (not isinstance(summary, dict) or not isinstance(summary.get("totals"), dict)
            or not isinstance(summary.get("by_skill"), dict) or not isinstance(summary.get("daily_history"), dict)):
        raise ValueError("invalid usage summary")
    _summary_sequence(summary)
    _normalize_by_skill(summary)
    _replay_usage_rows(summary, _ledger_rows(store) if rows is None else rows)
    return summary


# LLM: 仅规范化统计聚合键，不改变快照、pin、授权身份或原始明细里的调用者stable_id。
# 函数用途: 去掉能力包和普通Skill的引用包装前缀，避免同一方法被拆开计数。
def _canonical_skill_id(kind: str, skill_id: str) -> str:
    if kind == "capability_package" or skill_id.startswith("capability:"):
        skill_id = skill_id.removeprefix("capability:")
    return skill_id.removeprefix("skill:")


# LLM: 只对各身份四类事件求和并重算有效使用；最近元数据来自时间最新的旧行，生命周期与每日历史原样保留。
# 函数用途: 在内存中合并旧摘要的前缀别名，下次正常写入时保存规范键。
def _normalize_by_skill(summary: dict) -> None:
    merged = {}
    for skill_id, counts in summary["by_skill"].items():
        canonical = _canonical_skill_id(counts.get("kind", "skill"), skill_id)
        if not canonical:
            raise ValueError("invalid usage skill identity")
        target = merged.setdefault(canonical, _empty_counts())
        for event in USAGE_EVENTS:
            value = counts.get(event, 0)
            if type(value) is not int or value < 0:
                raise ValueError("invalid usage count")
            target[event] += value
        target["effective_uses"] = target["manual_get"] + target["host_injected"]
        if counts.get("last_used_at", "") >= target.get("last_used_at", ""):
            target.update({key: counts[key] for key in ("last_used_at", "last_source", "kind") if key in counts})
    summary["by_skill"] = merged


# LLM: 每事件恰增四类中的一项，旧汇总可由四项之和恢复水位；不把effective_uses再加一次。
# 函数用途: 校验生命周期计数并恢复唯一汇总序号。
def _summary_sequence(summary: dict) -> None:
    counts = [summary["totals"].get(event, 0) for event in USAGE_EVENTS]
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("invalid usage totals")
    total = sum(counts)
    sequence = summary.get("last_sequence", total)
    if type(sequence) is not int or sequence != total:
        raise ValueError("invalid usage watermark")
    summary["last_sequence"] = sequence


# LLM: 明细必须单调；旧回滚丢失且已计入汇总的缺口仅告警不捏造行，水位后的缺号仍拒绝；兼容无序号前缀。
# 函数用途: 给旧行补内存序号，只补记水位后连续事件，避免旧领先汇总毒化后续记账。
def _replay_usage_rows(summary: dict, rows: list[dict]) -> None:
    numbered = next(((index, row["sequence"]) for index, row in enumerate(rows) if "sequence" in row), None)
    if numbered is not None and (type(numbered[1]) is not int or numbered[1] < 1):
        raise ValueError("invalid usage sequence")
    base = max(summary["last_sequence"] - len(rows), 0) if numbered is None else numbered[1] - numbered[0] - 1
    if base < 0:
        raise ValueError("invalid legacy usage prefix")
    previous = base
    for row in rows:
        sequence = row.setdefault("sequence", previous + 1)
        if type(sequence) is not int or sequence <= previous:
            raise ValueError("usage sequence is not increasing")
        if sequence != previous + 1:
            if sequence > summary["last_sequence"] + 1:
                raise ValueError("usage watermark has a gap")
            logging.getLogger(__name__).warning("SKILL_USAGE_DETAIL_GAP")
        if sequence > summary["last_sequence"]:
            if sequence != summary["last_sequence"] + 1:
                raise ValueError("usage watermark has a gap")
            _add_event(summary, row)
        previous = sequence
    if previous < summary["last_sequence"]:
        logging.getLogger(__name__).warning("SKILL_USAGE_DETAIL_GAP")


# LLM: 全量和单技能计数共用同一口径，旧有效使用字段按事实重新计算，不另加一次。
# 函数用途: 为指定事件增加计数并更新有效使用总数。
def _increment(counts: dict, event: str) -> None:
    for key in USAGE_EVENTS:
        value = counts.setdefault(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError("invalid usage count")
    counts[event] += 1
    counts["effective_uses"] = counts["manual_get"] + counts["host_injected"]


# LLM: 仅保留UTC最近30个日历日的日期映射，不把最近30个非空日当作30天，不保留未来日期。
# 函数用途: 清理三十天之外的每日历史并按日期排序。
def _prune_daily(summary: dict) -> None:
    today = datetime.now(timezone.utc).date()
    oldest = (today - timedelta(days=29)).isoformat()
    summary["daily_history"] = {day: counts for day, counts in sorted(summary["daily_history"].items())
                                if oldest <= day <= today.isoformat()}


# LLM: 只对已核连续序号累加四类计数与UTC日期映射；by_skill统一规范身份，原始明细仍保留调用者身份以供追溯。
# 函数用途: 将一条事实累加到生命周期、单技能和每日汇总。
def _add_event(summary: dict, row: dict) -> None:
    event = row["event"]
    _increment(summary["totals"], event)
    skill_id = _canonical_skill_id(row["kind"], row["skill_id"])
    if not skill_id:
        raise ValueError("invalid usage skill identity")
    skill = summary["by_skill"].setdefault(skill_id, _empty_counts())
    _increment(skill, event)
    skill.update(last_used_at=row["at"], last_source=row["source"], kind=row["kind"])
    daily = summary["daily_history"].setdefault(row["date"], dict.fromkeys(USAGE_EVENTS, 0))
    daily[event] += 1
    summary["last_sequence"] = row["sequence"]
    _prune_daily(summary)


# LLM: 原锁内只联合核额一次，不对每个文件另扫owner目录；正常明细先写而回滚汇总先恢复，耐久事件由水位补记。
# 函数用途: 保存有界账本及生命周期汇总，不写任何方法正文。
def _save_usage(store, admission, rows: list, summary: dict) -> None:
    texts = {"usage_ledger.jsonl": "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
             "usage_summary.json": json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n"}
    previous = {name: _read_usage_text(store, name) for name in texts}
    changes = []
    for name, text in texts.items():
        path = store.directory / name
        changes.append(OwnerQuotaChange(path, len(text.encode("utf-8"))))
    admission.check(changes)
    try:
        _write_usage_pair(store, texts)
    except OSError:
        _write_usage_pair(store, dict(reversed(previous.items())))
        raise


# LLM: 调用方持原锁与配额，None仅用于撤销本次创建的文件；所有写/删仍从本owner根逐段nofollow。
# 函数用途: 应用或恢复一对使用文件，不读取或复制任何Skill正文。
def _write_usage_pair(store, texts: dict) -> None:
    root = store.skills_root.parent
    for name, text in texts.items():
        parts = (store.directory / name).relative_to(root).parts
        if text is None:
            unlink_file_beneath(root, parts)
        else:
            write_text_atomic_beneath(root, parts, text)
