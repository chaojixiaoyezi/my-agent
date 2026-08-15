"""审计日志测试 - audit.py 审计日志、操作记录。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestAuditEntry:
    """测试 AuditEntry 数据类。"""

    def test_audit_entry_to_dict(self, tmp_path: Path):
        """转换为字典。"""
        from agent_py_agent.agent.audit.logger import AuditEntry

        entry = AuditEntry(
            entry_id="audit_123456_abc",
            timestamp=1234567890.0,
            action="CREATE_TASK",
            user_id="user1",
            channel="chat",
            target_type="task",
            target_id="task_001",
            status="success",
            details={"goal": "测试任务"},
        )

        data = entry.to_dict()

        assert data["entry_id"] == "audit_123456_abc"
        assert data["action"] == "CREATE_TASK"
        assert data["user_id"] == "user1"

    def test_audit_entry_from_dict(self, tmp_path: Path):
        """从字典创建。"""
        from agent_py_agent.agent.audit.logger import AuditEntry

        data = {
            "entry_id": "audit_123456_abc",
            "timestamp": 1234567890.0,
            "action": "UPDATE_TASK",
            "user_id": "user1",
            "channel": "feishu",
            "target_type": "task",
            "target_id": "task_001",
            "status": "success",
            "details": {},
            "ip_address": "",
            "user_agent": "",
        }

        entry = AuditEntry.from_dict(data)

        assert entry.entry_id == "audit_123456_abc"
        assert entry.action == "UPDATE_TASK"


class TestAuditLogger:
    """测试 AuditLogger 类。"""

    def test_logger_init_creates_audit_directory(self, tmp_path: Path):
        """初始化时创建审计目录。"""
        from agent_py_agent.agent.audit.logger import AuditLogger

        class MockConfig:
            audit_log_path = str(tmp_path / "audit")

        logger = AuditLogger(MockConfig())

        assert (tmp_path / "audit").exists()

    def test_log_creates_entry(self, tmp_path: Path):
        """log 方法创建审计条目。"""
        from agent_py_agent.agent.audit.logger import (
            AuditAction,
            AuditLogger,
            AuditStatus,
            LogParams,
        )

        class MockConfig:
            audit_log_path = str(tmp_path / "audit")

        logger = AuditLogger(MockConfig())

        entry = logger.log(
            LogParams(
                action=AuditAction.CREATE_TASK,
                user_id="user1",
                channel="chat",
                target_type="task",
                target_id="task_001",
                status=AuditStatus.SUCCESS,
            )
        )

        assert entry is not None
        assert entry.action == "CREATE_TASK"
        assert entry.user_id == "user1"

    def test_log_writes_to_file(self, tmp_path: Path):
        """日志写入文件。"""
        from agent_py_agent.agent.audit.logger import (
            AuditAction,
            AuditLogger,
            AuditStatus,
            LogParams,
        )

        class MockConfig:
            audit_log_path = str(tmp_path / "audit")

        logger = AuditLogger(MockConfig())

        logger.log(
            LogParams(
                action=AuditAction.CREATE_TASK,
                user_id="user1",
                channel="chat",
                target_type="task",
                target_id="task_001",
                status=AuditStatus.SUCCESS,
            )
        )

        audit_file = tmp_path / "audit" / "audit.jsonl"
        assert audit_file.exists()

        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1

    def test_log_path_can_be_file(self, tmp_path: Path):
        """audit_log_path 指向文件时直接写该文件，兼容旧配置。"""
        from agent_py_agent.agent.audit.logger import (
            AuditAction,
            AuditLogger,
            AuditStatus,
            LogParams,
        )

        audit_file = tmp_path / "custom-audit.jsonl"

        class MockConfig:
            audit_log_path = str(audit_file)

        logger = AuditLogger(MockConfig())
        logger.log(
            LogParams(
                action=AuditAction.CREATE_TASK,
                user_id="user1",
                channel="chat",
                target_type="task",
                target_id="task_001",
                status=AuditStatus.SUCCESS,
            )
        )

        assert audit_file.exists()
        assert audit_file.is_file()

        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        data = json.loads(lines[0])
        assert data["action"] == "CREATE_TASK"

    def test_log_create_task_helper(self, tmp_path: Path):
        """log_create_task 便捷方法。"""
        from agent_py_agent.agent.audit.logger import AuditLogger

        class MockConfig:
            audit_log_path = str(tmp_path / "audit")

        logger = AuditLogger(MockConfig())

        entry = logger.log_create_task(
            task_id="task_001",
            user_id="user1",
            channel="chat",
            details={"goal": "测试任务"},
        )

        assert entry is not None

    def test_log_update_task_helper(self, tmp_path: Path):
        """log_update_task 便捷方法。"""
        from agent_py_agent.agent.audit.logger import AuditLogger

        class MockConfig:
            audit_log_path = str(tmp_path / "audit")

        logger = AuditLogger(MockConfig())

        entry = logger.log_update_task(
            task_id="task_001",
            user_id="user1",
            channel="chat",
            status_before="PLANNING",
            status_after="RUNNING",
        )

        assert entry is not None
        assert entry.action == "UPDATE_TASK"

    def test_log_dispatch_helper(self, tmp_path: Path):
        """log_dispatch 便捷方法。"""
        from agent_py_agent.agent.audit.logger import AuditLogger

        class MockConfig:
            audit_log_path = str(tmp_path / "audit")

        logger = AuditLogger(MockConfig())

        entry = logger.log_dispatch(
            task_id="task_001",
            user_id="user1",
            channel="chat",
        )

        assert entry is not None
        assert entry.action == "DISPATCH"

    def test_log_access_denied_helper(self, tmp_path: Path):
        """log_access_denied 便捷方法。"""
        from agent_py_agent.agent.audit.logger import AuditAction, AuditLogger

        class MockConfig:
            audit_log_path = str(tmp_path / "audit")

        logger = AuditLogger(MockConfig())

        entry = logger.log_access_denied(
            action=AuditAction.ADMIN_ACCESS,
            user_id="user1",
            channel="feishu",
            target_type="task",
            target_id="task_001",
            reason="permission denied",
        )

        assert entry is not None
        assert entry.status == "denied"

    def test_log_error_helper(self, tmp_path: Path):
        """log_error 便捷方法。"""
        from agent_py_agent.agent.audit.logger import AuditAction, AuditLogger

        class MockConfig:
            audit_log_path = str(tmp_path / "audit")

        logger = AuditLogger(MockConfig())

        entry = logger.log_error(
            action=AuditAction.DISPATCH,
            user_id="user1",
            channel="chat",
            target_type="task",
            target_id="task_001",
            error="dispatch failed",
        )

        assert entry is not None
        assert entry.status == "error"

    def test_multiple_logs_append_to_file(self, tmp_path: Path):
        """多次写入追加到文件。"""
        from agent_py_agent.agent.audit.logger import AuditAction, AuditLogger, LogParams

        class MockConfig:
            audit_log_path = str(tmp_path / "audit")

        logger = AuditLogger(MockConfig())

        logger.log(LogParams(action=AuditAction.CREATE_TASK, user_id="u1", channel="c1", target_type="t", target_id="1"))
        logger.log(LogParams(action=AuditAction.UPDATE_TASK, user_id="u2", channel="c2", target_type="t", target_id="2"))

        audit_file = tmp_path / "audit" / "audit.jsonl"
        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")

        assert len(lines) == 2


class TestAuditAction:
    """测试 AuditAction 枚举。"""

    def test_audit_action_values(self, tmp_path: Path):
        """审计动作枚举值。"""
        from agent_py_agent.agent.audit.logger import AuditAction

        assert AuditAction.CREATE_TASK.value == "CREATE_TASK"
        assert AuditAction.UPDATE_TASK.value == "UPDATE_TASK"
        assert AuditAction.DELETE_TASK.value == "DELETE_TASK"
        assert AuditAction.DISPATCH.value == "DISPATCH"


class TestAuditStatus:
    """测试 AuditStatus 枚举。"""

    def test_audit_status_values(self, tmp_path: Path):
        """审计状态枚举值。"""
        from agent_py_agent.agent.audit.logger import AuditStatus

        assert AuditStatus.SUCCESS.value == "success"
        assert AuditStatus.DENIED.value == "denied"
        assert AuditStatus.ERROR.value == "error"
