# LLM: P6只消费原权威冻结的C/W观察，不扫描/等待/取消/写drained；原W API保持，C明确独立坐标，同步生产collector及六类拒绝测试。
# 模块用途: 用六类完整在途清单和原P2整树证明判定准确角色能否排空。
from dataclasses import dataclass

from .multiprocess_records import MultiprocessRecordError
from .multiprocess_route import WorkerPartition
from .multiprocess_stop_scope import CoordinatorStopScope, require_stop_scope
from .multiprocess_stop_tree import TreeStopObservation, require_stopped_tree

# 六种来源都要完成独立观察；新增在途来源必须同步调用方、协议与逐类拒绝测试。
STOP_DRAIN_KINDS = ("foreground", "compression", "control", "subagent", "background", "executor_tree")


# LLM: 引用来自原 canonical 请求/runner/后台/执行者记录，不是新任务状态账本。
# 类用途: 描述一个分区内、某一类别的具体在途事项。
@dataclass(frozen=True)
class InFlightItem:
    kind: str
    partition: WorkerPartition | CoordinatorStopScope
    item_ref: str
    state: str


# LLM: observed_kinds 只可由可信调用方完成稳定枚举后给出；空清单不同于未读或读失败。
# 类用途: 冻结停止认领、完整观察类别和原在途清单。
@dataclass(frozen=True)
class DrainInventory:
    partition: WorkerPartition | CoordinatorStopScope
    claiming_stopped: bool
    observed_kinds: tuple[str, ...]
    items: tuple[InFlightItem, ...]
    tree_observation: TreeStopObservation | None


# LLM: 原因保留类别和引用，不靠自然语言推断哪项已结束。
# 类用途: 结构化说明某项为何阻止排空。
@dataclass(frozen=True)
class DrainBlocker:
    kind: str
    item_ref: str
    reason: str


# LLM: 本结果仅为消费已提供观察的合同判定，不替代调用方的来源/锁/时效验证。
# 类用途: 给出排空布尔值与所有已知阻塞项。
@dataclass(frozen=True)
class DrainAssessment:
    drained: bool
    blockers: tuple[DrainBlocker, ...]


# LLM: 只消费冻结观察，任何缺口都不能当作空闲；语义错误返回结构化阻塞而非空集合。
# 函数用途: 校验六类清单后判定是否可报告排空。
def assess_drain(inventory):
    try:
        _require_inventory(inventory)
    except MultiprocessRecordError as error:
        return DrainAssessment(False, (DrainBlocker("inventory", "", error.reason),))
    blockers = [DrainBlocker(kind, "", "unobserved") for kind in STOP_DRAIN_KINDS if kind not in inventory.observed_kinds]
    if inventory.claiming_stopped is not True:
        blockers.append(DrainBlocker("inventory", "", "claiming_not_stopped"))
    blockers.extend(DrainBlocker(i.kind, i.item_ref, i.state) for i in inventory.items if i.state != "settled")
    blockers.extend(_tree_blockers(inventory))
    return DrainAssessment(not blockers, tuple(blockers))


# LLM: 清单不可变/逐项可定位，C/W准确类型同源；不能丢坏项或把C映为W0，保原不完整/重复拒绝合同。
# 函数用途: 校验原角色、观察类别及完整清单结构。
def _require_inventory(inventory):
    if type(inventory) is not DrainInventory:
        raise MultiprocessRecordError("inventory_invalid", "inventory")
    require_stop_scope(inventory.partition)
    if type(inventory.observed_kinds) is not tuple or type(inventory.items) is not tuple:
        raise MultiprocessRecordError("inventory_not_frozen", "inventory")
    if any(type(k) is not str or k not in STOP_DRAIN_KINDS for k in inventory.observed_kinds):
        raise MultiprocessRecordError("inventory_kind_invalid", "observed_kinds")
    if len(set(inventory.observed_kinds)) != len(inventory.observed_kinds):
        raise MultiprocessRecordError("inventory_kind_duplicate", "observed_kinds")
    seen = set()
    for item in inventory.items:
        _require_item(inventory.partition, item)
        key = (item.kind, item.item_ref)
        if key in seen:
            raise MultiprocessRecordError("inventory_item_duplicate", "items")
        seen.add(key)


# LLM: foreign或跨C/W项是范围错误不能忽略，只有settled结清；原角色字段不是物理停止证明。
# 函数用途: 核对每项类别、引用和与清单相同的原角色坐标。
def _require_item(partition, item):
    if type(item) is not InFlightItem or type(item.kind) is not str or item.kind not in STOP_DRAIN_KINDS:
        raise MultiprocessRecordError("inventory_item_invalid", "items")
    if type(item.item_ref) is not str or not item.item_ref.strip() or type(item.state) is not str:
        raise MultiprocessRecordError("inventory_item_invalid", "items")
    require_stop_scope(item.partition)
    if item.partition != partition:
        raise MultiprocessRecordError("inventory_partition_mismatch", "items")


# LLM: 非空执行树必须经 P2；权威完整观察确认确实无执行者时允许空，不用 None 代表读取失败。
# 函数用途: 对齐非豁免执行树清单与原停止证明，保留所有未知原因。
def _tree_blockers(inventory):
    identifiers = sorted(i.item_ref for i in inventory.items if i.kind == "executor_tree")
    if not identifiers and inventory.tree_observation is None:
        return ()
    try:
        proof = require_stopped_tree(inventory.partition, inventory.tree_observation)
        if identifiers != sorted(proof["covered_scope"]["executor_reservation_ids"]):
            raise MultiprocessRecordError("executor_inventory_mismatch", "items")
    except MultiprocessRecordError as error:
        return (DrainBlocker("executor_tree", "", error.reason),)
    return ()
