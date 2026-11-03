# LLM: runtime.db 的结构化恢复状态仍是唯一权威；本模块仅去重日志，每次查询都调用当前 checker。
# 模块用途: 为后台各来源查询人工恢复阻断，不领取租约、不消费来源、不缓存执行资格。
from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable

_LOGGER = logging.getLogger("agent.conversation.background_claim_heartbeat")


# LLM: checker 按调用时解析；日志指纹沿原 scheduler 实例寿命共享，不新增持久状态或恢复动作。
# 类用途: 将权威恢复检查和重复日志控制放在同一小组件，供领取前后及就绪扫描共用。
class BackgroundRecoveryGuard:
    # LLM: 装配只保存精确查询能力，不读取仓库或预取阻断状态；调用方负责绑定当前权威入口。
    # 函数用途: 创建本调度器的恢复守卫，日志指纹初始为空。
    def __init__(self, checker: Callable[[], Callable[[str], dict[str, str] | None] | None]):
        self._checker = checker
        self._logged_blocks: dict[str, str] = {}

    # LLM: 不可读按原结构化原因阻断；没有 checker 沿原 None，解析 checker 的异常仍在查询 try 之外。
    # 函数用途: 重读指定任务的恢复条件，仅在阻断变化或解除时记录日志，原来源保持待处理。
    def block_for_task(self, task_id: str, *, expected_revision=None) -> dict | None:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            return None
        checker = self._checker()
        if not callable(checker):
            return None
        try:
            block = (checker(normalized_task_id) if expected_revision is None else
                     checker(normalized_task_id, expected_revision=expected_revision))
        except Exception as exc:  # noqa: BLE001 权威不可读时也不能放行模型/副作用
            block = {
                "schema_version": "main-agent-recovery-block.v1",
                "reason": "authority_state_unreadable",
                "task_id": normalized_task_id,
                "error_type": exc.__class__.__name__,
            }
            # 旧无版本checker的错误映射原样适配；真实仓储及显式版本调用返回完整合同。
            authority = getattr(checker, "__self__", None)
            if expected_revision is not None or callable(getattr(authority, "recovery_eligibility_for_task", None)):
                block.update(eligible=False, reason_code=block["reason"], source_revision=None,
                             next_check_at=None, handoff_ref=None)
        if block is None:
            if normalized_task_id in self._logged_blocks:
                self._logged_blocks.pop(normalized_task_id, None)
                _LOGGER.info("BACKGROUND_AUTHORITY_RECOVERY_RESUMED task=%s", normalized_task_id)
            return None
        fingerprint = json.dumps(block, ensure_ascii=False, sort_keys=True)
        if self._logged_blocks.get(normalized_task_id) != fingerprint:
            self._logged_blocks[normalized_task_id] = fingerprint
            _LOGGER.warning("BACKGROUND_AUTHORITY_RECOVERY_BLOCK %s", fingerprint)
        return block


# LLM: 仅保存当前消费者重试节奏，不消费due/wake或授予增长权限；重启由原持久责任重建。
# 类用途: 把存储配额暂不可用收敛为带检查时间的等待。
class StorageWaitBackoff:
    _SHARED_LIMIT = 256
    _shared: OrderedDict[str, StorageWaitBackoff] = OrderedDict()
    _shared_lock = threading.Lock()

    # LLM: 只共享同一owner的进程内退避节奏，不缓存配额授权；容量淘汰不改持久任务。
    # 函数用途: 让Agent重建后仍沿用该owner尚未到期的等待窗口。
    @classmethod
    def shared(cls, key: str) -> StorageWaitBackoff:
        with cls._shared_lock:
            wait = cls._shared.get(key)
            if wait is None:
                wait = cls._shared[key] = cls()
            cls._shared.move_to_end(key)
            while len(cls._shared) > cls._SHARED_LIMIT:
                cls._shared.popitem(last=False)
            return wait

    # LLM: 无I/O，不预取配额；常量仅限制故障探测节奏。
    # 函数用途: 建立等待投影，累计次数在恢复后仍可查询。
    def __init__(self):
        self.wait_count = 0
        self.consecutive = 0
        self.next_check_at = 0.0
        self.started_at = None
        self.error_type = ""

    # LLM: 使用原tick时钟，不改变持久调度时间。
    # 函数用途: 到下次检查时间才允许再探测存储。
    def ready(self, current):
        return current >= self.next_check_at

    # LLM: 只按typed异常记录类型，不解析正文；返回True仅表示首次进入等待。
    # 函数用途: 以1秒起翻倍、30秒封顶，记录累计等待次数。
    def failed(self, current, error):
        first = self.consecutive == 0
        self.wait_count += 1
        self.consecutive += 1
        if first:
            self.started_at = current
        self.error_type = type(error).__name__
        self.next_check_at = current + min(30.0, 2.0 ** min(self.consecutive - 1, 5))
        return first

    # LLM: 真实存储调用成功才复位，不能把跳过当作恢复。
    # 函数用途: 退出本次等待并返回是否发生恢复。
    def succeeded(self):
        resumed = self.consecutive > 0
        self.consecutive = 0
        self.next_check_at = 0.0
        return resumed

    # LLM: 纯观测投影，不是第二份调度账。
    # 函数用途: 提供首次等待、累计次数与下次检查事实。
    def snapshot(self):
        return {"status": "storage_wait" if self.consecutive else "ready", "wait_count": self.wait_count,
                "consecutive_waits": self.consecutive, "started_at": self.started_at,
                "next_check_at": self.next_check_at if self.consecutive else None,
                "reason_code": "owner_quota_unavailable", "error_type": self.error_type}
