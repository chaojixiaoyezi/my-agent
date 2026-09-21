# LLM: runtime.db 的结构化恢复状态仍是唯一权威；本模块仅去重日志，每次查询都调用当前 checker。
# 模块用途: 为后台各来源查询人工恢复阻断，不领取租约、不消费来源、不缓存执行资格。
from __future__ import annotations

import json
import logging
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
    def block_for_task(self, task_id: str) -> dict[str, str] | None:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            return None
        checker = self._checker()
        if not callable(checker):
            return None
        try:
            block = checker(normalized_task_id)
        except Exception as exc:  # noqa: BLE001 权威不可读时也不能放行模型/副作用
            block = {
                "schema_version": "main-agent-recovery-block.v1",
                "reason": "authority_state_unreadable",
                "task_id": normalized_task_id,
                "error_type": exc.__class__.__name__,
            }
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
