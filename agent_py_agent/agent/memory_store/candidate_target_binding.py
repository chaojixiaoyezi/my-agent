# LLM: 由候选账本锁内重建的纯索引，不是另一份版本权威；同规范证据对同目标的版本约束跨动作和入口共享。
# 模块用途: 防止旧消息或工具证据借改写候选、切换动作或入口重新取得正式目标版本。
from __future__ import annotations

from collections.abc import Iterable, Mapping

_MUTATIONS = frozenset({"replace", "merge", "remove"})


# LLM: 只消费宿主已解析的结构化引用；保留全部等价身份，不能换tool/artifact表示刷新版本。
# 函数用途: 提取规范消息或工具事件的所有已证明身份键。
def _reference_identities(ref: object) -> set[tuple[str, ...]]:
    if not isinstance(ref, Mapping):
        return set()
    values = {key: str(ref.get(key) or "").strip() for key in
              ("message_id", "thread_id", "operation_id", "owner_id", "run_id", "event_id", "artifact_ref")}
    keys: set[tuple[str, ...]] = set()
    if values["message_id"] and values["thread_id"]:
        keys.add(("message", values["thread_id"], values["message_id"]))
    if values["operation_id"] and values["run_id"]:
        keys.add(("operation", values["owner_id"], values["run_id"], values["operation_id"]))
    if values["event_id"]:
        keys.add(("event", values["run_id"], values["event_id"]))
    if values["artifact_ref"]:
        keys.add(("artifact", values["artifact_ref"]))
    return keys


# LLM: 仅变更绑定正式目标；逐证据索引保留合并前各条观察，不让引用并集、动作变化或新候选ID隐藏旧绑定。
# 函数用途: 生成本次变更须遵守的来源—目标版本键，新增事实不受此约束。
def _binding_keys(source: object) -> set[tuple[str, ...]]:
    if source.proposed_action not in _MUTATIONS or not source.target_entry_id:
        return set()
    references = (*source.source_message_refs, *source.source_tool_refs, *source.source_artifact_refs)
    return {(source.target_entry_id, *identity) for ref in references
            for identity in _reference_identities(ref)}


# LLM: 构造和更新仅持有调用方当前账本快照；任何已保存版本（含旧缺字段None）都不能被重放刷新。
# 类用途: 在候选原子批提交前核对并累积证据版本约束。
class TargetEvidenceVersionBindings:
    # LLM: 调用方在候选锁内传完整已验证账本，不能只传pending候选或当前candidate_id命中项。
    # 函数用途: 收集所有历史及终态候选的观察绑定。
    def __init__(self, candidates: Iterable[object]):
        self.versions: dict[tuple[str, ...], set[int | None]] = {}
        for candidate in candidates:
            self.add(candidate)

    # LLM: 每个被引用的来源都必须与其既有绑定一致，混入新证据不能替旧证据刷新版本。
    # 函数用途: 判断本次观察是否违反任一已保存的来源—目标版本约束。
    def conflicts(self, observation: object) -> bool:
        return any(key in self.versions and self.versions[key] != {observation.target_entry_version}
                   for key in _binding_keys(observation))

    # LLM: 只在通过调用方完整校验后累积；此索引不持久化，实际原子提交仍由CandidateService负责。
    # 函数用途: 使同一批中的后续观察也遵守之前已接受的绑定。
    def add(self, observation: object) -> None:
        for row in binding_history(observation):
            self.versions.setdefault(tuple(row["key"]), set()).add(row["version"])


# LLM: 历史只保存规范身份键和当时版本；严格拒绝损坏数据，不得静默丢弃约束。
# 函数用途: 验证同一候选行内的历史版本绑定。
def validate_binding_history(history: object) -> None:
    if not isinstance(history, list):
        raise ValueError("target_evidence_bindings must be a list")
    for row in history:
        if not isinstance(row, dict) or set(row) != {"key", "version"}:
            raise ValueError("invalid target evidence binding")
        key, version = row["key"], row["version"]
        if not isinstance(key, list) or len(key) < 3 or not all(isinstance(x, str) for x in key):
            raise ValueError("invalid target evidence binding key")
        if not key[0] or key[1] not in {"message", "operation", "event", "artifact"}:
            raise ValueError("invalid target evidence binding identity")
        if version is not None and (type(version) is not int or version <= 0):
            raise ValueError("invalid target evidence binding version")


# LLM: 在当前态被审核改写或retention前累积绑定；正文、摘要和时间不参与，不建第二权威文件。
# 函数用途: 返回不丢失已有约束的规范历史快照。
def binding_history(source: object) -> list[dict[str, object]]:
    history = getattr(source, "target_evidence_bindings", [])
    validate_binding_history(history)
    pairs = {(tuple(row["key"]), row["version"]) for row in history}
    pairs.update((key, source.target_entry_version) for key in _binding_keys(source))
    return [{"key": list(key), "version": version} for key, version in
            sorted(pairs, key=lambda pair: (pair[0], -1 if pair[1] is None else pair[1]))]
