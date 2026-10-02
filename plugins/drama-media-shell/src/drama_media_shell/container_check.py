# LLM: 固定迁移 drama-skills VID-15 容器核对语义；SDK 入口先安全读取 JSONL，本模块只比较结构和数值。
# 模块用途: 在剧集范围核对容器成员、镜头归属与时长总数，不读写文件也不生成媒体。
"""Reconcile an episode's delivery containers against its shot set (`VID-15`).

Every container can be correct on its own while the episode is wrong: a shot
packed into two containers bills its seconds twice, and a shot packed into none
disappears although its dialogue, bindings, and keyframe prompts are already
done. Neither error is visible from inside a single container, so the check is
a set comparison at episode scope.

The script reads accepted creator files and writes nothing.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "1.0.0"
# A .jsonl file opens with a header record declaring the upstream snapshots its
# references name. The header is a declaration, not one of the file's records.
SOURCES_RECORD_TYPE = "sources"


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 类用途: 表示容器或镜头输入无法执行检查。
class CheckError(ValueError):
    """The inputs cannot be checked at all, as opposed to failing a check."""


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 构造稳定的容器检查发现。
def _finding(code: str, message: str, **detail: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **detail}


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 把有限数值转换为秒数。
def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# The three conclusions `delivery-container.jsonl.md` requires a container to
# state about why its members belong together.
MEMBERSHIP_BASIS_KEYS = (
    "source_order_contiguous",
    "binding_chain_equal",
    "scene_boundary_not_crossed",
)


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 取得容器成员指向的镜头编号。
def _member_shot_id(member: Any) -> str | None:
    if not isinstance(member, dict):
        return None
    ref = member.get("shot_ref")
    if isinstance(ref, dict) and isinstance(ref.get("record_id"), str):
        return ref["record_id"]
    return None


# LLM: 单成员检查独立出来以限制嵌套；它仍按上游顺序登记归属、核时长并返回可累计秒数。
# 函数用途: 核对一个容器成员并返回其权威镜头时长。
def _reconcile_member(member: Any, container_id: str, state: dict[str, Any]) -> float:
    findings = state["findings"]
    shot_id = _member_shot_id(member)
    if shot_id is None:
        findings.append(_finding("VID15_MEMBER_HAS_NO_SHOT_REF", "a member does not name the shot it packs",
                                 container_id=container_id))
        return 0.0
    if shot_id not in state["episode_shots"]:
        findings.append(_finding("VID15_MEMBER_IS_NOT_AN_EPISODE_SHOT",
                                 "a container packs a shot that is not in this episode",
                                 container_id=container_id, shot_id=shot_id))
        return 0.0
    previous = state["owner_of"].get(shot_id)
    if previous is not None:
        findings.append(_finding("VID15_SHOT_PACKED_TWICE", "a shot belongs to more than one container",
                                 shot_id=shot_id, container_ids=sorted({previous, container_id})))
        return 0.0
    state["owner_of"][shot_id] = container_id
    state["packed"].add(shot_id)
    seconds = state["durations"].get(shot_id)
    if seconds is None:
        findings.append(_finding("VID15_MEMBER_SHOT_HAS_NO_DURATION",
                                 "a packed shot carries no numeric duration",
                                 container_id=container_id, shot_id=shot_id))
        return 0.0
    claimed = _seconds(member.get("accepted_duration")) if isinstance(member, dict) else None
    if claimed is None:
        findings.append(_finding("VID15_MEMBER_HAS_NO_ACCEPTED_DURATION",
                                 "a member must project the duration it packs",
                                 container_id=container_id, shot_id=shot_id))
    elif abs(claimed - seconds) > 1e-6:
        findings.append(_finding("VID15_MEMBER_DURATION_IS_STALE",
                                 "a member's accepted_duration does not match its shot",
                                 container_id=container_id, shot_id=shot_id,
                                 claimed=claimed, accepted=seconds))
    return seconds


# LLM: membership_basis 单独校验，避免容器循环里形成三层分支；缺对象和缺字段仍保留不同上游错误码。
# 函数用途: 核对容器三项成员归组依据。
def _validate_membership_basis(container: dict[str, Any], container_id: str,
                               findings: list[dict[str, Any]]) -> None:
    basis = container.get("membership_basis")
    if not isinstance(basis, dict):
        findings.append(_finding("VID15_MEMBERSHIP_BASIS_MISSING",
                                 "a container must record why its members belong together",
                                 container_id=container_id))
        return
    blank = sorted(key for key in MEMBERSHIP_BASIS_KEYS if not str(basis.get(key) or "").strip())
    if blank:
        findings.append(_finding("VID15_MEMBERSHIP_BASIS_INCOMPLETE",
                                 "every membership_basis conclusion must be stated",
                                 container_id=container_id, missing=blank))


# LLM: 单容器检查只循环其成员，成员细节由独立函数处理；保持上游顺序、累计和错误码不变。
# 函数用途: 核对一个容器并返回其成员总时长。
def _reconcile_container(container: dict[str, Any], state: dict[str, Any]) -> float:
    findings = state["findings"]
    container_id = container.get("container_id")
    if not isinstance(container_id, str):
        findings.append(_finding("VID15_CONTAINER_HAS_NO_ID", "a container record has no id"))
        return 0.0
    members = container.get("members")
    if not isinstance(members, list) or not members:
        findings.append(_finding("VID15_CONTAINER_HAS_NO_MEMBERS", "a container carries no members",
                                 container_id=container_id))
        return 0.0
    orders = [member.get("order") for member in members if isinstance(member, dict)]
    valid_orders = all(isinstance(value, int) and not isinstance(value, bool) for value in orders)
    if not valid_orders or orders != list(range(1, len(members) + 1)):
        findings.append(_finding("VID15_MEMBER_ORDER_IS_NOT_A_SEQUENCE",
                                 "members must carry order 1..n, ascending and without gaps",
                                 container_id=container_id, orders=orders))
    _validate_membership_basis(container, container_id, findings)
    member_total = sum(_reconcile_member(member, container_id, state) for member in members)
    stated = _seconds(container.get("container_duration"))
    if stated is None:
        findings.append(_finding("VID15_CONTAINER_DURATION_MISSING",
                                 "container_duration must be present and a number of seconds",
                                 container_id=container_id))
    elif abs(stated - member_total) > 1e-6:
        findings.append(_finding("VID15_CONTAINER_DURATION_IS_NOT_THE_SUM",
                                 "container_duration does not equal its members' durations",
                                 container_id=container_id, stated=stated,
                                 computed=round(member_total, 6)))
    return member_total


# LLM: 该入口迁移自固定上游校验器；状态集中传给单容器/成员函数以保持语义并限制嵌套深度。
# 函数用途: 核对容器成员、时长与剧集镜头集合。
def reconcile(
    containers: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    durations = {shot["shot_id"]: _seconds(shot.get("duration_seconds"))
                 for shot in shots if isinstance(shot.get("shot_id"), str)}
    episode_shots = set(durations)
    state: dict[str, Any] = {"findings": findings, "durations": durations,
                             "episode_shots": episode_shots, "owner_of": {}, "packed": set()}
    container_total = sum(_reconcile_container(container, state) for container in containers)
    packed = state["packed"]

    loose = sorted(episode_shots - packed)
    # A shot whose duration is still open is a legal state upstream, so it is
    # held out of the arithmetic instead of being reported as an error. Only a
    # shot packed into a container must already have one, because the
    # container's own duration claim depends on it.
    unmeasured = sorted(
        shot_id for shot_id in episode_shots if durations.get(shot_id) is None
    )
    loose_total = sum(
        durations[shot_id] or 0.0 for shot_id in loose if durations.get(shot_id) is not None
    )

    episode_total = sum(value for value in durations.values() if value is not None)
    if abs((container_total + loose_total) - episode_total) > 1e-6:
        findings.append(
            _finding(
                "VID15_EPISODE_TOTAL_DOES_NOT_RECONCILE",
                "containers plus loose shots do not add up to the episode total",
                packed_seconds=round(container_total, 6),
                loose_seconds=round(loose_total, 6),
                episode_seconds=round(episode_total, 6),
            )
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "containers": len(containers),
        "episode_shots": len(episode_shots),
        "packed_shots": len(packed),
        # Loose shots are legal: containers need not cover everything. They are
        # reported so the count is a decision rather than an oversight.
        "loose_shots": loose,
        # Reported, not a finding: these shots are excluded from every total
        # above, so a caller can tell an incomplete episode from a wrong one.
        "unmeasured_shots": unmeasured,
        "packed_seconds": round(container_total, 6),
        "loose_seconds": round(loose_total, 6),
        "episode_seconds": round(episode_total, 6),
        "findings": findings,
        "status": "pass" if not findings else "fail",
    }
