# LLM: Sibling roster packs make same-batch peers addressable without parsing business prose.
# 模块用途: create_subagents 批量创建后，把同批兄弟 run_id/name/role 写入每个 child 的 context_packs。

from __future__ import annotations


# LLM: attach_sibling_roster stores refs-only peer identity facts, not task-specific instructions.
# 函数用途: 同一批 child 创建完成后互相知道对方 run_id/名字/角色，coordinator 可按结构化 id 协调 peers。
def attach_sibling_roster(manager: object, tasks: list[object], *, save: bool = True) -> None:
    rows = _sibling_rows(tasks)
    if len(rows) <= 1:
        return
    pack = {
        "kind": "sibling_roster",
        "summary": f"同一批创建的 {len(rows)} 个子代理，可用于协作、点名查询、状态核对和结果汇总。",
        "roster": [_roster_line(row) for row in rows],
        "siblings": rows,
    }
    for task in tasks:
        packs = _context_packs(task)
        if _has_sibling_roster(packs, rows):
            continue
        packs.append(pack)
        task.context_packs = packs
        if save:
            _save_task(manager, task)


def _sibling_rows(tasks: list[object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for task in tasks:
        run_id = str(getattr(task, "id", "") or "").strip()
        if not run_id:
            continue
        rows.append({
            "run_id": run_id,
            "agent_name": str(getattr(task, "agent_name", "") or "").strip(),
            "role": str(getattr(task, "role", "") or "").strip(),
            "goal": _short_text(getattr(task, "goal", ""), limit=180),
        })
    return rows


def _roster_line(row: dict[str, object]) -> str:
    parts = [
        f"run_id={row.get('run_id')}",
        f"name={row.get('agent_name') or 'unnamed'}",
        f"role={row.get('role') or 'worker'}",
    ]
    goal = str(row.get("goal") or "")
    if goal:
        parts.append(f"goal={goal}")
    return " | ".join(parts)


def _context_packs(task: object) -> list[dict[str, object]]:
    value = getattr(task, "context_packs", []) or []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _has_sibling_roster(packs: list[dict[str, object]], rows: list[dict[str, object]]) -> bool:
    expected_ids = {str(row.get("run_id") or "") for row in rows}
    for pack in packs:
        if pack.get("kind") != "sibling_roster":
            continue
        existing_ids = {
            str(item.get("run_id") or "")
            for item in pack.get("siblings", [])
            if isinstance(item, dict)
        }
        if expected_ids and expected_ids == existing_ids:
            return True
    return False


def _short_text(value: object, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _save_task(manager: object, task: object) -> None:
    save = getattr(manager, "save", None)
    if callable(save):
        save(task)


__all__ = ["attach_sibling_roster"]
