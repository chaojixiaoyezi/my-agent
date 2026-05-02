"""审计日志测试。"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_py_agent.agent.audit import AuditAction, AuditLogger, AuditQuery
from agent_py_agent.agent.audit.logger import AuditStatus


class TestAuditAction(unittest.TestCase):
    """审计动作枚举测试。"""

    def test_action_values(self):
        """测试动作枚举值。"""
        self.assertEqual(AuditAction.CREATE_TASK.value, "CREATE_TASK")
        self.assertEqual(AuditAction.UPDATE_TASK.value, "UPDATE_TASK")
        self.assertEqual(AuditAction.DISPATCH.value, "DISPATCH")
        self.assertEqual(AuditAction.LOGIN.value, "LOGIN")

    def test_action_from_string(self):
        """测试从字符串创建动作。"""
        action = AuditAction("CREATE_TASK")
        self.assertEqual(action, AuditAction.CREATE_TASK)


class TestAuditLogger(unittest.TestCase):
    """审计日志记录器测试。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.audit_path = Path(self.temp_dir) / "audit"
        self.config = self._create_config()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_config(self):
        """创建测试配置。"""
        class MockConfig:
            audit_log_path = str(self.audit_path)
            audit_enabled = True
        return MockConfig()

    def test_log_creates_file(self):
        """测试记录日志创建文件。"""
        logger = AuditLogger(self.config)
        entry = logger.log(
            action=AuditAction.CREATE_TASK,
            user_id="test-user",
            channel="chat",
            target_type="task",
            target_id="task-123",
        )

        self.assertTrue(self.audit_path.exists())
        self.assertTrue((self.audit_path / "audit.jsonl").exists())

    def test_log_writes_jsonl(self):
        """测试写入 JSONL 格式。"""
        logger = AuditLogger(self.config)

        entry1 = logger.log(
            action=AuditAction.CREATE_TASK,
            user_id="user-1",
            channel="chat",
            target_type="task",
            target_id="task-1",
        )

        entry2 = logger.log(
            action=AuditAction.DISPATCH,
            user_id="user-2",
            channel="feishu",
            target_type="task",
            target_id="task-2",
        )

        audit_file = self.audit_path / "audit.jsonl"
        lines = audit_file.read_text().strip().split("\n")

        self.assertEqual(len(lines), 2)

        # 验证 JSON 格式
        data1 = json.loads(lines[0])
        self.assertEqual(data1["action"], "CREATE_TASK")
        self.assertEqual(data1["user_id"], "user-1")

    def test_log_with_details(self):
        """测试记录带详情。"""
        logger = AuditLogger(self.config)

        entry = logger.log(
            action=AuditAction.UPDATE_TASK,
            user_id="test-user",
            channel="chat",
            target_type="task",
            target_id="task-456",
            details={"status_before": "pending", "status_after": "running"},
        )

        audit_file = self.audit_path / "audit.jsonl"
        data = json.loads(audit_file.read_text().strip())

        self.assertEqual(data["details"]["status_before"], "pending")
        self.assertEqual(data["details"]["status_after"], "running")

    def test_log_access_denied(self):
        """测试记录访问拒绝。"""
        logger = AuditLogger(self.config)

        entry = logger.log_access_denied(
            action=AuditAction.QUERY,
            user_id="bad-user",
            channel="feishu",
            target_type="task",
            target_id="secret-task",
            reason="not authorized",
        )

        self.assertEqual(entry.status, "denied")

    def test_log_error(self):
        """测试记录错误。"""
        logger = AuditLogger(self.config)

        entry = logger.log_error(
            action=AuditAction.DISPATCH,
            user_id="test-user",
            channel="chat",
            target_type="task",
            target_id="task-789",
            error="task not found",
        )

        self.assertEqual(entry.status, "error")

    def test_log_create_task(self):
        """测试便捷方法创建任务。"""
        logger = AuditLogger(self.config)

        entry = logger.log_create_task(
            task_id="new-task",
            user_id="creator",
            channel="chat",
            details={"title": "Test Task"},
        )

        self.assertEqual(entry.action, "CREATE_TASK")
        self.assertEqual(entry.target_id, "new-task")


