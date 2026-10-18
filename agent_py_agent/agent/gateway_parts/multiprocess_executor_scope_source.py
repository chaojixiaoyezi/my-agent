# LLM: 冻结定位不抹原C门；可变facade不是权限来源，实际方法只能来自不可变原C provider。
# 模块用途: 把scope元数据和原活来源分开，在原C实际短门内复核facade未换来源。
from dataclasses import dataclass

from ..common.executor_registration import ExecutorRegistrationError
from .multiprocess_executor_ledger import ExecutorLedger
from .multiprocess_executor_original_source import OriginalCExecutorSource


# LLM: canonical只纯校验定位；所有实际提交/物理动作交同一不可变原provider，不调用facade的动态方法。
# 类用途: 保存原provider及可变调用壳的关系，既拒提前漂移，也在原实际门内拒检查后漂移。
@dataclass(frozen=True)
class _BoundExecutorSource:
    source: object
    canonical: ExecutorLedger
    original: OriginalCExecutorSource

    # LLM: 冻结定位不授信。
    # 函数用途: 返回原规范根。
    @property
    def root(self):
        return self.canonical.root

    # LLM: 分区固定，不从热调用壳恢复权限。
    # 函数用途: 返回原分区。
    @property
    def partition(self):
        return self.canonical.partition

    # LLM: 壳检查只核同一来源，closing后也可读；写入/实际动作仍由原provider在短门内核活C/W。
    # 函数用途: 拒root/partition、issuer、owner及provider引用漂移，不将定位校验当提交许可。
    def _require_source(self):
        self.original._require_source_identity()
        direct = self.source is self.original
        if (self.root != self.original.root or self.partition != self.original.partition
                or self.source.root != self.root or self.source.partition != self.partition
                or self.source.issuer is not self.original.issuer or self.source.owner != self.original.owner
                or (not direct and self.source.executor_source is not self.original)):
            raise ExecutorRegistrationError({'error_code': 'EXECUTOR_SCOPE_SOURCE_CHANGED'})

    # LLM: 纯校验不签发许可。
    # 函数用途: 检查原绑定字段。
    def _bound_record(self, record):
        self._require_source()
        return self.canonical._bound_record(record)

    # LLM: 实际预留只调用原provider，并把最终壳检查放进其原门。
    # 函数用途: 提交原唯一P1预留。
    def reserve(self, record):
        self._require_source()
        return self.original.reserve(record, source_guard=self._require_source)

    # LLM: 原门中重新核来源后沿原issuer推进，不动态读取壳的issuer。
    # 函数用途: 提交原P1登记。
    def advance(self, record, state, registration=None):
        self._require_source()
        return self.original.advance(record, state, registration, source_guard=self._require_source)

    # LLM: 原provider只读其冻结规范根。
    # 函数用途: 读原唯一账本。
    def read(self, identifier):
        self._require_source()
        return self.original.read(identifier)

    # LLM: 失败也不能使用另一C来源或普通ledger代清理。
    # 函数用途: 沿原门尝试原废弃。
    def abandon(self, record):
        self._require_source()
        return self.original.abandon(record, source_guard=self._require_source)

    # LLM: 最终壳复核和callback均在同一原C短门，不以预检或另一C门授权。
    # 函数用途: 沿原门做实际spawn/release。
    def perform_short_effect(self, record, phase, callback):
        self._require_source()
        return self.original.perform_short_effect(record, phase, callback, source_guard=self._require_source)


# LLM: 普通无门ledger保持旧纯定位冻结；带门来源须持真实不可变provider，拒未知方法壳而非裸写兜底。
# 函数用途: 建立本地原C桥，尚不是W/native/FD生产接线。
def freeze_executor_source(ledger):
    canonical = ExecutorLedger(ledger.root, ledger.partition)
    if type(ledger) is ExecutorLedger:
        return canonical
    if type(ledger) is OriginalCExecutorSource:
        original = ledger
    else:
        original = getattr(ledger, 'executor_source', None)
    if type(original) is not OriginalCExecutorSource:
        raise ExecutorRegistrationError({'error_code': 'EXECUTOR_SCOPE_SOURCE_INVALID'})
    return _BoundExecutorSource(ledger, canonical, original)
