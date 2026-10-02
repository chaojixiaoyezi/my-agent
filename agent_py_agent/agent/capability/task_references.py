# LLM: 本模块只规范和消费原任务的 Skill／能力包引用；安装状态归原安装表，任务 pins 归原任务链接，不建立第二本账。
# 模块用途: 让派工、定时执行与主会话长任务固定同一能力版本，包重新启用或替换后不会静默换代。
from __future__ import annotations

from collections.abc import Callable, Mapping

from ..runtime_context import current_subagent_run_id
from .skill_snapshot import SkillSnapshot, SkillSnapshotError


# LLM: 引用形状错误的结构化码只放在 error_code 上；仍是 ValueError 子类，既有 except ValueError 的调用方不变。
#   包装成 SkillSnapshotError 时读 error_code，不解析消息。改动时联查 test_skill_snapshot_error_codes。
# 类用途: 表示任务记录里的一条 Skill／能力包引用形状不合法，带结构化错误码。
class SkillReferenceError(ValueError):
    # LLM: 只接受大写下划线的码常量；消息就是码本身，调用方和测试都读 error_code，不解析 str(exc)。
    # 函数用途: 保存结构化错误码，消息与码相同，保持原 str(exc) 输出。
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


# LLM: 公开 Skill 保持原摘要引用；能力包必须显式类型、准确包 ID 和激活摘要，未知类型不能被降级成普通 Skill。
# 函数用途: 从已结构化的任务记录规范一条引用，拒绝不完整包身份，不读取正文或执行任何副作用。
def normalize_skill_reference(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise SkillReferenceError("SKILL_REFERENCE_INVALID")
    stable_id = value.get("stable_id")
    digest = value.get("content_sha256")
    if not isinstance(stable_id, str) or not stable_id.strip() or not _is_sha256(digest):
        raise SkillReferenceError("SKILL_REFERENCE_INVALID")
    stable_id = stable_id.strip()
    kind = value.get("kind", "skill")
    if kind == "skill" and not stable_id.startswith("capability:"):
        return {"stable_id": stable_id, "content_sha256": digest}
    package_id = value.get("package_id")
    activation_id = value.get("activation_id")
    if (kind != "capability_package" or not isinstance(package_id, str) or not package_id
            or stable_id != f"capability:{package_id}" or not _is_sha256(activation_id)):
        raise SkillReferenceError("SKILL_PACKAGE_REFERENCE_INVALID")
    return {
        "kind": "capability_package", "stable_id": stable_id, "name": package_id,
        "source": "capability_package", "content_sha256": digest,
        "package_id": package_id, "activation_id": activation_id,
    }


# LLM: 输入只有机器身份摘要；大小写与长度必须一致，不能将布尔或对象的文字投影当作哈希。
# 函数用途: 检查固定 SHA-256 字段的形状。
def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


# LLM: attributes 与 grants 是原 task 的权威事实；包冲突必须失败，不能按列表先后悄悄选择新安装。
# 函数用途: 收集子代理最初继承和后续授予的版本引用，返回副本供快照裁剪。
def task_skill_references(task: object) -> list[dict[str, object]]:
    attrs = getattr(task, "attributes", None)
    values = [attrs.get("skill_snapshot_refs")] if isinstance(attrs, dict) else []
    values.extend(getattr(grant, "capability_cards", None) for grant in getattr(task, "capability_grants", ()) or ())
    result: list[dict[str, object]] = []
    packages: dict[str, dict[str, str]] = {}
    for rows in values:
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            is_package = row.get("kind") == "capability_package" or str(row.get("stable_id", "")).startswith("capability:")
            if not is_package and (not row.get("stable_id") or not row.get("content_sha256")):
                continue
            if is_package:
                try:
                    ref = normalize_skill_reference(row)
                except SkillReferenceError as exc:
                    raise SkillSnapshotError(exc.error_code) from exc
                old = packages.get(ref["stable_id"])
                if old is not None and old != ref:
                    raise SkillSnapshotError("SKILL_PACKAGE_REFERENCE_CONFLICT")
                packages[ref["stable_id"]] = ref
            result.append(dict(row))
    return result


# LLM: 只取宿主当前回合绑定的 conversation IDs；缺省是临时无持久任务，不得把 request ID 猜成任务或跨线程读取。
# 函数用途: 返回主会话准确任务与线程编号，没有任务绑定时返回空。
def _conversation_binding(attrs: object) -> tuple[str, str]:
    if not isinstance(attrs, dict):
        return "", ""
    task_id = str(attrs.get("conversation_task_id") or "").strip()
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if task_id and not thread_id:
        raise SkillSnapshotError("SKILL_TASK_THREAD_REQUIRED")
    return task_id, thread_id


# LLM: 主任务 pins 不变；只有合法包引用的缺失/换代投影成不可用，坏绑定、坏引用及未知异常仍拒绝整轮。
# 函数用途: 新轮、后台或 Compact 后隔离失效包，保留其它能力及模型正常回应、收口任务的机会。
def scope_main_task_references(agent: object, snapshot: SkillSnapshot, attrs: object) -> SkillSnapshot:
    task_id, thread_id = _conversation_binding(attrs)
    if not task_id:
        return snapshot
    store = getattr(agent, "conversation_store", None)
    if store is None:
        raise SkillSnapshotError("SKILL_TASK_STORE_UNAVAILABLE")
    link, error = store.tasks.load_report(task_id)
    if error or link is None or link.thread_id != thread_id:
        raise SkillSnapshotError("SKILL_TASK_BINDING_INVALID")
    refs: dict[str, dict[str, str]] = {}
    unavailable: dict[str, str] = {}
    for value in link.skill_snapshot_refs:
        try:
            ref = normalize_skill_reference(value)
        except SkillReferenceError as exc:
            raise SkillSnapshotError(exc.error_code) from exc
        stable_id = ref["stable_id"]
        if ref.get("kind") != "capability_package":
            snapshot.restricted([stable_id], expected_refs=[ref])
            continue
        if stable_id in refs and refs[stable_id] != ref:
            raise SkillSnapshotError("SKILL_PACKAGE_REFERENCE_CONFLICT")
        refs[stable_id] = ref
        package = snapshot.resolve_package(ref["package_id"])
        if package is None:
            unavailable[stable_id] = "CAPABILITY_PACKAGE_PIN_UNAVAILABLE"
        elif package.to_ref() != ref:
            unavailable[stable_id] = "CAPABILITY_PACKAGE_PIN_STALE"
    return snapshot.without_unavailable_packages(unavailable)


# LLM: 包正文交付前写原任务 pins；可选执行权检查在原存储锁内复核，取消异常不能降级成普通读取失败。
# 函数用途: 在内容通过完整性检查后固定准确版本；孩子只验证继承引用，不扩大授权或替主任务记账。
def pin_package_reference(
    agent: object, value: object, *, execution_authority_check: Callable[[], None] | None = None,
) -> None:
    from ..common.cancellation import ToolCancelled

    if execution_authority_check is not None:
        execution_authority_check()
    try:
        reference = normalize_skill_reference(value)
    except SkillReferenceError as exc:
        raise SkillSnapshotError(exc.error_code) from exc
    if reference.get("kind") != "capability_package":
        raise SkillSnapshotError("SKILL_PACKAGE_REFERENCE_REQUIRED")
    run_id = current_subagent_run_id(agent)
    if run_id:
        task = agent.subagents.load(run_id)
        refs = [normalize_skill_reference(row) for row in task_skill_references(task)
                if row.get("stable_id") == reference["stable_id"]]
        if reference not in refs or reference["stable_id"] not in task.allowed_skills:
            raise SkillSnapshotError("SKILL_PACKAGE_REFERENCE_REQUIRED")
        return
    current = getattr(agent, "_current_run_params", None)
    task_id, thread_id = _conversation_binding(getattr(current, "task_attributes", None))
    if not task_id:
        return
    store = getattr(agent, "conversation_store", None)
    if store is None:
        raise SkillSnapshotError("SKILL_TASK_STORE_UNAVAILABLE")
    try:
        options = {"execution_authority_check": execution_authority_check} if execution_authority_check is not None else {}
        store.tasks.pin_skill_reference(task_id=task_id, thread_id=thread_id, reference=reference, **options)
    except (InterruptedError, ToolCancelled):
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise SkillSnapshotError("SKILL_TASK_REFERENCE_UNAVAILABLE") from exc


# LLM: 只读原任务 pins（子代理读本 run 的 task 引用，主会话读 TaskLink.skill_snapshot_refs），只返回规范后的能力包引用。
#   供宿主核验（能力包 v2 块 3）使用：没有任务绑定、绑定不对或读不了时返回空列表，不抛；读不到就不核验，不影响本轮其它行为。
# 函数用途: 列出当前回合钉住的能力包引用。
def pinned_package_references(agent: object, attrs: object) -> list[dict[str, str]]:
    try:
        refs = [normalize_skill_reference(row) for row in _raw_pins(agent, attrs)]
    except (SkillSnapshotError, SkillReferenceError, OSError, ValueError, RuntimeError):
        return []
    return [ref for ref in refs if ref.get("kind") == "capability_package"]


# 函数用途: 读当前回合的原始 pins：子代理读本 run 的 task，主会话读当前任务链接；没有绑定时返回空。
def _raw_pins(agent: object, attrs: object) -> list:
    run_id = current_subagent_run_id(agent)
    if run_id:
        subagents = getattr(agent, "subagents", None)
        return task_skill_references(subagents.load(run_id)) if subagents is not None else []
    task_id, thread_id = _conversation_binding(attrs)
    store = getattr(agent, "conversation_store", None)
    if not task_id or store is None:
        return []
    link, error = store.tasks.load_report(task_id)
    if error or link is None or link.thread_id != thread_id:
        return []
    return list(link.skill_snapshot_refs)