class TestAuditQuery(unittest.TestCase):
    """审计查询测试。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.audit_path = Path(self.temp_dir) / "audit"
        self.audit_file = self.audit_path / "audit.jsonl"
        self.config = self._create_config()

        # 创建一些测试数据
        self._create_test_data()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_config(self):
        """创建测试配置。"""
        class MockConfig:
            audit_log_path = str(self.audit_path)
            audit_enabled = True
        return MockConfig()

    def _create_test_data(self):
        """创建测试数据。"""
        self.audit_path.mkdir(parents=True, exist_ok=True)

        now = time.time()

        entries = [
            {
                "entry_id": "audit_1",
                "timestamp": now - 100,
                "action": "CREATE_TASK",
                "user_id": "alice",
                "channel": "chat",
                "target_type": "task",
                "target_id": "task-1",
                "status": "success",
                "details": {},
            },
            {
                "entry_id": "audit_2",
                "timestamp": now - 50,
                "action": "DISPATCH",
                "user_id": "alice",
                "channel": "chat",
                "target_type": "task",
                "target_id": "task-1",
                "status": "success",
                "details": {},
            },
            {
                "entry_id": "audit_3",
                "timestamp": now - 30,
                "action": "UPDATE_TASK",
                "user_id": "bob",
                "channel": "feishu",
                "target_type": "task",
                "target_id": "task-2",
                "status": "success",
                "details": {},
            },
            {
                "entry_id": "audit_4",
                "timestamp": now - 10,
                "action": "QUERY",
                "user_id": "bob",
                "channel": "chat",
                "target_type": "task",
                "target_id": "task-1",
                "status": "denied",
                "details": {},
            },
        ]

        with open(self.audit_file, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def test_query_all(self):
        """测试查询所有。"""
        query = AuditQuery(self.config)
        result = query.query(limit=10)

        self.assertEqual(result.total_count, 4)
        self.assertEqual(len(result.entries), 4)

    def test_query_by_user(self):
        """测试按用户查询。"""
        query = AuditQuery(self.config)
        result = query.query(user_id="alice", limit=10)

        self.assertEqual(result.total_count, 2)
        for entry in result.entries:
            self.assertEqual(entry.user_id, "alice")

    def test_query_by_action(self):
        """测试按动作查询。"""
        query = AuditQuery(self.config)
        result = query.query(action=AuditAction.CREATE_TASK, limit=10)

        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.entries[0].action, "CREATE_TASK")

    def test_query_by_status(self):
        """测试按状态查询。"""
        query = AuditQuery(self.config)
        result = query.query(status="denied", limit=10)

        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.entries[0].status, "denied")

    def test_query_pagination(self):
        """测试分页。"""
        query = AuditQuery(self.config)

        # 第一页
        result1 = query.query(limit=2, offset=0)
        self.assertEqual(len(result1.entries), 2)
        self.assertEqual(result1.total_count, 4)

        # 第二页
        result2 = query.query(limit=2, offset=2)
        self.assertEqual(len(result2.entries), 2)

        # 两页不重叠
        ids1 = {e.entry_id for e in result1.entries}
        ids2 = {e.entry_id for e in result2.entries}
        self.assertEqual(len(ids1 & ids2), 0)

    def test_query_sorted_by_time(self):
        """测试按时间倒序。"""
        query = AuditQuery(self.config)
        result = query.query(limit=10)

        # 应该按时间倒序
        timestamps = [e.timestamp for e in result.entries]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_summary_all_users(self):
        """测试所有用户摘要。"""
        query = AuditQuery(self.config)
        stats = query.summary()

        self.assertEqual(stats["total_actions"], 4)
        self.assertIn("CREATE_TASK", stats["by_action"])
        self.assertIn("DISPATCH", stats["by_action"])
        self.assertIn("success", stats["by_status"])
        self.assertIn("denied", stats["by_status"])

    def test_summary_specific_user(self):
        """测试指定用户摘要。"""
        query = AuditQuery(self.config)
        stats = query.summary(user_id="alice")

        self.assertEqual(stats["total_actions"], 2)
        self.assertEqual(stats["user_id"], "alice")

    def test_recent_users(self):
        """测试最近活跃用户。"""
        query = AuditQuery(self.config)
        users = query.recent_users(limit=10)

        self.assertEqual(len(users), 2)  # alice 和 bob

        # bob 应该是最新的（timestamp 最近）
        self.assertEqual(users[0]["user_id"], "bob")

    def test_recent_users_limit(self):
        """测试限制用户数量。"""
        query = AuditQuery(self.config)
        users = query.recent_users(limit=1)

        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["user_id"], "bob")  # 最新的

    def test_cleanup_old_entries(self):
        """测试清理旧条目。"""
        # 添加一些旧条目
        old_time = time.time() - 100000  # 超过一天
        old_entry = {
            "entry_id": "audit_old",
            "timestamp": old_time,
            "action": "LOGIN",
            "user_id": "old-user",
            "channel": "chat",
            "target_type": "session",
            "target_id": "old-session",
            "status": "success",
            "details": {},
        }

        with open(self.audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(old_entry, ensure_ascii=False) + "\n")

        query = AuditQuery(self.config)

        # 清理 1 天内的
        deleted = query.cleanup_old_entries(days=1)

        self.assertEqual(deleted, 1)

        # 再次查询
        result = query.query(limit=10)
        self.assertEqual(result.total_count, 4)  # 旧条目被删除

    def test_empty_audit_file(self):
        """测试空审计文件。"""
        self.audit_file.write_text("")

        query = AuditQuery(self.config)
        result = query.query(limit=10)

        self.assertEqual(result.total_count, 0)
        self.assertEqual(len(result.entries), 0)


if __name__ == "__main__":
    unittest.main()